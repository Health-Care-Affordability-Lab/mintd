"""Tests for `_FakeDvcOps` — protocol conformance + stub round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._dvc_invoke import dvc_cmd
from mintd._dvc_ops import DvcOps, SubprocessDvcOps
from mintd.imports import DataDependency
from tests._fakes.dvc_ops import _FakeDvcOps


def test_fake_satisfies_protocol() -> None:
    fake: DvcOps = _FakeDvcOps()
    assert callable(fake.import_)


def test_fake_records_call(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "cms_based"

    fake.import_(
        cwd=tmp_path,
        repo_url="https://github.com/example-org/provider-xw",
        path="outputs/cms_based/",
        dest=dest,
        rev="abc123",
        force=True,
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.repo_url == "https://github.com/example-org/provider-xw"
    assert call.path == "outputs/cms_based/"
    assert call.dest == dest
    assert call.rev == "abc123"
    assert call.force is True


def test_fake_writes_parseable_stub(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "cms_based"

    produced = fake.import_(
        cwd=tmp_path,
        repo_url="https://github.com/example-org/provider-xw",
        path="outputs/cms_based/",
        dest=dest,
    )

    assert produced == tmp_path / "cms_based.dvc"
    assert produced.exists()

    dep = DataDependency.from_dvc_file(produced)
    assert dep.producer_repo == "https://github.com/example-org/provider-xw"
    assert dep.output_path == "outputs/cms_based/"
    assert dep.local_path == "cms_based"


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
                "('data', 'final', 'aha_ccn_xw', 'crosswalk_aha_pos.dta')"
            ],
        ),
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    with pytest.raises(_dvc_ops.DvcStorageKeyError) as exc_info:
        ops.pull(cwd=tmp_path, targets=["data/final.dvc"])

    err = exc_info.value
    assert err.target == "data/final.dvc"
    assert "data/final/aha_ccn_xw/crosswalk_aha_pos.dta" in str(err)
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
    # 9 = eight in `_init_ops.py` / `_fast_sync_ops.py` + ONE in `_dvc_ops.py`.
    # Was 16 until unit A collapsed that file's eight duplicated
    # `run_streaming` blocks into `SubprocessDvcOps._spawn`. A FALLING count is
    # not automatically fine -- it is how this invariant would rot silently, by
    # spawns moving somewhere the scanner does not look -- so the number is
    # pinned and every move has to be explained here, which is the point.
    assert checked == 9, f"dvc spawn sites moved: {checked}"


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
