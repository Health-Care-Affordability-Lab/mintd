"""Typed view over `enclave_manifest.yaml` + `enclave_bump`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .catalog import CatalogClient
from .data import (
    BumpBlocked,
    ImportNotFound,
    PrimaryRemovedAtHead,
    _find_consumer_finding_for_target,
)
from .check import CheckFinding, _resolve_approved_product_url
from .producer import MissingPrimaryDataProduct, ProducerView
from ._dvc_ops import DvcOps

if TYPE_CHECKING:
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
    "enclave_pull",
    "enclave_remove",
]

class AppendOnlyViolation(Exception):
    def __init__(self, path: Path, changed_indices: list[int]) -> None:
        super().__init__(
            f"transferred[] entries changed at indices {changed_indices} in {path}"
        )
        self.path = path
        self.changed_indices = changed_indices

class AlreadyApproved(Exception):
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

def _diff_transferred(existing: list[TransferredItem], current: list[TransferredItem]) -> list[int]:
    overlap = min(len(existing), len(current))
    changed: list[int] = [i for i in range(overlap) if current[i] != existing[i]]
    if len(current) < len(existing):
        changed.extend(range(len(current), len(existing)))
    return changed

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
        raise ImportNotFound(f"no consumer finding for {name!r} (manifest={manifest_path})")
    if finding.kind is None:
        raise BumpBlocked(name, finding)
    if finding.kind == "up_to_date":
        return None
    if finding.kind != "drift":
        raise BumpBlocked(name, finding)
    repo_url = _resolve_approved_product_url(client, target)
    factory = producer_view_factory or ProducerView.at_head
    head_view, head_sha = factory(repo_url)
    try:
        head_view.primary_or_raise()
    except MissingPrimaryDataProduct as e:
        raise PrimaryRemovedAtHead(name, repo_url) from e
    del force
    new_manifest = manifest.apply_pin_bump(repo=name, new_pin=head_sha)
    new_manifest.save(manifest_path)
    return manifest_path

def enclave_remove(
    client: CatalogClient,
    *,
    manifest_path: Path,
    name: str,
    source_path: str | None = None,
    all_: bool = False,
    downloads_root: Path | None = None,
) -> Path:
    del client
    # all_ accepted for CLI parity but unused as bare `remove` wipes all entries
    manifest = EnclaveManifest.load(manifest_path)
    def _matches_approved(ap: ApprovedProduct) -> bool:
        if ap.repo != name:
            return False
        if source_path is not None:
            return ap.source_path == source_path
        return True
    matched = [ap for ap in manifest.approved_products if _matches_approved(ap)]
    if not matched:
        raise ImportNotFound(f"{name!r} not in approved_products[] in {manifest_path}")
    new_approved = [ap for ap in manifest.approved_products if not _matches_approved(ap)]
    new_downloaded = [
        d for d in manifest.downloaded
        if d.repo != name or (source_path is not None and d.output != source_path)
    ]
    new_manifest = manifest.model_copy(
        update={"approved_products": new_approved, "downloaded": new_downloaded}
    )
    new_manifest.save(manifest_path)
    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    repo_downloads = downloads_root / name
    # Wipe downloads/<repo>/ only if no other manifest entry still references it.
    # Guards: no remaining approved_products[] entry for this repo, AND no
    # remaining downloaded[] entry for this repo. transferred[] entries point at
    # data/<repo>/... (different root) so they don't gate this wipe.
    if (
        repo_downloads.exists()
        and not any(ap.repo == name for ap in new_approved)
        and not any(d.repo == name for d in new_downloaded)
    ):
        shutil.rmtree(repo_downloads)
    return manifest_path

def enclave_pull(
    client: CatalogClient,
    dvc_ops: DvcOps,
    *,
    manifest_path: Path,
    repo: str | None = None,
    force: bool = False,
    downloads_root: Path | None = None,
    producer_view_factory: Callable[[str, str], ProducerView] | None = None,
    today: date | None = None,
) -> tuple[Path, list[DownloadedItem]]:
    manifest = EnclaveManifest.load(manifest_path)
    targets = [ap for ap in manifest.approved_products if repo is None or ap.repo == repo]
    if repo is not None and not targets:
        raise ImportNotFound(f"{repo!r} not in approved_products[] in {manifest_path}")
    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    today_iso = (today or date.today()).isoformat()
    factory = producer_view_factory or (lambda url, pin: ProducerView.at(url, pin))
    new_downloaded: list[DownloadedItem] = list(manifest.downloaded)
    written: list[DownloadedItem] = []
    created_target_dirs: set[Path] = set()
    for ap in targets:
        # Idempotence: skip resolving if all outputs are already present.
        if not force and _all_already_downloaded(manifest.downloaded, ap):
             continue

        entry = client.fetch(ap.repo)
        repo_url = entry.repo_url
        if not repo_url:
            raise ValueError(f"catalog entry {ap.repo!r} has no repository.github_url")
        outputs = _resolve_outputs(ap, repo_url, factory)
        for output in outputs:
            if not force and _already_downloaded(manifest.downloaded, ap.repo, output, ap.pin):
                continue
            if force:
                new_downloaded = [
                    d for d in new_downloaded
                    if not (d.repo == ap.repo and d.output == output and d.contract_pin == ap.pin)
                ]
            staging_dir = downloads_root / ap.repo / "_staging"
            # Defensive: clear stale _staging from a prior interrupted run.
            # Without this, dvc_ops.import_ would refuse to overwrite the
            # existing dest, breaking future pulls until manual cleanup.
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            dest = staging_dir / Path(output.rstrip("/")).name
            dvc_path = dvc_ops.import_(
                repo_url=repo_url,
                path=output,
                dest=dest,
                rev=ap.pin,
                force=force,
            )
            artifact_pin = _read_artifact_pin(dvc_path)
            target_dir = downloads_root / ap.repo / f"{artifact_pin[:7]}-{today_iso}"
            if force and target_dir.exists() and target_dir not in created_target_dirs:
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            created_target_dirs.add(target_dir)
            # Defensive: clear any stale destination from a previous interrupted
            # run. Without this, shutil.move would nest dest inside the existing
            # target (e.g., target/dest/dest) when the prior run died after the
            # move but before manifest.save.
            final_dest = target_dir / dest.name
            if final_dest.exists():
                if final_dest.is_dir():
                    shutil.rmtree(final_dest)
                else:
                    final_dest.unlink()
            shutil.move(str(dest), str(final_dest))
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            item = DownloadedItem(
                repo=ap.repo,
                output=output,
                contract_pin=ap.pin,
                artifact_pin=artifact_pin,
                fetch_strategy="dvc-import",
                downloaded_at=datetime.now(),
                local_path=str(target_dir),
            )
            new_downloaded.append(item)
            written.append(item)
    new_manifest = manifest.model_copy(update={"downloaded": new_downloaded})
    new_manifest.save(manifest_path)
    return manifest_path, written

def _resolve_outputs(
    ap: ApprovedProduct,
    repo_url: str,
    factory: Callable[[str, str], ProducerView],
) -> list[str]:
    if ap.source_path is not None:
        return [ap.source_path]
    view = factory(repo_url, ap.pin)
    if ap.all:
        return view.output_paths()
    return [view.primary_or_raise()]

def _already_downloaded(
    downloaded: list[DownloadedItem], repo: str, output: str, pin: str
) -> bool:
    return any(
        d.repo == repo and d.output == output and d.contract_pin == pin
        for d in downloaded
    )

def _all_already_downloaded(downloaded: list[DownloadedItem], ap: ApprovedProduct) -> bool:
    if ap.all:
        return False
    return any(d.repo == ap.repo and d.output == (ap.source_path or "primary") and d.contract_pin == ap.pin for d in downloaded)

def _read_artifact_pin(dvc_path: Path) -> str:
    data = yaml.safe_load(dvc_path.read_text())
    outs = data.get("outs") or []
    if not outs:
        raise ValueError(f"{dvc_path} has no outs[]")
    first = outs[0]
    if not isinstance(first, dict):
        raise ValueError(f"{dvc_path} outs[0] is not a dict")
    md5 = first.get("md5")
    if not isinstance(md5, str):
        raise ValueError(f"{dvc_path} outs[0].md5 missing or non-str")
    return md5
