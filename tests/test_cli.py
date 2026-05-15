"""Tests for ``mintd.cli`` — slice 10 minimal CLI scaffolding.

All tests call ``cli.main(argv=[...])`` in-process and monkeypatch
``_resolve_clients`` to inject fakes. One subprocess smoke test
(``test_python_m_mintd_version_smoke``) exercises packaging via
``python -m mintd``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mintd import cli
from mintd.catalog import InMemoryCatalogClient
from mintd.check import CheckFinding
from mintd.data import BumpBlocked
from mintd.model import Metadata
from tests._fakes.dvc_ops import _FakeDvcOps

FIXTURES = Path(__file__).parent / "fixtures"
ENCLAVE_FIXTURE = FIXTURES / "enclave_manifest_v2_minimal.yaml"
STANDALONE_DVC = FIXTURES / "dvc_files" / "standalone_import.dvc"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _stage_dvc_import(tmp_path: Path) -> None:
    (tmp_path / "data" / "imports").mkdir(parents=True, exist_ok=True)
    shutil.copy(STANDALONE_DVC, tmp_path / "data" / "imports" / "cms_based.dvc")


@pytest.fixture
def patched_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemoryCatalogClient, _FakeDvcOps]:
    client = InMemoryCatalogClient()
    dvc_ops = _FakeDvcOps()
    monkeypatch.setattr(
        "mintd.cli._resolve_clients", lambda cfg: (client, dvc_ops)
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_catalog_client", lambda cfg: client
    )
    # Always return defaults; avoid touching the real ~/.config/mintd/.
    monkeypatch.setattr(
        "mintd.cli.Config.load",
        classmethod(lambda cls, path=None: cls()),
    )
    return client, dvc_ops


def _register_provider_xw(
    client: InMemoryCatalogClient, primary: str = "outputs/main.parquet"
) -> None:
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = primary
    client.register(Metadata.model_validate(data))


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_clean_project_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], patched_clients
) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    rc = cli.main(["check", str(tmp_path)])
    assert rc == 0


def test_check_missing_metadata_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], patched_clients
) -> None:
    rc = cli.main(["check", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "metadata.json" in out


def test_check_upgrades_renders_kind_prefix(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the slice-10 binding question: kind-driven prefix selection
    works without any message parsing."""
    drift = CheckFinding(
        severity="warning",
        section="consumer",
        message="upgrade available: producer now publishes 'X'",
        kind="drift",
    )
    monkeypatch.setattr(
        "mintd.cli.check_project", lambda *a, **kw: [drift]
    )
    rc = cli.main(["check", str(tmp_path), "--upgrades"])
    out = capsys.readouterr().out
    assert rc == 0  # no error-severity findings
    assert "↑" in out
    assert "upgrade available" in out


def test_check_json_flag_emits_one_line_per_finding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = [
        CheckFinding(severity="info", section="consumer", message="up to date", kind="up_to_date"),
        CheckFinding(severity="warning", section="consumer", message="x", kind="drift"),
    ]
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: findings)
    rc = cli.main(["check", str(tmp_path), "--json"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0
    assert len(out) == 2
    for line in out:
        record = json.loads(line)
        assert "kind" in record
        assert "severity" in record


# ---------------------------------------------------------------------------
# data import
# ---------------------------------------------------------------------------


def test_data_import_writes_dvc_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    rc = cli.main(
        [
            "data", "import", "provider-xw",
            "--dest-root", str(tmp_path),
        ]
    )
    assert rc == 0
    assert len(dvc_ops.calls) == 1
    assert dvc_ops.calls[0].path == "outputs/main.parquet"


def test_data_import_unknown_name_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    rc = cli.main(
        ["data", "import", "nope", "--dest-root", str(tmp_path)]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


def test_data_import_bump_up_to_date_prints_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mintd.cli.bump_import", lambda *a, **kw: None)
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "up to date" in out


def test_data_import_bump_drift_prints_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_dvc = tmp_path / "new.parquet.dvc"
    monkeypatch.setattr("mintd.cli.bump_import", lambda *a, **kw: new_dvc)
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(new_dvc) in out


def test_data_import_bump_unreachable_exits_two_with_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="producer unreachable: timeout",
        kind="unreachable",
    )

    def raises(*args: Any, **kwargs: Any) -> Any:
        raise BumpBlocked("provider-xw", finding)

    monkeypatch.setattr("mintd.cli.bump_import", raises)
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "retry" in err


def test_data_import_bump_pin_missing_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message="producer pin missing: abc1234 not found",
        kind="pin_missing",
    )

    def raises(*args: Any, **kwargs: Any) -> Any:
        raise BumpBlocked("provider-xw", finding)

    monkeypatch.setattr("mintd.cli.bump_import", raises)
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "retry" not in err


def test_data_import_bump_with_rev_exits_64(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """argparse misuse: --bump + --rev. Exit 64 per the spec."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["data", "import", "provider-xw", "--bump", "--rev", "abc123"])
    assert exc.value.code == 64


# ---------------------------------------------------------------------------
# enclave bump
# ---------------------------------------------------------------------------


def test_enclave_bump_up_to_date_prints_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mintd.cli.enclave_bump", lambda *a, **kw: None)
    rc = cli.main(["enclave", "bump", "provider-xw"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "up to date" in out


def test_enclave_bump_drift_rewrites_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "enclave_manifest.yaml"
    monkeypatch.setattr("mintd.cli.enclave_bump", lambda *a, **kw: manifest)
    rc = cli.main(["enclave", "bump", "provider-xw", "--manifest", str(manifest)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bumped:" in out


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_register_prints_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    rc = cli.main(["registry", "register", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "registered:" in out


def test_registry_update_prints_diff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client, primary="outputs/old.parquet")
    # Build a slightly different metadata.json to update with
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = "outputs/new.parquet"
    (tmp_path / "metadata.json").write_text(json.dumps(data))
    rc = cli.main(["registry", "update", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "→" in out


def test_registry_sync_prints_count(
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    rc = cli.main(["registry", "sync"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synced (0 entries)" in out


def test_registry_status_no_name_works_without_registry_url(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`registry status` (no name) reads only the local pending file.
    It should NOT require `registry_url` to be configured."""
    # Use a real Config (no registry_url) and a tmp cache dir so the
    # pending file path resolves under tmp_path.
    cfg = cli.Config(cache_dir=tmp_path)
    monkeypatch.setattr(
        "mintd.cli.Config.load",
        classmethod(lambda cls, path=None: cfg),
    )

    def must_not_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("_resolve_catalog_client must not be called for nameless status")

    monkeypatch.setattr("mintd.cli._resolve_catalog_client", must_not_call)

    rc = cli.main(["registry", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no pending registrations" in out


# ---------------------------------------------------------------------------
# Subprocess smoke (Decision #6 hybrid)
# ---------------------------------------------------------------------------


def test_python_m_mintd_version_smoke() -> None:
    """End-to-end check that `python -m mintd --version` works via
    __main__.py and the installed package surface."""
    result = subprocess.run(
        [sys.executable, "-m", "mintd", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "0.0.1" in result.stdout

def test_data_list_catalog_empty(patched_clients, capsys):
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert "no entries" in out

def test_data_list_catalog_populated(patched_clients, capsys):
    client, _ = patched_clients
    _register_provider_xw(client)
    
    # Register second
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = "other-project"
    data["project"]["full_name"] = "data_other-project"
    data["repository"]["github_url"] = "https://github.com/example-org/other-project"
    data["metadata"]["description"] = "other description"
    client.register(Metadata.model_validate(data))
    
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert "provider-xw" in out
    assert "other-project" in out

def test_data_list_imported_empty(patched_clients, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.main(["data", "list", "--imported"])
    out, _ = capsys.readouterr()
    assert "no imports" in out

def test_data_list_imported_populated(patched_clients, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _stage_dvc_import(tmp_path)
    cli.main(["data", "list", "--imported"])
    out, _ = capsys.readouterr()
    assert "provider-xw" in out
    assert "4f7c2a1" in out

def test_data_list_imported_with_type_exits_64(patched_clients):
    with pytest.raises(SystemExit) as exc:
        cli.main(["data", "list", "--imported", "--type", "data"])
    assert exc.value.code == 64

def test_enclave_list_empty_sections(patched_clients, capsys, tmp_path):
    manifest_path = tmp_path / "enclave_manifest.yaml"
    shutil.copy(ENCLAVE_FIXTURE, manifest_path)
    cli.main(["enclave", "list", "--manifest", str(manifest_path)])
    out, _ = capsys.readouterr()
    assert "approved_products:" in out
    assert "downloaded:" in out
    assert "transferred:" in out
    assert out.count("(none)") == 2
    assert "provider-xw" in out

def test_enclave_list_filtered_by_repo(patched_clients, capsys, tmp_path):
    manifest_path = tmp_path / "enclave_manifest.yaml"
    # Create multi-entry manifest matching EnclaveManifest schema
    content = """
enclave_name: test-enclave
approved_products:
  - repo: provider-xw
    registry_entry: entry1
    pin: 4f7c2a1
    source_path: path1
  - repo: other-provider
    registry_entry: entry2
    pin: abcdef0
    source_path: path2
downloaded: []
transferred: []
"""
    manifest_path.write_text(content)
    cli.main(["enclave", "list", "provider-xw", "--manifest", str(manifest_path)])
    out, _ = capsys.readouterr()
    assert "provider-xw" in out
    assert "other-provider" not in out

def test_enclave_list_missing_manifest_exits_one(patched_clients, capsys, tmp_path):
    rc = cli.main(["enclave", "list", "--manifest", str(tmp_path / "nope.yaml")])
    assert rc == 1
    _, err = capsys.readouterr()
    assert "not found" in err


# ---------------------------------------------------------------------------
# Slice 12 — enclave add
# ---------------------------------------------------------------------------


def test_enclave_add_subscribes(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"

    rc = cli.main(
        [
            "enclave", "add", "provider-xw",
            "--pin", "deadbeefcafe1234567890abcdef0123456789ab",
            "--manifest", str(manifest),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "subscribed:" in out
    assert "provider-xw" in out

    from mintd.enclave import EnclaveManifest
    loaded = EnclaveManifest.load(manifest)
    assert len(loaded.approved_products) == 1
    assert loaded.approved_products[0].repo == "provider-xw"


def test_enclave_add_duplicate_exits_one(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"

    cli.main(
        [
            "enclave", "add", "provider-xw",
            "--pin", "a" * 40,
            "--manifest", str(manifest),
        ]
    )
    capsys.readouterr()  # discard first-add output
    rc = cli.main(
        [
            "enclave", "add", "provider-xw",
            "--pin", "b" * 40,
            "--manifest", str(manifest),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "already in approved_products" in err


def test_enclave_add_source_path_and_all_exits_64(
    patched_clients, tmp_path: Path
) -> None:
    """argparse mutex: --source-path and --all conflict → exit 64."""
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "enclave", "add", "provider-xw",
                "--pin", "a" * 40,
                "--source-path", "outputs/x",
                "--all",
                "--manifest", str(tmp_path / "enclave_manifest.yaml"),
            ]
        )
    assert exc.value.code == 64


def test_enclave_add_missing_repo_url_exits_one(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Catalog entry without `repository.github_url` → ValueError; CLI
    converts to exit 1 + stderr message rather than propagating a traceback."""
    client, _ = patched_clients
    # Register a Metadata variant with an empty github_url. We bypass
    # Metadata validation by registering a raw CatalogEntry directly into
    # the InMemoryCatalogClient's internal dict.
    from mintd.catalog import CatalogEntry
    bad_entry = CatalogEntry.model_validate(
        {"project": {"name": "broken-repo", "type": "data"}, "repository": {}}
    )
    client._entries["broken-repo"] = bad_entry

    rc = cli.main(
        [
            "enclave", "add", "broken-repo",
            "--pin", "a" * 40,
            "--manifest", str(tmp_path / "enclave_manifest.yaml"),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "github_url" in err


# ---------------------------------------------------------------------------
# Slice 13 — enclave remove + enclave pull
# ---------------------------------------------------------------------------


def test_enclave_remove_subscribes(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Happy path: subscribe via enclave_add, then remove the subscription."""
    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"

    cli.main([
        "enclave", "add", "provider-xw",
        "--pin", "a" * 40,
        "--manifest", str(manifest),
    ])
    capsys.readouterr()  # discard add output

    rc = cli.main([
        "enclave", "remove", "provider-xw",
        "--manifest", str(manifest),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "removed: provider-xw" in out

    from mintd.enclave import EnclaveManifest
    loaded = EnclaveManifest.load(manifest)
    assert loaded.approved_products == []


def test_enclave_remove_unknown_exits_one(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from mintd.enclave import EnclaveManifest
    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    rc = cli.main([
        "enclave", "remove", "ghost",
        "--manifest", str(manifest),
    ])

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


def test_enclave_pull_happy_path(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime
    from mintd.enclave import ApprovedProduct, DownloadedItem, EnclaveManifest

    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(
                repo="provider-xw", registry_entry="e", pin="a" * 40,
                source_path="outputs/main.parquet",
            ),
        ],
    ).save(manifest)

    fake_item = DownloadedItem(
        repo="provider-xw",
        output="outputs/main.parquet",
        contract_pin="a" * 40,
        artifact_pin="f" * 32,
        fetch_strategy="dvc-import",
        downloaded_at=datetime.now(),
        local_path="downloads/provider-xw/fffffff-2026-05-20",
    )

    def fake_pull(*args: Any, **kwargs: Any) -> tuple[Path, list[DownloadedItem]]:
        return manifest, [fake_item]

    monkeypatch.setattr("mintd.cli.enclave_pull", fake_pull)

    rc = cli.main([
        "enclave", "pull", "provider-xw",
        "--manifest", str(manifest),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "pulled: provider-xw" in out


def test_enclave_pull_nothing_to_pull_message(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    def fake_pull(*args: Any, **kwargs: Any) -> tuple[Path, list]:
        return manifest, []

    monkeypatch.setattr("mintd.cli.enclave_pull", fake_pull)

    rc = cli.main([
        "enclave", "pull",
        "--manifest", str(manifest),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing to pull" in out


# ---------------------------------------------------------------------------
# Slice 14 — mintd init
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_init_ops(monkeypatch: pytest.MonkeyPatch):
    from tests._fakes.init_ops import _FakeInitOps
    fake = _FakeInitOps()
    monkeypatch.setattr("mintd.init.SubprocessInitOps", lambda *a, **k: fake)
    return fake


def test_init_data_project_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    rc = cli.main(["init", "data", "my_proj", "--path", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "metadata.json").exists()
    assert (tmp_path / ".gitignore").exists()

    out = capsys.readouterr().out
    assert "created: metadata.json" in out
    assert "created: .gitignore" in out
    assert "initialized: git" in out
    assert "initialized: dvc" in out

    assert patched_init_ops.git_calls == [tmp_path]
    assert patched_init_ops.dvc_calls == [tmp_path]


def test_init_enclave_skips_dvc_in_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    rc = cli.main(["init", "enclave", "my_workspace", "--path", str(tmp_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "initialized: dvc" not in out
    assert "initialized: git" in out
    assert patched_init_ops.dvc_calls == []


def test_init_existing_metadata_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    (tmp_path / "metadata.json").write_text("{}")
    rc = cli.main(["init", "data", "my_proj", "--path", str(tmp_path)])
    assert rc == 1

    err = capsys.readouterr().err
    assert "error:" in err
