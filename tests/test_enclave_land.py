"""Tests for `land.py` — the stdlib lander shipped inside every transfer archive.

Exercised by subprocess against a REAL `.tar.gz` built with `TarGzArchiveOps`
(never the fake), so the archive shape under test is the shape that ships.
`land.py` must run with no pip, no network, no DVC, no PyYAML and no mintd, so
nothing here imports it as a module.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import subprocess
import sys
import tarfile

import yaml

from mintd.enclave import (
    DownloadedItem,
    EnclaveManifest,
    TransferredItem,
    enclave_package,
    enclave_verify,
)


def _outside_repo(tmp_path: Path, *, repo: str = "ds-alpha") -> Path:
    """Build an outside-enclave repo with one downloaded product, package it
    for real, and return the archive path."""
    out = tmp_path / "outside"
    m_path = out / "enclave_manifest.yaml"
    pin = "aaabbb1" * 5
    vf = f"{pin[:7]}-2026-05-15"
    dl = out / "downloads" / repo / vf
    dl.mkdir(parents=True)
    (dl / "data.csv").write_text("col1,col2\n1,2\n")
    EnclaveManifest(
        enclave_name="enclave-hcal",
        downloaded=[
            DownloadedItem(
                repo=repo,
                output="data.csv",
                contract_pin="c" * 40,
                artifact_pin=pin,
                fetch_strategy="dvc-import",
                downloaded_at=datetime(2026, 5, 15),
                local_path=str(dl),
            )
        ],
    ).save(m_path)
    archive, _skipped = enclave_package(
        manifest_path=m_path,
        downloads_root=out / "downloads",
        output_dir=out / "transfers",
        today=date(2026, 5, 15),
    )
    return archive


def _inside_repo(tmp_path: Path, *, enclave_name: str = "enclave-hcal") -> Path:
    inside = tmp_path / "inside"
    inside.mkdir()
    EnclaveManifest(enclave_name=enclave_name).save(
        inside / "enclave_manifest.yaml"
    )
    return inside


def _extract(archive: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(dest)
    return dest


def _run_land(extracted: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(extracted / "land.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_archive_ships_lander_and_readme(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    with tarfile.open(archive, "r:gz") as tf:
        names = {n.lstrip("./") for n in tf.getnames()}
    assert "land.py" in names
    assert "README.md" in names
    assert "_transfer_manifest.json" in names
    assert "_transfer_manifest.yaml" in names


def test_land_places_data_and_records_audit_row(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, inside / "incoming")

    proc = _run_land(extracted, inside)
    assert proc.returncode == 0, proc.stderr

    landed = inside / "data" / "ds-alpha" / "aaabbb1-2026-05-15"
    assert (landed / "data.csv").read_text() == "col1,col2\n1,2\n"

    # The audit row must parse back as a real TransferredItem — this is the
    # BQ#2 guard: a non-mintd script wrote the trail, so mintd must still read it.
    reloaded = EnclaveManifest.load(inside / "enclave_manifest.yaml")
    assert len(reloaded.transferred) == 1
    t = reloaded.transferred[0]
    assert t.repo == "ds-alpha"
    assert t.artifact_pin == "aaabbb1" * 5
    assert Path(t.local_path).name == "aaabbb1-2026-05-15"
    # transfer_date must equal what `enclave_verify` would have recorded for the
    # same archive — it takes the date half of the manifest's UTC timestamp, not
    # the `today=` seam (which only drives the transfer_id and the OUTSIDE row).
    tm = yaml.safe_load((extracted / "_transfer_manifest.yaml").read_text())
    assert t.transfer_date == datetime.fromisoformat(tm["transfer_date"]).date()


def test_land_then_mintd_verify_is_noop(tmp_path: Path) -> None:
    """The round-trip that makes the two landing paths converge: after land.py,
    a machine that DOES have mintd must see nothing left to do."""
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, inside / "incoming")
    assert _run_land(extracted, inside).returncode == 0

    _path, written = enclave_verify(
        extracted_dir=extracted,
        manifest_path=inside / "enclave_manifest.yaml",
        data_root=inside / "data",
    )
    assert written == []


def test_land_is_idempotent(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, inside / "incoming")
    assert _run_land(extracted, inside).returncode == 0

    second = _run_land(extracted, inside)
    assert second.returncode == 0
    assert "nothing to land" in second.stdout
    reloaded = EnclaveManifest.load(inside / "enclave_manifest.yaml")
    assert len(reloaded.transferred) == 1


def test_land_appends_to_existing_transferred(tmp_path: Path) -> None:
    """The append must not disturb the existing prefix — transferred[] is
    append-only and mintd's own save() enforces that on the next write."""
    archive = _outside_repo(tmp_path)
    inside = tmp_path / "inside"
    inside.mkdir()
    prior = TransferredItem(
        repo="ds-prior",
        contract_pin="p" * 40,
        artifact_pin="q" * 32,
        transfer_date=date(2026, 1, 1),
        transfer_id="transfer-2026-01-01-000000",
        local_path="/prior/ds-prior",
    )
    EnclaveManifest(enclave_name="enclave-hcal", transferred=[prior]).save(
        inside / "enclave_manifest.yaml"
    )
    extracted = _extract(archive, inside / "incoming")
    assert _run_land(extracted, inside).returncode == 0

    reloaded = EnclaveManifest.load(inside / "enclave_manifest.yaml")
    assert len(reloaded.transferred) == 2
    assert reloaded.transferred[0] == prior
    # And mintd can still save it — the append-only guard sees an intact prefix.
    reloaded.save(inside / "enclave_manifest.yaml")


def test_land_refuses_wrong_enclave_and_moves_nothing(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path, enclave_name="enclave-cms")
    extracted = _extract(archive, inside / "incoming")

    proc = _run_land(extracted, inside)
    assert proc.returncode == 1
    assert "enclave-hcal" in proc.stderr and "enclave-cms" in proc.stderr
    assert not (inside / "data" / "ds-alpha").exists()
    assert (extracted / "ds-alpha" / "aaabbb1-2026-05-15" / "data.csv").is_file()
    assert EnclaveManifest.load(inside / "enclave_manifest.yaml").transferred == []


def test_land_dry_run_changes_nothing(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, inside / "incoming")
    before = (inside / "enclave_manifest.yaml").read_bytes()

    proc = _run_land(extracted, inside, "--dry-run")
    assert proc.returncode == 0
    assert "would land" in proc.stdout
    assert not (inside / "data" / "ds-alpha").exists()
    assert (inside / "enclave_manifest.yaml").read_bytes() == before


def test_land_without_extracting_gives_actionable_error(tmp_path: Path) -> None:
    """The single most likely mistake: running the lander from the USB stick."""
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, tmp_path / "elsewhere")
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "land.py").write_text(
        (extracted / "land.py").read_text(encoding="utf-8"), encoding="utf-8"
    )

    proc = _run_land(stray, inside)
    assert proc.returncode == 1
    assert "_transfer_manifest.json" in proc.stderr
    assert "tar -xzf" in proc.stderr


def test_land_finds_repo_from_outside_cwd(tmp_path: Path) -> None:
    """Extracted outside the repo, run from an unrelated cwd, --repo given."""
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, tmp_path / "media_usb")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_land(extracted, elsewhere, "--repo", str(inside))
    assert proc.returncode == 0, proc.stderr
    assert (inside / "data" / "ds-alpha" / "aaabbb1-2026-05-15" / "data.csv").is_file()


def test_land_leaves_no_data_behind_in_extraction_dir(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    inside = _inside_repo(tmp_path)
    extracted = _extract(archive, inside / "incoming")
    assert _run_land(extracted, inside).returncode == 0
    assert not (extracted / "ds-alpha").exists()


def test_land_readme_is_readable_and_names_this_transfer(tmp_path: Path) -> None:
    archive = _outside_repo(tmp_path)
    extracted = _extract(archive, tmp_path / "x")
    text = (extracted / "README.md").read_text(encoding="utf-8")
    assert "transfer-2026-05-15-000000" in text
    assert "enclave-hcal" in text
    assert "data/ds-alpha/aaabbb1-2026-05-15" in text
    assert "land.py" in text


def test_archive_root_is_group_readable(tmp_path: Path) -> None:
    """GNU tar applies the archived root member's mode to an existing
    extraction target; 0700 would strip group access on a shared server."""
    archive = _outside_repo(tmp_path)
    with tarfile.open(archive, "r:gz") as tf:
        root = [m for m in tf.getmembers() if m.name in (".", "./")]
        assert root, tf.getnames()[:5]
        assert root[0].mode & 0o055 == 0o055
