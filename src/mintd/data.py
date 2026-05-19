"""Orchestration for the `mintd data ...` command family.

Slice 4: `import_product` — catalog lookup → path resolution → `dvc import`.
Slice 5: lifts the `--rev` without `--path` restriction by resolving
`data_products.primary` via `ProducerView.at(repo, rev)` (the producer's
metadata.json at the pinned commit).
Slice 7: `bump_import` — consume slice-6 `_consumer_findings`, re-resolve
`data_products.primary` at the producer's HEAD via `ProducerView.at_head`,
and overwrite the consumer's `.dvc` file with the new pin.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._dvc_ops import DvcOps
from ._fast_sync_ops import FastSyncOps
from ._registry_git_ops import GitOpError, RegistryGitOps
from .catalog import CatalogClient
from .check import CheckFinding, check_project
from .data_ops import data_pull
from .imports import DataDependency, NotAnImportError
from .producer import MissingPrimaryDataProduct, ProducerError, ProducerView

__all__ = [
    "BumpBlocked",
    "ImportDestinationExists",
    "ImportNotFound",
    "MissingPrimaryDataProduct",
    "PrimaryRemovedAtHead",
    "ProducerError",
    "bump_import",
    "clone_and_pull_product",
    "import_product",
]


class ImportDestinationExists(Exception):
    """A `.dvc` file already exists at the destination. The consumer resolves
    by passing `force=True` or removing the file first."""


class ImportNotFound(Exception):
    """`bump_import(name=...)` was called with a name that isn't imported
    in the project's `data/imports/` directory."""


class BumpBlocked(Exception):
    """The consumer-section finding for this dep is an error or non-actionable
    warning. Bumping is unsafe until the user resolves the underlying producer
    issue. Carries the original `CheckFinding` so a CLI layer can render the
    producer-side reason."""

    def __init__(self, name: str, finding: CheckFinding) -> None:
        super().__init__(f"bump blocked for {name!r}: {finding.message}")
        self.name = name
        self.finding = finding


class PrimaryRemovedAtHead(Exception):
    """Producer's HEAD has `data_products.primary = None`. The consumer must
    either pin to an older SHA explicitly or stop importing this producer."""

    def __init__(self, name: str, repo: str) -> None:
        super().__init__(
            f"producer {repo!r} HEAD has no data_products.primary; cannot bump {name!r}"
        )
        self.name = name
        self.repo = repo


def import_product(
    client: CatalogClient,
    dvc_ops: DvcOps,
    name: str,
    *,
    dest_root: Path,
    path: str | None = None,
    rev: str | None = None,
    all_outputs: bool = False,
    force: bool = False,
    producer_view_factory: Callable[[str, str], ProducerView] | None = None,
) -> list[Path]:
    """Catalog-driven `dvc import`. Returns the list of `.dvc` files written."""

    entry = client.fetch(name)
    dumped = entry.model_dump()
    repo_url = _require_repo_url(dumped, name=name)

    if rev is not None and path is None and not all_outputs:
        factory = producer_view_factory or ProducerView.at
        view = factory(repo_url, rev)
        path = view.primary_or_raise()

    paths = _resolve_paths(dumped, path=path, all_outputs=all_outputs, name=name)

    produced: list[Path] = []
    for p in paths:
        dest = dest_root / Path(p.rstrip("/")).name
        target_dvc = dest.parent / (dest.name + ".dvc")
        if target_dvc.exists() and not force:
            raise ImportDestinationExists(
                f"{target_dvc} already exists; pass force=True or remove it"
            )
        produced.append(
            dvc_ops.import_(
                repo_url=repo_url,
                path=p,
                dest=dest,
                rev=rev,
                force=force,
            )
        )
    return produced


def _resolve_paths(
    entry: dict[str, Any],
    *,
    path: str | None,
    all_outputs: bool,
    name: str,
) -> list[str]:
    data_products = entry.get("data_products") or {}

    if all_outputs:
        outputs = data_products.get("outputs") or []
        return [o["path"] for o in outputs if isinstance(o, dict) and "path" in o]

    if path is not None:
        return [path]

    primary = data_products.get("primary")
    if not primary:
        raise MissingPrimaryDataProduct(
            f"catalog entry {name!r} has no data_products.primary; pass --path or --all"
        )
    return [primary]


def _require_repo_url(entry: dict[str, Any], *, name: str) -> str:
    repo = entry.get("repository") or {}
    url = repo.get("github_url")
    if not url:
        raise ValueError(f"catalog entry {name!r} has no repository.github_url")
    return url


_NAME_FORBIDDEN = ("/", "\\", "..")


def _validate_clone_name(name: str) -> None:
    if not name or name in {".", ".."} or any(s in name for s in _NAME_FORBIDDEN):
        raise ValueError(f"invalid product name: {name!r}")


def _resolve_clone_dest(
    entry: dict[str, Any], *, name: str, dest: Path | None
) -> Path:
    if dest is not None:
        return dest
    project_type = (entry.get("project") or {}).get("type") or "data"
    base = name
    for prefix in ("data_", "prj_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return Path.cwd() / f"{project_type}_{base}"


def clone_and_pull_product(
    client: CatalogClient,
    dvc_ops: DvcOps,
    registry_git_ops: RegistryGitOps,
    fast_sync_ops: FastSyncOps | None,
    *,
    name: str,
    dest: Path | None = None,
    rev: str | None = None,
    primary_only: bool = False,
    jobs: int | None = None,
) -> Path:
    """Clone a published data product into a working directory + dvc pull it.

    Looks up `name` in the registry, full-clones the producer repo to
    `./<type>_<name>/` (or `dest` if provided), then `dvc pull`s every
    tracked output by default. Pass ``primary_only=True`` to pull only
    `data_products.primary` (useful when the full product is multi-TB
    but the user only needs the headline output).

    Raises:
        ValueError: invalid `name` (path-traversal characters).
        CatalogNotFound: `name` not in registry.
        ImportDestinationExists: dest exists and is non-empty.
        ProducerError: clone failed (UNREACHABLE).
        MissingPrimaryDataProduct: `primary_only=True` and no primary set.
        DvcOpError: dvc pull failed after clone.
    """
    _validate_clone_name(name)
    entry = client.fetch(name)
    dumped = entry.model_dump()
    repo_url = _require_repo_url(dumped, name=name)
    resolved_dest = _resolve_clone_dest(dumped, name=name, dest=dest).resolve()
    if resolved_dest.exists() and any(resolved_dest.iterdir()):
        raise ImportDestinationExists(
            f"destination {resolved_dest} exists and is non-empty"
        )

    try:
        registry_git_ops.clone(
            repo_url, resolved_dest, shallow=False, branch=rev,
        )
    except GitOpError as exc:
        raise ProducerError.unreachable(
            repo=repo_url,
            pin=rev or "HEAD",
            detail=(
                f"clone to {resolved_dest} failed; "
                f"partial clone left in place: {exc}"
            ),
        ) from exc

    if primary_only:
        primary = (dumped.get("data_products") or {}).get("primary")
        if not primary:
            raise MissingPrimaryDataProduct(
                f"catalog entry {name!r} has no data_products.primary; "
                f"drop --primary to pull all tracked outputs"
            )
        targets: list[str] | None = [primary]
    else:
        targets = None

    # SubprocessDvcOps' subprocess.run calls don't pass cwd=, so they
    # inherit os.getcwd(). chdir into the clone before invoking data_pull
    # and restore on return (success OR failure).
    prev_cwd = Path.cwd()
    os.chdir(resolved_dest)
    try:
        data_pull(
            project_path=resolved_dest,
            targets=targets,
            dvc_ops=dvc_ops,
            fast_sync_ops=fast_sync_ops,
            jobs=jobs,
        )
    finally:
        os.chdir(prev_cwd)
    return resolved_dest


def bump_import(
    client: CatalogClient,
    dvc_ops: DvcOps,
    *,
    project_path: Path,
    name: str,
    force: bool = False,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
    check_findings: list[CheckFinding] | None = None,
) -> Path | None:
    """Re-resolve `name` at the producer's HEAD and rewrite its `.dvc` file.

    Slice 7 consumes slice-6 `_consumer_findings` directly — `check_project`
    is the canonical "find drift" surface; this function is the canonical
    "act on drift" surface. Walking dependencies here would duplicate
    detection in two places (the resolver-sin slice 6 retired).

    `ProducerView.at_head` returns the resolved SHA alongside the view so
    `dvc import --rev <sha>` records the concrete commit, not the symbolic
    `HEAD` — preserving the pin semantics slice 5 introduced.

    Returns the rewritten `.dvc` `Path` on success, or `None` if the
    finding says the dep is already `up to date`. Raises:

    - `ImportNotFound` — `name` is not present in `data/imports/`.
    - `BumpBlocked(name, finding)` — the producer is broken at the pin
      (`pin_missing` / `metadata_missing` / `metadata_invalid`) or the
      warning is non-actionable (`unreachable` / `schema_too_old`).
      Carries the original finding so the call site can render the
      producer-side reason.
    - `PrimaryRemovedAtHead` — HEAD's `data_products.primary` is `None`.

    The `force` kwarg is reserved for a future `--dry-run`; slice 7 always
    passes `force=True` to `dvc_ops.import_`.
    """
    del client  # accepted for signature symmetry with import_product; unused in slice 7

    index = _imports_index(project_path)
    if name not in index:
        raise ImportNotFound(f"{name!r} not imported in {project_path}")
    dvc_source = index[name]
    dep = DataDependency.from_dvc_file(dvc_source)

    findings = (
        check_findings
        if check_findings is not None
        else check_project(project_path, upgrades=True)
    )
    finding = _find_consumer_finding_for_target(findings, source=dvc_source)
    if finding is None:
        raise ImportNotFound(
            f"no consumer finding for {name!r} (source={dvc_source})"
        )

    if finding.kind is None:
        # Contract: consumer-section findings post-slice-9 always carry a kind.
        # A None here is a regression — never silently dispatch.
        raise BumpBlocked(name, finding)
    if finding.kind == "up_to_date":
        return None
    if finding.kind != "drift":
        # unreachable / schema_too_old / pin_missing / metadata_missing /
        # metadata_invalid / invalid_manifest / catalog_unresolved — all non-actionable.
        raise BumpBlocked(name, finding)

    factory = producer_view_factory or ProducerView.at_head
    head_view, head_sha = factory(dep.producer_repo)
    try:
        head_primary = head_view.primary_or_raise()
    except MissingPrimaryDataProduct as e:
        raise PrimaryRemovedAtHead(name, dep.producer_repo) from e

    del force  # reserved for future --dry-run; slice 7 always overwrites
    dest_root = dvc_source.parent
    return dvc_ops.import_(
        repo_url=dep.producer_repo,
        path=head_primary,
        dest=dest_root / Path(head_primary.rstrip("/")).name,
        rev=head_sha,
        force=True,
    )


def _imports_index(project_path: Path) -> dict[str, Path]:
    """Map each import's local-path name to its `.dvc` source file.

    Mirrors `scan_imports`' walk over `data/imports/*.dvc`. Only
    `dvc import` shapes (`deps[0].repo` present) are indexed; `dvc add`
    files raise `NotAnImportError` and are skipped.
    """
    index: dict[str, Path] = {}
    imports_dir = project_path / "data" / "imports"
    if not imports_dir.exists():
        return index
    for dvc_path in sorted(imports_dir.rglob("*.dvc")):
        try:
            dep = DataDependency.from_dvc_file(dvc_path)
        except NotAnImportError:
            continue
        index[dep.local_path] = dvc_path
    return index


def _find_consumer_finding_for_target(
    findings: list[CheckFinding], *, source: Path, field_path: str | None = None
) -> CheckFinding | None:
    for f in findings:
        if f.section == "consumer" and f.source == source and f.field_path == field_path:
            return f
    return None
