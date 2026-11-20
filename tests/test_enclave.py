"""`enclave_package`, the zip it writes, and the manifest it reads and appends to.

Packaging runs real dvc and git against a local producer, so the md5s it
checks are the ones dvc recorded and the version date is git's. The README
test runs the bundle's own commands with this system's unzip and MD5 tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mintd._config import Timeouts
from mintd._dvc_ops import SubprocessDvcOps
from mintd.catalog import InMemoryCatalogClient
from mintd.data import import_product
from mintd.enclave import (
    ChecksumMismatch,
    EnclaveManifest,
    ManifestNotAppendable,
    NothingNewToPackage,
    NothingToPackage,
    _append_rows,
    _member,
    enclave_package,
)
from mintd.errors import MintdError
from mintd.model import Metadata
from tests._harness.expect import bundle_steps
from tests._harness.git import _git

FOLDER = {"a.csv": b"alpha\n", "b.csv": b"beta\n", "c d.csv": b"gamma\n", "sub/é.csv": b"delta\n"}
HEADER = "enclave_name: enclave_x\n# approved by the data officer; transferred: below is the log\n"
# Committed at 23:30 on 2026-11-14 at -05:00, which is 2026-11-15 in UTC, and
# authored on another day: only the UTC committer date gives this folder.
VERSION_DATE = "2026-11-15"
SYSTEM = {"linux": "Linux", "darwin": "macOS", "win32": "Windows (PowerShell)"}.get(sys.platform)


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


@pytest.fixture
def world(local_producer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    for key, value in {
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "DVC_GLOBAL_CONFIG_DIR": str(tmp_path / "home" / "global"),
        "DVC_SYSTEM_CONFIG_DIR": str(tmp_path / "home" / "system"),
        "DVC_SITE_CACHE_DIR": str(tmp_path / "dvc-site"),
        "GIT_COMMITTER_DATE": "2026-11-14T23:30:00-05:00",
        "GIT_AUTHOR_DATE": "2026-11-10T12:00:00+00:00",
    }.items():
        monkeypatch.setenv(key, value)
    meta = local_producer.metadata()
    meta["project"].update(name="demo", full_name="data_demo")
    meta["repository"]["github_url"] = local_producer.url
    local_producer._write_metadata(meta)
    (local_producer.work / "data" / "final" / "sub").mkdir(parents=True)
    pin1 = local_producer.publish({"data/final": FOLDER})
    client = InMemoryCatalogClient()
    client.register(Metadata.model_validate(meta))
    enclave = tmp_path / "enclave"
    enclave.mkdir()
    _git(["init", "-b", "main", str(enclave)])
    return SimpleNamespace(
        producer=local_producer, pin1=pin1, client=client, enclave=enclave,
        manifest=enclave / "enclave_manifest.yaml", ops=SubprocessDvcOps(timeouts=Timeouts()),
    )


def _approve(w: SimpleNamespace, *pins: str, source_path: str | None = None, log: str = "transferred: []\n") -> str:
    rows = "".join(
        f"  - repo: demo\n    pin: {pin}\n" + (f"    source_path: {source_path}\n" if source_path else "")
        for pin in pins
    )
    text = HEADER + "approved_products:\n" + rows + log
    w.manifest.write_text(text, encoding="utf-8")
    return text


def _package(w: SimpleNamespace, **kwargs):
    return enclave_package(w.client, w.ops, manifest_path=w.manifest, output_dir=w.enclave / "transfers", **kwargs)


def _bundle(archive: Path) -> tuple[list[str], dict[str, bytes], dict[str, zipfile.ZipInfo], dict]:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        data = {i.filename: zf.read(i) for i in infos}
    manifest = json.loads(next(v for k, v in data.items() if k.endswith("/manifest.json")))
    return [i.filename for i in infos], data, {i.filename: i for i in infos}, manifest


def _rows(w: SimpleNamespace) -> list[dict]:
    return yaml.safe_load(w.manifest.read_text(encoding="utf-8"))["transferred"]


def test_package_writes_a_zip_that_mirrors_the_enclave_folder(world) -> None:
    approved = _approve(world, world.pin1)

    archive, skipped = _package(world)

    assert skipped == []
    names, data, infos, manifest = _bundle(archive)
    tid = archive.stem
    version = f"{VERSION_DATE}_{world.pin1[:7]}"
    dest = f"data/demo/{version}/data/final"
    files = {f"{dest}/{k}": v for k, v in FOLDER.items()}
    record = [f"transfers/{tid}/{r}" for r in ("MD5SUMS", "README.md", "manifest.json")]
    assert archive.name.startswith("transfer-") and archive.suffix == ".zip"
    assert set(names) == set(files) | set(record)
    assert names == sorted(names), "entries are in sorted order"
    for name, info in infos.items():
        assert info.compress_type == zipfile.ZIP_STORED, name
        assert stat.S_ISREG(info.external_attr >> 16), f"{name} is a regular file, not a link"
        assert "\\" not in name
    for name in record:
        assert (infos[name].external_attr >> 16) & 0o777 == 0o644, name
    assert {n: data[n] for n in files} == files, "the producer's bytes"
    assert data[f"transfers/{tid}/MD5SUMS"].decode() == "".join(
        f"{_md5(files[n])}  {n}\n" for n in sorted(files)
    )
    assert manifest["enclave_name"] == "enclave_x" and manifest["transfer_id"] == tid
    assert [(c["repo"], c["path"], c["version"], c["dest"]) for c in manifest["contents"]] == [
        ("demo", "data/final", version, dest)
    ]
    readme = data[f"transfers/{tid}/README.md"].decode()
    for needle in (archive.name, tid, "enclave_x", dest, "## Linux", "## macOS", "## Windows (PowerShell)",
                   "never overwrites", "Never edit a version folder by hand", "not extra files"):
        assert needle in readme, needle
    text = world.manifest.read_text(encoding="utf-8")
    assert text.startswith(approved.replace("transferred: []\n", "transferred:\n")), "only rows were added"
    assert [(r["event"], r["path"], r["version"], r["contract_pin"]) for r in _rows(world)] == [
        ("packaged", "data/final", version, world.pin1)
    ]
    pointer = yaml.safe_load((world.enclave / "data/imports/data_demo/data/final.dvc").read_text(encoding="utf-8"))
    assert pointer["deps"][0]["repo"]["rev_lock"] == world.pin1, "an ordinary import, at the approved pin"


def test_the_approved_pin_wins_over_an_import_at_another_commit(world) -> None:
    """`data import --bump` moves the import; until the manifest is edited,
    what crosses is still the approved version."""
    pin2 = world.producer.publish({"data/final": {**FOLDER, "a.csv": b"alpha v2\n"}}, message="v2")
    _approve(world, world.pin1)
    world.ops.init(cwd=world.enclave)
    import_product(world.client, world.ops, "demo", cwd=world.enclave, dest_root=world.enclave / "data/imports",
                   path="data/final", rev=pin2)

    archive, _ = _package(world)

    _, data, _, manifest = _bundle(archive)
    assert [c["contract_pin"] for c in manifest["contents"]] == [world.pin1]
    assert data[f"data/demo/{VERSION_DATE}_{world.pin1[:7]}/data/final/a.csv"] == b"alpha\n"


def test_two_approved_pins_of_one_output_both_cross(world) -> None:
    """They share one import folder, so each is written into the zip right
    after its import; the entries still come out sorted."""
    pin2 = world.producer.publish({"data/final": {**FOLDER, "a.csv": b"alpha v2\n"}}, message="v2")
    first, second = sorted([world.pin1, pin2], key=lambda p: p[:7], reverse=True)
    _approve(world, first, second, source_path="data/final")

    archive, _ = _package(world)

    names, data, _, _ = _bundle(archive)
    assert names == sorted(names)
    assert data[f"data/demo/{VERSION_DATE}_{world.pin1[:7]}/data/final/a.csv"] == b"alpha\n"
    assert data[f"data/demo/{VERSION_DATE}_{pin2[:7]}/data/final/a.csv"] == b"alpha v2\n"


def test_what_has_crossed_is_skipped_and_resend_bundles_it_again(world) -> None:
    _approve(world, world.pin1)
    first, _ = _package(world)
    after_first = world.manifest.read_bytes()

    with pytest.raises(NothingNewToPackage):
        _package(world)
    assert world.manifest.read_bytes() == after_first
    assert sorted(p.name for p in (world.enclave / "transfers").iterdir()) == [first.name]

    kept = first.read_bytes()
    with pytest.raises(MintdError, match="already exists"):
        _package(world, resend=True, output_archive=first)
    assert first.read_bytes() == kept, "an existing zip is never replaced or removed"
    assert world.manifest.read_bytes() == after_first

    again, skipped = _package(world, resend=True)

    assert skipped == []
    assert (first.stem.endswith("-000000"), again.stem.endswith("-000001")) == (True, True)
    assert [r["transfer_id"] for r in _rows(world)] == [first.stem, again.stem]


def test_a_row_from_older_mintd_counts_as_crossed_at_its_pin(world) -> None:
    """Older rows record no path; one at the approved pin means that version
    already crossed, so it is neither imported nor bundled again."""
    _approve(world, world.pin1, log=(
        "transferred:\n"
        f"- repo: demo\n  contract_pin: '{world.pin1}'\n  artifact_pin: '{'a' * 32}'\n"
        "  transfer_date: '2026-07-28'\n  transfer_id: transfer-2026-07-28-000000\n  local_path: /x\n"
    ))

    with pytest.raises(NothingNewToPackage):
        _package(world)
    assert not (world.enclave / "data").exists()


def test_a_file_that_no_longer_matches_its_pin_is_refused(world) -> None:
    _approve(world, world.pin1)
    _package(world)
    before = world.manifest.read_bytes()
    tampered = world.enclave / "data/imports/data_demo/data/final/b.csv"
    tampered.unlink()  # never write through a link into dvc's cache
    tampered.write_bytes(b"betA\n")

    with pytest.raises(ChecksumMismatch, match="does not match its pin"):
        _package(world, resend=True)

    assert world.manifest.read_bytes() == before
    assert len(list((world.enclave / "transfers").iterdir())) == 1, "the partial zip is removed"


def test_package_refuses_before_writing_when_transferred_is_not_last(tmp_path: Path) -> None:
    """Checked before dvc init, any import or any zip: no client or dvc is
    needed to refuse."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(f"enclave_name: e\ntransferred: []\napproved_products:\n  - repo: d\n    pin: {'c' * 40}\n", encoding="utf-8")

    with pytest.raises(ManifestNotAppendable):
        enclave_package(None, None, manifest_path=m, output_dir=tmp_path / "transfers")  # type: ignore[arg-type]

    assert sorted(p.name for p in tmp_path.iterdir()) == ["enclave_manifest.yaml"]


@pytest.mark.parametrize("row", [
    f"  - repo: d\n    contract_pin: '{'c' * 40}'\n",
    f"  -\n    repo: d\n    contract_pin: '{'c' * 40}'\n",
    f"  [{{repo: d, contract_pin: '{'c' * 40}'}}]\n",
], ids=["indented-block-list", "lone-dash", "indented-flow-list"])
def test_package_refuses_an_indented_log_before_writing(tmp_path: Path, row: str) -> None:
    """Rows are appended at column 0; after a log written as an indented or
    flow-style list they would not read back, and every later package would
    fail on the manifest. Refused before anything is imported or written."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(
        f"enclave_name: e\napproved_products:\n  - repo: d\n    pin: {'c' * 40}\ntransferred:\n{row}",
        encoding="utf-8",
    )
    before = m.read_bytes()

    with pytest.raises(ManifestNotAppendable, match="would not read back"):
        enclave_package(None, None, manifest_path=m, output_dir=tmp_path / "transfers")  # type: ignore[arg-type]

    assert m.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["enclave_manifest.yaml"]


def test_a_name_with_no_approval_is_nothing_to_package(tmp_path: Path) -> None:
    (tmp_path / "enclave_manifest.yaml").write_text(
        f"enclave_name: e\napproved_products:\n  - repo: other\n    pin: {'c' * 40}\ntransferred: []\n",
        encoding="utf-8",
    )

    with pytest.raises(NothingToPackage):
        enclave_package(
            None, None, manifest_path=tmp_path / "enclave_manifest.yaml", output_dir=tmp_path, name="demo",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("text", [None, "enclave_name: [\n", "enclave_name: e\napproved_products:\n  - repo: d\n    pin: abc1234\n"],
                         ids=["missing", "not-yaml", "short-pin"])
def test_a_missing_or_malformed_manifest_is_an_error_with_a_hint(tmp_path: Path, text: str | None) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    if text is not None:
        m.write_text(text, encoding="utf-8")

    with pytest.raises(MintdError) as raised:
        EnclaveManifest.load(m)

    assert str(m) in str(raised.value) and raised.value.hint


@pytest.mark.parametrize("pin", ["", "abc1234", "v1.0.0", "HEAD", "A" * 40])
def test_a_pin_must_be_a_full_commit_sha(pin: str, tmp_path: Path) -> None:
    """A tag or branch can move after it was approved; an empty pin used to
    mean HEAD in places."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(yaml.safe_dump({"enclave_name": "e", "approved_products": [{"repo": "d", "pin": pin}]}), encoding="utf-8")
    with pytest.raises(MintdError, match="not a valid enclave manifest"):
        EnclaveManifest.load(m)


def test_a_misspelt_key_is_refused_not_ignored(tmp_path: Path) -> None:
    """Ignoring `source-path` would approve the primary instead."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(f"enclave_name: e\napproved_products:\n  - repo: d\n    pin: {'c' * 40}\n    source-path: data/x\n",
                 encoding="utf-8")
    with pytest.raises(MintdError, match="not a valid enclave manifest"):
        EnclaveManifest.load(m)


def test_a_manifest_older_mintd_wrote_still_loads() -> None:
    old = yaml.safe_load((Path(__file__).parent / "fixtures" / "enclave_manifest_v2_minimal.yaml").read_text(encoding="utf-8"))
    old["downloaded"] = [{"repo": "demo-xw", "output": "outputs/cms_based/"}]
    old["transferred"] = [{"repo": "demo-xw", "contract_pin": "c" * 40, "artifact_pin": "a" * 32}]

    m = EnclaveManifest.model_validate(old)

    assert [(ap.repo, ap.source_path) for ap in m.approved_products] == [("demo-xw", "outputs/cms_based/")]
    assert m.transferred[0]["contract_pin"] == "c" * 40


@pytest.mark.parametrize("name", ["../x", "/abs/x", "a//b", "a/./b", "a/../b", "a\\b", "a\nb", "a\rb", "a/"])
def test_an_unsafe_member_name_is_refused(name: str) -> None:
    """Producer paths reach zip members and MD5SUMS lines; unzip must never
    write outside the enclave folder, and MD5SUMS is one path per line."""
    with pytest.raises(MintdError, match="refusing to bundle"):
        _member(name)
    assert _member("data/demo/2026-11-15_abc1234/data/final/c d.csv")


def test_rows_are_appended_and_nothing_before_them_changes(tmp_path: Path) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(HEADER + "approved_products: []\ntransferred: []  \n", encoding="utf-8")

    _append_rows(m, [{"event": "packaged", "n": 1}])
    first = m.read_text(encoding="utf-8")
    _append_rows(m, [{"event": "packaged", "n": 2}])

    assert first.startswith(HEADER + "approved_products: []\ntransferred:\n")
    assert m.read_text(encoding="utf-8").startswith(first)
    assert [r["n"] for r in yaml.safe_load(m.read_text(encoding="utf-8"))["transferred"]] == [1, 2]


def test_rows_append_after_a_log_older_mintd_wrote(tmp_path: Path) -> None:
    """A normal manifest: a column-0 block-style log, as older mintd wrote it,
    with a comment, takes new rows that read back as its last entries."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(
        HEADER + "approved_products: []\ntransferred:\n# landed by hand\n"
        f"- repo: d\n  contract_pin: '{'c' * 40}'\n  transfer_id: transfer-2026-07-28-000000\n",
        encoding="utf-8",
    )
    before = m.read_text(encoding="utf-8")

    _append_rows(m, [{"event": "packaged", "n": 1}])

    assert m.read_text(encoding="utf-8").startswith(before)
    assert [r.get("n") for r in yaml.safe_load(m.read_text(encoding="utf-8"))["transferred"]] == [None, 1]


def test_append_refuses_when_transferred_is_not_last(tmp_path: Path) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text("enclave_name: e\ntransferred: []\nnotes: x\n", encoding="utf-8")
    before = m.read_bytes()

    with pytest.raises(ManifestNotAppendable):
        _append_rows(m, [{"event": "packaged"}])

    assert m.read_bytes() == before


def _step(line: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one README line the way a person on this system pastes it."""
    if sys.platform == "win32":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(line.encode("utf-16-le")).decode()]
    elif sys.platform == "darwin":
        argv = ["zsh", "-c", line]  # macOS's default shell
    else:
        argv = ["sh", "-c", line]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=300)


def _lines(run: subprocess.CompletedProcess[str], ending: str) -> list[str]:
    return [line for line in (run.stdout + run.stderr).splitlines() if line.rstrip().endswith(ending)]


def test_the_readme_commands_unzip_and_check_this_bundle(world, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle README's two lines for this system, run as written against a
    real bundle: Linux runs `unzip -n` and `md5sum -c`, macOS the `md5` loop
    under zsh, and Windows `Expand-Archive` and the `Get-FileHash` loop under
    Windows PowerShell. Every entry is forced to zip64, a resend changes
    nothing in place, and a damaged file fails the check."""
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 4)
    _approve(world, world.pin1)
    archive, _ = _package(world)
    with zipfile.ZipFile(archive) as zf:
        assert all(i.extract_version >= 45 for i in zf.infolist() if i.file_size > 4), "zip64 entries"
        readme = zf.read(f"transfers/{archive.stem}/README.md").decode()
    unzip, check = bundle_steps(readme, SYSTEM)
    inside = tmp_path / "inside"
    inside.mkdir()
    shutil.copy(archive, inside / archive.name)
    version = inside / "data" / "demo" / f"{VERSION_DATE}_{world.pin1[:7]}" / "data" / "final"

    # 1. Unzip and check.
    assert _step(unzip, inside).returncode == 0
    ok = _step(check, inside)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert len(_lines(ok, ": OK")) == len(FOLDER)
    assert {k: (version / k).read_bytes() for k in FOLDER} == FOLDER

    # 2. A resend unzipped over it changes nothing in place, and adds its record.
    stamps = {k: (version / k).stat().st_mtime_ns for k in FOLDER}
    record1 = {p.name: p.read_bytes() for p in (inside / "transfers" / archive.stem).iterdir()}
    again, _ = _package(world, resend=True)
    shutil.copy(again, inside / again.name)
    unzip2, check2 = bundle_steps(zipfile.ZipFile(again).read(f"transfers/{again.stem}/README.md").decode(), SYSTEM)
    _step(unzip2, inside)  # on Windows, Expand-Archive reports each file already there
    assert {k: (version / k).stat().st_mtime_ns for k in FOLDER} == stamps
    assert {p.name: p.read_bytes() for p in (inside / "transfers" / archive.stem).iterdir()} == record1
    assert (inside / "transfers" / again.stem / "MD5SUMS").is_file(), "the resent bundle's record lands"
    second = _step(check2, inside)
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(_lines(second, ": OK")) == len(FOLDER)

    # Run from the wrong folder, or before unzipping, the check fails too.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _step(check, empty).returncode != 0, "a missing MD5SUMS is not a pass"

    # 3. A damaged file fails the check, and only that file.
    damaged = version / "b.csv"
    os.chmod(damaged, damaged.stat().st_mode | stat.S_IWUSR)
    damaged.write_bytes(damaged.read_bytes()[:-1] + b"!")
    bad = _step(check, inside)
    assert bad.returncode != 0
    failed = _lines(bad, ": FAILED")
    assert len(failed) == 1 and "b.csv" in failed[0], bad.stdout + bad.stderr
    assert len(_lines(bad, ": OK")) == len(FOLDER) - 1


def test_package_runs_with_its_spinner_in_human_mode(world, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The real handler with a real Reporter, as a person runs it. Each import
    shows its own spinner, and rich allows one live display at a time, so the
    handler must not hold another around the whole package."""
    from mintd import cli

    _approve(world, world.pin1)
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: world.client)
    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", lambda cfg, reporter=None, **_: world.ops)

    rc = cli.main(["enclave", "package", "--manifest", str(world.manifest)])

    assert rc == 0, capsys.readouterr().err
    assert len(list((world.enclave / "transfers").glob("*.zip"))) == 1


def test_a_pin_the_producer_cannot_serve_names_git_auth(world, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The pin's commit date is fetched with git, so an unreachable producer or
    a pin it does not have ends with the same advice `data import` gives."""
    from mintd import cli

    _approve(world, "f0" * 20, source_path="data/final")
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: world.client)
    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", lambda cfg, reporter=None, **_: world.ops)

    rc = cli.main(["enclave", "package", "--manifest", str(world.manifest)])

    assert rc == 1
    assert "check git auth" in capsys.readouterr().err
    assert not (world.enclave / "transfers").exists(), "nothing written"
