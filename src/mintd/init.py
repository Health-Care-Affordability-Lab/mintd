"""Project scaffolding — `mintd init`.

Renders the legacy `mintd create <type>` file set through the vendored
Jinja templates in `src/mintd/files/`, runs `git init`, and (for
non-enclave types) `dvc init`. Returns the project path and the list of
rendered files so the CLI can print per-file output.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ._console import Reporter
from ._init_ops import InitNonInteractive, InitOpError, InitOps, SubprocessInitOps
from ._storage_state import SLUG_REGEX, compute_storage_prefix
from ._templates import InitNameInvalid, render_scaffold
from .model import DvcStorage, Metadata, Storage
from .publish import atomic_write_json

_DVC_INIT_TYPES: frozenset[str] = frozenset({"data", "code", "project"})

_TIERS: list[tuple[str, str]] = [
    ("labonly", "Lab-only — internal data, private to lab members"),
    ("public", "Public — shareable with the world, no restrictions"),
    ("licensed", "Licensed — DUA / contractual restrictions, gated access"),
]


class InitDestinationExists(Exception):
    """`metadata.json` already exists at the target. Refusing to overwrite."""


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
    ops: InitOps | None = None,
) -> tuple[Path, list[Path]]:
    """Initialize a fresh mintd project with storage configuration."""
    if use_current_repo:
        project_path = target_dir
    else:
        project_path = target_dir / f"{project_type}_{name}"

    project_path.mkdir(parents=True, exist_ok=True)
    metadata_path = project_path / "metadata.json"
    if metadata_path.exists():
        raise InitDestinationExists(metadata_path)

    written = render_scaffold(
        project_type=project_type,
        name=name,
        language=language,
        target_dir=project_path,
    )

    ops = ops or SubprocessInitOps()
    ops.git_init(project_path)
    if project_type in _DVC_INIT_TYPES:
        ops.dvc_init(project_path)

    if classification is not None and project_type in _DVC_INIT_TYPES:
        if not bucket:
            raise InitOpError(
                "bucket not configured in ~/.mintd/config.yaml; run "
                "'mintd config setup' first"
            )

        prefix = compute_storage_prefix(
            classification=classification,  # type: ignore[arg-type]
            project_name=f"{project_type}_{name}",
            slug=slug,
        )
        remote_name = f"{project_type}_{name}"
        remote_url = f"s3://{bucket}/{prefix}"

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
                bucket=bucket,
                prefix=prefix,
                endpoint=endpoint or "",
                versioning=True,
                dvc=DvcStorage(remote_name=remote_name),
            )
            atomic_write_json(
                metadata_path,
                metadata.model_dump_json(by_alias=True, exclude_none=False, indent=2),
            )
        except Exception:
            # Rollback boundary: remove .dvc/ on remote-add or patch
            # failure. metadata.json is left in place (atomic write +
            # replay-safe; rerunning init re-applies the storage block).
            shutil.rmtree(project_path / ".dvc", ignore_errors=True)
            raise

    return project_path, written


__all__ = [
    "init_project",
    "_prompt_classification",
    "InitDestinationExists",
    "InitNameInvalid",
    "InitNonInteractive",
]
