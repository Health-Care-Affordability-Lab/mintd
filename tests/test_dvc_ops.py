"""Tests for `_FakeDvcOps` — protocol conformance + stub round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._dvc_invoke import dvc_cmd
from mintd._dvc_ops import DvcOpError, DvcOps, SubprocessDvcOps
from mintd.imports import DataDependency
from tests._fakes.dvc_ops import _FakeDvcOps


def test_fake_satisfies_protocol() -> None:
    fake: DvcOps = _FakeDvcOps()
    assert callable(fake.import_)


def test_fake_records_call(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "core_rows"

    fake.import_(
        cwd=tmp_path,
        repo_url="https://github.com/example-org/demo-map",
        path="outputs/core_rows/",
        dest=dest,
        rev="abc123",
        force=True,
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.repo_url == "https://github.com/example-org/demo-map"
    assert call.path == "outputs/core_rows/"
    assert call.dest == dest
    assert call.rev == "abc123"
    assert call.force is True


def test_fake_writes_parseable_stub(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "core_rows"

    produced = fake.import_(
        cwd=tmp_path,
        repo_url="https://github.com/example-org/demo-map",
        path="outputs/core_rows/",
        dest=dest,
    )

    assert produced == tmp_path / "core_rows.dvc"
    assert produced.exists()

    dep = DataDependency.from_dvc_file(produced)
    assert dep.producer_repo == "https://github.com/example-org/demo-map"
    assert dep.output_path == "outputs/core_rows/"
    assert dep.local_path == "core_rows"


def test_fake_handles_file_paths_with_suffix(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "main.parquet"

    produced = fake.import_(
        cwd=tmp_path,
        repo_url="https://github.com/example-org/p",
        path="outputs/main.parquet",
        dest=dest,
    )

    # Real `dvc import` writes <dest>.dvc, not <stem>.dvc.
    assert produced == tmp_path / "main.parquet.dvc"


# ---------------------------------------------------------------------------
# Slice 34 — `extra_args` pass-through on SubprocessDvcOps.pull / .import_
# ---------------------------------------------------------------------------


def _stub_run_streaming(captured: list[list[str]]):
    """Return a fake `run_streaming` that records argv and returns success."""
    class _R:
        returncode = 0
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

    def _fake(cmd, **kwargs):
        captured.append(list(cmd))
        return _R()

    return _fake


def test_subprocess_pull_appends_extra_args_after_typed_flags(
    monkeypatch, tmp_path: Path
) -> None:
    """`extra_args` items land between the typed `--remote`/`--jobs`
    block and the positional targets — readable argv shape and matches
    DVC's flag-anywhere acceptance."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    captured: list[list[str]] = []
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_run_streaming(captured))

    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.pull(
        cwd=tmp_path,
        targets=["data/foo"],
        remote="X",
        jobs=4,
        extra_args=["--verbose"],
    )

    assert captured == [
        [*dvc_cmd(), "pull", "--remote", "X", "--jobs", "4", "--verbose", "data/foo"],
    ]


def test_subprocess_pull_extra_args_none_keeps_legacy_argv(
    monkeypatch, tmp_path: Path
) -> None:
    """Backward compat: with `extra_args=None` (the default), argv is
    byte-for-byte the pre-slice-34 shape."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    captured: list[list[str]] = []
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_run_streaming(captured))

    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.pull(cwd=tmp_path, targets=["data/foo"], remote="X", jobs=4)

    assert captured == [
        [*dvc_cmd(), "pull", "--remote", "X", "--jobs", "4", "data/foo"],
    ]


def test_subprocess_import_appends_extra_args_after_typed_flags(
    monkeypatch, tmp_path: Path,
) -> None:
    """`dvc import` argv ends with the extra_args block, after the
    `--rev`/`--force` typed flags."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    captured: list[list[str]] = []
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_run_streaming(captured))

    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    dest = tmp_path / "out"
    ops.import_(
        cwd=tmp_path,
        repo_url="https://example/x",
        path="data/y",
        dest=dest,
        rev="abc",
        force=True,
        extra_args=["--verbose"],
    )

    assert captured == [
        [
            *dvc_cmd(), "import", "https://example/x", "data/y",
            "-o", str(dest), "--rev", "abc", "--force", "--verbose",
        ],
    ]


def test_pull_raises_dvc_not_installed_when_module_missing(monkeypatch, tmp_path: Path) -> None:
    """`sys.executable -m dvc` exits 1 + ModuleNotFoundError when dvc isn't
    in mintd's env. Surface as DvcNotInstalled (with the reinstall hint),
    not as a generic DvcPullError that buries the cause in stderr."""
    import pytest

    from mintd import _dvc_ops
    from mintd._config import Timeouts

    class _R:
        returncode = 1
        stdout_lines: list[str] = []
        stderr_lines = ["ModuleNotFoundError: No module named 'dvc'\n"]

    monkeypatch.setattr(_dvc_ops, "run_streaming", lambda *a, **k: _R())

    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    with pytest.raises(_dvc_ops.DvcNotInstalled, match="reinstall mintd"):
        ops.pull(cwd=tmp_path, targets=["data/foo"])


# Slice 47 — lazy `dvc init` op + typed not-in-repo error.


def test_subprocess_init_runs_dvc_init_in_cwd(monkeypatch, tmp_path) -> None:
    """`init(cwd=...)` shells out to `dvc init` in the given dir."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}

    class _R:
        returncode = 0
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

    def _fake(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["cwd"] = kwargs.get("cwd")
        return _R()

    monkeypatch.setattr(_dvc_ops, "run_streaming", _fake)
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.init(cwd=tmp_path)

    assert seen["cmd"] == [*dvc_cmd(), "init"]
    assert seen["cwd"] == tmp_path


def test_subprocess_init_tolerates_already_initialized(monkeypatch, tmp_path: Path) -> None:
    """Re-running `init` on a DVC repo must not raise — repeated pulls stay
    idempotent. `dvc init` exits non-zero with "'.dvc' exists" in that case."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    class _R:
        returncode = 1
        stdout_lines: list[str] = []
        stderr_lines = ["ERROR: failed to initiate DVC - '.dvc' exists. Use `-f` to force.\n"]

    monkeypatch.setattr(_dvc_ops, "run_streaming", lambda *a, **k: _R())
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.init(cwd=tmp_path)  # must not raise


def test_subprocess_import_raises_not_in_repo(monkeypatch, tmp_path) -> None:
    """`dvc import` outside a DVC repo surfaces as the typed DvcNotInRepoError,
    not a generic DvcOpError — so the CLI can give a `dvc init` hint instead of
    the misleading pin/repo one."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    class _R:
        returncode = 253
        stdout_lines: list[str] = []
        stderr_lines = [
            "ERROR: you are not inside of a DVC repository "
            "(checked up to mount point '/')\n"
        ]

    monkeypatch.setattr(_dvc_ops, "run_streaming", lambda *a, **k: _R())
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    with pytest.raises(_dvc_ops.DvcNotInRepoError):
        ops.import_(cwd=tmp_path, repo_url="http://x", path="out", dest=tmp_path / "d")


# Slice 48 — push scrapes its count from captured stdout under json_mode.
# (json_mode suppresses terminal *forwarding* only; capture into stdout_lines
# is unaffected — same invariant `status()` relies on.)


def _stub_push_run_streaming(stdout_lines: list[str], seen: dict):
    def _fake(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs

        class _R:
            returncode = 0
            stderr_lines: list[str] = []

        _R.stdout_lines = list(stdout_lines)
        return _R()

    return _fake


def test_subprocess_push_parses_count_from_captured_stdout(monkeypatch, tmp_path: Path) -> None:
    """`push` returns the scraped count even though `json_mode=True` is set —
    proving json_mode doesn't empty `r.stdout_lines`. Also: no `--json` in argv
    (dvc push rejects it)."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops, "run_streaming", _stub_push_run_streaming(["3 files pushed"], seen)
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    result = ops.push(cwd=tmp_path, remote="r")

    assert result.pushed == 3
    assert result.up_to_date is False
    assert "--json" not in seen["cmd"]
    assert seen["kwargs"].get("json_mode") is True


def test_subprocess_push_detects_up_to_date_from_stdout(monkeypatch, tmp_path: Path) -> None:
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops,
        "run_streaming",
        _stub_push_run_streaming(["Everything is up to date."], seen),
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    result = ops.push(cwd=tmp_path)

    assert result.pushed == 0
    assert result.up_to_date is True


def test_subprocess_push_appends_targets_after_flags(monkeypatch, tmp_path: Path) -> None:
    """Targets land at the END of the argv, AFTER `--remote`/`--jobs` —
    options-before-positionals, the same shape pull uses."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops, "run_streaming", _stub_push_run_streaming(["2 files pushed"], seen)
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.push(cwd=tmp_path, targets=["a.dvc", "dir/b"], remote="r", jobs=2)

    cmd = seen["cmd"]
    assert cmd[-2:] == ["a.dvc", "dir/b"]
    assert cmd.index("--remote") < cmd.index("a.dvc")
    assert cmd.index("--jobs") < cmd.index("a.dvc")


# Slice D (pull-all audit, fixes 5+6) — checkout timeout tier and the
# StorageKeyError tuple translation.


def _stub_result_run_streaming(seen: dict, *, returncode: int = 255, stderr_lines: list[str] | None = None):
    """Fake `run_streaming` recording cmd/kwargs; exits with ``returncode``
    (default: the failure the StorageKeyError translation tests need) and
    the given stderr."""

    class _R:
        stdout_lines: list[str] = []

    _R.returncode = returncode  # type: ignore[attr-defined]
    _R.stderr_lines = stderr_lines or []  # type: ignore[attr-defined]

    def _fake(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["kwargs"] = kwargs
        return _R()

    return _fake


def test_subprocess_checkout_runs_under_transfer_timeout(monkeypatch, tmp_path: Path) -> None:
    """`dvc checkout` materializes cache blobs into the workspace — tens of
    GB on a fresh clone of a real product. It must run under the transfer
    tier, not the 30s fast tier that SIGTERM'd it mid-materialization on
    non-reflink filesystems."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_result_run_streaming(seen, returncode=0))
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts(fast=1.0, transfer=345.0))
    ops.checkout(cwd=tmp_path, targets=["data/final.dvc"])

    assert seen["kwargs"]["wall_timeout"] == 345.0


def test_subprocess_checkout_default_timeouts_mean_no_wall_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    """Default config: transfer=None → checkout gets NO wall timeout (it
    previously inherited fast=30.0 and got killed)."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_result_run_streaming(seen, returncode=0))
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.checkout(cwd=tmp_path)

    assert seen["kwargs"]["wall_timeout"] is None


@pytest.mark.parametrize("argv", [
    ["data", "add", "data/raw"], ["data", "verify"], ["data", "remove", "data/raw.dvc"],
], ids=["add", "verify", "remove"])
@pytest.mark.parametrize(("fast", "transfer", "locked", "rc"), [
    (0.01, 600, False, 0),
    (0.01, None, False, 0),
    (600, 1, True, 1),
], ids=["fast-below-call-time", "default-transfer", "transfer-below-call-time"])
def test_dvc_calls_whose_run_time_grows_with_the_data_run_under_transfer(
    argv, fast, transfer, locked, rc, tmp_path: Path, monkeypatch, capsys, real_dvc,
) -> None:
    """`data add` runs `dvc add`, which hashes every byte; `data verify` runs
    `dvc status`, which hashes every file it has not hashed yet (all of them on
    a fresh clone); `data remove` runs `dvc remove`, which copies hardlinked or
    symlinked files out of the cache. All three grow with the data, so they run
    under `timeouts.transfer`, not the 30 s `fast` cap that stopped a
    first-time add of a 40 GiB folder.

    The clock starts at `proc.wait` (`_subprocess.py`), after the spawn. In
    the transfer-below case the test holds dvc's own repository lock, so dvc
    cannot finish: it spends 3.0 s on lock retries, then exits with "Unable to
    acquire lock". The 1 s `transfer` cap must fire first on any machine. In
    the fast-below case `fast` is below any dvc call's run time, and the call
    runs under a 600 s `transfer`. The default-transfer case leaves `transfer`
    unset, as shipped, so the call has no cap at all.

    Mutation: putting any of the three verbs back on `fast` turns its three cases
    red. The fast-below and default-transfer cases time out at 0.01 s; the
    transfer-below case ends in dvc's lock error instead of the 1 s timeout.
    Falling back to `fast` when `transfer` is unset turns default-transfer
    red."""
    from dvc.lock import make_lock

    from mintd import cli

    cfg = tmp_path / "mintd-config"
    cfg.mkdir()
    transfer_line = "" if transfer is None else f"  transfer: {transfer}\n"
    (cfg / "config.yaml").write_text(f"timeouts:\n  fast: {fast}\n{transfer_line}", encoding="utf-8")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(cfg))
    ws = tmp_path / "ws"
    (ws / "data" / "raw").mkdir(parents=True)
    (ws / "data" / "raw" / "x.csv").write_text("x\n")
    real_dvc(["init", "--no-scm", "-q"], cwd=ws, check=True)
    if argv[1] != "add":
        real_dvc(["add", "-q", "data/raw"], cwd=ws, check=True)
    monkeypatch.chdir(ws)  # data add runs in Path("."); data verify defaults --path to "."
    lock = make_lock(str(ws / ".dvc" / "tmp" / "lock"), tmp_dir=str(ws / ".dvc" / "tmp"))
    if locked:
        lock.lock()
    try:
        got = cli.main(argv)
    finally:
        if locked:
            lock.unlock()

    assert got == rc
    err = capsys.readouterr().err
    if locked:
        assert f"exceeded wall timeout of {float(transfer)}s" in err
    else:
        assert "exceeded wall timeout" not in err


def test_subprocess_pull_translates_storage_key_tuple(
    monkeypatch, tmp_path: Path,
) -> None:
    """dvc's `unexpected error - ('data', 'final', ...)` crash is translated
    into the owning .dvc target plus a `mintd data pull <target>` hint,
    instead of surfacing the bare tuple."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "final.dvc").write_text("outs: []\n")
    # No `monkeypatch.chdir`: `cwd=` is what resolves the owning `.dvc` now.
    # Its presence here used to be load-bearing -- the translator fell back to
    # `Path.cwd()` -- so removing it is what makes that fallback's deletion
    # observable.

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops,
        "run_streaming",
        _stub_result_run_streaming(
            seen,
            stderr_lines=[
                "ERROR: unexpected error - "
                "('data', 'final', 'region_lookup', 'lookup_table.dta')"
            ],
        ),
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    with pytest.raises(_dvc_ops.DvcStorageKeyError) as exc_info:
        ops.pull(cwd=tmp_path, targets=["data/final.dvc"])

    err = exc_info.value
    assert err.target == "data/final.dvc"
    assert "data/final/region_lookup/lookup_table.dta" in str(err)
    assert "data/final.dvc" in str(err)
    assert err.hint == "retry just this target: mintd data pull data/final.dvc"


def test_subprocess_checkout_translates_storage_key_tuple(
    monkeypatch, tmp_path: Path,
) -> None:
    """The same translation applies to `dvc checkout` (dvc's unguarded
    StorageKeyError sites live in its checkout phase)."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "final.dvc").write_text("outs: []\n")
    # No `monkeypatch.chdir`: `cwd=` is what resolves the owning `.dvc` now.
    # Its presence here used to be load-bearing -- the translator fell back to
    # `Path.cwd()` -- so removing it is what makes that fallback's deletion
    # observable.

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops,
        "run_streaming",
        _stub_result_run_streaming(
            seen,
            stderr_lines=["ERROR: unexpected error - ('data', 'final', 'part.parquet')"],
        ),
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    with pytest.raises(_dvc_ops.DvcStorageKeyError) as exc_info:
        ops.checkout(cwd=tmp_path, targets=["data/final.dvc"])

    err = exc_info.value
    assert err.target == "data/final.dvc"
    assert "checkout" in str(err)
    assert err.hint == "retry just this target: mintd data pull data/final.dvc"


def test_translate_storage_key_error_without_owning_dvc_file(
    tmp_path: Path,
) -> None:
    """No `<prefix>.dvc` on disk: the message still names the failing path
    and the hint stays actionable (generic targeted-retry shape)."""
    from mintd._dvc_ops import _translate_storage_key_error

    err = _translate_storage_key_error(
        "ERROR: unexpected error - ('data', 'final', 'x.dta')",
        op="pull",
        exit_code=255,
        cwd=tmp_path,
    )
    assert err is not None
    assert err.target is None
    assert "data/final/x.dta" in str(err)
    assert "mintd data pull" in err.hint


def test_translate_storage_key_error_ignores_other_stderr(tmp_path: Path) -> None:
    """Non-tuple failures keep the generic DvcPullError path: the translator
    returns None for ordinary stderr and for a non-string tuple."""
    from mintd._dvc_ops import _translate_storage_key_error

    assert _translate_storage_key_error(
        "ERROR: failed to pull data from the cloud",
        op="pull", exit_code=1, cwd=tmp_path,
    ) is None
    assert _translate_storage_key_error(
        "ERROR: unexpected error - (1, 2)",
        op="pull", exit_code=255, cwd=tmp_path,
    ) is None


# ---------------------------------------------------------------------------
# dvc telemetry opt-out
# ---------------------------------------------------------------------------


def test_dvc_env_carries_the_analytics_opt_out() -> None:
    """`dvc_env()` disables dvc's telemetry.

    dvc ships analytics on by default: left alone it writes a persistent
    machine id under ``$HOME`` and POSTs a report from a detached daemon on
    every spawn, and under CI that report carries the org name and acting
    account rather than an anonymous id. mintd scaffolds enclave and lab-only
    projects, so an unannounced outbound request on project creation is a
    governance question, not a default to inherit.

    Mutation that must redden this: drop the ``DVC_NO_ANALYTICS`` line from
    ``src/mintd/_dvc_invoke.py``.
    """
    from mintd._dvc_invoke import dvc_env

    assert dvc_env()["DVC_NO_ANALYTICS"] == "1"


def test_subprocess_dvc_ops_env_carries_the_opt_out_with_and_without_a_profile() -> None:
    """`_env()` used to return ``None`` — "inherit the parent env" — whenever no
    AWS profile was configured, which also inherited dvc's telemetry default.
    Both arms must now carry the opt-out."""
    from mintd._config import Timeouts
    from mintd._dvc_ops import SubprocessDvcOps

    plain = SubprocessDvcOps(timeouts=Timeouts())._env()
    profiled = SubprocessDvcOps(timeouts=Timeouts(), aws_profile_name="mintd")._env()

    assert plain["DVC_NO_ANALYTICS"] == "1"
    assert profiled["DVC_NO_ANALYTICS"] == "1"
    assert profiled["AWS_PROFILE"] == "mintd"


def test_every_function_that_spawns_dvc_passes_an_explicit_env() -> None:
    """No dvc spawn in `src/mintd/` may inherit the ambient environment.

    `dvc_env()` is only an opt-out for the call sites that pass it, so the
    invariant that matters is not "the helper exists" but "nothing spawns dvc
    without it". Scanned per *function*, because the argv is sometimes built
    into a local (`cmd = [*dvc_cmd(), "push"]`) and handed to `run_streaming`
    a few lines later — both halves live in one function body.

    Mutations that must redden this: delete `env=dvc_env()` from any
    `subprocess.run` in `_init_ops.py` or `_fast_sync_ops.py`; delete
    `env=self._env()` from any `run_streaming` in `_dvc_ops.py`; or weaken any
    of them to `env=None`, which is an `env` keyword and still inherits.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "mintd"
    spawners = {"run", "Popen", "run_streaming"}
    offenders: list[str] = []
    checked = 0

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.dump(fn)
            # `SubprocessDvcOps._spawn` is the single chokepoint every dvc
            # verb now spawns through, and it does NOT mention `dvc_cmd` --
            # the argv is built by the verb and handed in. Without naming it
            # here the scanner walks straight past the only line in
            # `_dvc_ops.py` that actually starts a process, and the whole file
            # passes vacuously. (That is exactly what happened when the eight
            # duplicated spawn blocks were collapsed into it: `offenders`
            # stayed empty while `checked` fell from 16 to 8, and only the
            # count below caught it.)
            if "'dvc_cmd'" not in body and fn.name != "_spawn":
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None)
                )
                if name not in spawners:
                    continue
                checked += 1
                env_kw = next((k for k in node.keywords if k.arg == "env"), None)
                if env_kw is None:
                    offenders.append(f"{path.name}:{node.lineno} in {fn.name}() — no env=")
                    continue
                # Presence is not enough: `env=None` *is* an `env` keyword and
                # means "inherit", which is exactly the semantics this change
                # removed from `_dvc_ops._env()` and so the realistic
                # regression. Only a call to `dvc_env()` / `self._env()` counts.
                value = env_kw.value
                builder = None
                if isinstance(value, ast.Call):
                    builder = (
                        value.func.attr
                        if isinstance(value.func, ast.Attribute)
                        else getattr(value.func, "id", None)
                    )
                if builder not in {"dvc_env", "_env"}:
                    offenders.append(
                        f"{path.name}:{node.lineno} in {fn.name}() — env={ast.unparse(value)}"
                    )

    assert offenders == [], f"dvc spawned with an inherited env: {offenders}"
    # Guard the scanner itself: a matcher that finds nothing passes vacuously.
    # 10 = nine in `_init_ops.py` / `_fast_sync_ops.py` + ONE in `_dvc_ops.py`.
    # Was 16 until unit A collapsed that file's eight duplicated
    # `run_streaming` blocks into `SubprocessDvcOps._spawn`; then 9, until
    # `InitOps.dvc_add` (init tracks a data product's data/raw) added one in
    # `_init_ops.py`, beside `dvc_init` and with the same `env=`. A FALLING count is
    # not automatically fine -- it is how this invariant would rot silently, by
    # spawns moving somewhere the scanner does not look -- so the number is
    # pinned and every move has to be explained here, which is the point.
    assert checked == 10, f"dvc spawn sites moved: {checked}"


# --- unit A: the `cwd` seam ------------------------------------------------
#
# Three pins on the protocol itself, not on any one call site. Between them
# they make "a verb forgot which repo it acts on" a test failure rather than a
# silent write into whatever directory the process happened to be standing in.

_VERBS = ("init", "import_", "push", "pull", "add", "status", "remove", "checkout")


@pytest.mark.parametrize("verb", _VERBS)
@pytest.mark.parametrize(
    "impl", [DvcOps, SubprocessDvcOps, _FakeDvcOps], ids=["protocol", "real", "fake"],
)
def test_every_dvc_ops_verb_requires_cwd(impl, verb: str) -> None:
    """`cwd` is keyword-only AND has no default, on all three implementations.

    Required is the whole point. `cwd: Path | None = None` would type-check
    every existing caller and keep ambient process cwd as the default — the
    exact trap unit A removes, wearing better types. With no default, a missed
    call site is a `mypy src/mintd` failure at CI time.

    Parametrized over the Protocol as well as the two implementations because
    a Protocol that has drifted from its implementations licenses nothing.
    """
    import inspect

    param = inspect.signature(getattr(impl, verb)).parameters.get("cwd")
    assert param is not None, f"{impl.__name__}.{verb} has no `cwd` parameter"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        f"{impl.__name__}.{verb}: `cwd` must be keyword-only, got {param.kind}"
    )
    assert param.default is inspect.Parameter.empty, (
        f"{impl.__name__}.{verb}: `cwd` must be required, got default {param.default!r}"
    )


def test_every_subprocess_verb_forwards_cwd_to_run_streaming(
    monkeypatch, tmp_path: Path
) -> None:
    """Declaring `cwd` and forwarding it are different things.

    A verb that accepts `cwd` and drops it on the floor passes the signature
    pin above while still shelling into `os.getcwd()` — which is precisely the
    bug, just harder to see. So: drive all eight through a stubbed
    `run_streaming` and assert every spawn was aimed.
    """
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: list[object] = []

    class _R:
        returncode = 0
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

    def _fake(cmd, **kwargs):
        seen.append(kwargs.get("cwd", "MISSING"))
        return _R()

    monkeypatch.setattr(_dvc_ops, "run_streaming", _fake)
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())

    ops.init(cwd=tmp_path)
    ops.import_(repo_url="http://x", path="o", dest=tmp_path / "d", cwd=tmp_path)
    ops.push(cwd=tmp_path)
    ops.pull(cwd=tmp_path)
    ops.add(tmp_path / "f", cwd=tmp_path)
    ops.status(cwd=tmp_path)
    ops.remove("n", cwd=tmp_path)
    ops.checkout(cwd=tmp_path)

    assert seen == [tmp_path] * len(_VERBS), (
        f"not every verb aimed its subprocess: {seen}"
    )


def test_path_typed_argv_is_absolutized_so_cwd_cannot_reanchor_it(
    monkeypatch, tmp_path: Path
) -> None:
    """`import_`'s `-o` and `add`'s path survive a `cwd` that is not the
    process cwd — and the return value is still built from the original.

    This is the half of unit A that is easy to miss and expensive to get
    wrong. `dest` and `path` mean "relative to where the user is standing";
    once a `cwd` is forwarded, a *relative* one would be re-read against the
    child's new directory instead. Measured against real dvc 3.67.1 with a
    nested enclave: the naive version fails `stage working dir
    '.../outer/enclave/enclave/downloads/_staging' does not exist` — the path
    segment appears twice. Absolutizing at the seam keeps today's meaning.

    `.absolute()` rather than `.resolve()` on purpose: resolve() follows
    symlinks, which under `tmp_path` on macOS rewrites `/var` to `/private/var`
    and makes the argv stop matching what the caller asked for.
    """
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    captured: list[list[str]] = []
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_run_streaming(captured))
    monkeypatch.chdir(tmp_path)
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())

    elsewhere = tmp_path / "other-repo"
    elsewhere.mkdir()

    produced = ops.import_(
        repo_url="http://x", path="outputs/d.csv",
        dest=Path("downloads/d.csv"), cwd=elsewhere,
    )
    out_arg = Path(captured[0][captured[0].index("-o") + 1])
    assert out_arg.is_absolute(), f"-o is relative and cwd will re-anchor it: {out_arg}"
    assert out_arg == tmp_path / "downloads" / "d.csv"
    # the caller's view is unchanged: still relative to what it passed
    assert produced == Path("downloads/d.csv.dvc")

    captured.clear()
    produced_add = ops.add(Path("data/final.csv"), cwd=elsewhere)
    assert Path(captured[0][-1]) == tmp_path / "data" / "final.csv"
    assert produced_add == Path("data/final.csv.dvc")


@pytest.mark.parametrize("verb", _VERBS)
def test_an_unusable_cwd_is_not_reported_as_a_broken_dvc_install(
    verb: str, tmp_path: Path
) -> None:
    """A bad `cwd` says "not a directory", never "reinstall mintd".

    Threading `cwd` to `subprocess` made an existing translation unsound.
    Every verb turned `FileNotFoundError` into `DvcNotInstalled("mintd's
    bundled dvc is missing — reinstall mintd.")`, which was correct only while
    `cwd` was always the process's own directory and so always existed.
    `subprocess` raises that same exception for a missing *working directory*.
    Measured on this branch before the fix: `mintd data verify --path
    /nope/nope` exited 2 telling the user to `pip install dvc`, on a machine
    where dvc was fine. Remediation advice that is actively wrong is worse
    than a bare stack trace, because it gets followed.

    Parametrized over all eight because the translation was duplicated eight
    times; it now lives once, in `_spawn`.
    """
    from mintd._config import Timeouts
    from mintd._dvc_ops import DvcNotInstalled, DvcRepoPathError, SubprocessDvcOps

    ops = SubprocessDvcOps(timeouts=Timeouts())
    missing = tmp_path / "no-such-dir"
    kwargs: dict = {"cwd": missing}
    args: tuple = ()
    if verb == "import_":
        kwargs |= {"repo_url": "http://x", "path": "o", "dest": tmp_path / "d"}
    elif verb == "add":
        args = (tmp_path / "f",)
    elif verb == "remove":
        args = ("n",)

    with pytest.raises(DvcRepoPathError) as exc_info:
        getattr(ops, verb)(*args, **kwargs)

    assert not isinstance(exc_info.value, DvcNotInstalled), (
        f"{verb} still blames the install for a bad cwd"
    )
    assert str(missing) in str(exc_info.value)
    assert exc_info.value.hint and str(missing) in exc_info.value.hint


def test_a_cwd_that_is_a_regular_file_does_not_escape_as_a_traceback(
    tmp_path: Path
) -> None:
    """`NotADirectoryError` is an `OSError` but NOT a `FileNotFoundError`, so
    before the fix it sailed past every `except` in the seam AND both arms in
    the CLI handler, reaching the user as a raw Python traceback — against
    this repo's standing rule that documented failure paths never traceback.

    Same root cause as the test above, different exception class, which is
    exactly why the guard is a positive `is_dir()` check rather than a wider
    `except`.
    """
    from mintd._config import Timeouts
    from mintd._dvc_ops import DvcRepoPathError, SubprocessDvcOps

    a_file = tmp_path / "notadir"
    a_file.write_text("x")
    ops = SubprocessDvcOps(timeouts=Timeouts())

    # DvcRepoPathError subclasses DvcOpError, which is the net every CLI
    # handler catches -- so pinning the class here pins "no traceback" too.
    with pytest.raises(DvcRepoPathError):
        ops.status(cwd=a_file)


# ---------------------------------------------------------------------------
# issue09 fix 4 — the `-o <existing-dir>` stderr shapes map to a typed error
# ---------------------------------------------------------------------------


class _CannedStderrDvcOps(SubprocessDvcOps):
    """`import_` with `_spawn` replaced by a canned failing result — the
    stderr classifier is the unit under test, no subprocess involved."""

    def __init__(self, stderr: str) -> None:
        from mintd._config import Timeouts

        super().__init__(timeouts=Timeouts())
        self._stderr = stderr

    def _spawn(self, cmd, **kwargs):  # type: ignore[override]
        from types import SimpleNamespace

        return SimpleNamespace(
            returncode=1, stderr_lines=[self._stderr], stdout_lines=[]
        )


GIT_IGNORED_STDERR = "ERROR: bad DVC file name 'final/final.dvc' is git-ignored."


@pytest.mark.parametrize(
    "stderr,dest_exists",
    [
        # dvc's overlap refusal after nesting the source inside the existing
        # directory dest ...
        ("ERROR: Paths for outs:\n'final'\n'final/final'\noverlap and are thus in the same tracked directory.", False),
        # ... and the gitignore-dependent variant of the SAME root cause: dvc
        # wrote a `.gitignore` for the first import and the nested stage file
        # lands under it. Only reachable with the destination present, which
        # is what `[real]` in the contract suite exercises.
        (GIT_IGNORED_STDERR, True),
        # The classic pre-existing `.dvc` shape keeps mapping too.
        ("ERROR: output 'final.dvc' already exists", False),
    ],
    ids=["overlap", "git-ignored-dest-present", "already-exists"],
)
def test_import_existing_dir_stderr_maps_to_destination_exists(
    tmp_path: Path, stderr: str, dest_exists: bool
) -> None:
    from mintd._dvc_ops import DvcImportDestinationExists

    ops = _CannedStderrDvcOps(stderr)
    dest = tmp_path / "final"
    if dest_exists:
        dest.mkdir()

    with pytest.raises(DvcImportDestinationExists) as ei:
        ops.import_(
            repo_url="https://example.org/repo",
            path="data/final/",
            dest=dest,
            cwd=tmp_path,
        )
    # The message names the DIRECTORY to remove, not the `.dvc`.
    assert str(dest) in str(ei.value)
    assert f"{dest}.dvc" not in str(ei.value)


def test_git_ignored_with_nothing_in_the_way_is_not_destination_exists(
    tmp_path: Path,
) -> None:
    """Same stderr, destination ABSENT: a user-edited `.gitignore`, a global
    `core.excludesFile`, or a `--dest-root` outside the scaffold's negation
    rules. Reporting "already exists; remove the directory or pass force=True"
    offered two remedies that do nothing here, and discarded dvc's own line —
    the only place the responsible ignore rule is named. The user acts on the
    advice, gets the identical message back, and loops.

    Mutation: drop the `not dest.exists()` guard -> DvcImportDestinationExists.
    """
    from mintd._dvc_ops import DvcDestinationGitIgnored, DvcImportDestinationExists

    dest = tmp_path / "final"
    assert not dest.exists()

    with pytest.raises(DvcDestinationGitIgnored) as ei:
        _CannedStderrDvcOps(GIT_IGNORED_STDERR).import_(
            repo_url="https://example.org/repo",
            path="data/final/",
            dest=dest,
            cwd=tmp_path,
        )

    # Still a DvcOpError, so every existing handler renders it unchanged...
    assert isinstance(ei.value, DvcOpError)
    assert not isinstance(ei.value, DvcImportDestinationExists)
    # ...but dvc's own line survives, and the remedies are the real ones.
    assert "git-ignored" in str(ei.value)
    assert "final/final.dvc" in str(ei.value)
    assert "--dest-root" in str(ei.value)
    assert "force=True" not in str(ei.value)


# dvc 3.x reports a path absent from the producer's tree at the resolved rev
# as an uncaught list repr. Measured at 3.67.1; dvc echoes the path
# normalized, whatever spelling mintd sent.
MISSING_PATH_STDERR = "ERROR: unexpected error - ['data/wip_only']\n"
# The same crash prefix when the path IS in the tree but its objects never
# reached the remote. A download problem, not a rev problem.
OBJECTS_MISSING_STDERR = (
    "ERROR: unexpected error - failed to load directory ('data', 'final'): "
    "[Errno 2] No such file or directory: '/r/files/md5/1b/4e63.dir'\n"
)


@pytest.mark.parametrize(
    "path", ["data/wip_only", "data/wip_only/", "./data/wip_only"]
)
def test_import_missing_path_is_typed_not_a_raw_dvc_crash(
    tmp_path: Path, path: str
) -> None:
    from mintd._dvc_ops import DvcImportPathNotFound

    with pytest.raises(DvcImportPathNotFound) as ei:
        _CannedStderrDvcOps(MISSING_PATH_STDERR).import_(
            repo_url="https://example.org/repo", path=path,
            dest=tmp_path / "d", cwd=tmp_path,
        )
    assert "not found at rev 'HEAD'" in str(ei.value)
    assert "--rev" in (ei.value.hint or "")


def test_objects_missing_from_the_remote_is_not_a_missing_path(
    tmp_path: Path,
) -> None:
    from mintd._dvc_ops import DvcImportPathNotFound

    with pytest.raises(DvcOpError) as ei:
        _CannedStderrDvcOps(OBJECTS_MISSING_STDERR).import_(
            repo_url="https://example.org/repo", path="data/final",
            dest=tmp_path / "d", cwd=tmp_path,
        )
    assert not isinstance(ei.value, DvcImportPathNotFound)


# dvc 3.67.1, measured by story 2: the catalog points at a producer URL that
# refuses connections, so dvc's git clone of it fails.
CLONE_FAILED_STDERR = (
    "ERROR: failed to import 'data/final/' - SCM error: Failed to clone repo "
    "'http://127.0.0.1:62870/demo.git' to '/tmp/tmp91fby229dvc-clone': "
    "HTTPConnectionPool(host='127.0.0.1', port=62870): Max retries exceeded with url: "
    "/demo.git/info/refs?service=git-upload-pack (Caused by NewConnectionError("
    "\"HTTPConnection(host='127.0.0.1', port=62870): Failed to establish a new "
    "connection: [Errno 61] Connection refused\"))\n"
)
# The same, measured: every file of the primary deleted from the producer's
# storage, so the pinned versions are gone.
STORAGE_MISSING_STDERR = (
    "ERROR: unexpected error - [Errno 2] No storage files available: 'data/final/a.csv'\n"
    "Having any troubles? Hit us up at https://dvc.org/support, we are always happy to help!\n"
)


def test_import_from_an_unreachable_producer_names_the_url_it_tried(tmp_path: Path) -> None:
    """Mutation: drop the `Failed to clone repo` match -> a plain DvcOpError, red."""
    from mintd._dvc_ops import DvcImportRepoUnreachable

    url = "http://127.0.0.1:62870/demo.git"
    with pytest.raises(DvcImportRepoUnreachable) as ei:
        _CannedStderrDvcOps(CLONE_FAILED_STDERR).import_(
            repo_url=url, path="data/final/", dest=tmp_path / "d", cwd=tmp_path,
        )
    assert ei.value.hint == (
        f"could not reach the producer's repository {url}: check your network and git access "
        f"(git ls-remote {url}); if it moved, the catalog entry is out of date, so tell the producer"
    )
    assert "Connection refused" in str(ei.value)  # dvc's own words stay in the error


@pytest.mark.parametrize(("text", "shown"), [
    ("http://alice:s3cr3t@host/r.git", "http://host/r.git"),
    ("https://s3cr3t@github.com/org/r.git", "https://github.com/org/r.git"),  # a token as the user
    ("ssh://git:s3cr3t@host/r.git", "ssh://git@host/r.git"),  # ssh keeps its login name
    ("ssh://git@host/r.git", "ssh://git@host/r.git"),
    ("git@github.com:org/r.git", "git@github.com:org/r.git"),
    ("git+https://ghp_s3cr3t@github.com/o/r.git", "git+https://github.com/o/r.git"),  # pip-style
    ("git+https://u:s3cr3t@h/r", "git+https://h/r"),
    # No path, and an @ later: ordinary text, not userinfo.
    ("https://example.org#frag@x", "https://example.org#frag@x"),
    ("https://example.org?q=a@b", "https://example.org?q=a@b"),
    ("see https://a.org,me@x", "see https://a.org,me@x"),
    ("clone 'https://u:s3cr3t@a/r' then 'http://b/r'", "clone 'https://a/r' then 'http://b/r'"),
])
def test_strip_userinfo_takes_credentials_out_of_every_url(text: str, shown: str) -> None:
    from mintd.errors import strip_userinfo

    assert strip_userinfo(text) == shown


def test_the_unreachable_hint_quotes_the_url_inside_its_command(tmp_path: Path) -> None:
    """The producer controls the catalog URL; pasting the suggested command must
    not run anything else. Mutation: drop `shlex.quote` -> red."""
    from mintd._dvc_ops import DvcImportRepoUnreachable

    url = "https://example.org/repo;touch pwned"
    with pytest.raises(DvcImportRepoUnreachable) as ei:
        _CannedStderrDvcOps(CLONE_FAILED_STDERR).import_(
            repo_url=url, path="data/final/", dest=tmp_path / "d", cwd=tmp_path,
        )
    assert "(git ls-remote 'https://example.org/repo;touch pwned')" in (ei.value.hint or "")


def test_import_with_files_missing_from_storage_says_the_producer_must_fix_it(tmp_path: Path) -> None:
    """Mutation: drop the `No storage files available` match -> a plain DvcOpError, red."""
    from mintd._dvc_ops import DvcImportStorageMissing

    with pytest.raises(DvcImportStorageMissing) as ei:
        _CannedStderrDvcOps(STORAGE_MISSING_STDERR).import_(
            repo_url="https://example.org/repo", path="data/final/", dest=tmp_path / "d", cwd=tmp_path,
        )
    assert ei.value.hint == (
        "the producer's data, or data it imports from another product, is missing from storage; "
        "that is the producer's to fix: send them this error, then re-run the import"
    )
    assert "data/final/a.csv" in str(ei.value)


def test_an_import_that_finds_dvcs_lock_held_says_it_wrote_nothing(tmp_path: Path) -> None:
    """dvc stops before writing anything (checked with dvc 3.67.1), as it does
    for no repo, no path and an unreachable producer, so a failed import names
    nothing it finds at the destination as its own. Mutation: drop the lock
    match -> a plain DvcOpError, which may have written, red."""
    from mintd._dvc_ops import DvcImportPathNotFound, DvcImportRepoUnreachable, DvcNotInRepoError, DvcRepoLocked

    locked = (
        "ERROR: Unable to acquire lock. Most likely another DVC process is running or was terminated abruptly.\n"
    )
    with pytest.raises(DvcRepoLocked) as ei:
        _CannedStderrDvcOps(locked).import_(
            repo_url="https://example.org/repo", path="data/final/", dest=tmp_path / "d", cwd=tmp_path,
        )
    assert ei.value.wrote_nothing and "Unable to acquire lock" in str(ei.value)
    assert all(e.wrote_nothing for e in (DvcImportPathNotFound, DvcImportRepoUnreachable, DvcNotInRepoError))
    assert not DvcOpError.wrote_nothing


def test_any_other_import_failure_keeps_no_hint_of_its_own(tmp_path: Path) -> None:
    """The verb's `_hint(DvcOpError, ...)` then supplies "retry with --dvc-arg=-v"."""
    with pytest.raises(DvcOpError) as ei:
        _CannedStderrDvcOps(OBJECTS_MISSING_STDERR).import_(
            repo_url="https://example.org/repo", path="data/final", dest=tmp_path / "d", cwd=tmp_path,
        )
    assert type(ei.value) is DvcOpError and ei.value.hint is None


def test_a_real_dvc_import_never_prints_the_catalog_urls_token(tmp_path: Path, capsys) -> None:
    """dvc's own live output echoes the URL it was given; the reporter's
    passthrough strips it. A real `dvc import` against a port that refuses at
    once (bound and closed again)."""
    import socket
    import subprocess as sp

    from mintd._config import Timeouts
    from mintd._console import Reporter
    from mintd._dvc_ops import DvcImportRepoUnreachable

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        url = f"http://alice:s3cr3tTOKEN@127.0.0.1:{s.getsockname()[1]}/priv.git"
    sp.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    SubprocessDvcOps(timeouts=Timeouts()).init(cwd=tmp_path)
    reporter = Reporter(no_color=True)
    with pytest.raises(DvcImportRepoUnreachable) as ei:
        SubprocessDvcOps(timeouts=Timeouts(), reporter=reporter).import_(
            repo_url=url, path="data/final", dest=tmp_path / "d", cwd=tmp_path,
        )
    reporter.error(str(ei.value), hint=ei.value.hint)
    out, err = capsys.readouterr()
    assert "Failed to clone repo" in err  # dvc's own line was shown...
    assert "s3cr3tTOKEN" not in out + err  # ...without the token


def test_strip_userinfo_stays_linear_on_long_tokens() -> None:
    """It runs over every printed line and every `--json` result, and a
    producer controls descriptions. Mutation: start a match at `\\b` -> slow, red."""
    import time

    from mintd.errors import strip_userinfo

    # Quadratic, this size takes tens of seconds; linear, about a millisecond.
    text = "see-also-" * 20000 + " https://u:p@h/r a@b"
    start = time.perf_counter()
    assert strip_userinfo(text).endswith(" https://h/r a@b")
    assert time.perf_counter() - start < 1.0
