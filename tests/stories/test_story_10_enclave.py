"""Story 10: Pinned data products cross into a locked-down server.

- Given: a published product with a folder output and a file big enough that
  S3 takes it in parts, and an enclave whose manifest, shaped as an existing
  enclave's is, approves both at one pin and keeps an older row already sent.
- When: I run `mintd enclave package` outside, copy the zip into the enclave
  folder, and run the two commands its README gives for this system:
  `unzip -n` and `md5sum -c` on Linux, `unzip -n` and the `md5` loop on macOS.
- Pass:
  - only the approved files download, and the manifest only gains rows;
  - every file lands under data/<product>/<UTC commit date>_<pin7>/ with the
    producer's bytes, and every check line is OK;
  - a bundle damaged in transit fails the check on the damaged file;
  - a rerun of package has nothing new, and a second unzip changes nothing;
  - a newer pin lands beside the older one; what already crossed is neither
    downloaded nor bundled again, and each bundle adds its own record.
- Catches: a bundle that lands without any file being checked.
- Mutation: in `enclave._bundle_readme`, drop `md5sum -c` for `true` -> the
  damaged bundle's check exits 0, red.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests._harness.expect import bundle_steps, md5, result, s3_state
from tests._harness.git import _git
from tests._harness.producer import LocalProducer, build_local_producer, register
from tests._harness.s3 import BUCKET, S3Server

NAME = "enclave-demo"
PREFIX = f"lab/{NAME}"
FOLDER = {"a.csv": b"alpha\n", "b.csv": b"beta\n"}
OLD_PIN = "a1" * 20  # a version that crossed long ago, under an older mintd
SYSTEM = {"linux": "Linux", "darwin": "macOS"}.get(sys.platform)


def _manifest(pin: str) -> str:
    """The shape an existing enclave's manifest has, with made-up values."""
    return (
        "schema_version: '2.0'\n"
        "enclave_name: enclave_demo\n"
        "registry_url: https://example.org/registry.git\n"
        "approved_products:\n"
        f"- repo: {NAME}\n  registry_entry: catalog/data/{NAME}.yaml\n  pin: {pin}\n  source_path: null\n  all: false\n"
        f"- repo: {NAME}\n  registry_entry: catalog/data/{NAME}.yaml\n  pin: {pin}\n  source_path: data/big.bin\n  all: false\n"
        f"- repo: {NAME}\n  registry_entry: catalog/data/{NAME}.yaml\n  pin: {OLD_PIN}\n  source_path: data/old\n  all: false\n"
        "downloaded: []\n"
        "transferred:\n"
        f"- repo: {NAME}\n  contract_pin: {OLD_PIN}\n  artifact_pin: {'a' * 32}\n"
        "  transfer_date: '2026-07-28'\n  transfer_id: transfer-2026-07-28-000000\n  local_path: /outside/downloads\n"
    )


def _rows(manifest: Path) -> list[dict]:
    return [json.loads(line[2:]) for line in manifest.read_text(encoding="utf-8").splitlines() if line.startswith("- {")]


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): md5(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


def _version(producer: LocalProducer, pin: str) -> str:
    seconds = int(_git(["log", "-1", "--format=%ct", pin], cwd=producer.work).strip())
    return f"{datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()}_{pin[:7]}"


def _land(zip_path: Path, inside: Path) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    """Copy the zip into the enclave folder and run its README's two lines."""
    shutil.copy(zip_path, inside / zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        readme = zf.read(f"transfers/{zip_path.stem}/README.md").decode()
    shell = "zsh" if sys.platform == "darwin" else "sh"
    return tuple(  # type: ignore[return-value]
        subprocess.run([shell, "-c", line], cwd=inside, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        for line in bundle_steps(readme, SYSTEM)
    )


def _damage(zip_path: Path, member: str) -> None:
    """Flip the last byte of `member`'s stored data, as a bad copy would."""
    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(member)
    raw = bytearray(zip_path.read_bytes())
    start = info.header_offset + 30 + int.from_bytes(raw[info.header_offset + 26:info.header_offset + 28], "little") \
        + int.from_bytes(raw[info.header_offset + 28:info.header_offset + 30], "little")
    raw[start + info.compress_size - 1] ^= 0xFF
    zip_path.write_bytes(bytes(raw))


@pytest.fixture
def published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, s3_server: S3Server, remote_registry_empty: Path, mintd) -> tuple[LocalProducer, bytes]:
    """The producer, and its big file's bytes."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")  # for the in-process dvc push
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    producer = build_local_producer(tmp_path / "prod", remote={
        "url": f"s3://{BUCKET}/{PREFIX}", "endpointurl": s3_server.endpoint, "version_aware": "true",
    })
    meta = producer.metadata()
    meta["project"].update(name=NAME, full_name=f"data_{NAME}")
    meta["repository"]["github_url"] = producer.url
    producer._write_metadata(meta)
    producer.add_output("data/big.bin")
    # Over s3fs's 100 MiB single-PUT ceiling, so dvc push uploads it in parts.
    big = os.urandom(110 * 2**20)
    producer.publish({"data/final": FOLDER, "data/big.bin": big})
    register(remote_registry_empty, producer.metadata(), tmp_path / "registry-work")
    mintd.config.write_text(
        f"registry_url: {remote_registry_empty}\ncache_dir: {tmp_path / 'mintd-cache'}\n", encoding="utf-8"
    )
    return producer, big


@pytest.mark.skipif(SYSTEM is None, reason="the stories run on Linux and macOS")
def test_pinned_products_cross_into_a_locked_down_server(published, mintd, s3_server: S3Server, tmp_path: Path) -> None:
    producer, big = published
    pin1 = producer.head_sha
    assert s3_state(s3_server, PREFIX).puts["data/big.bin"] >= 2, "the big file went up in parts"
    outside = tmp_path / "outside"
    outside.mkdir()
    manifest = outside / "enclave_manifest.yaml"
    manifest.write_text(_manifest(pin1), encoding="utf-8")
    _git(["init", "-b", "main", str(outside)])
    _git(["add", "-A"], cwd=outside)
    _git(["commit", "-m", "enclave"], cwd=outside)
    written = manifest.read_text(encoding="utf-8")
    s3_server.gets.clear()

    # 1. Package.
    run = mintd("enclave", "package", cwd=outside)

    assert run.returncode == 0, run.stderr
    zip1 = outside / result(run.stdout)["archive"]
    assert set(s3_state(s3_server, PREFIX).gets) == {"data/final/a.csv", "data/final/b.csv", "data/big.bin"}
    assert manifest.read_text(encoding="utf-8").startswith(written), "rows are only appended"
    assert [(r["path"], r["contract_pin"]) for r in _rows(manifest)] == [("data/final", pin1), ("data/big.bin", pin1)]

    # 2. Land it in a fresh enclave folder.
    inside = tmp_path / "inside"
    inside.mkdir()
    unzip, check = _land(zip1, inside)
    v1 = f"data/{NAME}/{_version(producer, pin1)}"
    assert unzip.returncode == 0, unzip.stderr
    assert check.returncode == 0, check.stdout + check.stderr
    assert [line.endswith(": OK") for line in check.stdout.splitlines()] == [True] * 3
    data = {k: v for k, v in _tree(inside).items() if k.startswith("data/")}
    assert data == {
        f"{v1}/data/final/a.csv": md5(FOLDER["a.csv"]),
        f"{v1}/data/final/b.csv": md5(FOLDER["b.csv"]),
        f"{v1}/data/big.bin": hashlib.md5(big, usedforsecurity=False).hexdigest(),
    }
    assert sorted(p.name for p in inside.iterdir()) == sorted([zip1.name, "data", "transfers"])
    landed = _tree(inside)
    again, _ = _land(zip1, inside)
    assert again.returncode == 0 and _tree(inside) == landed, "a second unzip changes nothing"

    # 3. A bundle damaged in transit fails the check, on the damaged file.
    spoiled = tmp_path / "spoiled"
    spoiled.mkdir()
    damaged = tmp_path / "damaged" / zip1.name
    damaged.parent.mkdir()
    shutil.copy(zip1, damaged)
    _damage(damaged, f"{v1}/data/final/b.csv")
    _, bad = _land(damaged, spoiled)
    assert bad.returncode != 0
    assert [line for line in bad.stdout.splitlines() if line.endswith("FAILED")] == [f"{v1}/data/final/b.csv: FAILED"]

    # 4. Nothing new.
    rerun = mintd("enclave", "package", cwd=outside, json=False)
    assert rerun.returncode == 0 and "nothing new to package" in rerun.stderr, rerun.stderr

    # 5. A bump: the folder moves to a newer pin; the big file stays approved at
    # the old one, which has already crossed.
    producer.publish({"data/final": {"a.csv": b"alpha v2\n", "b.csv": b"beta\n"}}, message="v2")
    pin2 = producer.head_sha
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(f"pin: {pin1}\n  source_path: null", f"pin: {pin2}\n  source_path: null", 1),
        encoding="utf-8",
    )
    s3_server.gets.clear()

    bump = mintd("enclave", "package", cwd=outside)

    assert bump.returncode == 0, bump.stderr
    zip2 = outside / result(bump.stdout)["archive"]
    assert "data/big.bin" not in s3_state(s3_server, PREFIX).gets, "what crossed is not downloaded again"
    with zipfile.ZipFile(zip2) as zf:
        assert not [n for n in zf.namelist() if n.endswith("big.bin")], "nor bundled again"
    before = _tree(inside)
    unzip2, check2 = _land(zip2, inside)
    v2 = f"data/{NAME}/{_version(producer, pin2)}"
    assert unzip2.returncode == 0 and check2.returncode == 0, check2.stdout + check2.stderr
    after = _tree(inside)
    assert {k: v for k, v in after.items() if k in before} == before, "the old version is untouched"
    assert after[f"{v2}/data/final/a.csv"] == md5(b"alpha v2\n"), "the new version lands beside it"
    assert sorted(p.name for p in (inside / "transfers").iterdir()) == sorted([zip1.stem, zip2.stem])
    assert len(_rows(manifest)) == 3
