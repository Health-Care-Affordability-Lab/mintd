"""`mintd publish` — the producer-side write transaction.

Four steps in order: bump metadata.json → dvc push → git tag → catalog update.
DVC failure rolls back metadata.json. Tag/catalog failures leave the
manifest bumped + DVC pushed; the CLI prints partial-state warnings.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from ._dvc_ops import DvcOpError, DvcOps
from ._registry_git_ops import GitOpError, GitTagAlreadyExists, RegistryGitOps
from .catalog import CatalogClient, CatalogNotFound, FieldChange, _dict_diff
from .check import check_project
from .model import Metadata


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class PublishError(Exception):
    def __init__(self, message: str, *, pushed: bool = False, tagged: bool = False, catalog_updated: bool = False, recovery_hint: str = ""):
        self.pushed = pushed
        self.tagged = tagged
        self.catalog_updated = catalog_updated
        self.recovery_hint = recovery_hint
        super().__init__(message)


class InvalidCurrentVersion(PublishError):
    pass


class VersionNotIncreasing(PublishError):
    pass


class WorkingTreeDirty(PublishError):
    pass


class PublishBlocked(PublishError):
    def __init__(self, findings):
        self.findings = findings
        super().__init__(f"{len(findings)} error finding(s) block publish")


class DvcPushFailed(PublishError):
    pass


class TagFailed(PublishError):
    pass


class CatalogUpdateFailed(PublishError):
    pass


@dataclass(frozen=True)
class PublishResult:
    version: str
    dry_run: bool
    diff: list[FieldChange]
    pushed: bool
    tagged: bool
    catalog_updated: bool


def publish_project(
    *,
    project_path: Path,
    version: str | None = None,
    dry_run: bool = False,
    client: CatalogClient,
    dvc_ops: DvcOps,
    git_ops: RegistryGitOps,
    message: str | None = None,
) -> PublishResult:
    metadata_path = project_path / "metadata.json"
    
    # Pre-flight check
    findings = check_project(project_path, upgrades=False)
    error_findings = [f for f in findings if f.severity == "error"]
    if error_findings:
        raise PublishBlocked(error_findings)
    
    current = Metadata.from_json_file(metadata_path)
    new_version = _resolve_version(current.mint.version, version)
    
    # Working-tree gate
    if not dry_run and not git_ops.is_working_tree_clean(project_path):
        raise WorkingTreeDirty(
            f"working tree at {project_path} has uncommitted changes",
            recovery_hint="Commit or stash your changes before publishing.",
        )
    
    new_metadata = current.model_copy(deep=True)
    new_metadata.mint.version = new_version
    diff = _compute_diff(current, new_metadata)
    
    if dry_run:
        return PublishResult(
            version=new_version, dry_run=True, diff=diff,
            pushed=False, tagged=False, catalog_updated=False,
        )
    
    # Step 1: write metadata.json atomically (if changed)
    original_metadata_json = metadata_path.read_text(encoding="utf-8")
    if diff:
        _atomic_write_json(metadata_path, new_metadata.model_dump_json(indent=2))

    # Step 2: dvc push. Catch DvcOpError (parent of DvcPushError + DvcNotInstalled)
    # so both subprocess timeouts AND missing-binary failures trigger the rollback.
    try:
        dvc_ops.push()
    except DvcOpError as exc:
        if diff:
            # Atomic restore to exactly original
            _atomic_write_json(metadata_path, original_metadata_json)
        raise DvcPushFailed(
            f"dvc push failed: {exc}",
            recovery_hint="metadata.json was rolled back; fix the DVC remote and rerun `mintd publish`.",
        ) from exc
    
    # Step 3: commit the bump (only if dvc push succeeded)
    if diff:
        try:
            git_ops.commit_all(project_path, message or f"chore: bump mint.version to {new_version}")
        except GitOpError as exc:
            # If commit fails, we've already pushed artifacts but metadata is dirty.
            # Restore to original state
            _atomic_write_json(metadata_path, original_metadata_json)
            git_ops.reset_hard(project_path, "HEAD")
            raise PublishError(f"failed to commit metadata bump: {exc}") from exc
    
    # Step 4: git tag
    tag_name = f"v{new_version}"
    try:
        git_ops.tag(project_path, tag_name, message or f"mintd publish {tag_name}")
    except (GitOpError, GitTagAlreadyExists) as exc:
        raise TagFailed(
            f"git tag {tag_name} failed: {exc}",
            pushed=True,
            recovery_hint=(
                f"metadata.json is committed and DVC artifacts are pushed; tag {tag_name} was not created.\n"
                f"To retry: rerun `mintd publish {new_version}` (idempotent).\n"
                f"If the tag already exists, delete it first with `git tag -d {tag_name}` then retry."
            ),
        ) from exc
    
    # Step 5: catalog update
    try:
        client.update(new_metadata)
    except CatalogNotFound as exc:
        raise CatalogUpdateFailed(
            f"catalog update failed: {exc}",
            pushed=True, tagged=True,
            recovery_hint="DVC + tag completed. Run `mintd registry register` first, then rerun `mintd publish` (idempotent retry).",
        ) from exc
    
    return PublishResult(
        version=new_version, dry_run=False, diff=diff,
        pushed=True, tagged=True, catalog_updated=True,
    )


def _resolve_version(current: str, requested: str | None) -> str:
    m = _SEMVER_RE.match(current)
    if not m:
        raise InvalidCurrentVersion(f"current mint.version {current!r} is not valid semver (MAJOR.MINOR.PATCH expected)")
    
    if requested is None:
        return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    
    rm = _SEMVER_RE.match(requested)
    if not rm:
        raise VersionNotIncreasing(f"requested version {requested!r} is not valid semver")
        
    if _semver_tuple(requested) < _semver_tuple(current):
        raise VersionNotIncreasing(f"requested version {requested} is lower than current {current}")

    return requested


def _semver_tuple(v: str) -> tuple[int, int, int]:
    m = _SEMVER_RE.match(v)
    assert m
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _atomic_write_json(path: Path, content: str) -> None:
    """Write `content` to `path` atomically.

    Sequence: write to a sibling tmp file → fsync the tmp file's contents →
    rename onto `path` → fsync the parent directory. The parent-dir fsync
    ensures the rename is durable on POSIX. NOT calling `os.sync()` —
    that's a system-wide flush which can stall on slow filesystems.
    """
    import os
    from ._atomic import _try_fsync_parent_dir
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "r+") as f:
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _try_fsync_parent_dir(path)


# Public alias so other modules (slice 22's metadata_migrate) can reuse the
# atomic write without duplicating the fsync ceremony. Existing slice-15
# callsites still import the private name.
atomic_write_json = _atomic_write_json


def _compute_diff(old: Metadata, new: Metadata) -> list[FieldChange]:
    return _dict_diff(old.model_dump(), new.model_dump())
