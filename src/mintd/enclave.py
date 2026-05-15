"""Typed view over `enclave_manifest.yaml` + `enclave_bump`.

Slice 8 ships:
- `EnclaveManifest` Pydantic model with append-only enforcement on
  `transferred[]` at the I/O boundary (`save()` diffs against the existing
  on-disk manifest; `TransferredItem` is frozen).
- `enclave_bump` — the manifest-side counterpart of slice-7 `bump_import`.
  Consumes slice-6 `_consumer_findings`, re-resolves the producer's HEAD
  via `ProducerView.at_head`, and rewrites `approved_products[].pin`.

The pull/package/cross-air-gap pipeline (`mintd enclave pull / package /
unpack / verify`) is out of scope for slice 8.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .catalog import CatalogClient
from .data import (
    BumpBlocked,
    ImportNotFound,
    PrimaryRemovedAtHead,
    _find_consumer_finding_for_target,
    _require_repo_url,
)
from .producer import MissingPrimaryDataProduct, ProducerView

if TYPE_CHECKING:
    # Avoid module-level import of check.py — check.py imports EnclaveManifest
    # from this module, so a runtime import would create a cycle. CheckFinding
    # only appears in annotations, which are strings under `from __future__
    # import annotations`.
    from .check import CheckFinding


__all__ = [
    "AlreadyApproved",
    "AppendOnlyViolation",
    "ApprovedProduct",
    "DownloadedItem",
    "EnclaveManifest",
    "TransferredItem",
    "enclave_add",
    "enclave_bump",
]


class AppendOnlyViolation(Exception):
    """`transferred[]` is permanent. Mutating or removing an existing entry
    raises this at `EnclaveManifest.save` time. `changed_indices` are the
    positions in the *existing* on-disk manifest where the new in-memory
    manifest diverged (modified, removed, or reordered)."""

    def __init__(self, path: Path, changed_indices: list[int]) -> None:
        super().__init__(
            f"transferred[] entries changed at indices {changed_indices} in {path}"
        )
        self.path = path
        self.changed_indices = changed_indices


class AlreadyApproved(Exception):
    """The manifest already contains an `approved_products[]` entry with
    this repo. `enclave_add` refuses to add a duplicate; the user should
    `enclave bump` to update an existing subscription's pin."""

    def __init__(self, name: str, manifest_path: Path) -> None:
        super().__init__(
            f"{name!r} already in approved_products[] of {manifest_path}"
        )
        self.name = name
        self.manifest_path = manifest_path


class ApprovedProduct(BaseModel):
    model_config = ConfigDict(frozen=False)

    repo: str
    registry_entry: str
    pin: str
    source_path: str | None = None
    all: bool = False


class DownloadedItem(BaseModel):
    model_config = ConfigDict(frozen=False)

    repo: str
    output: str
    contract_pin: str
    artifact_pin: str
    fetch_strategy: Literal["dvc-import", "subtree"]
    downloaded_at: datetime
    local_path: str


class TransferredItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    contract_pin: str
    artifact_pin: str
    transfer_date: date
    transfer_id: str
    local_path: str


class EnclaveManifest(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    enclave_name: str
    approved_products: list[ApprovedProduct] = []
    downloaded: list[DownloadedItem] = []
    transferred: list[TransferredItem] = []

    @classmethod
    def load(cls, path: Path) -> "EnclaveManifest":
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        if path.exists():
            existing = EnclaveManifest.load(path)
            changed = _diff_transferred(existing.transferred, self.transferred)
            if changed:
                raise AppendOnlyViolation(path, changed)
        path.write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False))

    def apply_pin_bump(self, *, repo: str, new_pin: str) -> "EnclaveManifest":
        for i, ap in enumerate(self.approved_products):
            if ap.repo == repo:
                new_products = list(self.approved_products)
                new_products[i] = ap.model_copy(update={"pin": new_pin})
                return self.model_copy(update={"approved_products": new_products})
        raise ImportNotFound(f"{repo!r} not in approved_products[] in this manifest")


def _diff_transferred(
    existing: list[TransferredItem], current: list[TransferredItem]
) -> list[int]:
    """Indices of `existing` entries that have been modified or removed.

    New entries appended past `len(existing)` are allowed and not reported.
    Both modifications within the overlap *and* tail removals are reported,
    so a "modify entry 0, drop entry 2" change surfaces `[0, 2]` — not
    just `[2]`.
    """
    overlap = min(len(existing), len(current))
    changed: list[int] = [i for i in range(overlap) if current[i] != existing[i]]
    if len(current) < len(existing):
        changed.extend(range(len(current), len(existing)))
    return changed


def enclave_bump(
    client: CatalogClient,
    *,
    manifest_path: Path,
    project_path: Path | None = None,
    name: str,
    force: bool = False,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
    check_findings: list[CheckFinding] | None = None,
) -> Path | None:
    """Re-resolve `name` at the producer's HEAD and rewrite its
    `approved_products[].pin` entry in the enclave manifest.

    Mirrors `bump_import`'s severity dispatch verbatim. Returns
    `manifest_path` on success, `None` if the finding says up-to-date.
    Raises `ImportNotFound`, `BumpBlocked`, or `PrimaryRemovedAtHead`.
    Append-only on `transferred[]` is enforced via `EnclaveManifest.save`.
    """
    # Lazy import breaks the check.py ↔ enclave.py cycle.
    from .check import check_project

    project_path = project_path if project_path is not None else manifest_path.parent
    manifest = EnclaveManifest.load(manifest_path)

    target: ApprovedProduct | None = None
    for ap in manifest.approved_products:
        if ap.repo == name:
            target = ap
            break
    if target is None:
        raise ImportNotFound(f"{name!r} not in approved_products[] in {manifest_path}")

    findings = (
        check_findings
        if check_findings is not None
        else check_project(project_path, upgrades=True, client=client)
    )
    finding = _find_consumer_finding_for_target(
        findings, source=manifest_path, field_path=f"approved_products[{name}]"
    )
    if finding is None:
        raise ImportNotFound(
            f"no consumer finding for {name!r} (manifest={manifest_path})"
        )

    if finding.kind is None:
        # Contract: consumer-section findings post-slice-9 always carry a kind.
        raise BumpBlocked(name, finding)
    if finding.kind == "up_to_date":
        return None
    if finding.kind != "drift":
        # unreachable / schema_too_old / pin_missing / metadata_missing /
        # metadata_invalid / invalid_manifest / catalog_unresolved — all non-actionable.
        raise BumpBlocked(name, finding)

    repo_url = _resolve_approved_product_url(client, target)
    factory = producer_view_factory or ProducerView.at_head
    head_view, head_sha = factory(repo_url)
    try:
        head_view.primary_or_raise()
    except MissingPrimaryDataProduct as e:
        raise PrimaryRemovedAtHead(name, repo_url) from e

    del force  # reserved for future --dry-run
    new_manifest = manifest.apply_pin_bump(repo=name, new_pin=head_sha)
    new_manifest.save(manifest_path)
    return manifest_path


def _resolve_approved_product_url(client: CatalogClient, ap: ApprovedProduct) -> str:
    """Slice-8 Decision #2α: catalog is canonical for repo identity."""
    entry = client.fetch(ap.repo)
    return _require_repo_url(entry.model_dump(), name=ap.repo)


def enclave_add(
    client: CatalogClient,
    *,
    manifest_path: Path,
    name: str,
    pin: str | None = None,
    source_path: str | None = None,
    all_: bool = False,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
) -> Path:
    """Subscribe a producer by appending an `ApprovedProduct` to the manifest.

    Validates the producer exists in the catalog (raises `CatalogNotFound`
    via passthrough). Refuses duplicates (`AlreadyApproved`). Resolves
    HEAD via `ProducerView.at_head` if `pin` is None. Append-only on
    `transferred[]` is enforced via `EnclaveManifest.save`.

    Slice-12 ordering rationale: catalog validation first (cheapest;
    fail-fast on unknown producer); duplicate check next (in-memory walk
    over `approved_products`); HEAD resolution last (costs a `git
    ls-remote` round-trip — skip if we'd refuse anyway).
    """
    entry = client.fetch(name)
    repo_url = entry.repo_url
    if not repo_url:
        raise ValueError(f"catalog entry {name!r} has no repository.github_url")

    if manifest_path.exists():
        manifest = EnclaveManifest.load(manifest_path)
        for ap in manifest.approved_products:
            if ap.repo == name:
                raise AlreadyApproved(name, manifest_path)
    else:
        manifest = EnclaveManifest(enclave_name=manifest_path.parent.name)

    if pin is None:
        factory = producer_view_factory or ProducerView.at_head
        head_view, resolved_pin = factory(repo_url)
        # If neither --source-path nor --all is set, the bump needs *something*
        # to subscribe to — fail fast if HEAD has no primary.
        if source_path is None and not all_:
            head_view.primary_or_raise()
    else:
        resolved_pin = pin

    new_ap = ApprovedProduct(
        repo=name,
        registry_entry=f"catalog/data/{name}.yaml",
        pin=resolved_pin,
        source_path=source_path,
        all=all_,
    )
    new_manifest = manifest.model_copy(
        update={"approved_products": [*manifest.approved_products, new_ap]}
    )
    new_manifest.save(manifest_path)
    return manifest_path
