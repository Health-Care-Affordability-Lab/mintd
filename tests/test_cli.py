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

from mintd.model import FastPullResult
from tests._fakes.fast_sync_ops import _FakeFastSyncOps
from mintd import cli
from mintd.catalog import CatalogAlreadyExists, CatalogNotFound, InMemoryCatalogClient
from mintd.check import CheckFinding
from mintd.data import BumpBlocked
from mintd.model import Metadata
from mintd._dvc_ops import DvcPullError
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
    # Always return defaults; avoid touching the real ~/.config/mintd/.
    monkeypatch.setattr(
        "mintd.cli.Config.load",
        classmethod(lambda cls, path=None: cls()),
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_clients", lambda cfg, reporter=None, **_: (client, dvc_ops)
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_catalog_client", lambda cfg, **_: client
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_fast_sync_ops", lambda cfg, **_: None
    )
    return client, dvc_ops


@pytest.fixture
def recording_reporter(monkeypatch: pytest.MonkeyPatch):
    """Inject a RecordingReporter as the CLI's reporter so presence
    assertions (status/update_status/error events) are deterministic."""
    from tests._fakes.reporter import RecordingReporter
    rep = RecordingReporter()
    monkeypatch.setattr("mintd.cli._build_reporter", lambda args: rep)
    return rep


def _register_provider_xw(
    client: InMemoryCatalogClient, primary: str = "outputs/main.parquet"
) -> Metadata:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = primary
    metadata = Metadata.model_validate(data)
    client.register(metadata)
    return metadata


def test_cli_data_pull_uses_fast_sync_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients
) -> None:
    _, dvc_ops = patched_clients
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, fallback_targets=[])
    monkeypatch.setattr("mintd.cli._resolve_fast_sync_ops", lambda cfg: fast_fake)
    # Slice-22: data pull now refuses to run outside a DVC project; create
    # the .dvc/ marker so the probe passes.
    (tmp_path / ".dvc").mkdir()
    rc = cli.main(["data", "pull", "data/raw.csv", "--path", str(tmp_path)])
    assert rc == 0
    assert len(fast_fake.calls) == 1
    assert fast_fake.calls[0].targets == ["data/raw.csv"]
    assert len(dvc_ops.checkout_calls) == 1
    assert dvc_ops.checkout_calls[0].targets == ["data/raw.csv"]
    assert dvc_ops.pull_calls == []


def test_cli_data_pull_dvc_error_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    _, dvc_ops = patched_clients
    dvc_ops.pull_raises = DvcPullError("oops")
    (tmp_path / ".dvc").mkdir()
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 1
    assert "oops" in capsys.readouterr().err


def test_cli_data_pull_threads_dvc_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """Repeated `--dvc-arg` tokens reach `dvc_ops.pull(extra_args=...)`.
    Duplicate `--jobs` (one mintd-typed, one in `--dvc-arg`) survives end-
    to-end as literal pass-through; mintd does not dedupe."""
    _, dvc_ops = patched_clients
    (tmp_path / ".dvc").mkdir()
    rc = cli.main([
        "data", "pull", "data/raw.csv",
        "--path", str(tmp_path),
        "--jobs", "4",
        "--dvc-arg=--verbose",
        "--dvc-arg=--jobs",
        "--dvc-arg=16",
    ])
    assert rc == 0
    assert len(dvc_ops.pull_calls) == 1
    call = dvc_ops.pull_calls[0]
    assert call.jobs == 4
    assert call.extra_args == ["--verbose", "--jobs", "16"]


def test_cli_data_push_calls_data_push(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    _, dvc_ops = patched_clients
    rc = cli.main(["data", "push"])
    assert rc == 0
    assert len(dvc_ops.push_calls) == 1


def test_cli_data_add_prints_dvc_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    path = tmp_path / "raw.csv"
    path.write_text("data")
    rc = cli.main(["data", "add", str(path)])
    assert rc == 0
    assert "raw.csv.dvc" in capsys.readouterr().out


def test_cli_data_verify_dirty_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    _, dvc_ops = patched_clients
    dvc_ops.status_result = {"a.csv": "dirty"}
    rc = cli.main(["data", "verify"])
    assert rc == 1
    assert "a.csv: dirty" in capsys.readouterr().out


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
    rc = cli.main(["--json", "check", str(tmp_path)])
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


def test_cli_data_import_threads_dvc_args(
    tmp_path: Path,
    patched_clients,
) -> None:
    """`--dvc-arg` lands on every recorded `dvc_ops.import_(extra_args=...)`
    call on the non-bump path."""
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    rc = cli.main([
        "data", "import", "provider-xw",
        "--dest-root", str(tmp_path),
        "--dvc-arg=--verbose",
    ])
    assert rc == 0
    assert len(dvc_ops.calls) == 1
    assert dvc_ops.calls[0].extra_args == ["--verbose"]


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
    patched_clients,
    recording_reporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.data import BumpResult

    monkeypatch.setattr(
        "mintd.cli.bump_import",
        lambda *a, **kw: BumpResult(changed=False, old_pin="abc1234def", new_pin=None, dvc_path=None),
    )
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "up to date" in msg
    assert "abc1234" in msg


def test_data_import_bump_drift_prints_path(
    tmp_path: Path,
    patched_clients,
    recording_reporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.data import BumpResult

    new_dvc = tmp_path / "new.parquet.dvc"
    monkeypatch.setattr(
        "mintd.cli.bump_import",
        lambda *a, **kw: BumpResult(
            changed=True, old_pin="old1234567", new_pin="new7654321", dvc_path=new_dvc
        ),
    )
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "bumped" in msg
    assert "old1234" in msg
    assert "new7654" in msg


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
    captured = capsys.readouterr()
    out_combined = captured.out + captured.err  # Reporter writes to stderr
    assert rc == 0
    # Slice 30 polish: human-readable success line + PR URL when known.
    assert "Registration PR" in out_combined or "Registered" in out_combined


def test_registry_update_prints_diff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client, primary="outputs/old.parquet")
    # Build a slightly different metadata.json to update with
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = "outputs/new.parquet"
    (tmp_path / "metadata.json").write_text(json.dumps(data))
    rc = cli.main(["registry", "update", str(tmp_path)])
    captured = capsys.readouterr()
    out_combined = captured.out + captured.err  # Reporter writes to stderr
    assert rc == 0
    assert "→" in out_combined


def test_registry_sync_prints_count(
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    rc = cli.main(["registry", "sync"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "synced (0 entries)" in out


# ---------------------------------------------------------------------------
# Slice 36 — Pattern A/B/D
# ---------------------------------------------------------------------------


def _write_v1_metadata(tmp_path: Path) -> None:
    """Write a metadata.json with schema_version '1.1' to tmp_path."""
    (tmp_path / "metadata.json").write_text(
        json.dumps({"schema_version": "1.1", "project": {"name": "x"}}),
        encoding="utf-8",
    )


def test_cli_registry_update_v1_schema_emits_migrate_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """v1 metadata.json → exit 1, hint contains `mintd update metadata`, no Traceback."""
    _write_v1_metadata(tmp_path)
    rc = cli.main(["registry", "update", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "mintd update metadata" in err
    assert "Traceback" not in err


def test_cli_registry_register_v1_schema_emits_migrate_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    _write_v1_metadata(tmp_path)
    rc = cli.main(["registry", "register", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "mintd update metadata" in err
    assert "Traceback" not in err


def test_cli_registry_update_v2_validation_error_renders_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """A v2-shaped file with a missing required field → exit 1 with
    'N field error(s)' and the mintd check hint; no Traceback."""
    (tmp_path / "metadata.json").write_text(
        json.dumps({"schema_version": "2.0", "project": {"name": "x"}}),
        encoding="utf-8",
    )
    rc = cli.main(["registry", "update", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "field error" in err
    assert "mintd check" in err
    assert "Traceback" not in err


def test_cli_registry_register_v2_validation_error_renders_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    (tmp_path / "metadata.json").write_text(
        json.dumps({"schema_version": "2.0", "project": {"name": "x"}}),
        encoding="utf-8",
    )
    rc = cli.main(["registry", "register", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "field error" in err
    assert "mintd check" in err
    assert "Traceback" not in err


def test_cli_registry_update_catalog_not_found_includes_register_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """`registry update` on an unregistered project → exit 1 + register hint."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients

    def _raise(*a: Any, **kw: Any) -> None:
        raise CatalogNotFound("never-registered")

    monkeypatch.setattr(client, "update", _raise)

    rc = cli.main(["registry", "update", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "mintd registry register" in err


def test_cli_registry_register_catalog_already_exists_includes_update_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """`registry register` on already-registered project → exit 1 + update hint."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients

    def _raise(*a: Any, **kw: Any) -> None:
        raise CatalogAlreadyExists("already-here")

    monkeypatch.setattr(client, "register", _raise)
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: [])

    rc = cli.main(["registry", "register", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "mintd registry update" in err


def test_cli_registry_register_blocks_on_check_project_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """Pattern A's check gate: if check_project returns an error finding,
    register exits 1 and client.register is NEVER called."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients
    err_finding = CheckFinding(
        severity="error",
        section="producer",
        message="storage.bucket is empty",
        kind="storage_bucket_empty",
        hint="set storage.bucket in metadata.json",
    )
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: [err_finding])

    def must_not_call(*a: Any, **kw: Any) -> None:
        pytest.fail("client.register must not be called when check fails")

    monkeypatch.setattr(client, "register", must_not_call)

    rc = cli.main(["registry", "register", str(tmp_path)])
    capsys.readouterr()
    assert rc == 1


def test_cli_registry_register_passes_through_when_check_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: [])
    rc = cli.main(["registry", "register", str(tmp_path)])
    assert rc == 0


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
    __main__.py and the installed package surface, and that the CLI
    derives its version from installed metadata (single source of truth)."""
    from importlib.metadata import version as pkg_version

    result = subprocess.run(
        [sys.executable, "-m", "mintd", "--version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    out = result.stdout.strip()
    assert out.startswith("mintd ")
    reported = out.removeprefix("mintd ").strip()
    assert reported  # non-empty
    assert reported == pkg_version("mintd")  # CLI derives from installed metadata

def test_data_list_catalog_empty(patched_clients, capsys):
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert "no entries" in out

def test_data_list_catalog_populated(patched_clients, capsys):
    client, _ = patched_clients
    _register_provider_xw(client)
    
    # Register second
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
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

def test_data_list_imported_with_type_exits_64(patched_clients, capsys):
    # Slice 25: handler now uses reporter.error + return 2 (architectural
    # consistency) instead of argparse's SystemExit(64) for arg-combo errors.
    rc = cli.main(["data", "list", "--imported", "--type", "data"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--imported" in err and "--type" in err


# Slice 22: data list grouped + truncated + --json -------------------------


def _register_with_type(client, name: str, ptype: str, description: str) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    data["project"]["type"] = ptype
    data["project"]["full_name"] = f"{ptype}_{name}"
    data["repository"]["github_url"] = f"https://github.com/example-org/{name}"
    data["metadata"]["description"] = description
    client.register(Metadata.model_validate(data))


def test_cli_data_list_groups_by_type(patched_clients, capsys):
    client, _ = patched_clients
    _register_with_type(client, "alpha", "data", "Alpha description")
    _register_with_type(client, "tooling", "code", "Code utility")
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert "data (1)" in out
    assert "code (1)" in out
    assert "alpha" in out
    assert "tooling" in out


def test_cli_data_list_canonical_order_data_before_code(patched_clients, capsys):
    """Pin the canonical type order (data, code, project, enclave). A
    refactor that switched to alphabetical sort would put `code` first."""
    client, _ = patched_clients
    _register_with_type(client, "alpha", "data", "Alpha")
    _register_with_type(client, "tooling", "code", "Code utility")
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert out.index("data (1)") < out.index("code (1)")


def test_cli_data_list_no_description_placeholder(patched_clients, capsys):
    """Entries with an empty description render the `(no description)`
    placeholder, not an empty cell."""
    client, _ = patched_clients
    _register_with_type(client, "empty-desc", "data", "")
    cli.main(["data", "list"])
    out = capsys.readouterr().out
    assert "(no description)" in out


def test_cli_data_list_custom_width_truncates(patched_clients, capsys):
    """`--width N` overrides the default 80-char truncation threshold."""
    client, _ = patched_clients
    desc = "X" * 60
    _register_with_type(client, "wide", "data", desc)
    cli.main(["data", "list", "--width", "20"])
    out = capsys.readouterr().out
    assert "..." in out
    # 20-char limit means description column is well shorter than 60.
    rendered_line = next(line for line in out.splitlines() if "wide" in line)
    desc_part = rendered_line.split("wide", 1)[1].strip()
    assert len(desc_part) <= 25  # 20 chars + "..." margin


def test_cli_data_list_truncates_long_descriptions(patched_clients, capsys):
    client, _ = patched_clients
    long_desc = "X" * 500
    _register_with_type(client, "wide", "data", long_desc)
    cli.main(["data", "list"])
    out, _ = capsys.readouterr()
    assert "..." in out
    assert long_desc not in out


def test_cli_data_list_detailed_skips_truncation(patched_clients, capsys):
    client, _ = patched_clients
    long_desc = "Y" * 500
    _register_with_type(client, "wide", "data", long_desc)
    cli.main(["data", "list", "--detailed"])
    out, _ = capsys.readouterr()
    assert long_desc in out


def test_cli_data_list_json_emits_structured_output(patched_clients, capsys):
    client, _ = patched_clients
    _register_with_type(client, "alpha", "data", "Alpha desc")
    _register_with_type(client, "tooling", "code", "Code util")
    cli.main(["--json", "data", "list"])
    out, _ = capsys.readouterr()
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert all({"name", "project_type", "description"} <= set(e) for e in payload)
    # Sorted by (project_type, name); code < data alphabetically.
    assert [e["name"] for e in payload] == ["tooling", "alpha"]


def test_cli_data_list_json_does_not_truncate(patched_clients, capsys):
    client, _ = patched_clients
    long_desc = "Z" * 500
    _register_with_type(client, "wide", "data", long_desc)
    cli.main(["--json", "data", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["description"] == long_desc


# Slice 22: data pull friendly DVC-repo probe ------------------------------


def test_cli_data_pull_no_dvc_project_friendly_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], patched_clients
) -> None:
    rc = cli.main(["data", "pull", "dol-form5500", "--path", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "mintd data clone" in err
    assert "dol-form5500" in err
    assert str(tmp_path.resolve()) in err


def test_cli_data_pull_in_dvc_project_proceeds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    (tmp_path / ".dvc").mkdir()
    monkeypatch.setattr("mintd.cli._resolve_fast_sync_ops", lambda cfg, **_: None)
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "not inside a DVC project" not in err


# Slice 24: mintd data clone -----------------------------------------------


def test_cli_data_clone_invokes_clone_and_pull_product(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    from mintd.data import CloneResult

    received: dict[str, object] = {}

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        received.update(kwargs)
        return CloneResult(dest=Path("/tmp/sentinel"), rev="abc1234def", remote_bucket="my-bucket")

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    monkeypatch.setattr(
        "mintd.cli._resolve_git_ops", lambda cfg, **_: object()
    )

    rc = cli.main([
        "data", "clone", "provider-xw",
        "--dest", "/tmp/x",
        "--rev", "v1.2",
        "--primary",
        "--jobs", "4",
    ])

    assert rc == 0
    assert received["name"] == "provider-xw"
    assert received["dest"] == Path("/tmp/x")
    assert received["rev"] == "v1.2"
    assert received["primary_only"] is True
    assert received["jobs"] == 4
    captured = capsys.readouterr()
    # Slice 25: success line is chatter → stderr; result payload → stdout.
    # Slice 38b: success line now names the product, rev, and remote bucket.
    assert "cloned provider-xw" in captured.err
    assert "abc1234" in captured.err
    assert "s3://my-bucket" in captured.err


def test_cli_data_clone_threads_dvc_args(
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """`--dvc-arg` reaches `clone_and_pull_product(extra_dvc_args=...)`."""
    from mintd.data import CloneResult

    received: dict[str, object] = {}

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        received.update(kwargs)
        return CloneResult(dest=Path("/tmp/sentinel"), rev=None, remote_bucket=None)

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    monkeypatch.setattr("mintd.cli._resolve_git_ops", lambda cfg, **_: object())

    rc = cli.main([
        "data", "clone", "provider-xw",
        "--dest", "/tmp/x",
        "--dvc-arg=--verbose",
        "--dvc-arg=-v",
    ])
    assert rc == 0
    assert received["extra_dvc_args"] == ["--verbose", "-v"]


def test_cli_data_clone_threads_repeated_paths(
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """Repeatable `--path` reaches `clone_and_pull_product(paths=[...])`."""
    from mintd.data import CloneResult

    received: dict[str, object] = {}

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        received.update(kwargs)
        return CloneResult(dest=Path("/tmp/sentinel"), rev=None, remote_bucket=None)

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    monkeypatch.setattr("mintd.cli._resolve_git_ops", lambda cfg, **_: object())

    rc = cli.main([
        "data", "clone", "provider-xw",
        "--path", "data/final/",
        "--path", "data/intermediate/defs_30min.parquet",
    ])
    assert rc == 0
    assert received["paths"] == [
        "data/final/",
        "data/intermediate/defs_30min.parquet",
    ]
    assert received["primary_only"] is False


def test_cli_data_clone_path_and_primary_exits_64(
    patched_clients,
) -> None:
    """argparse mutex: --path and --primary conflict → exit 64."""
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "data", "clone", "provider-xw",
            "--path", "data/final/",
            "--primary",
        ])
    assert exc.value.code == 64


def test_cli_data_clone_unknown_path_reports_tracked_outputs(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """UnknownProductPath renders the tracked-outputs message + hint, rc 1."""
    def _raise(*a, **kw):
        from mintd.data import UnknownProductPath
        raise UnknownProductPath(
            "catalog entry 'provider-xw' has no tracked output 'data/nope.csv'; "
            "tracked outputs: data/final (primary)"
        )

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _raise)
    monkeypatch.setattr("mintd.cli._resolve_git_ops", lambda cfg, **_: object())

    rc = cli.main(["data", "clone", "provider-xw", "--path", "data/nope.csv"])
    assert rc == 1
    # Reporter wraps at console width, which can split asserted phrases
    # across lines (first seen on the Windows CI runner) — compare against
    # whitespace-normalized output.
    err = " ".join(capsys.readouterr().err.split())
    assert "data/nope.csv" in err
    assert "data/final (primary)" in err
    assert "drop --path" in err


def test_cli_data_clone_returns_one_on_catalog_not_found(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    def _raise(*a, **kw):
        from mintd.catalog import CatalogNotFound
        raise CatalogNotFound("provider-xw")

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _raise)
    monkeypatch.setattr(
        "mintd.cli._resolve_git_ops", lambda cfg, **_: object()
    )

    rc = cli.main(["data", "clone", "provider-xw"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "provider-xw" in err


def test_cli_data_clone_returns_one_on_producer_error(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    from mintd.producer import ProducerError

    def _raise(*a, **kw):
        raise ProducerError.unreachable(
            repo="https://x",
            pin="HEAD",
            detail="clone to /tmp/y failed; partial clone left in place: boom",
        )

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _raise)
    monkeypatch.setattr(
        "mintd.cli._resolve_git_ops", lambda cfg, **_: object()
    )

    rc = cli.main(["data", "clone", "provider-xw"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "boom" in err
    assert "/tmp/y" in err

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
    recording_reporter,
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

    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "provider-xw" in msg
    assert "aaaaaaa" in msg
    assert "1 output(s)" in msg


def test_enclave_pull_nothing_to_pull_message(
    patched_clients,
    recording_reporter,
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

    assert rc == 0
    msg = recording_reporter.events_of("info")[-1][1]
    assert "nothing to pull" in msg


# ---------------------------------------------------------------------------
# Slice 14 — mintd init
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_init_ops(monkeypatch: pytest.MonkeyPatch):
    from tests._fakes.init_ops import _FakeInitOps
    from mintd._config import Config
    fake = _FakeInitOps()
    monkeypatch.setattr("mintd.init.SubprocessInitOps", lambda *a, **k: fake)
    # Slice 30: CLI init now prompts for classification (interactive-only)
    # and reads bucket/endpoint from ~/.mintd/config.yaml. Tests don't
    # have a TTY or a guaranteed config file; stub both deterministically.
    monkeypatch.setattr(
        "mintd.init._prompt_classification",
        lambda *, reporter, prompt_fn=None, isatty_fn=None: ("labonly", None),
    )
    monkeypatch.setattr(
        "mintd._config.Config.load",
        classmethod(lambda cls, path=None: Config(
            storage_bucket_prefix="cooper-globus",
            storage_endpoint="",
        )),
    )
    return fake


def test_init_data_project_happy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    rc = cli.main(["init", "data", "my_proj", "--path", str(tmp_path)])
    assert rc == 0
    project_path = tmp_path / "data_my_proj"
    assert (project_path / "metadata.json").exists()
    assert (project_path / ".gitignore").exists()

    out = capsys.readouterr().out
    assert "metadata.json" in out
    assert ".gitignore" in out
    assert "initialized: git" in out
    assert "initialized: dvc" in out

    assert patched_init_ops.git_calls == [project_path]
    assert patched_init_ops.dvc_calls == [project_path]


def test_init_use_current_repo_writes_into_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    rc = cli.main(
        ["init", "data", "my_proj", "--path", str(tmp_path), "--use-current-repo"]
    )
    assert rc == 0
    assert (tmp_path / "metadata.json").exists()
    assert not (tmp_path / "data_my_proj").exists()


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
    # Default scaffold lands in {tmp_path}/data_my_proj/metadata.json; pre-create it.
    project_path = tmp_path / "data_my_proj"
    project_path.mkdir()
    (project_path / "metadata.json").write_text("{}")
    rc = cli.main(["init", "data", "my_proj", "--path", str(tmp_path)])
    assert rc == 1

    err = capsys.readouterr().err
    assert "error:" in err


def test_init_rejects_invalid_lang(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    """argparse `choices=...` should reject an unknown --lang value."""
    with pytest.raises(SystemExit):
        cli.main(
            ["init", "data", "my_proj", "--path", str(tmp_path), "--lang", "ocaml"]
        )
    err = capsys.readouterr().err
    assert "ocaml" in err or "invalid choice" in err


# ---------------------------------------------------------------------------
# Slice 15 — mintd publish
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_git_ops(monkeypatch: pytest.MonkeyPatch):
    from tests._fakes.registry_git_ops import _FakeRegistryGitOps
    fake = _FakeRegistryGitOps()
    monkeypatch.setattr("mintd.cli._resolve_git_ops", lambda cfg, **_: fake)
    return fake


def _init_git_in(path: Path) -> None:
    """Initialize a git repo in `path` so the slice-15 working-tree check passes."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test",
         "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=path, check=True,
    )


def test_cli_publish_dry_run_renders_diff(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    patched_git_ops,
) -> None:
    client, _ = patched_clients
    metadata = _register_provider_xw(client)
    metadata.data_products.primary = "data/final/"
    (tmp_path / "metadata.json").write_text(metadata.model_dump_json(indent=2))
    _init_git_in(tmp_path)

    rc = cli.main(["publish", "--dry-run", "--path", str(tmp_path)])

    # --dry-run writes preview to stderr
    err = capsys.readouterr().err
    assert rc == 0
    assert "About to publish" in err
    assert "Primary output:" in err

def test_cli_publish_blocked_by_check_errors_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    patched_git_ops,
) -> None:
    # No metadata.json → check_project emits an error finding → publish blocked.
    _init_git_in(tmp_path)

    rc = cli.main(["publish", "--dry-run", "--path", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err


def test_cli_publish_full_flow_calls_each_op(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    patched_git_ops,
) -> None:
    client, dvc_ops = patched_clients
    metadata = _register_provider_xw(client)
    # Ensure it's publish-valid
    metadata.data_products.primary = "data/final/"
    (tmp_path / "metadata.json").write_text(metadata.model_dump_json(indent=2))
    _init_git_in(tmp_path)
    # Stage the metadata so the working tree is clean.
    subprocess.run(["git", "add", "metadata.json"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test",
         "commit", "-q", "-m", "add metadata"],
        cwd=tmp_path, check=True,
    )

    rc = cli.main(["publish", "--path", str(tmp_path), "--yes"])

    assert rc == 0
    # dvc push called
    assert len(dvc_ops.push_calls) >= 1
    # git tag called
    assert len(patched_git_ops.tag_calls) >= 1


# ---------------------------------------------------------------------------
# Slice 16 — enclave package + enclave verify
# ---------------------------------------------------------------------------


def test_cli_package_creates_archive(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    archive = tmp_path / "out" / "transfer-2026-05-15-000000.tar.gz"

    captured: dict[str, Any] = {}

    def fake_package(*args: Any, **kwargs: Any) -> Path:
        captured.update(kwargs)
        return archive

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(
        [
            "enclave",
            "package",
            "--manifest",
            str(manifest),
            "--output",
            str(archive),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "packaged:" in out
    # --output was passed → output_archive set, output_dir = None.
    assert captured["output_archive"] == archive
    assert captured["output_dir"] is None


def test_cli_package_nothing_to_package_exits_one(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest, NothingToPackage

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    def fake_package(*args: Any, **kwargs: Any) -> Path:
        raise NothingToPackage("no items")

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(
        ["enclave", "package", "--manifest", str(manifest)]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "no items" in err


def test_cli_package_unsafe_symlink_exits_one(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`UnsafeArchiveMember` must be caught in the CLI handler — without
    this, packaging a downloads dir with a hostile symlink would crash
    with a raw Python traceback (caught in R2 review as P0)."""
    from mintd._archive_ops import UnsafeArchiveMember
    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    def fake_package(*args: Any, **kwargs: Any) -> Path:
        raise UnsafeArchiveMember(
            "symlink /downloads/repo/v/evil resolves outside src_dir"
        )

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(["enclave", "package", "--manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "resolves outside" in err


def test_cli_verify_writes_transferred_entries(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date
    from mintd.enclave import EnclaveManifest, TransferredItem

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    extracted = tmp_path / "extracted"
    extracted.mkdir()

    item = TransferredItem(
        repo="ds-alpha",
        contract_pin="c" * 40,
        artifact_pin="a" * 32,
        transfer_date=date(2026, 5, 15),
        transfer_id="transfer-2026-05-15-000000",
        local_path="/abs/data/ds-alpha/v1",
    )

    def fake_verify(*args: Any, **kwargs: Any) -> tuple[Path, list[TransferredItem]]:
        return manifest, [item]

    monkeypatch.setattr("mintd.cli.enclave_verify", fake_verify)

    rc = cli.main(
        [
            "enclave",
            "verify",
            str(extracted),
            "--manifest",
            str(manifest),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "verified:" in out
    assert "ds-alpha" in out


def test_cli_verify_traversal_attack_exits_one_with_clear_error(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest, PathTraversalDetected

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    extracted = tmp_path / "extracted"
    extracted.mkdir()

    def fake_verify(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        raise PathTraversalDetected("evil/../etc")

    monkeypatch.setattr("mintd.cli.enclave_verify", fake_verify)

    rc = cli.main(
        ["enclave", "verify", str(extracted), "--manifest", str(manifest)]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "evil/../etc" in err


# ---------------------------------------------------------------------------
# Slice 21 — mintd config show / setup / validate
# ---------------------------------------------------------------------------


def test_cli_config_show_prints_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text("registry_url: https://e.com/r.git\n")
    rc = cli.main(["config", "show", "--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "registry_url: https://e.com/r.git" in out


def test_cli_config_setup_set_writes_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cfg.yaml"
    rc = cli.main(
        ["config", "setup", "--path", str(target),
         "--set", "registry_url=https://foo"]
    )
    assert rc == 0
    assert "registry_url: https://foo" in target.read_text(encoding="utf-8")


def test_cli_config_validate_invalid_yaml_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text("dvc_timeout: oranges\n")
    rc = cli.main(["config", "validate", "--path", str(p)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "✗ schema" in out


def test_cli_config_setup_dry_run_does_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "cfg.yaml"
    rc = cli.main(
        ["config", "setup", "--path", str(target),
         "--set", "registry_url=https://x", "--dry-run"]
    )
    assert rc == 0
    assert not target.exists()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "registry_url: https://x" in out


def test_cli_config_setup_set_missing_equals_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--set no-equals-value` surfaces parse_set_pair's ConfigError."""
    p = tmp_path / "cfg.yaml"
    rc = cli.main(
        ["config", "setup", "--path", str(p), "--set", "no-equals-here"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "KEY=VALUE" in err
    assert not p.exists()


def test_cli_config_setup_from_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--from - reads stdin (sentinel translated to None in apply_from_file)."""
    import io as _io
    target = tmp_path / "cfg.yaml"
    monkeypatch.setattr("sys.stdin", _io.StringIO("registry_url: piped\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    rc = cli.main(["config", "setup", "--path", str(target), "--from", "-"])
    assert rc == 0
    assert "registry_url: piped" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Slice 32 — publish preview gate + --yes
# ---------------------------------------------------------------------------

def test_cli_publish_yes_flag_skips_prompt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    patched_git_ops,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 32: --yes bypasses the interactive preview prompt."""
    client, _ = patched_clients
    metadata = _register_provider_xw(client)
    metadata.data_products.primary = "data/final/"
    (tmp_path / "metadata.json").write_text(metadata.model_dump_json(indent=2))
    _init_git_in(tmp_path)
    subprocess.run(["git", "add", "metadata.json"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test",
         "commit", "-q", "-m", "add metadata"],
        cwd=tmp_path, check=True,
    )
    # input() should NOT be called.
    def _explode(_prompt):
        raise AssertionError("input() called despite --yes")
    monkeypatch.setattr("builtins.input", _explode)
    rc = cli.main(["publish", "--path", str(tmp_path), "--yes"])
    assert rc == 0


def test_cli_publish_non_tty_without_yes_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    patched_git_ops,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 32: non-TTY without --yes exits 1 with an actionable hint."""
    client, _ = patched_clients
    metadata = _register_provider_xw(client)
    metadata.data_products.primary = "data/final/"
    (tmp_path / "metadata.json").write_text(metadata.model_dump_json(indent=2))
    _init_git_in(tmp_path)
    subprocess.run(["git", "add", "metadata.json"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test",
         "commit", "-q", "-m", "add metadata"],
        cwd=tmp_path, check=True,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    rc = cli.main(["publish", "--path", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--yes" in err or "interactive" in err.lower()


# ---------------------------------------------------------------------------
# Slice 38a — feedback presence (status / labels / hints)
# ---------------------------------------------------------------------------


def _register_with_storage(client: InMemoryCatalogClient, name: str = "provider-xw") -> None:
    """Register an entry with a versioned storage block so `data ls` reaches
    the S3 listing path."""
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    data["project"]["full_name"] = f"data_{name}"
    data["repository"]["github_url"] = f"https://github.com/example-org/{name}"
    data["storage"] = {
        "provider": "s3",
        "bucket": "test-bucket",
        "prefix": "products/example",
        "endpoint": "https://s3.example.com",
        "versioning": True,
        "dvc": {"remote_name": name},
    }
    client.register(Metadata.model_validate(data))


def _raises(exc):
    def _fn(*a, **k):
        raise exc
    return _fn


def test_cli_config_validate_shows_connectivity_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_reporter
) -> None:
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr("mintd.cli.config_ops.validate_config", lambda *a, **k: [])
    monkeypatch.setattr("mintd.cli.config_ops.render_validation", lambda *a, **k: ("ok", 0))
    cli.main(["config", "validate"])
    assert ("status", "Validating S3 connectivity...") in recording_reporter.events


def test_cli_data_import_single_output_shows_status(
    tmp_path: Path, patched_clients, recording_reporter
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client)
    cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    statuses = [e[1] for e in recording_reporter.events_of("status")]
    assert any("Importing provider-xw" in s for s in statuses)


def test_cli_data_import_all_updates_status_per_output(
    tmp_path: Path, patched_clients, recording_reporter
) -> None:
    client, _ = patched_clients
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "multi"
    data["project"]["full_name"] = "data_multi"
    data["repository"]["github_url"] = "https://github.com/example-org/multi"
    data["data_products"]["outputs"] = [
        {"path": f"outputs/o{i}.parquet", "description": "", "primary": i == 0, "last_published": ""}
        for i in range(2)
    ]
    client.register(Metadata.model_validate(data))
    cli.main(["data", "import", "multi", "--all", "--dest-root", str(tmp_path)])
    labels = [e[1] for e in recording_reporter.events_of("update_status")]
    assert any("(1/2)" in s for s in labels)
    assert any("(2/2)" in s for s in labels)
    # The determinate progress bar must NOT be used (subprocess invariant).
    assert recording_reporter.events_of("progress") == []


def test_cli_data_import_bump_catches_dvc_op_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter, capsys: pytest.CaptureFixture[str],
) -> None:
    from mintd._dvc_ops import DvcOpError
    monkeypatch.setattr("mintd.cli.bump_import", _raises(DvcOpError("boom")))
    rc = cli.main(["data", "import", "provider-xw", "--bump"])
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and errs[0][2]  # has a hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_data_push_catches_dvc_push_error_with_hint(
    patched_clients, recording_reporter,
) -> None:
    from mintd._dvc_ops import DvcPushError
    _, dvc_ops = patched_clients
    dvc_ops.push_raises = DvcPushError("denied")
    rc = cli.main(["data", "push"])
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and "mintd config validate" in (errs[0][2] or "")


def test_cli_data_verify_shows_status(
    tmp_path: Path, patched_clients, recording_reporter,
) -> None:
    cli.main(["data", "verify", "--path", str(tmp_path)])
    assert ("status", "Verifying DVC data...") in recording_reporter.events


def test_cli_data_ls_shows_status_during_listing(
    monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    from mintd._s3_listing_ops import S3ListingResult
    client, _ = patched_clients
    _register_with_storage(client)
    fake_result = S3ListingResult(
        bucket="test-bucket", prefix="products/example",
        endpoint="https://s3.example.com", objects=[], truncated_to_prefix=None,
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_s3_listing_ops", lambda cfg: (lambda **k: fake_result)
    )
    cli.main(["data", "ls", "provider-xw"])
    statuses = [e[1] for e in recording_reporter.events_of("status")]
    assert any("Listing provider-xw on S3" in s for s in statuses)


def test_cli_enclave_bump_shows_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    monkeypatch.setattr("mintd.cli.enclave_bump", lambda *a, **k: None)
    cli.main(["enclave", "bump", "provider-xw", "--manifest", str(tmp_path / "m.yaml")])
    assert any(
        "Bumping provider-xw" in e[1] for e in recording_reporter.events_of("status")
    )


def test_cli_enclave_pull_shows_outer_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    monkeypatch.setattr("mintd.cli.enclave_pull", lambda *a, **k: (Path("."), []))
    cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert ("status", "Pulling enclave data...") in recording_reporter.events


def test_cli_enclave_pull_dvc_op_error_names_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter, capsys: pytest.CaptureFixture[str],
) -> None:
    from mintd._dvc_ops import DvcPullError
    from mintd.enclave import EnclavePullError
    monkeypatch.setattr(
        "mintd.cli.enclave_pull",
        _raises(EnclavePullError("repo-b", DvcPullError("x"))),
    )
    rc = cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and "repo-b" in (errs[0][2] or "")
    assert "Traceback" not in capsys.readouterr().err


def test_cli_enclave_pull_not_in_repo_hint_is_not_pin_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter,
) -> None:
    """A not-DVC-initialized enclave must not get the misleading pin/repo hint
    (slice 47, Q3) — the pin/repo are fine; the fix is `dvc init`."""
    from mintd._dvc_ops import DvcNotInRepoError
    from mintd.enclave import EnclavePullError
    monkeypatch.setattr(
        "mintd.cli.enclave_pull",
        _raises(EnclavePullError("repo-b", DvcNotInRepoError("nope"))),
    )
    rc = cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert rc == 1
    hint = recording_reporter.events_of("error")[0][2] or ""
    assert "pin/repo" not in hint
    assert "dvc init" in hint.lower()


def test_cli_enclave_pull_path_not_found_keeps_pin_repo_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter,
) -> None:
    """A genuine pin/repo failure (path missing at rev) keeps the pin/repo hint."""
    from mintd._dvc_ops import DvcImportPathNotFound
    from mintd.enclave import EnclavePullError
    monkeypatch.setattr(
        "mintd.cli.enclave_pull",
        _raises(EnclavePullError("repo-b", DvcImportPathNotFound("missing"))),
    )
    rc = cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert rc == 1
    hint = recording_reporter.events_of("error")[0][2] or ""
    assert "pin/repo" in hint


def test_cli_registry_sync_shows_refresh_status(
    monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    client, _ = patched_clients
    monkeypatch.setattr(client, "sync", lambda: 0)
    cli.main(["registry", "sync"])
    assert ("status", "Refreshing registry cache...") in recording_reporter.events


def test_spinner_dvc_handlers_thread_reporter_into_resolve_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_reporter,
) -> None:
    """Pins fix #2: every spinner-wrapped dvc handler must pass the reporter
    into _resolve_clients so dvc subprocess stderr flows through the spinner
    (passthrough_stderr), not raw to the terminal."""
    captured: list = []
    client = InMemoryCatalogClient()
    _register_provider_xw(client)
    fake_dvc = _FakeDvcOps()
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: client)

    def spy(config, reporter=None, **_):
        captured.append(reporter)
        return client, fake_dvc

    monkeypatch.setattr("mintd.cli._resolve_clients", spy)
    monkeypatch.setattr("mintd.cli.enclave_pull", lambda *a, **k: (Path("."), []))

    cli.main(["data", "push"])
    cli.main(["data", "verify", "--path", str(tmp_path)])
    cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])

    assert len(captured) == 4
    assert all(r is recording_reporter for r in captured)


def test_no_handler_calls_sys_stderr_directly() -> None:
    """Meta-test: every `print(..., file=sys.stderr)` in cli.py is inside the
    documented allowlist. Pins the slice-38a print→reporter migration.

    Allowlist rationale:
      - error: argparse framework override (not a handler).
      - _handle_data_pull: frozen surface (slice 36/37 own its rendering).
      - _handle_config_show / _handle_config_setup / _handle_update_metadata:
        not in the 38a audit (decision 4 — touched handlers only).
      - _render_bump_blocked: shared renderer; 38b cleanup candidate.
    """
    import ast
    src = Path("src/mintd/cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    parent: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    allowlist = {
        "error", "_handle_data_pull", "_handle_config_show",
        "_handle_config_setup", "_handle_update_metadata", "_render_bump_blocked",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        uses_stderr = any(
            kw.arg == "file"
            and isinstance(kw.value, ast.Attribute)
            and isinstance(kw.value.value, ast.Name)
            and kw.value.value.id == "sys"
            and kw.value.attr == "stderr"
            for kw in node.keywords
        )
        if not uses_stderr:
            continue
        # Walk up to the enclosing FunctionDef.
        cur = node
        fn_name = None
        while cur in parent:
            cur = parent[cur]
            if isinstance(cur, ast.FunctionDef):
                fn_name = cur.name
                break
        if fn_name not in allowlist:
            offenders.append(fn_name or "<module>")
    assert offenders == [], f"unexpected sys.stderr writers: {offenders}"


# ---------------------------------------------------------------------------
# Slice 38b — completion-line richness (check (e): state what happened)
# ---------------------------------------------------------------------------


def test_format_duration_cases() -> None:
    from mintd.cli import _format_duration

    assert _format_duration(0.142) == "142ms"
    assert _format_duration(12.4) == "12s"
    assert _format_duration(185) == "3m05s"


def _write_import_dvc(path: Path, *, url: str, rev: str, size: int, nfiles: int) -> None:
    path.write_text(
        "deps:\n"
        f"- path: outputs/final.parquet\n"
        "  repo:\n"
        f"    url: {url}\n"
        f"    rev_lock: {rev}\n"
        "outs:\n"
        f"- path: final.parquet\n"
        f"  size: {size}\n"
        f"  nfiles: {nfiles}\n",
        encoding="utf-8",
    )


def test_import_summary_parses_import_dvc(tmp_path: Path) -> None:
    from mintd.cli import _import_summary

    dvc = tmp_path / "final.parquet.dvc"
    _write_import_dvc(
        dvc, url="https://github.com/example-org/data_src", rev="deadbeef1234", size=2048, nfiles=3
    )
    summary = _import_summary([dvc])
    assert summary["pin"] == "deadbeef1234"
    assert summary["producer_repo"] == "https://github.com/example-org/data_src"
    assert summary["total_bytes"] == 2048
    assert summary["file_count"] == 3
    assert summary["dest"] == str(tmp_path)


def test_import_summary_falls_back_on_non_import_dvc(tmp_path: Path) -> None:
    from mintd.cli import _import_summary

    dvc = tmp_path / "raw.csv.dvc"
    dvc.write_text("outs:\n- path: raw.csv\n  size: 99\n", encoding="utf-8")
    summary = _import_summary([dvc])
    assert summary["pin"] is None
    assert summary["producer_repo"] is None
    assert summary["total_bytes"] == 99
    assert summary["file_count"] == 1


def test_cli_data_pull_success_shows_count_size_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    from mintd.data_ops import PullSummary

    (tmp_path / ".dvc").mkdir()
    monkeypatch.setattr(
        "mintd.cli.data_pull",
        lambda *a, **k: PullSummary(file_count=4, total_bytes=2048, elapsed_s=12.4),
    )
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "4 file(s)" in msg
    assert "2 KB" in msg
    assert "in 12s" in msg


def test_cli_data_pull_success_omits_size_when_zero_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    from mintd.data_ops import PullSummary

    (tmp_path / ".dvc").mkdir()
    monkeypatch.setattr(
        "mintd.cli.data_pull",
        lambda *a, **k: PullSummary(file_count=2, total_bytes=0, elapsed_s=1.0),
    )
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "2 file(s)" in msg
    assert "KB" not in msg and "MB" not in msg


def test_cli_data_import_success_shows_provenance_and_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client)
    dvc = tmp_path / "final.parquet.dvc"
    _write_import_dvc(
        dvc, url="https://github.com/example-org/provider-xw", rev="abc1234def0", size=4096, nfiles=2
    )
    monkeypatch.setattr("mintd.cli.import_product", lambda *a, **k: [dvc])
    rc = cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "imported provider-xw" in msg
    assert "abc1234" in msg
    assert "2 file(s)" in msg
    assert "4 KB" in msg


def test_cli_data_import_json_mode_no_checkmark_on_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client)
    dvc = tmp_path / "final.parquet.dvc"
    _write_import_dvc(
        dvc, url="https://github.com/example-org/provider-xw", rev="abc1234def0", size=4096, nfiles=2
    )
    monkeypatch.setattr("mintd.cli.import_product", lambda *a, **k: [dvc])
    rc = cli.main(["--json", "data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "✓" not in out
    payload = json.loads(out)
    assert payload["pin"] == "abc1234def0"
    assert payload["total_bytes"] == 4096


def test_cli_registry_update_success_names_changed_fields(
    tmp_path: Path, patched_clients, recording_reporter
) -> None:
    client, _ = patched_clients
    _register_provider_xw(client, primary="outputs/old.parquet")
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = "outputs/new.parquet"
    (tmp_path / "metadata.json").write_text(json.dumps(data))
    rc = cli.main(["registry", "update", str(tmp_path)])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "updated provider-xw" in msg
    assert "field(s)" in msg
    assert "primary" in msg


def test_cli_config_validate_success_shows_target_and_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_reporter
) -> None:
    from mintd.config_ops import ValidationStep

    class _Cfg:
        storage_endpoint = "https://s3.example.com"
        aws_profile_name = "mintd"

    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: _Cfg()))
    monkeypatch.setattr(
        "mintd.cli.config_ops.validate_config",
        lambda *a, **k: [ValidationStep(name="s3", status="ok", message="ok", latency_ms=42)],
    )
    rc = cli.main(["config", "validate", "--bucket", "my-bucket"])
    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "s3://my-bucket" in msg
    assert "s3.example.com" in msg
    assert "mintd" in msg
    assert "42ms" in msg


def test_cli_check_renders_severity_footer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = [
        CheckFinding(severity="error", section="schema", message="bad", kind="generic"),
        CheckFinding(severity="warning", section="consumer", message="meh", kind="drift"),
    ]
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: findings)
    rc = cli.main(["check", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 error(s), 1 warning(s)" in out
    assert "consumer" in out and "schema" in out


def test_cli_check_clean_footer_says_no_issues(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mintd.cli.check_project", lambda *a, **kw: [])
    rc = cli.main(["check", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no issues found" in out


def test_cli_publish_success_echoes_tag_and_pr(
    tmp_path: Path,
    recording_reporter,
    patched_clients,
    patched_git_ops,
) -> None:
    client, dvc_ops = patched_clients
    metadata = _register_provider_xw(client)
    metadata.data_products.primary = "data/final/"
    (tmp_path / "metadata.json").write_text(metadata.model_dump_json(indent=2))
    _init_git_in(tmp_path)
    subprocess.run(["git", "add", "metadata.json"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test",
         "commit", "-q", "-m", "add metadata"],
        cwd=tmp_path, check=True,
    )

    rc = cli.main(["publish", "--path", str(tmp_path), "--yes"])

    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "published provider-xw" in msg
    assert "tag v" in msg
    assert "PR" in msg
