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
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


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
