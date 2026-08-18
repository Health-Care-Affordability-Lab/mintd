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

# Written before the first scaffold file, removed only after a fully
# successful init. Its presence is what makes "this directory is init's own
# half-finished output" provable rather than inferred.
_SENTINEL_NAME = ".mintd-init-incomplete"

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


def _resuming(sentinel: Path, metadata_path: Path, full_name: str) -> bool:
    """True iff a previous ``init_project`` crashed here, on THIS project.

    The sentinel is what proves the state is init's own output. Inferring it
    from the metadata's shape instead (scaffold present, ``storage`` null,
    name matches) would also match a hand-authored or half-migrated
    metadata.json, and silently wire lab storage into someone else's file.

    Any read/parse/shape failure is a non-match, never an exception: a
    corrupt metadata.json must make init refuse, not raise JSONDecodeError
    out of the CLI.
    """
    if not sentinel.is_file():
        return False
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        return bool(raw["project"]["full_name"] == full_name)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _https_remote(raw: str) -> str:
    """A git remote URL in the form the ``github_url`` field wants.

    ``git@github.com:org/repo.git`` -> ``https://github.com/org/repo``. A
    trailing ``.git`` always comes off; beyond that, anything not in the
    scp-like form keeps its shape -- a bare path, an ``ssh://`` URL, a
    non-GitHub host. This value is only ever *suggested* to a human who then
    confirms it, so reporting an unrecognized remote close to verbatim beats
    guessing at an https equivalent it may not have.
    """
    url = raw.strip()
    if url.startswith("git@") and ":" in url:
        host, _, path = url[len("git@"):].partition(":")
        url = f"https://{host}/{path}"
    return url.removesuffix(".git")


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

    # An init that crashed here already wrote the scaffold, so every target
    # "collides" with itself. Resume re-runs only the storage wiring and
    # leaves the tree alone -- including anything the user edited after the
    # failed run. Ceiling: a crash *mid-render* leaves a partial tree that
    # resume will not repair; --force re-renders it.
    # lexists, not exists: a broken symlink is still something the user put
    # there, and write_text would follow it out of the project.
    targets = scaffold_targets(
        project_type=project_type, name=name, language=language
    )
    collisions = sorted(
        rel for rel in targets if os.path.lexists(project_path / rel)
    )

    # Resume only over a COMPLETE tree. A crash *mid-render* also leaves the
    # sentinel and a matching metadata.json (it is written 2nd of 14), but
    # resuming there would skip the render and report success over a scaffold
    # missing .gitignore and dvc.yaml. Partial trees fall through to the
    # refusal below, where --force re-renders and completes them.
    sentinel = project_path / _SENTINEL_NAME
    resuming = (
        not force
        and len(collisions) == len(targets)
        and _resuming(sentinel, metadata_path, full_name)
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

    if collisions and not force and not resuming:
        raise InitDestinationExists(
            f"refusing to overwrite {len(collisions)} existing "
            f"file(s) in {project_path}:\n  " + "\n  ".join(collisions),
            hint=(
                "move them aside, or re-run with --force to overwrite them.\n"
                "--force re-renders every file above, metadata.json included, "
                "so anything you added to it is lost."
            ),
        )

    ops = ops or SubprocessInitOps()

    # Storage identity, and the one reason storage wiring can refuse, are both
    # knowable before any write -- so they belong here, with the other
    # refusals. Checked after the render instead, a refusal lands on a user
    # whose files have already been replaced, which is exactly what the
    # comment at the top of this block promises does not happen.
    prefix = remote_name = remote_url = ""
    existing_remote: str | None = None
    if storage_bucket:
        prefix = compute_storage_prefix(
            classification=classification,  # type: ignore[arg-type]
            project_name=full_name,
            slug=slug,
        )
        remote_name = full_name
        remote_url = f"s3://{storage_bucket}/{prefix}"
        # `dvc init` is skipped when .dvc/ already exists, and that is exactly
        # when an existing remote of this name can be one mintd never wrote.
        # Whether it is ours is a question about the remote, not about how
        # init got here: "crashed here once" and "--force" both say nothing
        # about who put it there. Its URL does.
        if (project_path / ".dvc").is_dir():
            existing_remote = ops.dvc_remote_url(project_path, remote_name)
        if existing_remote is not None and existing_remote != remote_url:
            raise InitOpError(
                f"DVC remote {remote_name!r} already points at "
                f"{existing_remote}, not {remote_url}. mintd will not repoint "
                "an existing remote -- pushes would go somewhere you did not "
                f"choose. If it is stale, `dvc remote remove {remote_name}` "
                "and rerun; otherwise rename it."
            )

    try:
        project_path.mkdir(parents=True, exist_ok=True)
        # unlink first, same reason as _write_file: the sentinel path is
        # not a scaffold target, so _escapes never sees it, and
        # write_text would follow a symlink there and truncate whatever
        # it points at.
        sentinel.unlink(missing_ok=True)
        sentinel.write_text("", encoding="utf-8")
        written = (
            []
            if resuming
            else render_scaffold(
                project_type=project_type,
                name=name,
                language=language,
                target_dir=project_path,
            )
        )
    except OSError as exc:
        # mkdir raises FileExistsError / NotADirectoryError / PermissionError
        # for a --path that is a regular file or unwritable. cli.py catches
        # InitOpError, not OSError, so without this the user gets a traceback.
        raise InitOpError(f"cannot write to {project_path}: {exc}") from exc

    if resuming and reporter is not None:
        reporter.info(
            f"resuming interrupted init in {project_path}; configuring storage only"
        )

    ops.git_init(project_path)
    # `dvc init` fails outright on an existing .dvc/ (needs -f), which a
    # resume -- or --use-current-repo into an existing DVC repo -- would hit.
    # The remote-add rollback rmtree's .dvc/, so a resume after *that*
    # failure still gets a fresh init, which re-stages .dvc/* as before.
    dvc_initialized_here = False
    if project_type in _DVC_INIT_TYPES and not (project_path / ".dvc").is_dir():
        ops.dvc_init(project_path)
        dvc_initialized_here = True

    if storage_bucket:
        # prefix / remote_name / remote_url / existing_remote were all
        # computed in the preflight, along with the refusal for a remote
        # pointing somewhere else.
        if existing_remote is None and not dvc_initialized_here and reporter is not None:
            # Adding into a .dvc/ mintd did not create: `-d` makes this the
            # default remote, which is a change to a file the user owns.
            reporter.warn(
                f"pointing DVC remote {remote_name!r} at {remote_url} "
                "in an existing .dvc/config; this becomes the default remote"
            )

        try:
            # Always call it, even when the remote is already there: only its
            # first step writes the URL, and the endpointurl / profile /
            # version_aware steps after it are what a run interrupted
            # mid-sequence still needs. `exists` skips just the add.
            ops.dvc_remote_add(
                project_path,
                name=remote_name,
                url=remote_url,
                default=True,
                endpoint=endpoint,
                profile=profile,
                exists=existing_remote is not None,
            )

            # Slice 30 defensive raw-dict pop:
            # Don't call Metadata.model_validate_json on the file
            # directly — if a template (current or future) emits a
            # partial storage block, model_validate_json would crash
            # before our patch can fix it. Read raw dict, drop any
            # pre-existing storage key, then validate. The pop is a
            # no-op in the standard v2 path (templates strip storage
            # entirely) but survives template regressions.
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                raw.pop("storage", None)
                metadata = Metadata.model_validate(raw)
            except (ValueError, TypeError) as exc:
                # On the resume path this file is one the user may have edited
                # between the failed run and the rerun. pydantic's
                # ValidationError is a ValueError; unmapped it reaches the CLI
                # as a raw traceback.
                raise InitOpError(
                    f"{metadata_path} is not valid mintd metadata: {exc}"
                ) from exc
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
            # failure. metadata.json is left in place, and so is the
            # `.mintd-init-incomplete` sentinel -- together they are what
            # lets the identical `mintd init` command resume and re-apply
            # just the storage block, instead of refusing on the scaffold
            # it wrote itself.
            # `dvc init` staged `.dvc/*` in git's index before it failed;
            # unstage those entries too so a subsequent rerun/commit
            # doesn't carry a phantom `.dvc/config` (best-effort, never
            # raises — must not mask the original failure).
            # ONLY when this call created it. Skipping `dvc init` over a
            # pre-existing .dvc/ means the rollback can now reach one mintd
            # never made -- whose config.local (credentials, gitignored) and
            # cache (unpushed blobs) are not ours to delete and not always
            # recoverable.
            if dvc_initialized_here:
                shutil.rmtree(project_path / ".dvc", ignore_errors=True)
                ops.git_unstage(project_path, [".dvc"])
            elif reporter is not None:
                # Nothing to undo safely, but `dvc remote add -d` has already
                # repointed core.remote in a config we do not own. Say so.
                reporter.warn(
                    f"left {project_path / '.dvc' / 'config'} as-is; "
                    "mintd may have changed your default remote"
                )
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

    # `mintd check` reports an empty repository.github_url as an *error*
    # (unit G), and publish refuses any error finding -- so when the render had
    # no `registry_org` to derive from, init is handing back a project it
    # already knows cannot pass check. Say so here, where the empty value gets
    # written, rather than leaving the user to meet it on their next `check`.
    #
    # Read the value back out of the file instead of re-deriving it from
    # config: this is the exact value `check` will read, so the warning cannot
    # disagree with the error it is warning about. Best-effort like the restage
    # above -- an unreadable metadata.json must not fail an otherwise healthy
    # init, and `check` reports that case on its own anyway.
    if reporter is not None:
        try:
            meta_raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            url_missing = not str(meta_raw["repository"]["github_url"] or "").strip()
        except (OSError, ValueError, KeyError, TypeError):
            url_missing = False
        if url_missing:
            # Not gated on --use-current-repo: on the plain path `git init`
            # just made a fresh repo, so this read returns None by itself --
            # while `--force` into an existing clone arrives here with a real
            # origin and no such flag. "The URL is empty" is the only gate
            # that matters, and it is the branch we are already in.
            # Best-effort, for the same reason as the restage and sentinel
            # blocks below: this runs after an otherwise-healthy init, and a
            # raise here would fail it. The seam maps a missing binary and a
            # timeout to None itself, but not a PermissionError on cwd -- and
            # an InitOps impl is free to raise anything. A suggestion is the
            # most optional thing in this function; losing it must cost the
            # user nothing more than the suggestion.
            try:
                origin = ops.git_origin_url(project_path)
            except (OSError, InitOpError):
                origin = None
            suggestion = (
                f" This repo's git origin is {_https_remote(origin)} — "
                "probably what belongs in the field."
                if origin
                else ""
            )
            reporter.warn(
                f"{metadata_path}: repository.github_url is empty, so "
                "`mintd check` reports an error here and publish refuses. "
                "Run `mintd config setup` to set registry_org (future "
                "scaffolds derive it from that), and fill this one in."
                + suggestion
            )

    # Last thing, and only on full success: from here the directory is a
    # finished project, not init's half-finished output. Best-effort for the
    # same reason as the restage above -- a raw OSError here would escape the
    # CLI's except clause *after* an otherwise healthy init.
    try:
        sentinel.unlink(missing_ok=True)
    except OSError:
        if reporter is not None:
            reporter.warn(
                f"could not remove {_SENTINEL_NAME}; "
                "delete it before rerunning init"
            )

    return project_path, written


__all__ = [
    "init_project",
    "_prompt_classification",
    "InitDestinationExists",
    "InitNameInvalid",
    "InitNonInteractive",
]
