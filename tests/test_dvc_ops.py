"""Tests for `_FakeDvcOps` — protocol conformance + stub round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._dvc_invoke import dvc_cmd
from mintd._dvc_ops import DvcOps
from mintd.imports import DataDependency

from tests._fakes.dvc_ops import _FakeDvcOps


def test_fake_satisfies_protocol() -> None:
    fake: DvcOps = _FakeDvcOps()
    assert callable(fake.import_)


def test_fake_records_call(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "cms_based"

    fake.import_(
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
    monkeypatch,
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
        targets=["data/foo"],
        remote="X",
        jobs=4,
        extra_args=["--verbose"],
    )

    assert captured == [
        [*dvc_cmd(), "pull", "--remote", "X", "--jobs", "4", "--verbose", "data/foo"],
    ]


def test_subprocess_pull_extra_args_none_keeps_legacy_argv(
    monkeypatch,
) -> None:
    """Backward compat: with `extra_args=None` (the default), argv is
    byte-for-byte the pre-slice-34 shape."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    captured: list[list[str]] = []
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_run_streaming(captured))

    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.pull(targets=["data/foo"], remote="X", jobs=4)

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


def test_pull_raises_dvc_not_installed_when_module_missing(monkeypatch) -> None:
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
        ops.pull(targets=["data/foo"])


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


def test_subprocess_init_tolerates_already_initialized(monkeypatch) -> None:
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
    ops.init()  # must not raise


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
        ops.import_(repo_url="http://x", path="out", dest=tmp_path / "d")


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


def test_subprocess_push_parses_count_from_captured_stdout(monkeypatch) -> None:
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
    result = ops.push(remote="r")

    assert result.pushed == 3
    assert result.up_to_date is False
    assert "--json" not in seen["cmd"]
    assert seen["kwargs"].get("json_mode") is True


def test_subprocess_push_detects_up_to_date_from_stdout(monkeypatch) -> None:
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(
        _dvc_ops,
        "run_streaming",
        _stub_push_run_streaming(["Everything is up to date."], seen),
    )
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    result = ops.push()

    assert result.pushed == 0
    assert result.up_to_date is True


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


def test_subprocess_checkout_runs_under_transfer_timeout(monkeypatch) -> None:
    """`dvc checkout` materializes cache blobs into the workspace — tens of
    GB on a fresh clone of a real product. It must run under the transfer
    tier, not the 30s fast tier that SIGTERM'd it mid-materialization on
    non-reflink filesystems."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_result_run_streaming(seen, returncode=0))
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts(fast=1.0, transfer=345.0))
    ops.checkout(targets=["data/final.dvc"])

    assert seen["kwargs"]["wall_timeout"] == 345.0


def test_subprocess_checkout_default_timeouts_mean_no_wall_timeout(
    monkeypatch,
) -> None:
    """Default config: transfer=None → checkout gets NO wall timeout (it
    previously inherited fast=30.0 and got killed)."""
    from mintd import _dvc_ops
    from mintd._config import Timeouts

    seen: dict = {}
    monkeypatch.setattr(_dvc_ops, "run_streaming", _stub_result_run_streaming(seen, returncode=0))
    ops = _dvc_ops.SubprocessDvcOps(timeouts=Timeouts())
    ops.checkout()

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
    monkeypatch.chdir(tmp_path)

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
        ops.pull(targets=["data/final.dvc"])

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
    monkeypatch.chdir(tmp_path)

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
        ops.checkout(targets=["data/final.dvc"])

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
