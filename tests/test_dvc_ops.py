"""Tests for `_FakeDvcOps` — protocol conformance + stub round-trip."""

from __future__ import annotations

from pathlib import Path

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
