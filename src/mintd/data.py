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

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._dvc_ops import DvcOps
from .catalog import CatalogClient
from .check import CheckFinding, check_project
from .imports import DataDependency, NotAnImportError
from .producer import MissingPrimaryDataProduct, ProducerView

__all__ = [
    "BumpBlocked",
    "ImportDestinationExists",
    "ImportNotFound",
    "MissingPrimaryDataProduct",
    "PrimaryRemovedAtHead",
    "bump_import",
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


_DRIFT_PREFIX = "upgrade available:"
_UP_TO_DATE_MESSAGE = "up to date"


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
    finding = _find_consumer_finding_for_source(findings, dvc_source)
    if finding is None:
        raise ImportNotFound(
            f"no consumer finding for {name!r} (source={dvc_source})"
        )

    if finding.severity == "info":
        # `_consumer_findings`' only info template in upgrades mode is
        # "up to date"; treat any other info as a no-op for forward compat.
        return None
    if finding.severity == "error":
        raise BumpBlocked(name, finding)
    # severity == "warning"
    if not finding.message.startswith(_DRIFT_PREFIX):
        # Non-actionable warning (unreachable / schema_too_old). Producer
        # is in a state the consumer can't safely bump past.
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


def _find_consumer_finding_for_source(
    findings: list[CheckFinding], source: Path
) -> CheckFinding | None:
    for f in findings:
        if f.section == "consumer" and f.source == source:
            return f
    return None
