"""Project scaffolding — `mintd init`.

Renders the legacy `mintd create <type>` file set through the vendored
Jinja templates in `src/mintd/files/`, runs `git init`, and (for
non-enclave types) `dvc init`. Returns the project path and the list of
rendered files so the CLI can print per-file output.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ._console import Reporter
from ._init_ops import InitNonInteractive, InitOpError, InitOps, SubprocessInitOps
from ._storage_state import SLUG_REGEX, compute_storage_prefix
from ._templates import (
    InitNameInvalid,
    project_full_name,
    render_scaffold,
    scaffold_targets,
    validate_project_name,
)
from .model import DvcStorage, Metadata, Storage
from .publish import atomic_write_json

_DVC_INIT_TYPES: frozenset[str] = frozenset({"data", "code", "project"})

_TIERS: list[tuple[str, str]] = [
    ("labonly", "Lab-only — internal data, private to lab members"),
    ("public", "Public — shareable with the world, no restrictions"),
    ("licensed", "Licensed — DUA / contractual restrictions, gated access"),
]


class InitDestinationExists(Exception):
    """The target already holds files the scaffold would overwrite."""

    def __init__(self, msg: str, *, hint: str | None = None) -> None:
        super().__init__(msg)
        self.hint = hint


def _escapes(project_path: Path, rel: str) -> bool:
    """True if writing ``rel`` would land outside ``project_path``.

    ``os.path.lexists`` catches a symlinked *leaf*, but a symlinked parent
    directory (``code/ -> ../shared_code``) is transparent to it: mkdir is
    satisfied by the link and write_text then follows it out of the project.
    Resolving the whole path is what closes that.
    """
    try:
        resolved = (project_path / rel).resolve()
        resolved.relative_to(project_path.resolve())
    except OSError:
        return False
    except ValueError:
        return True
    return False


def _prompt_classification(
    *,
    reporter: Reporter,
    prompt_fn: Callable[[str], str] = input,
    isatty_fn: Callable[[], bool] = sys.stdin.isatty,
) -> tuple[str, str | None]:
    """Interactive classification + slug prompt.

    Returns ``(tier, slug)`` where ``slug`` is None for ``labonly`` /
    ``public`` and a validated URL-safe string for ``licensed``. Raises
    ``InitNonInteractive`` when stdin isn't a TTY — init's tier choice
    is governance-critical and must not be flag-driven.
    """
    if not isatty_fn():
        raise InitNonInteractive("init is interactive; run from a terminal")

    reporter.info("Choose a storage classification for this product:")
    for i, (_key, desc) in enumerate(_TIERS, 1):
        reporter.info(f"  {i}. {desc}")

    while True:
        raw = prompt_fn("Choice [1-3]: ").strip()
        try:
            idx = int(raw)
        except ValueError:
            reporter.warn(f"Not a number: {raw!r}. Enter 1, 2, or 3.")
            continue
        if 1 <= idx <= len(_TIERS):
            tier = _TIERS[idx - 1][0]
            break
        reporter.warn(f"Out of range: {idx}. Enter 1, 2, or 3.")

    slug: str | None = None
    if tier == "licensed":
        while True:
            slug = prompt_fn("Slug (licensor / DUA, e.g. 'optum'): ").strip()
            if not slug:
                reporter.warn("Slug is required for licensed tier.")
                continue
            if not SLUG_REGEX.match(slug):
                reporter.warn(
                    f"Invalid slug {slug!r}. Must match {SLUG_REGEX.pattern}."
                )
                continue
            break

    return tier, slug


def init_project(
    *,
    project_type: Literal["data", "code", "project", "enclave"],
    name: str,
    target_dir: Path,
    language: Literal["python", "r", "stata"] = "python",
    use_current_repo: bool = False,
    classification: str | None = None,
    slug: str | None = None,
    bucket: str | None = None,
    endpoint: str | None = None,
    profile: str | None = None,
    force: bool = False,
    ops: InitOps | None = None,
    reporter: Reporter | None = None,
) -> tuple[Path, list[Path]]:
    """Initialize a fresh mintd project with storage configuration."""
    # Preflight: validate, then refuse, then write. Nothing below this block
    # touches the filesystem until every reason to refuse has been checked --
    # a failed init must not leave a half-made project behind.
    validate_project_name(name)
    full_name = project_full_name(project_type, name)
    project_path = target_dir if use_current_repo else target_dir / full_name
    metadata_path = project_path / "metadata.json"

    wants_storage = classification is not None and project_type in _DVC_INIT_TYPES
    if wants_storage and not bucket:
        # Hoisted from the storage block below, where it used to raise after
        # the scaffold, `git init` and `dvc init` had all run -- and *outside*
        # the rollback boundary, so nothing was undone and the rerun then hit
        # the destination guard. Raised here it leaves nothing behind at all.
        raise InitOpError(
            "bucket not configured in ~/.mintd/config.yaml; run "
            "'mintd config setup' first, then rerun the same command"
        )
    # Validated once, above; re-bound as a plain str so the storage block
    # needs no second check. Truthy iff storage is wanted and configured.
    storage_bucket: str = bucket if wants_storage and bucket else ""

    targets = scaffold_targets(
        project_type=project_type, name=name, language=language
    )
    # lexists, not exists: a broken symlink is still something the user put
    # there, and write_text would follow it out of the project.
    collisions = sorted(
        rel for rel in targets if os.path.lexists(project_path / rel)
    )
    # Refused even under --force: --force authorizes overwriting files in
    # THIS project, never following a link out of it.
    escaping = sorted(rel for rel in targets if _escapes(project_path, rel))
    if escaping:
        raise InitDestinationExists(
            f"refusing to write outside {project_path} through a symlink:\n  "
            + "\n  ".join(escaping),
            hint="a directory in the scaffold's path is a symlink; "
            "replace it with a real directory",
        )

    if collisions and not force:
        raise InitDestinationExists(
            f"refusing to overwrite {len(collisions)} existing "
            f"file(s) in {project_path}:\n  " + "\n  ".join(collisions),
            hint=(
                "move them aside, or re-run with --force to overwrite them.\n"
                "--force re-renders every file above, metadata.json included, "
                "so anything you added to it is lost."
            ),
        )

    try:
        project_path.mkdir(parents=True, exist_ok=True)
        written = render_scaffold(
            project_type=project_type,
            name=name,
            language=language,
            target_dir=project_path,
        )
    except OSError as exc:
        # mkdir raises FileExistsError / NotADirectoryError / PermissionError
        # for a --path that is a regular file or unwritable. cli.py catches
        # InitOpError, not OSError, so without this the user gets a traceback.
        raise InitOpError(f"cannot write to {project_path}: {exc}") from exc

    ops = ops or SubprocessInitOps()
    ops.git_init(project_path)
    if project_type in _DVC_INIT_TYPES:
        ops.dvc_init(project_path)

    if storage_bucket:
        prefix = compute_storage_prefix(
            classification=classification,  # type: ignore[arg-type]
            project_name=project_full_name(project_type, name),
            slug=slug,
        )
        remote_name = project_full_name(project_type, name)
        remote_url = f"s3://{storage_bucket}/{prefix}"

        try:
            ops.dvc_remote_add(
                project_path,
                name=remote_name,
                url=remote_url,
                default=True,
                endpoint=endpoint,
                profile=profile,
            )

            # Slice 30 defensive raw-dict pop:
            # Don't call Metadata.model_validate_json on the file
            # directly — if a template (current or future) emits a
            # partial storage block, model_validate_json would crash
            # before our patch can fix it. Read raw dict, drop any
            # pre-existing storage key, then validate. The pop is a
            # no-op in the standard v2 path (templates strip storage
            # entirely) but survives template regressions.
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            raw.pop("storage", None)
            metadata = Metadata.model_validate(raw)
            metadata.storage = Storage(
                provider="s3",
                bucket=storage_bucket,
                prefix=prefix,
                endpoint=endpoint or "",
                versioning=True,
                dvc=DvcStorage(remote_name=remote_name),
            )
            atomic_write_json(
                metadata_path,
                metadata.model_dump_json(by_alias=True, exclude_none=False, indent=2)
                + "\n",
            )
        except Exception:
            # Rollback boundary: remove .dvc/ on remote-add or patch
            # failure. metadata.json is left in place (atomic write +
            # replay-safe; rerunning init re-applies the storage block).
            # `dvc init` staged `.dvc/*` in git's index before it failed;
            # unstage those entries too so a subsequent rerun/commit
            # doesn't carry a phantom `.dvc/config` (best-effort, never
            # raises — must not mask the original failure).
            shutil.rmtree(project_path / ".dvc", ignore_errors=True)
            ops.git_unstage(project_path, [".dvc"])
            raise

    # `dvc init` stages `.dvc/config`, and the subsequent
    # `dvc config cache.type` + any `dvc remote add` rewrite it — leaving
    # an `AM` (staged-then-modified) index entry. Restage it once so a
    # teammate's `git commit` captures the config *with* the remote, not a
    # stale half-staged copy. Covers both index-dirtying sources (cache.type
    # fires even when classification is None) in one place. A failed restage
    # must not fail an otherwise-healthy init — rerunning would then hit
    # InitDestinationExists — so warn and return success.
    if project_type in _DVC_INIT_TYPES:
        try:
            ops.git_add(project_path, [".dvc/config"])
        except InitOpError:
            if reporter is not None:
                reporter.warn(
                    "could not restage .dvc/config; run: git add .dvc/config"
                )

    return project_path, written


__all__ = [
    "init_project",
    "_prompt_classification",
    "InitDestinationExists",
    "InitNameInvalid",
    "InitNonInteractive",
]
