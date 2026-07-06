"""Tests for `mintd.enclave.enclave_package` — slice 16."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pytest

from mintd._archive_ops import (
    ArchiveAlreadyExists,
    TarGzArchiveOps,
    UnsafeArchiveMember,
)
from mintd.enclave import (
    DownloadedItem,
    EnclaveManifest,
    NothingToPackage,
    TransferredItem,
    enclave_package,
)
from tests._fakes.archive_ops import _FakeArchiveOps


def _stage_download(
    tmp_path: Path,
    manifest_path: Path,
    *,
    repo: str = "ds-alpha",
    artifact_pin: str = "aaabbb1",
    pre_seeded_transferred: list[TransferredItem] | None = None,
) -> tuple[str, DownloadedItem]:
    version_folder = f"{artifact_pin[:7]}-2026-05-15"
    dl_dir = tmp_path / "downloads" / repo / version_folder
    dl_dir.mkdir(parents=True)
    (dl_dir / "data.csv").write_text("col1,col2\n1,2\n")
    item = DownloadedItem(
        repo=repo,
        output="data.csv",
        contract_pin="c" * 40,
        artifact_pin=artifact_pin * 5,  # ≥32 char artifact pin
        fetch_strategy="dvc-import",
        downloaded_at=datetime(2026, 5, 15),
        local_path=str(dl_dir),
    )
    manifest = EnclaveManifest(
        enclave_name="test-enclave",
        downloaded=[item],
        transferred=list(pre_seeded_transferred or []),
    )
    manifest.save(manifest_path)
    return version_folder, item


def test_package_creates_archive(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    _stage_download(tmp_path, m_path)
    fake = _FakeArchiveOps()
    out_dir = tmp_path / "out"
    archive = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=out_dir,
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    assert archive.exists()
    assert archive.name == "transfer-2026-05-15-000000.tar.gz"
    assert len(fake.calls) == 1


def test_package_appends_to_transferred(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    version_folder, _ = _stage_download(tmp_path, m_path)
    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 1
    t = reloaded.transferred[0]
    assert t.repo == "ds-alpha"
    assert t.transfer_id == "transfer-2026-05-15-000000"
    # `.resolve()` must produce an absolute path regardless of how
    # `downloads_root` was passed. os.path.isabs is the portable check
    # (a Windows absolute path is 'C:\\...', not '/...').
    assert os.path.isabs(t.local_path)
    _lp = Path(t.local_path).as_posix()
    assert _lp.endswith(f"ds-alpha/{version_folder}") or _lp.endswith(
        f"ds-alpha{os.sep}{version_folder}"
    )


def test_package_filters_by_repo(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"

    # Stage two repos; only "a" should be packaged.
    items: list[DownloadedItem] = []
    for repo, pin in (("a", "aaaaaaa"), ("b", "bbbbbbb")):
        version_folder = f"{pin[:7]}-2026-05-15"
        dl_dir = tmp_path / "downloads" / repo / version_folder
        dl_dir.mkdir(parents=True)
        (dl_dir / "data.csv").write_text("x\n")
        items.append(
            DownloadedItem(
                repo=repo,
                output="data.csv",
                contract_pin="c" * 40,
                artifact_pin=pin * 5,
                fetch_strategy="dvc-import",
                downloaded_at=datetime(2026, 5, 15),
                local_path=str(dl_dir),
            )
        )
    EnclaveManifest(enclave_name="test", downloaded=items).save(m_path)

    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        name="a",
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 1
    assert reloaded.transferred[0].repo == "a"


def test_package_empty_raises_nothing_to_package(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(m_path)
    with pytest.raises(NothingToPackage):
        enclave_package(
            manifest_path=m_path,
            output_dir=tmp_path / "out",
            archive_ops=_FakeArchiveOps(),
            today=date(2026, 5, 15),
        )


def test_package_refuses_overwrite_archive(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    _stage_download(tmp_path, m_path)
    pre_existing = tmp_path / "out" / "transfer-2026-05-15-000000.tar.gz"
    pre_existing.parent.mkdir()
    pre_existing.write_bytes(b"already here")
    with pytest.raises(ArchiveAlreadyExists):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_archive=pre_existing,
            archive_ops=_FakeArchiveOps(),
            today=date(2026, 5, 15),
        )


def test_package_transfer_id_sequence(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    pre_seeded = TransferredItem(
        repo="ds-alpha",
        contract_pin="c" * 40,
        artifact_pin="a" * 32,
        transfer_date=date(2026, 5, 15),
        transfer_id="transfer-2026-05-15-000000",
        local_path="/some/abs/path",
    )
    _stage_download(tmp_path, m_path, pre_seeded_transferred=[pre_seeded])

    fake = _FakeArchiveOps()
    archive = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "transfers",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    assert archive.name == "transfer-2026-05-15-000001.tar.gz"
    reloaded = EnclaveManifest.load(m_path)
    # Original entry preserved + 1 new entry appended.
    assert len(reloaded.transferred) == 2
    assert reloaded.transferred[1].transfer_id == "transfer-2026-05-15-000001"


def test_package_rejects_unsafe_symlink_in_downloads(tmp_path: Path) -> None:
    """A `src_dir` containing a symlink pointing outside itself must be
    refused by `TarGzArchiveOps.pack`. We exercise the seam directly
    because `enclave_package` materialises a fresh `tempfile` staging
    directory via `shutil.copytree` (which dereferences symlinks) before
    handing it to `pack`."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.csv").write_text("x\n")
    outside = tmp_path / "outside_secret"
    outside.write_text("top secret")
    os.symlink(str(outside), str(src / "evil_link"))
    dest_archive = tmp_path / "out.tar.gz"
    with pytest.raises(UnsafeArchiveMember):
        TarGzArchiveOps().pack(src, dest_archive)


def test_package_hostile_symlink_in_downloads_caught_by_pack(tmp_path: Path) -> None:
    """Regression: `shutil.copytree(src, dest)` without `symlinks=True`
    dereferences symlinks before `TarGzArchiveOps.pack` runs, silently
    bundling sensitive host content (e.g., a `→ /etc/passwd` symlink
    becomes a plain file). Fix preserves symlinks through copytree so
    the pack-time guard catches them."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _, item = _stage_download(tmp_path, m_path)
    outside = tmp_path / "secrets"
    outside.write_text("top secret\n")
    dl_dir = Path(item.local_path)
    os.symlink(str(outside), str(dl_dir / "exfil"))
    with pytest.raises(UnsafeArchiveMember):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=TarGzArchiveOps(),
            today=date(2026, 5, 15),
        )
    # And the manifest is untouched: append-only contract preserved
    # even when pack rejects the staging dir.
    reloaded = EnclaveManifest.load(m_path)
    assert reloaded.transferred == []


def test_package_preserves_transferred_byte_identical_on_failure(
    tmp_path: Path,
) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    pre_seeded = TransferredItem(
        repo="ds-alpha",
        contract_pin="c" * 40,
        artifact_pin="a" * 32,
        transfer_date=date(2026, 1, 1),
        transfer_id="transfer-2026-01-01-000000",
        local_path="/some/abs/path",
    )
    _stage_download(tmp_path, m_path, pre_seeded_transferred=[pre_seeded])
    before = EnclaveManifest.load(m_path).transferred[0].model_dump()

    fake = _FakeArchiveOps(raise_on_pack=RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=fake,
            today=date(2026, 5, 15),
        )
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 1
    assert reloaded.transferred[0].model_dump() == before
