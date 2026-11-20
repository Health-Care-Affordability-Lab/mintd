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
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mintd._config import Timeouts
from mintd._dvc_ops import SubprocessDvcOps
from mintd._dvc_state import dvc_cache_dir
from mintd._fast_sync_ops import DvcFileEntry, cache_path_for, dir_manifest_md5, ensure_dir_manifest, read_cached_dir_manifest
from mintd._console import Reporter
from mintd.check import _POINTER_ABSENT, _match_out
from mintd.catalog import InMemoryCatalogClient
from mintd.data import import_product
from mintd.enclave import (
    ChecksumMismatch,
    EnclaveManifest,
    ManifestNotAppendable,
    NothingNewToPackage,
    NothingToPackage,
    _append_rows,
    _records,
    _member,
    _check_against_producer,
    _hold,
    _Version,
    enclave_package,
)
from mintd.errors import MintdError
from mintd.model import Metadata
from tests._harness.expect import bundle_steps
from tests._harness.git import _git
from tests._harness.producer import build_local_producer

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

    with pytest.raises(ChecksumMismatch, match="does not match its pin") as raised:
        _package(world, resend=True)

    assert raised.value.hint == (
        f"import it again at the pin: 'mintd data import demo --force --path data/final --rev {world.pin1}'"
    ), "the cache copy hashes to its name, so nothing is named to delete"
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


@pytest.mark.parametrize("text", [
    "enclave_name: e\ntransferred: []\nnotes: x\n",
    # The file still parses and `transferred` is still a list, but the rows
    # land under `notes`: only the "ends in exactly these rows" check refuses.
    "enclave_name: e\ntransferred:\n- {a: 1}\nnotes:\n- x\n",
], ids=["a-later-key", "a-later-key-holding-a-list"])
def test_append_refuses_when_transferred_is_not_last(tmp_path: Path, text: str) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text(text, encoding="utf-8")
    before = m.read_bytes()

    with pytest.raises(ManifestNotAppendable):
        _append_rows(m, [{"event": "packaged"}])

    assert m.read_bytes() == before


def _step(line: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one README line the way a person on this system pastes it."""
    env = None
    if sys.platform == "win32":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(line.encode("utf-16-le")).decode()]
        # A PowerShell 7 parent (CI's shell) leaves its own module path, where
        # Windows PowerShell finds no Get-Content; a window opened from the
        # Start menu has none.
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    elif sys.platform == "darwin":
        argv = ["zsh", "-c", line]  # macOS's default shell
    else:
        argv = ["sh", "-c", line]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, timeout=300, env=env)


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
    assert len(_lines(ok, ": OK")) == len(FOLDER), ok.stdout + ok.stderr
    assert {k: (version / k).read_bytes() for k in FOLDER} == FOLDER
    # A last line with no newline after it is checked too.
    sums = inside / "transfers" / archive.stem / "MD5SUMS"
    sums.write_bytes(sums.read_bytes().rstrip(b"\r\n"))
    trimmed = _step(check, inside)
    assert trimmed.returncode == 0 and len(_lines(trimmed, ": OK")) == len(FOLDER), trimmed.stdout + trimmed.stderr

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
    assert len(_lines(second, ": OK")) == len(FOLDER), second.stdout + second.stderr

    # Run from the wrong folder, or before unzipping, the check fails too.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _step(check, empty).returncode != 0, "a missing MD5SUMS is not a pass"
    # A check that checked nothing is not a pass either.
    (empty / "transfers" / archive.stem).mkdir(parents=True)
    (empty / "transfers" / archive.stem / "MD5SUMS").write_bytes(b"")
    assert _step(check, empty).returncode != 0, "an empty MD5SUMS is not a pass"

    # 3. A damaged file fails the check, and only that file.
    damaged = version / "b.csv"
    os.chmod(damaged, damaged.stat().st_mode | stat.S_IWUSR)
    damaged.write_bytes(damaged.read_bytes()[:-1] + b"!")
    bad = _step(check, inside)
    assert bad.returncode != 0
    failed = _lines(bad, ": FAILED")
    assert len(failed) == 1 and "b.csv" in failed[0], bad.stdout + bad.stderr
    assert len(_lines(bad, ": OK")) == len(FOLDER) - 1


@pytest.mark.parametrize("flags, source_path", [([], None), (["--json"], "data/final/a.csv")],
                         ids=["human", "json, with a warning"])
def test_package_runs_through_the_cli(world, monkeypatch: pytest.MonkeyPatch, capsys, flags, source_path) -> None:
    """The real handler with a real Reporter, as a person runs it. Each import
    shows its own spinner, and rich allows one live display at a time, so the
    handler must not hold another around the whole package. With --json the
    result carries the warnings manifest.json records."""
    from mintd import cli

    _approve(world, world.pin1, source_path=source_path)
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: world.client)
    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", lambda cfg, reporter=None, **_: world.ops)

    rc = cli.main([*flags, "enclave", "package", "--manifest", str(world.manifest)])

    out = capsys.readouterr()
    assert rc == 0, out.err
    (archive,) = (world.enclave / "transfers").glob("*.zip")
    if flags:
        # The test's own dvc ops print to stdout; the result is the last line.
        assert json.loads(out.out.strip().splitlines()[-1])["warnings"] == _bundle(archive)[3]["warnings"] != []


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


def test_a_row_with_a_character_past_the_bmp_reads_back(tmp_path: Path) -> None:
    """An emoji in an output path: JSON escapes it as a surrogate pair, which
    YAML would read back as two lone surrogates, so the row writes it as
    itself, and the line is still one JSON object."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text("enclave_name: e\ntransferred: []\n", encoding="utf-8")
    row = {"event": "packaged", "path": "out/\U0001F600 caf\u00e9.csv"}

    _append_rows(m, [row])

    assert yaml.safe_load(m.read_text(encoding="utf-8"))["transferred"] == [row]
    assert json.loads(m.read_text(encoding="utf-8").splitlines()[-1][2:]) == row


@pytest.mark.parametrize("log", [
    f"transferred: [{{repo: d, contract_pin: '{'c' * 40}'}}]\n",
    f"transferred: [\n  {{repo: d, contract_pin: '{'c' * 40}'}}]\n",
], ids=["one-line-flow-log", "flow-log-opened-on-the-key-line"])
def test_a_log_in_another_shape_is_never_told_to_add_a_second_key(tmp_path: Path, log: str) -> None:
    """A second `transferred:` key would win on load and drop the rows above
    it, so a log that exists in a shape rows cannot follow gets the read-back
    refusal, not the 'add transferred: []' hint."""
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text("enclave_name: e\n" + log, encoding="utf-8")
    before = m.read_bytes()

    with pytest.raises(ManifestNotAppendable, match="would not read back") as raised:
        _append_rows(m, [{"event": "packaged"}])

    assert "add `transferred: []`" not in (raised.value.hint or "")
    assert m.read_bytes() == before


def test_an_empty_log_with_a_comment_takes_rows_and_keeps_the_comment(tmp_path: Path) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text("enclave_name: e\ntransferred: []  # none sent yet\n", encoding="utf-8")

    _append_rows(m, [{"event": "packaged", "n": 1}])

    text = m.read_text(encoding="utf-8")
    assert "# none sent yet" in text
    assert yaml.safe_load(text)["transferred"] == [{"event": "packaged", "n": 1}]


def test_a_manifest_with_no_log_is_told_to_add_one(tmp_path: Path) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    m.write_text("enclave_name: e\napproved_products: []\n", encoding="utf-8")

    with pytest.raises(ManifestNotAppendable, match="no transferred: section") as raised:
        _append_rows(m, [{"event": "packaged"}])

    assert "add `transferred: []`" in (raised.value.hint or "")


def test_a_refusal_at_append_time_leaves_no_zip(world) -> None:
    """The manifest is edited while the bundle is built, after the early probe
    passed: the rows cannot be recorded, so the finished zip is removed rather
    than left to cross unrecorded."""
    _approve(world, world.pin1)

    def edit_then_answer(url: str, pin: str) -> int:
        world.manifest.write_text(world.manifest.read_text(encoding="utf-8") + "notes:\n- x\n", encoding="utf-8")
        return 1794717000

    with pytest.raises(ManifestNotAppendable):
        _package(world, commit_time=edit_then_answer)

    assert list((world.enclave / "transfers").glob("*.zip")) == []
    assert "packaged" not in world.manifest.read_text(encoding="utf-8")


def test_the_record_check_reads_the_log_on_disk(tmp_path: Path) -> None:
    m = tmp_path / "enclave_manifest.yaml"
    tid = "transfer-2026-11-19-000000"
    assert not _records(m, tid), "no manifest"
    m.write_text("enclave_name: e\ntransferred: []\n", encoding="utf-8")
    assert not _records(m, tid)

    _append_rows(m, [{"event": "packaged", "transfer_id": tid}])

    assert _records(m, tid)
    assert not _records(m, "transfer-2026-11-19-000001"), "another transfer's rows"
    m.write_text("enclave_name: [\n", encoding="utf-8")
    assert not _records(m, tid), "a manifest that cannot be read"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX folder permissions, as a non-root user")
def test_rows_that_fail_before_they_land_leave_no_zip(world) -> None:
    """The enclave folder is read-only, so the manifest's temp file cannot be
    made: nothing is recorded, so the finished zip goes."""
    _approve(world, world.pin1)
    written = world.manifest.read_text(encoding="utf-8")
    first, _ = _package(world)  # makes .dvc/, data/imports/ and transfers/
    world.manifest.write_text(written, encoding="utf-8")
    first.unlink()
    world.enclave.chmod(0o555)
    try:
        with pytest.raises(PermissionError) as raised:
            _package(world)
    finally:
        world.enclave.chmod(0o755)

    assert world.manifest.name in str(raised.value.filename), "it failed writing the manifest"
    assert list((world.enclave / "transfers").glob("*.zip")) == []
    assert world.manifest.read_text(encoding="utf-8") == written


def test_an_interrupt_after_the_rows_land_keeps_their_zip(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C just after the manifest's rename: the rows are on disk, so the
    zip they record stays, rather than a log naming a bundle that is gone."""
    _approve(world, world.pin1)
    real_replace = os.replace

    def ctrl_c_after_the_manifest_lands(src, dst):
        real_replace(src, dst)
        if Path(dst).name == world.manifest.name:
            raise KeyboardInterrupt

    # Census +1, by review: "a zip exists exactly when the log records it" is
    # on a data-integrity path, and an interrupt after the rename can only be
    # injected at the write itself.
    monkeypatch.setattr("mintd._atomic.os.replace", ctrl_c_after_the_manifest_lands)
    with pytest.raises(KeyboardInterrupt):
        _package(world)

    zips = list((world.enclave / "transfers").glob("*.zip"))
    assert len(zips) == 1
    assert f'"transfer_id": "{zips[0].stem}"' in world.manifest.read_text(encoding="utf-8")


def _rows_for(w: SimpleNamespace, *approvals: str) -> None:
    """A manifest approving each `key: value` row given, all for `demo`."""
    rows = "".join(f"  - repo: demo\n    {a}\n" for a in approvals)
    w.manifest.write_text(HEADER + "approved_products:\n" + rows + "transferred: []\n", encoding="utf-8")


@pytest.mark.parametrize("tamper", ["drop b.csv", "swap a.csv for other bytes"])
def test_a_cached_file_list_that_does_not_hash_to_its_pin_is_refused(world, tamper: str) -> None:
    """A cache changed or damaged in place can stay valid JSON. A list without
    b.csv would bundle the folder without it while the log records it whole;
    a swapped md5, with the file swapped to match, would bundle other bytes
    and an MD5SUMS that agrees with them. The list must hash to the md5 the
    import pointer pins."""
    written = _approve(world, world.pin1)
    world.ops.init(cwd=world.enclave)
    import_product(world.client, world.ops, "demo", cwd=world.enclave, dest_root=world.enclave / "data" / "imports",
                   path="data/final", rev=world.pin1)
    (listing,) = dvc_cache_dir(world.enclave).rglob("*.dir")
    listing.chmod(0o644)
    entries = json.loads(listing.read_text(encoding="utf-8"))
    if tamper == "drop b.csv":
        entries = [e for e in entries if e["relpath"] != "b.csv"]
    else:
        for e in entries:
            if e["relpath"] == "a.csv":
                e["md5"] = _md5(b"evil\n")
        (a,) = (world.enclave / "data" / "imports").rglob("a.csv")
        a.unlink()
        a.write_bytes(b"evil\n")
    listing.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(ChecksumMismatch, match="does not hash to it") as raised:
        _package(world)

    assert raised.value.hint == (
        f"delete {cache_path_for(dvc_cache_dir(world.enclave), listing.parent.name + listing.name)}, then "
        f"import it again at the pin: 'mintd data import demo --force --path data/final --rev {world.pin1}'"
    ), "data pull leaves a changed list in place, and --force alone can adopt it"
    assert list((world.enclave / "transfers").glob("*.zip")) == []
    assert world.manifest.read_text(encoding="utf-8") == written


@pytest.mark.parametrize("paths", [
    ("data/final", "data/final/a.csv"),
    ("data/final/a.csv", "data/final"),
    ("data/final", "data/final/sub"),
], ids=["folder-then-file", "file-then-folder", "folder-then-sub-folder"])
def test_a_path_inside_an_approved_folder_comes_from_the_folders_import(world, paths: tuple[str, str]) -> None:
    """Approved at one pin in one run, the folder is imported once and the
    path inside it read from that import: a second `dvc import` into the
    folder is refused. Each file is bundled once, and each approval logs a row."""
    _rows_for(world, *(f"pin: {world.pin1}\n    source_path: {p}" for p in paths))

    archive, _ = _package(world)

    dest = f"data/demo/{VERSION_DATE}_{world.pin1[:7]}/data/final"
    assert [n for n in _bundle(archive)[0] if n.startswith("data/")] == sorted(f"{dest}/{k}" for k in FOLDER)
    assert "warnings" not in _bundle(archive)[3], "the folder's import is checked against the producer"
    imports = world.enclave / "data" / "imports"
    assert [q.relative_to(imports).as_posix() for q in imports.rglob("*.dvc")] == ["data_demo/data/final.dvc"], (
        "one import, the folder's, where an import of the folder alone puts it"
    )
    rows = {r["path"]: r for r in _rows(world)}
    assert list(rows) == list(paths)
    inner = {"data/final/a.csv": _md5(FOLDER["a.csv"]),
             "data/final/sub": dir_manifest_md5([DvcFileEntry(md5=_md5(FOLDER["sub/é.csv"]), relpath="é.csv", size=0)])}
    for p in paths:
        if p in inner:
            assert rows[p]["artifact_pin"] == inner[p] and rows[p]["dest"] == f"data/demo/{VERSION_DATE}_{world.pin1[:7]}/{p}"
    with pytest.raises(NothingNewToPackage):
        _package(world)


def test_appending_keeps_crlf_line_endings(world) -> None:
    """Every byte before the new rows stays, and the rows end lines as the
    file does."""
    old = f"- {{repo: demo, contract_pin: {'c' * 40}, transfer_id: transfer-2026-07-28-000000}}\n"
    raw = _approve(world, world.pin1, log="transferred:\n" + old).replace("\n", "\r\n").encode("utf-8")
    world.manifest.write_bytes(raw)

    _package(world)

    after = world.manifest.read_bytes()
    assert after.startswith(raw)
    assert after.count(b"\n") == after.count(b"\r\n")
    assert [r.get("event") for r in _rows(world)] == [None, "packaged"]


def test_appending_keeps_a_read_only_manifest_read_only(world) -> None:
    _approve(world, world.pin1)
    world.manifest.chmod(0o444)
    if sys.platform == "win32":
        # Windows will not replace a read-only file: the run fails, and leaves
        # no zip and no temp file behind.
        with pytest.raises(PermissionError):
            _package(world)
        assert [q.name for q in world.enclave.rglob("*") if q.name.endswith((".tmp", ".zip"))] == []
        return

    _package(world)

    assert stat.S_IMODE(world.manifest.stat().st_mode) == 0o444
    assert [r["event"] for r in _rows(world)] == ["packaged"]


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs extra rights on Windows")
def test_appending_writes_through_a_symlinked_manifest(world) -> None:
    _approve(world, world.pin1)
    real = world.enclave / "kept-elsewhere.yaml"
    world.manifest.rename(real)
    world.manifest.symlink_to(real)

    _package(world)

    assert world.manifest.is_symlink() and world.manifest.resolve() == real.resolve()
    assert [r["event"] for r in yaml.safe_load(real.read_text(encoding="utf-8"))["transferred"]] == ["packaged"]


@pytest.mark.parametrize("source_path", ["''", "./data/final", "/data/final", "data//final", "data/../final"])
def test_a_source_path_that_names_no_output_is_refused_before_any_import(world, source_path: str) -> None:
    _rows_for(world, f"pin: {world.pin1}\n    source_path: {source_path}")

    with pytest.raises(MintdError, match="source_path"):
        _package(world)

    assert not (world.enclave / ".dvc").exists(), "nothing was imported"


def test_a_source_path_with_a_trailing_slash_still_loads(world) -> None:
    """Older manifests may write one; it names the same output."""
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final/")

    _package(world)

    assert [r["path"] for r in _rows(world)] == ["data/final"]


def test_all_approves_every_output_and_a_primary_row_beside_it_adds_nothing(world) -> None:
    world.producer.add_output("data/extra.csv")
    pin = world.producer.publish({"data/final": FOLDER, "data/extra.csv": b"x\n"}, message="two outputs")
    world.client = InMemoryCatalogClient()
    world.client.register(Metadata.model_validate(world.producer.metadata()))
    _rows_for(world, f"pin: {pin}\n    all: true", f"pin: {pin}")

    archive, _ = _package(world)

    version = f"data/demo/{VERSION_DATE}_{pin[:7]}"
    assert sorted(r["path"] for r in _rows(world)) == ["data/extra.csv", "data/final"]
    assert [n for n in _bundle(archive)[0] if n.startswith("data/")] == sorted(
        [f"{version}/data/extra.csv"] + [f"{version}/data/final/{k}" for k in FOLDER]
    )


def test_package_with_a_repo_bundles_only_that_products_approvals(world, tmp_path: Path) -> None:
    other = build_local_producer(tmp_path / "other-producer")
    meta = other.metadata()
    meta["project"].update(name="other", full_name="data_other")
    meta["repository"]["github_url"] = other.url
    other._write_metadata(meta)
    (other.work / "data" / "final").mkdir(parents=True, exist_ok=True)
    pin = other.publish({"data/final": {"x.csv": b"x\n"}})
    world.client.register(Metadata.model_validate(meta))
    world.manifest.write_text(
        HEADER + "approved_products:\n"
        f"  - repo: demo\n    pin: {world.pin1}\n  - repo: other\n    pin: {pin}\n" + "transferred: []\n",
        encoding="utf-8",
    )

    first, _ = _package(world, name="other")
    second, _ = _package(world, name="demo")

    assert [c["repo"] for c in _bundle(first)[3]["contents"]] == ["other"]
    assert [c["repo"] for c in _bundle(second)[3]["contents"]] == ["demo"]
    assert [r["repo"] for r in _rows(world)] == ["other", "demo"]
    with pytest.raises(NothingToPackage, match="for nope"):
        _package(world, name="nope")


def _v2(w: SimpleNamespace) -> str:
    """A second pin whose a.csv differs from pin1's."""
    return w.producer.publish({"data/final": {**FOLDER, "a.csv": b"alpha v2\n"}}, message="v2")


def _version_of(w: SimpleNamespace, pin: str) -> str:
    return f"data/demo/{VERSION_DATE}_{pin[:7]}"


def test_a_file_inside_a_folder_approved_at_another_pin_is_read_at_its_own_pin(world) -> None:
    """One import folder holds one pin: the folder is imported at each pin in
    turn, and a.csv is read from the import at its own."""
    pin2 = _v2(world)
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final", f"pin: {pin2}\n    source_path: data/final/a.csv")

    archive, _ = _package(world)

    _, data, _, _ = _bundle(archive)
    v1, v2 = _version_of(world, world.pin1), _version_of(world, pin2)
    assert {n: b for n, b in data.items() if n.startswith("data/")} == {
        **{f"{v1}/data/final/{k}": b for k, b in FOLDER.items()}, f"{v2}/data/final/a.csv": b"alpha v2\n",
    }
    assert {(r["path"], r["contract_pin"]) for r in _rows(world)} == {("data/final", world.pin1), ("data/final/a.csv", pin2)}


def test_a_file_inside_a_folder_imported_earlier_at_another_pin_is_read_at_its_own_pin(world) -> None:
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final")
    _package(world)
    pin2 = _v2(world)
    text = world.manifest.read_text(encoding="utf-8")
    world.manifest.write_text(
        text.replace("\ntransferred:", f"\n  - repo: demo\n    pin: {pin2}\n    source_path: data/final/a.csv\ntransferred:", 1),
        encoding="utf-8",
    )

    archive, _ = _package(world)

    _, data, _, _ = _bundle(archive)
    assert {n: b for n, b in data.items() if n.startswith("data/")} == {
        f"{_version_of(world, pin2)}/data/final/a.csv": b"alpha v2\n",
    }
    assert (_rows(world)[-1]["path"], _rows(world)[-1]["contract_pin"]) == ("data/final/a.csv", pin2)


def test_a_file_resent_at_an_older_pin_beside_a_newer_folder(world) -> None:
    """The folder crossed at pin1; now the folder at pin2 is new, and a.csv is
    sent again at pin1. Each is read from the folder's import at its own pin."""
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final")
    _package(world)
    pin2 = _v2(world)
    text = world.manifest.read_text(encoding="utf-8")
    world.manifest.write_text(text.replace("\ntransferred:", (
        f"\n  - repo: demo\n    pin: {pin2}\n    source_path: data/final"
        f"\n  - repo: demo\n    pin: {world.pin1}\n    source_path: data/final/a.csv\ntransferred:"), 1), encoding="utf-8")

    archive, _ = _package(world)

    _, data, _, _ = _bundle(archive)
    v1, v2 = _version_of(world, world.pin1), _version_of(world, pin2)
    assert {n: b for n, b in data.items() if n.startswith("data/")} == {
        f"{v1}/data/final/a.csv": b"alpha\n",
        **{f"{v2}/data/final/{k}": b for k, b in {**FOLDER, "a.csv": b"alpha v2\n"}.items()},
    }
    assert {(r["path"], r["contract_pin"]) for r in _rows(world)[1:]} == {
        ("data/final", pin2), ("data/final/a.csv", world.pin1),
    }


def test_a_path_the_approved_folder_does_not_hold_is_refused(world) -> None:
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final", f"pin: {world.pin1}\n    source_path: data/final/nope.csv")

    with pytest.raises(MintdError, match="data/final/nope.csv is not in data/final"):
        _package(world)

    assert list((world.enclave / "transfers").glob("*.zip")) == []
    assert _rows(world) == []


@pytest.mark.skipif(sys.platform == "win32", reason="DVC's trust of a read-only cache object, reproduced on POSIX")
def test_an_import_that_adopted_a_changed_cache_is_refused_and_the_hint_recovers(world) -> None:
    """A forced import with no pin, at HEAD == the approved pin, adopts a
    changed list DVC trusts (cache objects are read-only) and rewrites the
    import pointer to match it. Only the producer's own pointer still names
    the real tree, so package checks against that."""
    _approve(world, world.pin1)
    world.ops.init(cwd=world.enclave)
    where = {"cwd": world.enclave, "dest_root": world.enclave / "data" / "imports", "path": "data/final"}
    import_product(world.client, world.ops, "demo", rev=world.pin1, **where)
    (listing,) = dvc_cache_dir(world.enclave).rglob("*.dir")
    listing.chmod(0o644)
    entries = [e for e in json.loads(listing.read_text(encoding="utf-8")) if e["relpath"] != "b.csv"]
    listing.write_text(json.dumps(entries), encoding="utf-8")
    listing.chmod(0o444)
    import_product(world.client, world.ops, "demo", force=True, **where)
    (pointer,) = (world.enclave / "data" / "imports").rglob("final.dvc")
    assert "nfiles: 3" in pointer.read_text(encoding="utf-8"), "the forced import adopted the changed list"

    with pytest.raises(ChecksumMismatch, match="is not what demo's own pointer records") as raised:
        _package(world)

    assert list((world.enclave / "transfers").glob("*.zip")) == []
    hint = raised.value.hint or ""
    assert hint.endswith(f"import it again at the pin: 'mintd data import demo --force --path data/final --rev {world.pin1}'")
    named = re.match(r"delete (.*), then ", hint)
    assert named and named[1] == str(cache_path_for(dvc_cache_dir(world.enclave), listing.parent.name + listing.name)), (
        "only the changed list under the producer's md5; the adopted one hashes to its name"
    )
    for stale in named[1].split(", "):
        Path(stale).chmod(0o644)
        Path(stale).unlink()
    import_product(world.client, world.ops, "demo", force=True, rev=world.pin1, **where)
    archive, _ = _package(world)
    dest = f"{_version_of(world, world.pin1)}/data/final"
    assert [n for n in _bundle(archive)[0] if n.startswith("data/")] == sorted(f"{dest}/{k}" for k in FOLDER)


def _lock_outs(w: SimpleNamespace) -> tuple[str, str]:
    """A folder a stage writes as per-file outs; DVC also writes a .gitignore there."""
    pin = w.producer.publish_pipeline(
        "stages:\n"
        "  lane:\n"
        "    cmd: python -c \"import pathlib; "
        "d=pathlib.Path('data/lane'); d.mkdir(parents=True, exist_ok=True); "
        "(d/'a.csv').write_bytes(b'a\\n'); (d/'b.csv').write_bytes(b'b\\n')\"\n"
        "    outs:\n"
        "      - data/lane/a.csv\n"
        "      - data/lane/b.csv\n"
    )
    return pin, "data/lane"


def _several_pointers(w: SimpleNamespace) -> tuple[str, str]:
    """A folder holding one `.dvc` pointer per file, and none of its own."""
    (w.producer.work / "data" / "pf").mkdir(parents=True, exist_ok=True)
    pin = w.producer.publish({"data/final": FOLDER, "data/pf/a.csv": b"a\n", "data/pf/b.csv": b"b\n"}, message="pointers")
    return pin, "data/pf"


@pytest.mark.parametrize("shape", ["per-file lock outs", "several pointers", "sub-folder of a tree", "file in a tree"])
def test_a_path_the_producers_pointer_does_not_name_crosses_with_a_warning(world, capsys, shape: str) -> None:
    """The producer has no identity of its own for these paths, so their bytes
    are checked against the local import only: each crosses, with one warning
    on screen and in the transfer's manifest.json naming it."""
    pin, path = {
        "per-file lock outs": lambda: _lock_outs(world),
        "several pointers": lambda: _several_pointers(world),
        "sub-folder of a tree": lambda: (world.pin1, "data/final/sub"),
        "file in a tree": lambda: (world.pin1, "data/final/a.csv"),
    }[shape]()
    _rows_for(world, f"pin: {pin}\n    source_path: {path}")

    archive, _ = _package(world, reporter=Reporter())

    warning = (f"demo {path} at {pin[:7]}: the bytes match the local import, but are not checked against "
               "the producer's own record, whose pointer does not name this path on its own")
    assert _bundle(archive)[3]["warnings"] == [warning]
    assert warning in " ".join(capsys.readouterr().err.split())
    assert [r["path"] for r in _rows(world)] == [path]


def test_an_anchored_file_whose_import_differs_from_the_producers_pointer_is_refused(world) -> None:
    pin = world.producer.publish({"data/final": FOLDER, "data/one.csv": b"one\n"}, message="one file")
    _rows_for(world, f"pin: {pin}\n    source_path: data/one.csv")
    first, _ = _package(world)
    assert "warnings" not in _bundle(first)[3], "the producer's pointer names data/one.csv"
    (pointer,) = (world.enclave / "data" / "imports").rglob("one.csv.dvc")
    pointer.write_text(pointer.read_text(encoding="utf-8").replace(_md5(b"one\n"), _md5(b"evil\n")), encoding="utf-8")
    copy = pointer.parent / "one.csv"
    copy.unlink()
    copy.write_bytes(b"evil\n")

    with pytest.raises(ChecksumMismatch, match="is not what demo's own pointer records"):
        _package(world, resend=True)


def test_the_producer_check_reads_every_pointer_shape(tmp_path: Path) -> None:
    """How check's own matcher names a path decides it: on its own (an exact
    out, or an entry of a per-file pointer) the import is compared and any
    difference refused; inside a tree, beneath it per-file lock outs, or
    absent, it is not anchored and not refused. A failed read is neither."""
    files = (("sub/a.csv", "1" * 32), ("sub/b.csv", "2" * 32))  # all under sub/: sub's digest is the folder's
    per_file = {"outs": [{"path": "final", "files": [{"relpath": r, "md5": m} for r, m in files]}]}
    tree = {"outs": [{"path": "final", "md5": "7" * 32 + ".dir"}]}
    lock = {"stages": {"s": {"outs": [{"path": f"data/final/{r}", "md5": m} for r, m in files]}}}

    def anchored(v: _Version, doc: dict, base: str = "data") -> bool:
        def match(path: str) -> tuple[str | None, str | None, str]:
            identity, kind, within = _match_out(doc, path, base_dir=base)
            return identity or _POINTER_ABSENT, kind, within
        return _check_against_producer(v, match, tmp_path / "x.dvc", tmp_path)

    folder = _Version("demo", "data/final", "c" * 40, "9" * 32 + ".dir", tmp_path, files)
    sub = _Version("demo", "data/final/sub", "c" * 40, "8" * 32 + ".dir", tmp_path, (("a.csv", "1" * 32), ("b.csv", "2" * 32)))
    assert anchored(folder, per_file) and anchored(sub, per_file)
    with pytest.raises(ChecksumMismatch):
        anchored(replace(sub, files=(("a.csv", "1" * 32), ("b.csv", "3" * 32))), per_file)
    with pytest.raises(ChecksumMismatch):
        anchored(folder, tree)
    # The file one folder too deep: the producer lists sub/a.csv, the import
    # holds it at sub/sub/a.csv. Placed at the base check's matcher returns,
    # the entries do not match; a guess over every suffix of the path did.
    with pytest.raises(ChecksumMismatch):
        anchored(replace(sub, files=(("sub/a.csv", "1" * 32), ("sub/b.csv", "2" * 32))), per_file)
    assert not anchored(sub, tree)
    assert not anchored(folder, lock, base="")
    assert not anchored(folder, {"outs": []})
    with pytest.raises(MintdError, match="could not read"):
        _check_against_producer(folder, lambda p: (None, None, ""), tmp_path / "x.dvc", tmp_path)


def test_a_files_list_beside_a_tree_md5_is_not_read(world) -> None:
    """A `files:` list counts only with no top-level md5, as check reads it.
    A pointer that keeps the producer's tree md5 but lists other files, with
    a.csv swapped to match, is read from the hash-checked tree instead."""
    _approve(world, world.pin1)
    _package(world)
    (pointer,) = (world.enclave / "data" / "imports").rglob("final.dvc")
    doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    doc["outs"][0]["files"] = [{"relpath": "a.csv", "md5": _md5(b"evil\n")}]
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    copy = pointer.parent / "final" / "a.csv"
    copy.unlink()
    copy.write_bytes(b"evil\n")

    with pytest.raises(ChecksumMismatch, match="does not match its pin"):
        _package(world, resend=True)


def test_a_parent_folder_imported_on_disk_never_downgrades_a_path_the_producer_names(world) -> None:
    """`data` imported on disk becomes the import unit, and the producer names
    `data` nowhere. data/one.csv, which it names, is still checked against
    that: an import whose tree was changed and re-hashed to match is refused."""
    pin = world.producer.publish({"data/final": FOLDER, "data/one.csv": b"one\n"}, message="one file")
    _rows_for(world, f"pin: {pin}\n    source_path: data/one.csv")
    world.ops.init(cwd=world.enclave)
    import_product(world.client, world.ops, "demo", cwd=world.enclave, dest_root=world.enclave / "data" / "imports",
                   path="data", rev=pin)
    first, _ = _package(world)
    assert "warnings" not in _bundle(first)[3], "data/one.csv is named, and matches"
    (pointer,) = (world.enclave / "data" / "imports").rglob("data.dvc")
    doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    cache = dvc_cache_dir(world.enclave)
    entries = read_cached_dir_manifest(cache, doc["outs"][0]["md5"]) or []
    changed = [replace(e, md5=_md5(b"evil\n")) if e.relpath == "one.csv" else e for e in entries]
    doc["outs"][0]["md5"] = ensure_dir_manifest(cache, changed)
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    copy = pointer.parent / "data" / "one.csv"
    copy.unlink()
    copy.write_bytes(b"evil\n")

    with pytest.raises(ChecksumMismatch, match="is not what demo's own pointer records") as raised:
        _package(world, resend=True)

    assert raised.value.hint == f"import it again at the pin: 'mintd data import demo --force --path data --rev {pin}'", (
        "data/one.csv was read from the data import, so that is what to import again"
    )


def test_the_per_file_refusal_names_the_folder_it_was_read_from(world) -> None:
    """a.csv is read from the data/final import, so that is what to import
    again; the cache copy hashes to its name, so the hint deletes nothing."""
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final", f"pin: {world.pin1}\n    source_path: data/final/a.csv")
    _package(world)
    copy = world.enclave / "data/imports/data_demo/data/final/a.csv"
    copy.unlink()
    copy.write_bytes(b"alpha!\n")

    with pytest.raises(ChecksumMismatch, match="does not match its pin") as raised:
        _package(world, resend=True)

    assert raised.value.hint == f"import it again at the pin: 'mintd data import demo --force --path data/final --rev {world.pin1}'"


def test_two_paths_inside_a_tree_both_cross_with_a_warning(world) -> None:
    """Both are read from the data/final/sub import, inside a folder the
    producer tracks as one tree; the second check of that unit is answered
    from the memo of producer reads, which keeps how it matched."""
    _rows_for(world, f"pin: {world.pin1}\n    source_path: data/final/sub", f"pin: {world.pin1}\n    source_path: data/final/sub/é.csv")

    archive, _ = _package(world)

    assert [w.split(":")[0] for w in _bundle(archive)[3]["warnings"]] == [
        f"demo data/final/sub at {world.pin1[:7]}", f"demo data/final/sub/é.csv at {world.pin1[:7]}",
    ]


def test_a_folder_list_naming_a_file_twice_is_refused_at_its_root(world) -> None:
    """The producer names data/one.csv exactly. The data import's tree on disk,
    re-hashed to match its name, lists one.csv twice: the good md5, then an
    evil one, with the copy holding the evil bytes. Read first-entry, the
    producer check passed; zipped last-entry, the evil bytes crossed. dvc
    never lists a path twice, so the tree is not one it wrote."""
    pin = world.producer.publish({"data/final": FOLDER, "data/one.csv": b"one\n"}, message="one file")
    _rows_for(world, f"pin: {pin}\n    source_path: data/one.csv")
    world.ops.init(cwd=world.enclave)
    import_product(world.client, world.ops, "demo", cwd=world.enclave, dest_root=world.enclave / "data" / "imports",
                   path="data", rev=pin)
    (pointer,) = (world.enclave / "data" / "imports").rglob("data.dvc")
    doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    cache = dvc_cache_dir(world.enclave)
    entries = read_cached_dir_manifest(cache, doc["outs"][0]["md5"]) or []
    doubled = entries + [replace(e, md5=_md5(b"evil\n")) for e in entries if e.relpath == "one.csv"]
    doc["outs"][0]["md5"] = ensure_dir_manifest(cache, doubled)
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    copy = pointer.parent / "data" / "one.csv"
    copy.unlink()
    copy.write_bytes(b"evil\n")

    with pytest.raises(ChecksumMismatch, match="lists a path twice"):
        _package(world)

    assert list((world.enclave / "transfers").glob("*.zip")) == []


def test_a_zip_member_with_two_sources_that_disagree_is_refused(tmp_path: Path) -> None:
    """Where bytes enter the zip: a second source for one member with another
    md5 is refused, whatever the source, so a later entry never wins silently."""
    v = _Version("demo", "data/final", "c" * 40, "9" * 32 + ".dir", tmp_path, (("a.csv", "1" * 32),))
    members: dict = {}
    _hold(members, "data/demo/v/data/final/a.csv", (tmp_path / "a.csv", "1" * 32, v), "again")
    _hold(members, "data/demo/v/data/final/a.csv", (tmp_path / "a.csv", "1" * 32, v), "again")

    with pytest.raises(ChecksumMismatch, match="would be bundled twice") as raised:
        _hold(members, "data/demo/v/data/final/a.csv", (tmp_path / "a.csv", "2" * 32, v), "again")

    assert raised.value.hint == "again"
    assert members["data/demo/v/data/final/a.csv"][1] == "1" * 32


def test_the_producer_pointer_is_read_as_dvc_writes_it(tmp_path: Path) -> None:
    """DVC (YAML 1.2) writes a file named `no` unquoted. Read as PyYAML's 1.1
    reads it, that is the boolean False, and the producer's `.files` digest
    could never match the import, which every local reader reads as 1.2."""
    from mintd._producer_git_ops import FetchError
    from mintd.check import _pointer_match

    m1, m2 = "1" * 32, "2" * 32
    pointer = f"outs:\n- path: final\n  hash: md5\n  files:\n  - relpath: a.csv\n    md5: {m1}\n  - relpath: no\n    md5: {m2}\n"

    class Producer:
        def fetch_path_at(self, repo: str, pin: str, path: str) -> bytes:
            if path == "data/final.dvc":
                return pointer.encode()
            raise FetchError.path_missing(repo, pin, path)

    folder = _Version("demo", "data/final", "c" * 40, "9" * 32 + ".dir", tmp_path, (("a.csv", m1), ("no", m2)))

    assert _check_against_producer(
        folder, lambda p: _pointer_match(Producer(), "demo", "c" * 40, p, {}), tmp_path / "x.dvc", tmp_path
    )
