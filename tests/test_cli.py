"""Tests for ``mintd.cli`` — slice 10 minimal CLI scaffolding.

All tests call ``cli.main(argv=[...])`` in-process and monkeypatch
the ``_resolve_*`` factories to inject fakes. One subprocess smoke test
(``test_python_m_mintd_version_smoke``) exercises packaging via
``python -m mintd``.
"""

from __future__ import annotations

import ast
import builtins
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
from mintd._config import ConfigError
from mintd.data import BumpBlocked
from mintd.model import Metadata
from mintd._dvc_ops import DvcPullError
from mintd._registry_git_ops import GitOpError
from tests._enclave_fixtures import stage_enclave_manifest
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
        "mintd.cli._resolve_dvc_ops", lambda cfg, reporter=None, **_: dvc_ops
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


def test_cli_data_pull_wall_timeout_renders_sentence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """Slice D (pull-all audit, fix 5): a WallTimeoutExceeded escaping a
    handler renders as a full sentence with a config hint — never the bare
    seconds float."""
    from mintd._subprocess import WallTimeoutExceeded

    _, dvc_ops = patched_clients
    dvc_ops.pull_raises = WallTimeoutExceeded(30.0)
    (tmp_path / ".dvc").mkdir()
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "command exceeded wall timeout of 30.0s" in err
    assert "hint:" in err
    assert "config.yaml" in err
    # The float never appears on its own line (the old rendering).
    assert not any(line.strip() == "30.0" for line in err.splitlines())


def test_cli_data_pull_storage_key_error_names_target_and_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """Slice D (pull-all audit, fix 6): dvc's StorageKeyError tuple crash
    surfaces as `error: ... <target> ...` plus the targeted-retry hint, not
    the opaque `('data', 'final', ...)` tuple."""
    from mintd._dvc_ops import DvcStorageKeyError

    _, dvc_ops = patched_clients
    dvc_ops.pull_raises = DvcStorageKeyError(
        "dvc pull failed (exit 255): storage key error on "
        "'data/final/aha_ccn_xw/crosswalk_aha_pos.dta' (target data/final.dvc)"
        " — plain dvc cannot serve this version-aware output",
        target="data/final.dvc",
        hint="retry just this target: mintd data pull data/final.dvc",
    )
    (tmp_path / ".dvc").mkdir()
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "storage key error" in err
    assert "data/final.dvc" in err
    assert "hint:" in err
    assert "mintd data pull data/final.dvc" in err


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


def test_cli_data_push_forwards_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """End-to-end regression for the reported bug: a positional target reaches
    the fake as `["data/x.dvc"]` instead of being silently dropped."""
    _, dvc_ops = patched_clients
    rc = cli.main(["data", "push", "data/x.dvc"])
    assert rc == 0
    assert dvc_ops.push_calls[0].targets == ["data/x.dvc"]


def test_cli_data_push_no_targets_is_none(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """Bare `data push` pins the `args.targets or None` conversion: no
    positionals means a full-repo push (targets is None)."""
    _, dvc_ops = patched_clients
    rc = cli.main(["data", "push"])
    assert rc == 0
    assert dvc_ops.push_calls[0].targets is None


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


def test_data_import_repeated_path_imports_each(
    tmp_path: Path,
    patched_clients,
) -> None:
    """D-B: `--path` is repeatable — each lands as its own `dvc import`.
    Asserted on the dvc CALLS, not on `args.import_path`: a parser-only
    assertion green-lights a signature that mishandles a list."""
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    rc = cli.main([
        "data", "import", "provider-xw",
        "--dest-root", str(tmp_path),
        "--path", "outputs/a.csv",
        "--path", "outputs/b.csv",
    ])
    assert rc == 0
    assert [c.path for c in dvc_ops.calls] == ["outputs/a.csv", "outputs/b.csv"]


def test_data_import_plain_import_still_imports_the_primary(
    tmp_path: Path,
    patched_clients,
) -> None:
    """M8's falsifier: `--path`'s `default=[]` must become None before
    `_resolve_paths`, or a plain import loops zero times and exits 0 having
    written nothing."""
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    rc = cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    assert rc == 0
    assert [c.path for c in dvc_ops.calls] == ["outputs/main.parquet"]


def test_data_import_jobs_reaches_the_dvc_argv(
    tmp_path: Path,
    patched_clients,
) -> None:
    """`--jobs` rides `extra_args` as `-j <n>` (no DvcOps protocol change)."""
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    rc = cli.main([
        "data", "import", "provider-xw",
        "--dest-root", str(tmp_path),
        "--dvc-arg=--verbose",
        "--jobs", "4",
    ])
    assert rc == 0
    assert dvc_ops.calls[0].extra_args == ["--verbose", "-j", "4"]


def test_data_import_forwards_the_dvc_argv_on_both_arms() -> None:
    """`dvc_args` is assembled ABOVE the `if args.bump:` branch, and was only
    ever passed on the plain one — so `--bump --jobs 8` exited 0, printed
    nothing, and ran at default parallelism. `--timeout` on the same arm always
    worked, which is what made the gap invisible.

    Read from the SOURCE rather than through a double: the only runtime path
    to `dvc_ops.import_` under `--bump` goes through `check_project`, and
    stubbing either that or `bump_import` would grow the internal-stub census
    `test_substrate_rules.py` pins shrink-only. `test_bump_forwards_extra_dvc_args`
    (tests/test_data.py) covers the plumbing below this seam.

    Mutation: drop `extra_dvc_args=` from either call -> this reddens.
    """
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    handler = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_handle_data_import"
    )
    forwarded = {
        node.func.id: {kw.arg for kw in node.keywords}
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"bump_import", "import_product"}
    }

    assert set(forwarded) == {"bump_import", "import_product"}, forwarded
    for callee, kwargs in forwarded.items():
        assert "extra_dvc_args" in kwargs, f"{callee} drops the assembled dvc argv"


def test_data_import_timeout_reaches_the_dvc_ops_factory(
    tmp_path: Path,
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--timeout` overrides `timeouts.transfer` on the config the dvc-ops
    factory receives — the same `_timeouts_for` seam `data clone` uses."""
    _client, dvc_ops = patched_clients
    _register_provider_xw(_client)
    seen: list[Any] = []

    def capturing(cfg: Any, reporter: Any = None, **_: Any) -> Any:
        seen.append(cfg)
        return dvc_ops

    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", capturing)
    rc = cli.main([
        "data", "import", "provider-xw",
        "--dest-root", str(tmp_path),
        "--timeout", "5",
    ])
    assert rc == 0
    assert seen[0].timeouts.transfer == 5


def test_data_import_and_clone_share_the_selector_flags() -> None:
    """Parser parity: `--path` (repeatable), `--jobs` and `--timeout` parse
    identically on `data import` and `data clone` — the clone help text
    claims "same selector as `mintd data import --path`", which used to be
    false."""
    parser = cli._build_parser()

    imp = parser.parse_args([
        "data", "import", "x", "--path", "a", "--path", "b",
        "--jobs", "3", "--timeout", "7",
    ])
    clone = parser.parse_args([
        "data", "clone", "x", "--path", "a", "--path", "b",
        "--jobs", "3", "--timeout", "7",
    ])

    assert imp.import_path == ["a", "b"]
    assert clone.paths == ["a", "b"]
    assert imp.jobs == clone.jobs == 3
    assert imp.timeout == clone.timeout == 7.0


def test_data_import_bump_rejects_more_than_one_path(
    tmp_path: Path,
    patched_clients,
) -> None:
    """Silently bumping the first of several `--path`s is the last-writer-
    wins defect D-A killed; argparse misuse exits 64."""
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "data", "import", "provider-xw", "--bump",
            "--path", "a", "--path", "b",
        ])
    assert exc.value.code == 64


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


def test_bump_blocked_catalog_unresolved_prefers_the_findings_own_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`catalog_unresolved` now covers a failed catalog *read* as well as a
    missing client, so the renderer's fixed "set registry_url" string would
    misdirect a user whose registry_url is already correct."""
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message="cannot read the catalog to resolve producer URL for provider-xw: fatal: unable to access",
        kind="catalog_unresolved",
        hint="check your network and `registry_url`; if both are fine, delete the local registry cache",
    )

    def raises(*args: Any, **kwargs: Any) -> Any:
        raise BumpBlocked("provider-xw", finding)

    monkeypatch.setattr("mintd.cli.bump_import", raises)
    rc = cli.main(["data", "import", "provider-xw", "--bump"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "delete the local registry cache" in err
    assert "check that registry_url is set" not in err


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


def test_measure_clone_result_excludes_git_and_dvc_trees(tmp_path: Path) -> None:
    """Regression: the clone ✓-line measured .dvc/cache alongside the
    workspace, double-counting every pulled byte (37 GB of product
    reported as 74.3 GB). Only workspace files count."""
    (tmp_path / "data" / "final").mkdir(parents=True)
    (tmp_path / "data" / "final" / "a.parquet").write_bytes(b"x" * 100)
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "blob").write_bytes(b"g" * 999)
    (tmp_path / ".dvc" / "cache" / "files" / "md5" / "ab").mkdir(parents=True)
    (tmp_path / ".dvc" / "cache" / "files" / "md5" / "ab" / "cdef").write_bytes(b"x" * 100)
    (tmp_path / ".dvc" / "config").write_text("[core]\n")

    files, total = cli._measure_clone_result(tmp_path)
    assert files == 1
    assert total == 100


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

    monkeypatch.chdir(tmp_path)
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

    monkeypatch.chdir(tmp_path)
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


def test_init_refuses_to_overwrite_and_force_overrides(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    """Refusal names the problem and the way out; --force takes it."""
    (tmp_path / "README.md").write_text("MY NOTES\n", encoding="utf-8")
    argv = [
        "init", "data", "my_proj",
        "--path", str(tmp_path),
        "--use-current-repo",
    ]

    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "hint:" in err
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "MY NOTES\n"

    assert cli.main([*argv, "--force"]) == 0
    assert (tmp_path / "README.md").read_text(encoding="utf-8") != "MY NOTES\n"


def test_init_path_is_a_regular_file_exits_one_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_init_ops,
) -> None:
    """A --path that is a regular file is user error, not a crash.

    At HEAD `mkdir` raised NotADirectoryError straight through cli.py's
    except clause, which catches only InitDestinationExists / InitNameInvalid
    / InitOpError -- so the user got a raw traceback.
    """
    afile = tmp_path / "afile"
    afile.write_text("not a dir\n", encoding="utf-8")

    rc = cli.main(["init", "data", "my_proj", "--path", str(afile)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "error:" in captured.err
    assert "Traceback" not in captured.err + captured.out



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

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        captured.update(kwargs)
        return archive, []

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


def test_cli_package_resend_flag_forwarded(
    patched_clients,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--resend is the documented recovery for a bundle that never arrived, so
    the flag reaching enclave_package is the whole contract at this layer."""
    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    captured: dict[str, Any] = {}

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        captured.update(kwargs)
        return tmp_path / "t.tar.gz", []

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(
        ["enclave", "package", "--manifest", str(manifest), "--resend"]
    )

    assert rc == 0
    assert captured["resend"] is True


def test_cli_package_defaults_to_not_resending(
    patched_clients,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    captured: dict[str, Any] = {}

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        captured.update(kwargs)
        return tmp_path / "t.tar.gz", []

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    cli.main(["enclave", "package", "--manifest", str(manifest)])

    assert captured["resend"] is False


def test_cli_package_nothing_new_exits_zero(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything already across the gap is the routine steady state. Exit 1
    would make a scripted `pull && package` loop a red run forever."""
    from mintd.enclave import EnclaveManifest, NothingNewToPackage

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        raise NothingNewToPackage("all 2 downloaded product(s) have already crossed")

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(["enclave", "package", "--manifest", str(manifest)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "nothing new to package" in captured.err
    assert "--resend" in captured.err
    # No archive was built, so nothing may claim one was.
    assert "packaged:" not in captured.out
    assert "error:" not in captured.err


def test_cli_package_reports_skipped_and_landing(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently shipping B while skipping A is the exact operation the user
    complained about; the skip and the landing commands both have to surface."""
    from datetime import datetime

    from mintd.enclave import DownloadedItem, EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    archive = tmp_path / "transfer-2026-05-15-000000.tar.gz"
    skipped = [
        DownloadedItem(
            repo="ds-alpha",
            output="data.csv",
            contract_pin="c" * 40,
            artifact_pin="a" * 32,
            fetch_strategy="dvc-import",
            downloaded_at=datetime(2026, 5, 15),
            local_path=str(tmp_path / "downloads" / "ds-alpha" / "v1"),
        )
    ]

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        return archive, skipped

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(["enclave", "package", "--manifest", str(manifest)])

    captured = capsys.readouterr()
    assert rc == 0
    assert "packaged:" in captured.out
    assert "ds-alpha" in captured.err
    assert "--resend" in captured.err
    # The landing instructions travel with the researcher, who walks to a
    # different machine after running this.
    assert "tar -xzf" in captured.err
    assert "land.py" in captured.err


def test_cli_package_skipped_count_counts_products_not_rows(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One product with two outputs is two downloaded[] rows. "skipped 2
    products: ds-alpha" would read as two products sharing a name."""
    from datetime import datetime

    from mintd.enclave import DownloadedItem, EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    skipped = [
        DownloadedItem(
            repo="ds-alpha",
            output=out,
            contract_pin="c" * 40,
            artifact_pin=pin * 32,
            fetch_strategy="dvc-import",
            downloaded_at=datetime(2026, 5, 15),
            local_path=str(tmp_path / "downloads" / "ds-alpha" / "v1"),
        )
        for out, pin in (("a.parquet", "a"), ("b.parquet", "b"))
    ]

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        return tmp_path / "t.tar.gz", skipped

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    cli.main(["enclave", "package", "--manifest", str(manifest)])

    err = capsys.readouterr().err
    assert "skipped 1 already-transferred product:" in err
    assert "skipped 2" not in err


def test_cli_package_json_emits_one_object(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json means one compact object on stdout and no prose anywhere."""
    import json

    from mintd.enclave import EnclaveManifest

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    archive = tmp_path / "transfer-2026-05-15-000000.tar.gz"

    def fake_package(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        return archive, []

    monkeypatch.setattr("mintd.cli.enclave_package", fake_package)

    rc = cli.main(["--json", "enclave", "package", "--manifest", str(manifest)])

    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["archive"] == str(archive)
    # The landing hint is stderr prose and must not pollute machine output.
    assert "tar -xzf" not in captured.out


def test_cli_verify_tarball_hint_says_extract(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pointing verify at the .tar.gz is the likeliest mistake. The old hint
    ("re-export from source") sends the researcher back across the air gap for
    an archive that is fine."""
    from mintd.enclave import EnclaveManifest, TransferManifestNotFound

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)

    def fake_verify(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        raise TransferManifestNotFound("_transfer_manifest.yaml not found at x.tar.gz")

    monkeypatch.setattr("mintd.cli.enclave_verify", fake_verify)

    rc = cli.main(
        ["enclave", "verify", str(tmp_path / "x.tar.gz"), "--manifest", str(manifest)]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "tar -xzf" in err
    assert "re-export from source" not in err


def test_cli_verify_wrong_enclave_says_nothing_moved(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.enclave import EnclaveManifest, WrongEnclave

    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test").save(manifest)
    extracted = tmp_path / "extracted"
    extracted.mkdir()

    def fake_verify(*args: Any, **kwargs: Any) -> tuple[Path, list[Any]]:
        raise WrongEnclave(
            "transfer was built for enclave 'enclave-hcup' but x is enclave 'enclave-cms'"
        )

    monkeypatch.setattr("mintd.cli.enclave_verify", fake_verify)

    rc = cli.main(
        ["enclave", "verify", str(extracted), "--manifest", str(manifest)]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "built for enclave" in err
    assert "nothing was moved" in err
    assert "re-export from source" not in err


def test_cli_verify_missing_enclave_manifest_no_traceback(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Running verify outside the repo root used to raise a raw
    FileNotFoundError at the user.

    The extracted dir has to be valid: verify checks the transfer manifest
    before it loads the enclave manifest, so a bare empty dir fails earlier
    for a different reason.
    """
    import yaml

    extracted = tmp_path / "extracted"
    (extracted / "ds-alpha" / "aaabbb1-2026-05-15").mkdir(parents=True)
    (extracted / "_transfer_manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "enclave_name": "test",
                "transfer_date": "2026-05-15T12:00:00+00:00",
                "transfer_id": "transfer-2026-05-15-000000",
                "contents": [
                    {
                        "repo": "ds-alpha",
                        "version_folder": "aaabbb1-2026-05-15",
                        "contract_pin": "c" * 40,
                        "artifact_pin": "a" * 32,
                    }
                ],
            }
        )
    )
    missing = tmp_path / "nope" / "enclave_manifest.yaml"

    rc = cli.main(
        ["enclave", "verify", str(extracted), "--manifest", str(missing)]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "--manifest" in err
    assert "Traceback" not in err


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
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert rc == 1
    errs = recording_reporter.events_of("error")
    hint = errs[0][2] or ""
    assert errs and "repo-b" in hint
    # `pull` takes a positional repo, not --repo — the retry hint must be runnable.
    assert "--repo" not in hint
    assert "mintd enclave pull repo-b" in hint
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
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])
    assert rc == 1
    hint = recording_reporter.events_of("error")[0][2] or ""
    assert "pin/repo" in hint
    # `pull` takes a positional repo, not --repo — the retry hint must be runnable.
    assert "--repo" not in hint
    assert "mintd enclave pull repo-b" in hint


def test_cli_enclave_bump_force_producer_error_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter, capsys: pytest.CaptureFixture[str],
) -> None:
    """`bump --force` reaches ProducerView.at_head without the check_project
    reachability gate, so an unreachable producer surfaces as ProducerError.
    The handler must render it as a clean error, not a traceback."""
    from mintd.producer import ProducerError
    monkeypatch.setattr(
        "mintd.cli.enclave_bump",
        _raises(ProducerError.unreachable("provider-xw", "a" * 40)),
    )
    rc = cli.main(
        ["enclave", "bump", "provider-xw", "--force", "--manifest", str(tmp_path / "m.yaml")]
    )
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and errs[0][2]  # has a non-empty hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_enclave_bump_force_catalog_not_found_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter, capsys: pytest.CaptureFixture[str],
) -> None:
    """`bump --force` resolves the catalog entry directly (bypassing
    check_project), so a removed/offline entry raises CatalogNotFound. The
    handler must render it cleanly, not as a traceback."""
    from mintd.catalog import CatalogNotFound
    monkeypatch.setattr(
        "mintd.cli.enclave_bump",
        _raises(CatalogNotFound("provider-xw not in registry")),
    )
    rc = cli.main(
        ["enclave", "bump", "provider-xw", "--force", "--manifest", str(tmp_path / "m.yaml")]
    )
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and errs[0][2]  # has a non-empty hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_enclave_bump_force_missing_repo_url_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients,
    recording_reporter, capsys: pytest.CaptureFixture[str],
) -> None:
    """A catalog entry with no repository.github_url raises ValueError on the
    force path; the handler must render it cleanly, not as a traceback."""
    monkeypatch.setattr(
        "mintd.cli.enclave_bump",
        _raises(ValueError("catalog entry 'provider-xw' has no repository.github_url")),
    )
    rc = cli.main(
        ["enclave", "bump", "provider-xw", "--force", "--manifest", str(tmp_path / "m.yaml")]
    )
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert errs and errs[0][2]  # has a non-empty hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_enclave_bump_malformed_manifest_points_at_manifest(
    tmp_path: Path, patched_clients, recording_reporter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A malformed enclave_manifest.yaml raises pydantic ValidationError (a
    ValueError subclass) at load; the handler must point at the local manifest,
    not misattribute it to the producer/catalog, and not traceback."""
    m_path = tmp_path / "m.yaml"
    m_path.write_text("enclave_name: test\napproved_products: notalist\n")
    rc = cli.main(["enclave", "bump", "provider-xw", "--manifest", str(m_path)])
    assert rc == 1
    hint = recording_reporter.events_of("error")[0][2] or ""
    assert "manifest" in hint.lower()
    assert "producer" not in hint.lower()
    assert "Traceback" not in capsys.readouterr().err


def test_cli_registry_sync_shows_refresh_status(
    monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    client, _ = patched_clients
    monkeypatch.setattr(client, "sync", lambda: 0)
    cli.main(["registry", "sync"])
    assert ("status", "Refreshing registry cache...") in recording_reporter.events


def test_spinner_dvc_handlers_thread_reporter_into_the_ops_factories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_reporter,
) -> None:
    """Pins fix #2: every spinner-wrapped dvc handler must pass the reporter
    into the factory that builds its DvcOps, so dvc subprocess stderr flows
    through the spinner (passthrough_stderr), not raw to the terminal.

    Two factories since the issue30 split: `data push`/`data verify` resolve
    dvc alone, `data import`/`enclave pull` need the catalog too. Both spies
    append to one list — all four handlers must still thread the reporter."""
    captured: list = []
    client = InMemoryCatalogClient()
    _register_provider_xw(client)
    fake_dvc = _FakeDvcOps()
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: client)

    def dvc_only_spy(config, reporter=None, **_):
        captured.append(reporter)
        return fake_dvc

    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", dvc_only_spy)
    monkeypatch.setattr("mintd.cli.enclave_pull", lambda *a, **k: (Path("."), []))

    cli.main(["data", "push"])
    cli.main(["data", "verify", "--path", str(tmp_path)])
    cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    cli.main(["enclave", "pull", "--manifest", str(tmp_path / "m.yaml")])

    assert len(captured) == 4
    assert all(r is recording_reporter for r in captured)


def test_no_handler_calls_sys_stderr_directly() -> None:
    """Meta-test: the set of functions in cli.py that call
    `print(..., file=sys.stderr)`, asserted by set equality. Pins the
    slice-38a print→reporter migration. Set equality, not `offenders == []`,
    so the literal can only shrink: a name that stops writing to stderr must
    be removed from it, and a seventh `file=sys.stderr` writer must be added
    deliberately.

    SCOPE HOLE CLOSED (1b). The matcher used to read `file=` keywords only, so
    `sys.stderr.write(...)` and the positional form already in-tree at
    `cli.py:198` (`self.print_usage(sys.stderr)`) were both invisible: a new
    handler using either would not have reddened this test. All three forms
    now count. The allowlist is unchanged and that is the measured result, not
    an assumption — widening finds exactly the same six names, because
    `cli.py:198`'s enclosing `error` was already allowlisted. So this closes an
    evasion without relabelling anything, which is the only honest way to close
    a ratchet hole.

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
    writers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        def _is_stderr(expr: ast.expr) -> bool:
            return (
                isinstance(expr, ast.Attribute)
                and expr.attr == "stderr"
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "sys"
            )

        uses_stderr = (
            # print(..., file=sys.stderr)
            any(kw.arg == "file" and _is_stderr(kw.value) for kw in node.keywords)
            # self.print_usage(sys.stderr)  — positional
            or any(_is_stderr(arg) for arg in node.args)
            # sys.stderr.write(...)
            or (isinstance(node.func, ast.Attribute) and _is_stderr(node.func.value))
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
        writers.add(fn_name or "<module>")

    assert writers == allowlist, (
        f"unexpected sys.stderr writers: {sorted(writers - allowlist)}; "
        f"stale allowlist entries: {sorted(allowlist - writers)}"
    )


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
        lambda *a, **k: PullSummary(targets_pulled=4, total_bytes=2048, elapsed_s=12.4),
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
        lambda *a, **k: PullSummary(targets_pulled=2, total_bytes=0, elapsed_s=1.0),
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

def test_cli_data_pull_error_count_exits_nonzero_with_summary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Slice C (pull-all audit, fix 4): version-aware targets fast-sync could
    not serve make `mintd data pull` exit non-zero, with a summary error line
    pointing at the per-target errors (each already carries its own
    targeted-retry hint). No ✓ success line."""
    from mintd.data_ops import PullSummary

    (tmp_path / ".dvc").mkdir()
    monkeypatch.setattr(
        "mintd.cli.data_pull",
        lambda *a, **k: PullSummary(
            targets_pulled=3, total_bytes=2048, elapsed_s=2.0, error_count=2,
        ),
    )
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert len(errs) == 1
    _, msg, hint = errs[0]
    assert "2 target(s) failed" in msg
    assert "3 file(s) pulled" in msg
    assert "retry command" in hint
    assert recording_reporter.events_of("success") == []


def test_cli_data_pull_error_count_zero_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Slice C: the imports-only full-fallback shape (error_count=0) keeps
    the pre-fix behavior — exit 0, ✓ success line."""
    from mintd.data_ops import PullSummary

    (tmp_path / ".dvc").mkdir()
    monkeypatch.setattr(
        "mintd.cli.data_pull",
        lambda *a, **k: PullSummary(
            targets_pulled=2, total_bytes=0, elapsed_s=1.0, error_count=0,
        ),
    )
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 0
    assert recording_reporter.events_of("error") == []
    assert "2 file(s)" in recording_reporter.events_of("success")[-1][1]


def test_cli_data_pull_error_count_json_mode_includes_errors_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Slice C, json mode: the result payload carries an `errors` count and
    the exit code is still non-zero."""
    from mintd.data_ops import PullSummary

    (tmp_path / ".dvc").mkdir()
    recording_reporter.json_mode = True
    monkeypatch.setattr(
        "mintd.cli.data_pull",
        lambda *a, **k: PullSummary(
            targets_pulled=1, total_bytes=512, elapsed_s=0.5, error_count=1,
        ),
    )
    rc = cli.main(["data", "pull", "--path", str(tmp_path)])
    assert rc == 1
    payloads = recording_reporter.events_of("result")
    assert payloads and payloads[-1][1] == {
        "pulled": 1, "bytes": 512, "elapsed_s": 0.5, "errors": 1,
    }


def test_cli_data_pull_incomplete_targets_exit_nonzero_no_success_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Pull-all audit follow-up: per-file download failures that survived the
    retries (incomplete_targets) leave the out absent from the workspace — the
    run must exit non-zero with reporter.error naming the target, NOT print
    the ✓ success line (previously: warn + '✓ pulled' + exit 0)."""
    _, dvc_ops = patched_clients
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(
        success=False,
        synced_count=0,
        fallback_targets=[],
        incomplete_targets=["data/final"],
        files_dir_failures=["data/final: b.csv: 404"],
        reason="per-file download failures (not demoted to dvc pull): data/final",
    )
    monkeypatch.setattr("mintd.cli._resolve_fast_sync_ops", lambda cfg: fast_fake)
    (tmp_path / ".dvc").mkdir()
    rc = cli.main(["data", "pull", "data/final", "--path", str(tmp_path)])
    assert rc == 1
    assert recording_reporter.events_of("success") == []
    errors = recording_reporter.events_of("error")
    assert any(
        "data/final" in msg and hint == "retry just this target: mintd data pull data/final"
        for _, msg, hint in errors
    )
    assert any("pull incomplete" in msg for _, msg, _h in errors)
    # Never handed to the plain dvc pull fallback.
    assert dvc_ops.pull_calls == []


def test_cli_data_clone_pull_errors_exit_nonzero_no_success_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Pull-all audit fix 4, clone surface: when the post-clone pull could
    not serve targets (CloneResult.pull_error_count > 0), `mintd data clone`
    must NOT print '✓ cloned ...' and must exit non-zero — previously the
    PullSummary was discarded and CI saw success with the product missing."""
    from mintd.data import CloneResult

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        return CloneResult(
            dest=Path("/tmp/sentinel"), rev="abc1234def",
            remote_bucket="my-bucket", pull_error_count=2,
        )

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    rc = cli.main(["data", "clone", "provider-xw"])
    assert rc == 1
    assert recording_reporter.events_of("success") == []
    errors = recording_reporter.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "pull incomplete" in msg
    assert "2 target(s) failed" in msg
    assert "retry command" in hint


def test_cli_data_clone_pull_errors_json_mode_includes_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Clone json mode: the result payload carries the pull `errors` count
    and the exit code is non-zero."""
    from mintd.data import CloneResult

    recording_reporter.json_mode = True

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        return CloneResult(
            dest=Path("/tmp/sentinel"), rev=None, remote_bucket=None,
            pull_error_count=1,
        )

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    rc = cli.main(["data", "clone", "provider-xw"])
    assert rc == 1
    payloads = recording_reporter.events_of("result")
    assert payloads and payloads[-1][1]["errors"] == 1


def test_cli_data_clone_zero_pull_errors_keeps_success_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter
) -> None:
    """Clone with a clean pull (pull_error_count=0) keeps the ✓ line and
    exit 0 — the failure path must not regress the happy path."""
    from mintd.data import CloneResult

    def _stub(client, dvc_ops, registry_git_ops, fast_sync_ops, **kwargs):
        return CloneResult(dest=Path("/tmp/sentinel"), rev=None, remote_bucket=None)

    monkeypatch.setattr("mintd.cli.clone_and_pull_product", _stub)
    rc = cli.main(["data", "clone", "provider-xw"])
    assert rc == 0
    assert recording_reporter.events_of("error") == []
    assert any(
        "cloned provider-xw" in msg
        for _, msg in recording_reporter.events_of("success")
    )


# --- registry branch collisions never reach the user as a traceback --------


def _publishable_project(tmp_path: Path, client) -> None:
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


def test_cli_publish_registry_branch_exists_exits_one_with_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
    patched_clients,
    patched_git_ops,
) -> None:
    """The field crash: a push rejected because the branch already carries an
    open PR must surface as one error + hint naming what already landed."""
    from mintd._registry_git_ops import RegistryBranchExists

    client, _ = patched_clients
    _publishable_project(tmp_path, client)

    def _raise(*a: Any, **kw: Any) -> None:
        raise RegistryBranchExists(
            ["git", "push"], "! [rejected] update/provider-xw (fetch first)",
            "update/provider-xw",
        )

    monkeypatch.setattr(client, "update", _raise)

    rc = cli.main(["publish", "--path", str(tmp_path), "--yes"])

    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert len(errs) == 1
    _, msg, hint = errs[0]
    assert hint is not None
    assert "update/provider-xw" in hint
    assert "tag v" in hint
    # The retry advice must be runnable: the failing run already made the tag.
    assert "git tag -d v" in hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_registry_update_branch_exists_names_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
    patched_clients,
) -> None:
    """`mintd registry update` is guarded the same way as publish."""
    from mintd._registry_git_ops import RegistryBranchExists

    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients

    def _raise(*a: Any, **kw: Any) -> None:
        raise RegistryBranchExists(
            ["git", "push"], "! [rejected] (fetch first)", "update/test_project",
        )

    monkeypatch.setattr(client, "update", _raise)

    rc = cli.main(["registry", "update", str(tmp_path)])

    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert len(errs) == 1
    _, msg, hint = errs[0]
    assert "update/test_project" in msg
    assert hint is not None and "gh pr list" in hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_publish_success_names_reused_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
    patched_clients,
    patched_git_ops,
) -> None:
    """The exit-0 half: publishing onto an already-open PR says so, and links
    the PR that was updated rather than implying a new one was opened."""
    from mintd.catalog import FieldChange, UpdateResult

    client, _ = patched_clients
    _publishable_project(tmp_path, client)

    monkeypatch.setattr(
        client, "update",
        lambda *a, **kw: UpdateResult(
            name="provider-xw",
            changes=[FieldChange(field_path="mint.version", before="0.1.0", after="0.1.1")],
            dry_run=False,
            pr_number=42,
            pr_url="https://github.com/example-org/registry/pull/42",
            pr_reused=True,
        ),
    )

    rc = cli.main(["publish", "--path", str(tmp_path), "--yes"])

    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "updated PR" in msg
    assert "/pull/42" in msg


def test_cli_registry_update_gh_unavailable_exits_one_with_hint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
    patched_clients,
) -> None:
    """`update` now asks `gh pr list` before branching, so a missing or
    unauthenticated gh surfaces one call earlier than it used to — and must
    still be an error + hint, not a traceback."""
    from mintd._registry_git_ops import GhAuthError

    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients

    def _raise(*a: Any, **kw: Any) -> None:
        raise GhAuthError("gh: not authenticated")

    monkeypatch.setattr(client, "update", _raise)

    rc = cli.main(["registry", "update", str(tmp_path)])

    assert rc == 1
    errs = recording_reporter.events_of("error")
    assert len(errs) == 1
    _, msg, hint = errs[0]
    assert hint is not None and "gh auth status" in hint
    assert "Traceback" not in capsys.readouterr().err


def test_cli_registry_update_success_names_reused_pr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
    patched_clients,
) -> None:
    """AC2's other half: `mintd registry update` must also say the PR was
    updated rather than implying it opened a fresh one."""
    from mintd.catalog import FieldChange, UpdateResult

    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    client, _ = patched_clients

    monkeypatch.setattr(
        client, "update",
        lambda *a, **kw: UpdateResult(
            name="test_project",
            changes=[FieldChange(field_path="metadata.description", before="", after="x")],
            dry_run=False,
            pr_number=42,
            pr_url="https://github.com/example-org/registry/pull/42",
            pr_reused=True,
        ),
    )

    rc = cli.main(["registry", "update", str(tmp_path)])

    assert rc == 0
    msg = recording_reporter.events_of("success")[-1][1]
    assert "updated PR" in msg
    assert "/pull/42" in msg


# ---------------------------------------------------------------------------
# P2a (issue13) — the three check_project gates must be handed a catalog client
#
# A project with one approved product in enclave_manifest.yaml is blocked from
# check, publish AND registry register by a finding about mintd's own wiring.
# These tests deliberately do NOT patch `mintd.cli.check_project` — the three
# existing register tests (:663/:688/:706) do, which is why the suite misses it.
# ---------------------------------------------------------------------------


def test_registry_register_with_enclave_manifest_reaches_client_register(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    stage_enclave_manifest(tmp_path)
    client, _ = patched_clients
    _register_provider_xw(client)
    registered: list[str] = []
    real_register = client.register

    def spy(metadata: Metadata, **kwargs: Any):
        registered.append(metadata.project.name)
        return real_register(metadata, **kwargs)

    monkeypatch.setattr(client, "register", spy)

    rc = cli.main(["registry", "register", str(tmp_path)])

    assert rc == 0
    assert registered == ["test_project"]


def test_plain_check_resolves_a_catalog_client(
    tmp_path: Path,
    patched_clients,
) -> None:
    """Pins fix 3 (the hoist): plain `check` resolves a client best-effort, so
    an enclave consumer has a default surface that reports it healthy. Stays
    red after the publish/register fixes alone."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    stage_enclave_manifest(tmp_path)
    client, _ = patched_clients
    _register_provider_xw(client)

    rc = cli.main(["check", str(tmp_path)])

    assert rc == 0


def test_plain_check_without_registry_url_still_reports_catalog_unresolved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard on fix 3: best-effort resolution must not swallow the
    diagnostic on a machine with no registry_url."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    stage_enclave_manifest(tmp_path)
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))

    def _no_registry_url(*a: Any, **kw: Any):
        raise ConfigError("registry_url required for this command; set it in ...")

    monkeypatch.setattr("mintd.cli._resolve_catalog_client", _no_registry_url)

    rc = cli.main(["check", str(tmp_path)])

    out = capsys.readouterr().out
    assert rc == 1
    assert "catalog client not provided" in out


def test_check_upgrades_unreachable_registry_prints_an_error_not_a_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """A registry that cannot be cloned is a documented failure path."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    stage_enclave_manifest(tmp_path)
    client, _ = patched_clients

    def _unreachable(name: str):
        raise GitOpError(
            ["git", "clone", "--depth=1", "/nonexistent/registry.git"],
            "fatal: repository '/nonexistent/registry.git' does not exist",
        )

    monkeypatch.setattr(client, "fetch", _unreachable)

    rc = cli.main(["check", str(tmp_path), "--upgrades"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert rc == 1
    assert "cannot read the catalog" in output
    assert "does not exist" in output  # git's own words reach the user
    assert "Traceback" not in output


def test_every_check_project_call_site_passes_a_client() -> None:
    """Meta-test: every `check_project(...)` call in src/mintd passes a
    `client=` keyword. This is the anti-drift ratchet that keeps "one gate"
    enforceable without making the parameter keyword-required (unit A owns
    that migration).

    Allowlist rationale — `data.py`'s import-bump call is inert for this
    defect: `_find_consumer_finding_for_target` matches on
    `source == <.dvc file>`, and enclave-manifest findings carry
    `source=<manifest>`, so they never select a bump target. Asserted by set
    equality, so the allowlist can only shrink.
    """
    import ast

    src_dir = Path(__file__).resolve().parents[1] / "src" / "mintd"
    offenders: set[str] = set()
    for path in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "check_project":
                continue
            if not any(kw.arg == "client" for kw in node.keywords):
                offenders.add(path.name)

    assert offenders == {"data.py"}, (
        f"check_project call sites missing client=: {sorted(offenders)}"
    )


# ---------------------------------------------------------------------------
# P2b (issue30) — local commands must not demand a registry_url they never use
# ---------------------------------------------------------------------------


def _local_data_argv(verb: str, tmp_path: Path) -> list[str]:
    target = tmp_path / "raw.csv"
    target.write_text("data")
    return {
        "add": ["data", "add", str(target)],
        "pull": ["data", "pull", "--path", str(tmp_path)],
        "push": ["data", "push"],
        "verify": ["data", "verify"],
        "remove": ["data", "remove", "raw.csv"],
    }[verb]


@pytest.fixture
def unconfigured_machine(monkeypatch: pytest.MonkeyPatch) -> _FakeDvcOps:
    """A laptop with no registry_url: the real `_resolve_catalog_client` raises
    ConfigError off the default Config. `SubprocessDvcOps` is replaced at the
    constructor so no dvc subprocess runs, whichever factory builds it."""
    dvc_ops = _FakeDvcOps()
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr("mintd.cli.SubprocessDvcOps", lambda **kwargs: dvc_ops)
    monkeypatch.setattr("mintd.cli._resolve_fast_sync_ops", lambda cfg: None)
    return dvc_ops


@pytest.mark.parametrize("verb", ["add", "pull", "push", "verify", "remove"])
def test_local_data_commands_do_not_require_registry_url(
    verb: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unconfigured_machine: _FakeDvcOps,
) -> None:
    """All five wrap dvc and never contact the catalog."""
    (tmp_path / ".dvc").mkdir()
    dvc_ops = unconfigured_machine

    rc = cli.main(_local_data_argv(verb, tmp_path))

    err = capsys.readouterr().err
    assert "registry_url" not in err
    assert rc == 0
    assert any([
        dvc_ops.add_calls, dvc_ops.pull_calls, dvc_ops.push_calls,
        dvc_ops.status_calls, dvc_ops.remove_calls,
    ]), f"`data {verb}` never reached its DVC work"


def test_local_data_handlers_never_build_a_catalog_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unconfigured_machine: _FakeDvcOps,
) -> None:
    """The structural twin: not merely tolerating a missing registry_url, but
    never asking for the collaborator in the first place."""
    (tmp_path / ".dvc").mkdir()

    def must_not_call(*a: Any, **kw: Any) -> None:
        pytest.fail("local data handlers must not build a catalog client")

    monkeypatch.setattr("mintd.cli._resolve_catalog_client", must_not_call)

    for verb in ("add", "pull", "push", "verify", "remove"):
        assert cli.main(_local_data_argv(verb, tmp_path)) == 0


def test_publish_builds_dvc_ops_with_a_reporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_reporter,
) -> None:
    """`dvc push` is the longest operation mintd performs; without a reporter
    it streams no progress. Asserted at the SubprocessDvcOps constructor —
    `patched_clients`' `**_` lambda would swallow the kwarg — and by identity,
    since a throwaway `Reporter()` would satisfy a not-None check while losing
    everything `_build_reporter` reads off argv (verbose/quiet/json/no-color)."""
    captured: dict[str, Any] = {}

    def recorder(**kwargs: Any) -> _FakeDvcOps:
        captured.update(kwargs)
        return _FakeDvcOps()

    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cls()))
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg, **_: InMemoryCatalogClient())
    monkeypatch.setattr("mintd.cli.SubprocessDvcOps", recorder)

    cli.main(["publish", "--path", str(tmp_path), "--dry-run"])

    assert captured, "publish never built a SubprocessDvcOps"
    assert captured["reporter"] is recording_reporter


def test_enclave_pull_allows_the_relative_default_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    """The guard must not break the ordinary invocation.

    `--manifest` defaults to a RELATIVE `enclave_manifest.yaml`, whose parent
    is `.` — which always resolves to the process cwd. Standing in the enclave
    and typing `mintd enclave pull` has to keep working, or the guard has
    broken the only path anyone actually uses.
    """
    called = []
    # Object-form on purpose: the pinned substrate census counts string-form
    # `monkeypatch.setattr("mintd.…")` sites, and a new test should not move a
    # ratchet it has nothing to do with.
    monkeypatch.setattr(
        cli, "enclave_pull", lambda *a, **k: called.append(1) or (Path("."), []),
    )
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["enclave", "pull"])

    assert rc == 0
    assert called == [1]


def test_enclave_pull_refusal_hint_survives_spaces_and_carries_every_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    """The hint is runnable AS PRINTED — including a path with a space, and
    including `--force`.

    Two review rounds found two separate defects in the f-string version of
    this hint: it dropped `--force` (so the pasted retry SKIPS already-
    downloaded products instead of re-fetching them — a different operation
    from the one the user asked for), and it did not quote, so an enclave
    under `~/…/My Drive/…` produced `cd /x/My Drive/enclave` — which `cd`s to
    `/x/My` and fails.

    Both are the same root cause: a concatenated string makes every flag
    something you can forget and every path something you can forget to quote.
    The hint is built as argv and joined with `shlex` now, so this test asserts
    against `shlex.split` round-tripping rather than against a literal.
    """
    import shlex

    called = []
    monkeypatch.setattr(
        cli, "enclave_pull", lambda *a, **k: called.append(1) or (Path("."), []),
    )
    enclave = tmp_path / "My Drive" / "enclave_x"
    enclave.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    rc = cli.main([
        "enclave", "pull", "provider-xw",
        "--manifest", str(enclave / "audit.yaml"), "--force",
    ])

    assert rc == 1
    assert called == [], "the pull ran anyway; the guard is decorative"
    errors = [e for e in recording_reporter.events if e[0] == "error"]
    assert any("must run from inside the enclave" in e[1] for e in errors), errors
    hint = next(e[2] for e in errors if e[2])
    cd_part, _, retry_part = hint.partition(" && ")
    # the `cd` target survives shlex round-trip as ONE argument, spaces intact
    assert shlex.split(cd_part) == ["cd", str(enclave)]
    # and every flag the user gave is carried into the retry
    assert shlex.split(retry_part) == [
        "mintd", "enclave", "pull", "provider-xw",
        "--manifest", "audit.yaml", "--force",
    ]


def test_enclave_pull_refusal_names_one_filesystem_root_not_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    """The message compares resolved paths, so it must PRINT resolved paths.

    `Path.cwd()` is physical by definition — POSIX `getcwd` resolves symlinks —
    so an unresolved `enclave_dir` printed beside it showed two roots that look
    unrelated: "must run from inside the enclave (/tmp/x/enclave), not from
    /private/tmp/x" on any macOS, where `/tmp` is itself a symlink. The user
    cannot tell those are the same place.
    """
    monkeypatch.setattr(cli, "enclave_pull", lambda *a, **k: (Path("."), []))
    real = tmp_path / "real"
    (real / "enclave").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    cli.main(["enclave", "pull", "--manifest", str(link / "enclave" / "m.yaml")])

    msg = next(e[1] for e in recording_reporter.events if e[0] == "error")
    assert str(real / "enclave") in msg, msg
    assert str(link) not in msg, f"unresolved path leaked into the message: {msg}"


def test_enclave_pull_missing_manifest_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_clients, recording_reporter,
) -> None:
    """A missing manifest reports; it does not raise.

    `EnclaveManifest.load` raises `FileNotFoundError`, which was in none of the
    handler's except arms, so it reached the user as a raw traceback. The cwd
    guard added in this unit funnels every user into exactly this path — stand
    in the enclave, run it — so leaving the leak would have traded one bad
    message for a stack trace. `_handle_enclave_list` and
    `_handle_enclave_verify` already fixed the identical leak; this mirrors them.
    """
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["enclave", "pull"])

    assert rc == 1
    errors = [e for e in recording_reporter.events if e[0] == "error"]
    assert any("not found" in e[1] for e in errors), errors
    assert any(e[2] and "--manifest" in e[2] for e in errors), errors


# ---------------------------------------------------------------------------
# P5 — one producer, many subscriptions (issue33)
# ---------------------------------------------------------------------------


def test_enclave_add_second_path_warns_pin_was_inherited(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """D1's consequence must reach the user AT ADD TIME. If the recorded pin
    predates the newly added path the add still succeeds and the PULL is what
    fails, so the add names the remedy.

    Asserts SHORT tokens only: Reporter writes through a rich Console bound to
    stderr, which soft-wraps at 80 columns off a tty, so a long phrase can be
    split across lines.
    """
    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"

    cli.main([
        "enclave", "add", "provider-xw", "--pin", "a" * 40,
        "--source-path", "data/final/a", "--manifest", str(manifest),
    ])
    capsys.readouterr()  # discard first-add output

    rc = cli.main([
        "enclave", "add", "provider-xw",
        "--source-path", "data/final/b", "--manifest", str(manifest),
    ])

    err = capsys.readouterr().err
    assert rc == 0
    assert "inherited" in err
    assert "--force" in err
    assert "provider-xw" in err


def test_enclave_add_hints_only_name_flags_the_parser_accepts() -> None:
    """The MissingPrimaryDataProduct hint used to say `--path`, a flag this
    parser does not define — following it exited 64 with `unrecognized
    arguments`. Ratchet over the whole handler rather than that one string, so
    any future hint naming a nonexistent flag fails here too.

    Static: no doubles, and no producer resolve (which is what raises the
    exception in the first place, and would need a real repo to reach).
    """
    import inspect
    import re as _re

    parser = cli._build_parser()
    enclave = parser._subparsers._group_actions[0].choices["enclave"]
    add = enclave._subparsers._group_actions[0].choices["add"]
    accepted = {opt for action in add._actions for opt in action.option_strings}

    source = inspect.getsource(cli._handle_enclave_add)
    hints = _re.findall(r'hint=(["\'])(.*?)\1', source, _re.S)
    named = {f for _, text in hints for f in _re.findall(r"--[a-z][a-z0-9-]*", text)}

    assert named, "no hints found; the regex or the handler moved"
    assert "--source-path" in named
    assert named <= accepted, f"hints name flags the parser rejects: {named - accepted}"


def test_enclave_list_distinguishes_all_from_primary(
    patched_clients, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An `--all` row and a bare primary both have source_path None, so the
    list rendered them identically — on the very screen the AlreadyApproved
    hint and D2's refusal both send users to."""
    manifest = tmp_path / "enclave_manifest.yaml"
    manifest.write_text("""
enclave_name: test-enclave
approved_products:
  - repo: provider-xw
    registry_entry: e
    pin: 4f7c2a1
    source_path: data/final/a
  - repo: provider-xw
    registry_entry: e
    pin: 4f7c2a1
    all: true
  - repo: provider-xw
    registry_entry: e
    pin: 4f7c2a1
downloaded: []
transferred: []
""")

    cli.main(["enclave", "list", "--manifest", str(manifest)])

    out, _ = capsys.readouterr()
    assert "data/final/a" in out
    assert "<all>" in out
    assert out.count("<primary>") == 1


def test_enclave_remove_ambiguous_exits_one_without_traceback(
    patched_clients, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """D2: refuse rather than wipe subscriptions the user did not name."""
    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"
    cli.main(["enclave", "add", "provider-xw", "--pin", "a" * 40,
              "--source-path", "data/final/a", "--manifest", str(manifest)])
    cli.main(["enclave", "add", "provider-xw",
              "--source-path", "data/final/b", "--manifest", str(manifest)])
    capsys.readouterr()
    before = manifest.read_bytes()

    rc = cli.main(["enclave", "remove", "provider-xw", "--manifest", str(manifest)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "data/final/a" in err
    assert "data/final/b" in err
    assert manifest.read_bytes() == before


def test_enclave_remove_primary_and_source_path_exits_64(
    patched_clients, tmp_path: Path
) -> None:
    """argparse mutex: the three remove selectors conflict → exit 64."""
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "enclave", "remove", "provider-xw",
            "--source-path", "data/final/a", "--primary",
            "--manifest", str(tmp_path / "enclave_manifest.yaml"),
        ])
    assert exc.value.code == 64


def test_enclave_add_does_not_claim_inheritance_when_it_resolved_its_own_pin(
    patched_clients,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The advisory must key on "this add INHERITED a pin", not on "the repo
    now has more than one row". They diverge when the sibling's pin is not
    inheritable -- here a hand-edited empty pin, which `enclave_add`
    deliberately declines to inherit -- and the row-count version then names a
    pin that was never recorded.

    Needs no producer resolve: with the sibling's pin blank, an explicit --pin
    is taken verbatim rather than refused by D1.
    """
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    client, _ = patched_clients
    _register_provider_xw(client)
    manifest = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="provider-xw", registry_entry="x", pin="",
                            source_path="data/final/a"),
        ],
    ).save(manifest)

    rc = cli.main([
        "enclave", "add", "provider-xw", "--pin", "b" * 40,
        "--source-path", "data/final/b", "--manifest", str(manifest),
    ])

    err = capsys.readouterr().err
    assert rc == 0
    assert len(EnclaveManifest.load(manifest).approved_products) == 2
    assert "inherited" not in err, "nothing was inherited; the sibling pin is blank"


def test_data_import_escaping_path_is_reported_not_raised(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """The containment guard's raise site must render like every other
    documented failure. `data clone` already catches `UnknownProductPath`;
    the import handler did not, so a leading slash in `--path` — an ordinary
    typo — exited `main()` as a Python traceback.

    Mutation: drop the `except UnknownProductPath` arm -> this reddens.
    """
    client, dvc_ops = patched_clients
    _register_provider_xw(client)

    rc = cli.main(
        ["data", "import", "provider-xw", "--path", "/etc", "--dest-root", str(tmp_path)]
    )

    assert rc == 1
    assert dvc_ops.calls == []
    err = " ".join(capsys.readouterr().err.split())
    assert "outside the import root" in err
    # The hint is the only thing distinguishing this clause from the broad
    # `except (... ValueError)` below it, which also renders the message.
    assert "relative to the producer" in err


def test_data_import_escaping_full_name_is_contained(
    tmp_path: Path,
    patched_clients,
) -> None:
    """The namespace comes from the SAME untrusted entry as the output path,
    so a guard anchored on `dest_root / full_name` would measure an escaped
    path against an equally escaped base and pass. Anchor is `dest_root`.

    `_import_namespace`'s one-component rule now refuses `..` before the
    containment check ever runs, so this test reddens on the namespace rule;
    the containment anchor is owned by the output-path tests (and is now
    `nested_root`, which the namespace rule is what makes safe).
    """
    client, dvc_ops = patched_clients
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "../../../../elsewhere"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    data["data_products"]["primary"] = "outputs/main.parquet"
    client.register(Metadata.model_validate(data))

    rc = cli.main(
        ["data", "import", "provider-xw", "--dest-root", str(tmp_path / "imports")]
    )

    assert rc == 1
    assert dvc_ops.calls == []


def test_data_import_bump_renders_an_unreachable_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """`--bump` reads the catalog now (product name -> namespace) where it
    used to `del client`, so an unreachable registry became a NEW failure
    mode on this path — and `GitCatalogClient.fetch` raises `GitOpError`,
    which the bump handler did not catch. Offline / VPN-gated registries are
    ordinary for this fleet, so it must render, not traceback.

    Mutation: drop the `except GitOpError` arm -> this reddens.
    """
    client, _ = patched_clients
    _register_provider_xw(client)

    def _unreachable(*a: object, **k: object):
        raise GitOpError(["git", "fetch"], "Could not resolve host: github.com")

    monkeypatch.setattr(client, "fetch", _unreachable)

    rc = cli.main(["data", "import", "provider-xw", "--bump"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Could not resolve host" in err


def test_data_import_bump_renders_a_bad_namespace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    patched_clients,
) -> None:
    """The namespace guard's raise site on the `--bump` arm. That arm's
    except chain did not catch `UnknownProductPath` (a `ValueError`) and
    neither does `main()`, so without the handler `mintd data import ..
    --bump` exits through a raw traceback.

    Whitespace-normalised because the reporter wraps long messages at the
    terminal width, which can split the phrase across lines.

    Mutation: drop `UnknownProductPath` from the bump arm's except tuple ->
    this reddens.
    """
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["data", "import", "..", "--bump"])

    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "single folder name" in err


_AMBIGUOUS_DVC = """\
deps:
- path: outputs/main.parquet
  repo:
    url: https://github.com/example-org/provider-xw
    rev_lock: {sha}
outs:
- path: {out}
  md5: d41d8cd98f00b204e9800998ecf8427e.dir
"""


def test_data_import_duplicate_pointer_reports_instead_of_tracebacking(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """Two `.dvc` under one namespace recording the same producer path make
    `_imports_index` raise `AmbiguousImport` on the PLAIN import arm — the
    handler caught it only under `--bump`, so a consumer holding a legacy
    plus a mirrored pointer got a raw traceback out of `main()`.

    Mutation: drop `AmbiguousImport` from `_handle_data_import`'s non-bump
    `except` chain -> this reddens.
    """
    client, _dvc_ops = patched_clients
    _register_provider_xw(client)
    ns = tmp_path / "data_provider-xw"
    ns.mkdir()
    (ns / "main.parquet.dvc").write_text(_AMBIGUOUS_DVC.format(sha="a" * 40, out="main.parquet"))
    (ns / "legacy.dvc").write_text(_AMBIGUOUS_DVC.format(sha="b" * 40, out="legacy"))

    rc = cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])

    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "record the same producer path" in err
    assert "remove one" in err


def _reachable_raises(root: str) -> set[str]:
    """Exception NAMES raised by `src/mintd/data.py` functions reachable from
    `root`, following calls to other module-level functions in that file.

    Derived, not listed: a raise site added to a helper `import_product`
    already calls is picked up with no literal to remember. Three raise sites
    in this unit (`UnknownProductPath`, `GitOpError`, `AmbiguousImport`)
    shipped with no handler on this arm.

    SCOPE HOLES, stated rather than closed: it sees `raise` statements only
    (never an implicit `AttributeError` — `_section` is what covers those),
    and only module-level `def`s in data.py (a helper moved to another module
    disappears from the walk). `_EXPECTED_HINTS` below is the tripwire for
    both: the set is pinned, so a name appearing OR vanishing fails loudly.
    """
    tree = ast.parse((Path(cli.__file__).parent / "data.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    raised: set[str] = set()
    seen: set[str] = set()
    todo = [root]
    while todo:
        name = todo.pop()
        if name in seen or name not in fns:
            continue
        seen.add(name)
        for node in ast.walk(fns[name]):
            if isinstance(node, ast.Raise) and node.exc is not None:
                func = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if isinstance(func, ast.Attribute):  # ProducerError.unreachable(...)
                    func = func.value
                if isinstance(func, ast.Name):
                    raised.add(func.id)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                todo.append(node.func.id)
    return raised


#: Every exception the non-bump arm must render, and the hint fragment that
#: proves it was rendered by ITS OWN clause rather than swallowed by a
#: broader one. Without the hints, collapsing the whole chain into a single
#: `except Exception` passes every case here.
_EXPECTED_HINTS: dict[str, str | None] = {
    "AmbiguousImport": None,
    # Raised behind `client.fetch` (`CatalogCache.ensure_fresh` -> git
    # clone/fetch/reset), so it is inside the handler's `try` without being
    # visible to a scan of data.py.
    "GitOpError": "check git auth",
    "ImportDestinationExists": None,
    "MissingPrimaryDataProduct": None,
    "UnknownProductPath": "relative to the producer",
    "ValueError": None,
}

#: Exception types whose constructor is not a bare message.
_EXC_ARGS: dict[str, tuple[Any, ...]] = {"GitOpError": (["git", "fetch"], "boom")}


def test_import_raise_sites_all_have_a_handler_case() -> None:
    """The tripwire for `_reachable_raises`' two scope holes.

    A NEW name means a raise site nothing renders — add the clause, then the
    entry. A VANISHED name means the walk went blind (a helper moved out of
    data.py), not that the raise site is gone.
    """
    assert _reachable_raises("import_product") | {"GitOpError"} == set(_EXPECTED_HINTS)


@pytest.mark.parametrize("exc_name", sorted(_EXPECTED_HINTS))
def test_data_import_renders_every_reachable_exception(
    exc_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    patched_clients,
) -> None:
    """Every exception type reachable inside `_handle_data_import`'s non-bump
    `try` renders through the reporter with rc 1 — with its own hint — never
    a raw traceback out of `main()` (which catches only KeyboardInterrupt /
    WallTimeoutExceeded / ConfigError).

    The type is raised from the `dvc_ops.import_` boundary — the last call in
    the `try` — because what is under test is the handler's `except` chain,
    not each raise site's own trigger.

    Mutations: drop any name from that chain, fold a hinted clause into the
    bare-message tuple, or collapse the chain into `except Exception` -> the
    matching case reddens.
    """
    client, dvc_ops = patched_clients
    _register_provider_xw(client)
    exc_type = getattr(cli, exc_name, None) or getattr(builtins, exc_name)

    def _raise(**_: Any) -> Path:
        raise exc_type(*_EXC_ARGS.get(exc_name, ("boom",)))

    monkeypatch.setattr(dvc_ops, "import_", _raise)

    rc = cli.main(["data", "import", "provider-xw", "--dest-root", str(tmp_path)])

    assert rc == 1
    err = " ".join(capsys.readouterr().err.split())
    assert "boom" in err
    hint = _EXPECTED_HINTS[exc_name]
    if hint is not None:
        assert hint in err, "rendered without its own clause's hint"


def test_a_corrupt_registry_entry_is_an_error_not_a_traceback(
    patched_clients,
    recording_reporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--bump` reads the catalog (product name -> namespace), which it did not
    before. Neither `pydantic.ValidationError` nor `yaml.YAMLError` was caught
    on that arm or in `main()`, so one merge-conflict marker in someone else's
    registry file exited through a stack trace. `main()` catches it now, so
    every catalog-reading verb is covered, not just this one.

    Mutation: remove the `CatalogEntryInvalid` arm from `main()` -> raises.
    """
    from mintd.catalog import CatalogEntryInvalid

    client, _ = patched_clients
    monkeypatch.setattr(client, "fetch", lambda name: (_ for _ in ()).throw(
        CatalogEntryInvalid("catalog entry is unreadable (/r/provider-xw.yaml): boom")
    ))

    rc = cli.main(["data", "import", "provider-xw", "--bump"])

    assert rc == 1
    msg = recording_reporter.events_of("error")[-1][1]
    assert "unreadable" in msg
    assert "provider-xw.yaml" in msg
