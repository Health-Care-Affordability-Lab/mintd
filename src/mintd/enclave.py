"""Typed view over `enclave_manifest.yaml` + `enclave_bump`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ._archive_ops import ArchiveOps, TarGzArchiveOps
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
    "InvalidTransferManifest",
    "NothingToPackage",
    "PathTraversalDetected",
    "TransferContent",
    "TransferManifest",
    "TransferredItem",
    "enclave_add",
    "enclave_bump",
    "enclave_package",
    "enclave_pull",
    "enclave_remove",
    "enclave_verify",
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

class NothingToPackage(Exception):
    """`enclave_package` filtered `downloaded[]` to an empty set."""


class InvalidTransferManifest(Exception):
    """`_transfer_manifest.yaml` malformed or references a missing directory."""


class PathTraversalDetected(Exception):
    """A `TransferContent` member would escape the extracted dir (CVE-2007-4559)."""

    def __init__(self, member: str) -> None:
        super().__init__(
            f"transfer manifest references {member!r} which escapes the dest dir"
        )
        self.member = member


class TransferContent(BaseModel):
    model_config = ConfigDict(frozen=True)
    repo: str
    version_folder: str  # e.g. "e8f3a2b-2026-05-11"
    contract_pin: str
    artifact_pin: str


class TransferManifest(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    enclave_name: str
    transfer_date: datetime
    transfer_id: str
    contents: list[TransferContent] = []


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


def _next_transfer_id(manifest: EnclaveManifest, today_iso: str) -> str:
    """Pick the next sequence number for today's transfers.

    Sequence resets daily. Format: `transfer-YYYY-MM-DD-NNNNNN`.
    """
    prefix = f"transfer-{today_iso}-"
    used: set[int] = set()
    for t in manifest.transferred:
        if not t.transfer_id.startswith(prefix):
            continue
        suffix = t.transfer_id.removeprefix(prefix)
        if suffix.isdigit():
            used.add(int(suffix))
    seq = 0
    while seq in used:
        seq += 1
    return f"{prefix}{seq:06d}"


def enclave_package(
    *,
    manifest_path: Path,
    name: str | None = None,
    downloads_root: Path | None = None,
    output_archive: Path | None = None,
    output_dir: Path | None = None,
    archive_ops: ArchiveOps | None = None,
    today: date | None = None,
) -> Path:
    """Bundle outside-enclave `downloaded[]` into a `.tar.gz` transfer archive.

    Exactly one of `output_archive` / `output_dir` must be provided. When
    only `output_dir` is given, the archive filename is derived from the
    computed `transfer_id` (`<output_dir>/<transfer_id>.tar.gz`), which
    guarantees uniqueness across same-day runs.

    Filters `downloaded[]` to `name` if given; raises `NothingToPackage`
    when the filtered set is empty. Appends one `TransferredItem` per
    packaged entry to the outside-enclave manifest, saved through the
    slice-8 append-only seam. If `archive_ops.pack` raises, the manifest
    is never mutated (pack runs inside the `TemporaryDirectory`; save
    runs only after it exits cleanly).

    Returns the produced archive path.
    """
    if output_archive is None and output_dir is None:
        raise ValueError("Either output_archive or output_dir must be provided")

    manifest = EnclaveManifest.load(manifest_path)
    targets = [d for d in manifest.downloaded if name is None or d.repo == name]
    if not targets:
        raise NothingToPackage(
            f"no downloaded[] entries{' for ' + name if name else ''} in {manifest_path}"
        )

    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    today_iso = (today or date.today()).isoformat()
    transfer_id = _next_transfer_id(manifest, today_iso)

    if output_archive is None:
        assert output_dir is not None  # for mypy; checked above
        output_archive = output_dir / f"{transfer_id}.tar.gz"

    contents: list[TransferContent] = []
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        for d in targets:
            version_folder = Path(d.local_path).name
            src = downloads_root / d.repo / version_folder
            if not src.exists():
                raise InvalidTransferManifest(
                    f"downloaded[] entry references missing dir: {src}"
                )
            dest = tmp / d.repo / version_folder
            # `symlinks=True` preserves symlinks so the `pack` time
            # symlink-traversal guard can inspect them. Without it,
            # `copytree` dereferences hostile symlinks (e.g.,
            # `/etc/passwd`) into plain files inside the staging dir,
            # silently bypassing `TarGzArchiveOps.pack`'s check.
            shutil.copytree(src, dest, symlinks=True)
            contents.append(
                TransferContent(
                    repo=d.repo,
                    version_folder=version_folder,
                    contract_pin=d.contract_pin,
                    artifact_pin=d.artifact_pin,
                )
            )

        transfer_manifest = TransferManifest(
            enclave_name=manifest.enclave_name,
            transfer_date=datetime.now(timezone.utc),
            transfer_id=transfer_id,
            contents=contents,
        )
        (tmp / "_transfer_manifest.yaml").write_text(
            yaml.safe_dump(
                transfer_manifest.model_dump(mode="json"), sort_keys=False
            )
        )

        ops = archive_ops or TarGzArchiveOps()
        ops.pack(tmp, output_archive)

    # `pack` succeeded — now record each packaged item in `transferred[]`.
    # `.resolve()` ensures the stored path is absolute regardless of whether
    # `downloads_root` was passed as a relative path.
    new_transferred = list(manifest.transferred)
    for content in contents:
        local_path = str(
            (downloads_root / content.repo / content.version_folder).resolve()
        )
        new_transferred.append(
            TransferredItem(
                repo=content.repo,
                contract_pin=content.contract_pin,
                artifact_pin=content.artifact_pin,
                transfer_date=date.fromisoformat(today_iso),
                transfer_id=transfer_id,
                local_path=local_path,
            )
        )
    new_manifest = manifest.model_copy(update={"transferred": new_transferred})
    new_manifest.save(manifest_path)
    return output_archive


def enclave_verify(
    *,
    extracted_dir: Path,
    manifest_path: Path,
    data_root: Path | None = None,
) -> tuple[Path, list[TransferredItem]]:
    """Reconcile a user-extracted transfer dir into the inside-enclave manifest.

    Path-traversal guard runs **before** any filesystem mutation. Three
    string-level pre-checks (`is_absolute()` and `..` segments on both
    `content.repo` and `content.version_folder`) plus two resolve-based
    checks (the constructed member path, and an `rglob` walk for symlinks
    inside the data) together cover the CVE-2007-4559 family. All
    `startswith` comparisons append `os.sep` to avoid sibling-directory
    false positives.

    Idempotent: entries whose `(repo, contract_pin, artifact_pin)` triple
    is already in `transferred[]` are skipped, so re-running on the same
    extracted dir is a no-op.

    Returns `(manifest_path, written)` where `written` lists only the
    newly-appended `TransferredItem`s.
    """
    manifest_yaml = extracted_dir / "_transfer_manifest.yaml"
    if not manifest_yaml.is_file():
        raise InvalidTransferManifest(
            f"_transfer_manifest.yaml not found at {manifest_yaml}"
        )

    try:
        raw = yaml.safe_load(manifest_yaml.read_text()) or {}
        transfer = TransferManifest.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise InvalidTransferManifest(str(e)) from e

    # Load the inside-enclave manifest up front so the validation loop
    # can skip entries that are already in `transferred[]`. Without
    # this, a re-run after a successful `verify` would fail the
    # existence check (the data was moved into `data_root`) — breaking
    # the idempotence contract.
    manifest = EnclaveManifest.load(manifest_path)
    data_root = data_root or (manifest_path.parent / "data")
    existing_keys = {
        (t.repo, t.contract_pin, t.artifact_pin) for t in manifest.transferred
    }

    extracted_abs = extracted_dir.resolve()
    extracted_prefix = str(extracted_abs) + os.sep

    # Track destination paths seen so far in this single transfer to
    # reject manifests that would move two entries to the same dest
    # (which would surface as a `FileNotFoundError` from the second
    # `shutil.move`, leaving the first move stranded without a
    # `transferred[]` entry).
    seen_dests: set[Path] = set()
    for content in transfer.contents:
        # (a) String pre-check on `repo`. Without this, an absolute `repo`
        # would silently discard the left operand of `Path.__truediv__`
        # (e.g., `extracted_dir / "/etc" / "passwd"` → `Path("/etc/passwd")`).
        # Path-traversal pre-checks run unconditionally — even for
        # already-verified entries — so a hostile re-uploaded manifest
        # is rejected before any filesystem access. Empty string and `.`
        # are also rejected because both produce `Path(...).parts == ()`,
        # bypassing the `..` check; their effect with `Path.__truediv__`
        # is to resolve back to `extracted_dir` / `data_root`. Nested
        # paths (e.g., `A/B`) are rejected because the `dest`-collision
        # check below operates on leaf paths; a manifest pairing
        # `version_folder = "B"` and `version_folder = "B/C"` would
        # otherwise pass collision validation, then crash mid-move
        # when the second `shutil.move` finds `B/C`'s source under the
        # already-moved `B`. Repo/version_folder are flat segments by
        # design (see `_resolve_outputs` in slice 13).
        if (
            not content.repo
            or content.repo == "."
            or "/" in content.repo
            or "\\" in content.repo
            or Path(content.repo).is_absolute()
            or ".." in Path(content.repo).parts
        ):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )
        # (b) String pre-check on `version_folder`. `..` resolves
        # silently *inside* `extracted_dir` if paired with a deep
        # subpath, bypassing a pure `resolve()`-based check. Empty
        # string, `.`, and nested paths are rejected for the same
        # reasons as in the `repo` check above.
        if (
            not content.version_folder
            or content.version_folder == "."
            or "/" in content.version_folder
            or "\\" in content.version_folder
            or Path(content.version_folder).is_absolute()
            or ".." in Path(content.version_folder).parts
        ):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )

        # Skip filesystem checks for entries already in transferred[].
        # The first verify moved them out of `extracted_dir`, so the
        # existence check would falsely fail — see the idempotence
        # contract in the docstring.
        key = (content.repo, content.contract_pin, content.artifact_pin)
        if key in existing_keys:
            continue

        # (c) Existence check — safe now that string-level guards passed.
        member = extracted_dir / content.repo / content.version_folder
        if not member.exists():
            raise InvalidTransferManifest(
                f"manifest references {content.repo}/{content.version_folder} but dir not present"
            )

        # (d) Resolve check — catches symlink at the version_folder
        # itself pointing outside `extracted_dir`.
        resolved = str(member.resolve())
        if resolved != str(extracted_abs) and not resolved.startswith(extracted_prefix):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )

        # (e) Symlink walk — catches symlinks inside the versioned data
        # pointing outside `extracted_dir`. Target need not exist;
        # `p.resolve()` still produces an absolute path we can check.
        for p in member.rglob("*"):
            if p.is_symlink():
                target = str(p.resolve())
                if target != str(extracted_abs) and not target.startswith(extracted_prefix):
                    raise PathTraversalDetected(str(p))

        # (f) Dest collision check — refuse to overwrite an existing
        # `data_root/<repo>/<version_folder>` (legitimate prior data)
        # and refuse two contents that target the same dest. Done in
        # the validation pass so partial moves can't strand entries on
        # disk without `transferred[]` rows.
        dest = data_root / content.repo / content.version_folder
        if dest in seen_dests:
            raise InvalidTransferManifest(
                f"transfer manifest contains duplicate destination {dest}"
            )
        seen_dests.add(dest)
        if dest.exists():
            raise InvalidTransferManifest(
                f"refusing to overwrite existing dest {dest} for new transferred[] entry"
            )

    new_transferred = list(manifest.transferred)
    written: list[TransferredItem] = []
    for content in transfer.contents:
        key = (content.repo, content.contract_pin, content.artifact_pin)
        if key in existing_keys:
            # Idempotent — already verified.
            continue
        src = extracted_dir / content.repo / content.version_folder
        dest = data_root / content.repo / content.version_folder
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Validation pass already confirmed `dest` doesn't exist; if it
        # appeared between then and now, an external process is racing
        # us and we'd rather error out than silently overwrite.
        if dest.exists():
            raise InvalidTransferManifest(
                f"dest {dest} appeared during verify (concurrent modification?)"
            )
        shutil.move(str(src), str(dest))
        item = TransferredItem(
            repo=content.repo,
            contract_pin=content.contract_pin,
            artifact_pin=content.artifact_pin,
            transfer_date=transfer.transfer_date.date(),
            transfer_id=transfer.transfer_id,
            local_path=str(dest.resolve()),
        )
        new_transferred.append(item)
        written.append(item)

    new_manifest = manifest.model_copy(update={"transferred": new_transferred})
    new_manifest.save(manifest_path)
    return manifest_path, written
