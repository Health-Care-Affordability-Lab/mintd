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
    NothingNewToPackage,
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
    archive, _skipped = enclave_package(
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
    archive, _skipped = enclave_package(
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


def _seed(
    tmp_path: Path,
    manifest_path: Path,
    downloaded: list[tuple[str, str]],
    transferred: list[tuple[str, str]] = [],
    *,
    day: str = "2026-05-15",
    contract_pins: dict[tuple[str, str], str] | None = None,
) -> None:
    """Stage `(repo, artifact_pin_seed)` downloads on disk + in the manifest.

    `transferred` seeds `(repo, artifact_pin_seed)` rows so a test can express
    "these bytes already crossed the gap" without restating the whole model.
    """
    items: list[DownloadedItem] = []
    for repo, seed in downloaded:
        pin = seed * 5
        vf = f"{pin[:7]}-{day}"
        dl_dir = tmp_path / "downloads" / repo / vf
        dl_dir.mkdir(parents=True, exist_ok=True)
        (dl_dir / "data.csv").write_text("x\n")
        items.append(
            DownloadedItem(
                repo=repo,
                output="data.csv",
                contract_pin=(contract_pins or {}).get((repo, seed), "c" * 40),
                artifact_pin=pin,
                fetch_strategy="dvc-import",
                downloaded_at=datetime(2026, 5, 15),
                local_path=str(dl_dir),
            )
        )
    EnclaveManifest(
        enclave_name="test-enclave",
        downloaded=items,
        transferred=[
            TransferredItem(
                repo=repo,
                contract_pin="c" * 40,
                artifact_pin=seed * 5,
                transfer_date=date(2026, 5, 14),
                transfer_id="transfer-2026-05-14-000000",
                local_path=f"/prior/{repo}",
            )
            for repo, seed in transferred
        ],
    ).save(manifest_path)


def test_package_skips_already_shipped_bytes(tmp_path: Path) -> None:
    """The core incremental contract: bytes already in transferred[] don't re-ship."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1"), ("ds-beta", "bbbccc2")],
        transferred=[("ds-alpha", "aaabbb1")],
    )
    fake = _FakeArchiveOps()
    _archive, skipped = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    staged = fake.staged[0]
    assert any(p.startswith("ds-beta") for p in staged)
    assert not any(p.startswith("ds-alpha") for p in staged)
    assert [d.repo for d in skipped] == ["ds-alpha"]
    reloaded = EnclaveManifest.load(m_path)
    # Prior row preserved, exactly one appended — for ds-beta only.
    assert len(reloaded.transferred) == 2
    assert reloaded.transferred[1].repo == "ds-beta"


def test_package_all_shipped_raises_nothing_new(tmp_path: Path) -> None:
    """Nothing new is a routine no-op, not an error, and must not touch state."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1")],
        transferred=[("ds-alpha", "aaabbb1")],
    )
    before = m_path.read_bytes()
    fake = _FakeArchiveOps()
    with pytest.raises(NothingNewToPackage):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=fake,
            today=date(2026, 5, 15),
        )
    assert fake.calls == []
    # No transfer id minted, no save: byte-identical.
    assert m_path.read_bytes() == before


def test_nothing_new_is_a_nothing_to_package(tmp_path: Path) -> None:
    """Subclassing keeps every pre-existing `except NothingToPackage` correct."""
    assert issubclass(NothingNewToPackage, NothingToPackage)


def test_package_resend_reships(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1")],
        transferred=[("ds-alpha", "aaabbb1")],
    )
    fake = _FakeArchiveOps()
    archive, skipped = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
        resend=True,
    )
    assert archive.exists()
    assert skipped == []
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 2
    assert reloaded.transferred[1].artifact_pin == "aaabbb1" * 5
    assert (
        reloaded.transferred[0].transfer_id != reloaded.transferred[1].transfer_id
    )


def test_package_dedupes_same_bytes_under_two_contract_pins(tmp_path: Path) -> None:
    """D4: a same-day pin bump yields two downloaded[] rows sharing one
    local_path (the folder name omits contract_pin). Copying both crashes
    `copytree` with FileExistsError. Copy once instead."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1"), ("ds-alpha", "aaabbb1")],
        contract_pins={("ds-alpha", "aaabbb1"): "c" * 40},
    )
    # Second row carries a different contract pin, same bytes/folder.
    man = EnclaveManifest.load(m_path)
    man.downloaded[1] = man.downloaded[1].model_copy(
        update={"contract_pin": "d" * 40}
    )
    man.save(m_path)

    fake = _FakeArchiveOps()
    _archive, _skipped = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    reloaded = EnclaveManifest.load(m_path)
    # One copy crossed, so exactly one audit row — carrying the newer pin.
    assert len(reloaded.transferred) == 1
    assert reloaded.transferred[0].contract_pin == "d" * 40


def test_package_dedup_key_ignores_contract_pin(tmp_path: Path) -> None:
    """D6/BQ#4: same bytes under a NEW contract pin must not re-ship."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1")],
        transferred=[("ds-alpha", "aaabbb1")],
        contract_pins={("ds-alpha", "aaabbb1"): "z" * 40},  # bumped since transfer
    )
    with pytest.raises(NothingNewToPackage):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=_FakeArchiveOps(),
            today=date(2026, 5, 15),
        )


def test_package_dedup_key_is_not_the_pull_date(tmp_path: Path) -> None:
    """The key must not be local_path/version_folder: both embed the PULL date,
    so a re-pull on another day would silently re-ship gigabytes."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1")],
        transferred=[("ds-alpha", "aaabbb1")],
        day="2026-07-28",  # re-pulled on a later day -> different version_folder
    )
    with pytest.raises(NothingNewToPackage):
        enclave_package(
            manifest_path=m_path,
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=_FakeArchiveOps(),
            today=date(2026, 7, 28),
        )


def test_package_empty_selection_still_raises_nothing_to_package(
    tmp_path: Path,
) -> None:
    """An unknown --repo is a different failure from 'nothing new', and keeps
    the pre-existing hint ('run mintd enclave pull first') correct."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(tmp_path, m_path, downloaded=[("ds-alpha", "aaabbb1")])
    with pytest.raises(NothingToPackage) as ei:
        enclave_package(
            manifest_path=m_path,
            name="ds-nope",
            downloads_root=tmp_path / "downloads",
            output_dir=tmp_path / "out",
            archive_ops=_FakeArchiveOps(),
            today=date(2026, 5, 15),
        )
    assert not isinstance(ei.value, NothingNewToPackage)


# --- S4b: the generated per-transfer README ----------------------------------


def test_bundle_contains_readme(tmp_path: Path) -> None:
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(tmp_path, m_path, downloaded=[("ds-alpha", "aaabbb1")])
    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    assert "README.md" in fake.staged[0]
    text = fake.staged_text("README.md")
    assert "transfer-2026-05-15-000000" in text
    assert "test-enclave" in text
    assert "ds-alpha" in text
    assert f"{'aaabbb1' * 5}"[:7] in text


def test_bundle_readme_lists_only_packaged_products(tmp_path: Path) -> None:
    """The one way this README could still lie: naming a product the
    incremental filter excluded."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1"), ("ds-beta", "bbbccc2")],
        transferred=[("ds-alpha", "aaabbb1")],
    )
    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    text = fake.staged_text("README.md")
    assert "ds-beta" in text
    assert "ds-alpha" not in text


def test_bundle_readme_destination_matches_verify(tmp_path: Path) -> None:
    """The destination the README prints must be the one `enclave_verify`
    actually computes — prose/code drift here misplaces real data."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(tmp_path, m_path, downloaded=[("ds-alpha", "aaabbb1")])
    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    version_folder = f"{('aaabbb1' * 5)[:7]}-2026-05-15"
    # `enclave_verify` moves to data_root/<repo>/<version_folder> (enclave.py).
    assert f"data/ds-alpha/{version_folder}" in fake.staged_text("README.md")


def test_bundle_readme_names_the_real_archive(tmp_path: Path) -> None:
    """--output can name the archive anything. The README travels inside it, so
    a filename derived from transfer_id would tell the researcher to extract a
    file that is not on the media."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(tmp_path, m_path, downloaded=[("ds-alpha", "aaabbb1")])
    fake = _FakeArchiveOps()
    archive = tmp_path / "out" / "q4-2026-shipment.tar.gz"
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_archive=archive,
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    readme = fake.staged_text("README.md")
    assert "q4-2026-shipment.tar.gz" in readme
    assert "transfer-2026-05-15-000000.tar.gz" not in readme


def test_package_skips_pruned_downloads(tmp_path: Path) -> None:
    """Pruning `downloads/` after a transfer is a blessed workflow. A fully
    transferred product whose dir is gone must not abort the whole bundle."""
    m_path = tmp_path / "enclave_manifest.yaml"
    _seed(
        tmp_path,
        m_path,
        downloaded=[("ds-alpha", "aaabbb1"), ("ds-beta", "bbbccc2")],
        transferred=[("ds-alpha", "aaabbb1")],
    )
    import shutil

    shutil.rmtree(tmp_path / "downloads" / "ds-alpha")
    fake = _FakeArchiveOps()
    archive, skipped = enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )
    assert archive.exists()
    staged = fake.staged[0]
    assert any("ds-beta" in p for p in staged)
    assert not any("ds-alpha" in p for p in staged)
    assert [d.repo for d in skipped] == ["ds-alpha"]


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


def test_unsubscribed_product_is_not_packaged(tmp_path: Path) -> None:
    """THE CUSTODY INVARIANT, end to end: `enclave remove` then `enclave
    package` must not ship the product that was just revoked.

    `enclave_package` selects purely off `downloaded[]` and never consults
    `approved_products` (verified: zero references in its body), so the ONLY
    thing standing between a revoked subscription and a one-way transfer into
    the enclave is `enclave_remove` dropping the provenance row. A review round
    caught a fix that kept the row whenever a bare-primary sibling survived --
    the manifest looked right, the archive did not. This asserts on the archive
    because that is where the harm lands; asserting on downloaded[] alone would
    have passed against the very defect it exists to catch.
    """
    from mintd.enclave import ApprovedProduct, enclave_remove

    m_path = tmp_path / "enclave_manifest.yaml"
    keep_dir = tmp_path / "downloads" / "ds-alpha" / "aaaaaaa-2026-05-15"
    drop_dir = tmp_path / "downloads" / "ds-alpha" / "bbbbbbb-2026-05-15"
    for d, payload in ((keep_dir, "kept"), (drop_dir, "revoked")):
        d.mkdir(parents=True)
        (d / "data.csv").write_text(payload)

    def _item(output: str, pin: str, local: Path) -> DownloadedItem:
        return DownloadedItem(
            repo="ds-alpha", output=output, contract_pin="c" * 40,
            artifact_pin=pin * 5, fetch_strategy="dvc-import",
            downloaded_at=datetime(2026, 5, 15), local_path=str(local),
        )

    EnclaveManifest(
        enclave_name="test-enclave",
        approved_products=[
            # the sibling that survives -- a BARE PRIMARY, whose resolved output
            # is unknowable locally. That ambiguity is what tempts an
            # implementation into keeping everything.
            ApprovedProduct(repo="ds-alpha", registry_entry="e", pin="c" * 40),
            ApprovedProduct(repo="ds-alpha", registry_entry="e", pin="c" * 40,
                            source_path="data/restricted"),
        ],
        downloaded=[_item("data/primary", "aaabbb1", keep_dir),
                    _item("data/restricted", "bbbccc2", drop_dir)],
    ).save(m_path)

    class _NoClient:
        def fetch(self, name):  # enclave_remove does not use it
            raise AssertionError("no catalog fetch on this path")

    enclave_remove(_NoClient(), manifest_path=m_path, name="ds-alpha",
                   source_path="data/restricted",
                   downloads_root=tmp_path / "downloads")

    fake = _FakeArchiveOps()
    enclave_package(
        manifest_path=m_path,
        downloads_root=tmp_path / "downloads",
        output_dir=tmp_path / "out",
        archive_ops=fake,
        today=date(2026, 5, 15),
    )

    packed = "\n".join(fake.staged[-1])
    assert "aaaaaaa" in packed, "the surviving subscription must still ship"
    assert "bbbbbbb" not in packed, "the REVOKED product must not cross the gap"
