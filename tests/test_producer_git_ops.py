"""Tests for `GitArchiveFetcher` — error mapping via mocked `subprocess.run`."""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Callable

import pytest

from mintd._producer_git_ops import FetchError, GitArchiveFetcher

REPO = "https://github.com/example-org/provider_xw"
PIN = "a" * 40


def _make_tar_with_metadata(content: bytes = b'{"schema_version":"2.0"}') -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="metadata.json")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_empty_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    return buf.getvalue()


def _make_symlink_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="metadata.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "elsewhere.json"
        tar.addfile(info)
    return buf.getvalue()


class _Dispatcher:
    """Mock for `subprocess.run`. Routes by the first non-flag argument and
    respects the `text=` kwarg per the real subprocess contract.
    """

    def __init__(self, plan: dict[str, dict[str, Any]]) -> None:
        self.plan = plan
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(argv)
        subcmd = self._subcmd_for(argv)
        spec = self.plan.get(subcmd)
        if spec is None:
            raise AssertionError(f"unexpected subcommand: {subcmd} (argv={argv})")
        if "raise" in spec:
            raise spec["raise"]
        text = kwargs.get("text", True)
        stdout = spec.get("stdout", b"" if not text else "")
        stderr = spec.get("stderr", b"" if not text else "")
        if text:
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
        else:
            if isinstance(stdout, str):
                stdout = stdout.encode("utf-8")
            if isinstance(stderr, str):
                stderr = stderr.encode("utf-8")
        return subprocess.CompletedProcess(
            args=argv,
            returncode=spec.get("returncode", 0),
            stdout=stdout,
            stderr=stderr,
        )

    @staticmethod
    def _subcmd_for(argv: list[str]) -> str:
        for token in argv[1:]:
            if token in {"archive", "clone", "fetch", "show"}:
                return token
        return argv[0]

    def count(self, subcmd: str) -> int:
        return sum(1 for argv in self.calls if self._subcmd_for(argv) == subcmd)


def _install(monkeypatch: pytest.MonkeyPatch, dispatcher: _Dispatcher) -> _Dispatcher:
    monkeypatch.setattr("mintd._producer_git_ops.subprocess.run", dispatcher)
    return dispatcher


def test_git_archive_argv_includes_format_tar(monkeypatch: pytest.MonkeyPatch) -> None:
    d = _install(
        monkeypatch,
        _Dispatcher({"archive": {"returncode": 0, "stdout": _make_tar_with_metadata()}}),
    )

    GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert d.calls[0] == [
        "git",
        "archive",
        "--format=tar",
        "--remote",
        REPO,
        PIN,
        "metadata.json",
    ]


def test_git_archive_happy_path_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'{"schema_version":"2.0","project":{}}'
    _install(
        monkeypatch,
        _Dispatcher({"archive": {"returncode": 0, "stdout": _make_tar_with_metadata(content)}}),
    )

    result = GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert result == content


def test_git_archive_empty_tar_maps_to_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Dispatcher({"archive": {"returncode": 0, "stdout": _make_empty_tar()}}),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.METADATA_MISSING


def test_git_archive_symlink_member_maps_to_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Dispatcher({"archive": {"returncode": 0, "stdout": _make_symlink_tar()}}),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.METADATA_MISSING
    assert "symlink" in ei.value.detail or "hardlink" in ei.value.detail


def test_git_archive_did_not_match_any_maps_to_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {
                    "returncode": 128,
                    "stderr": b"fatal: pathspec 'metadata.json' did not match any files",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.METADATA_MISSING
    assert d.count("clone") == 0
    assert d.count("fetch") == 0
    assert d.count("show") == 0


def test_git_archive_recognizable_network_error_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {
                    "returncode": 128,
                    "stderr": b"fatal: unable to access 'x': Could not resolve host: github.com",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert d.count("clone") == 0
    assert d.count("fetch") == 0
    assert d.count("show") == 0


def test_git_archive_recognizable_pin_error_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {
                    "returncode": 128,
                    "stderr": b"fatal: bad revision unknown revision abc",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.PIN_MISSING
    assert d.count("clone") == 0


def test_git_archive_unsupported_falls_back_to_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show_bytes = b'{"schema_version":"2.0","project":{}}'
    d = _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {
                    "returncode": 128,
                    "stderr": b"fatal: Operation not supported by server",
                },
                "clone": {"returncode": 0, "stdout": "", "stderr": ""},
                "fetch": {"returncode": 0, "stdout": "", "stderr": ""},
                "show": {"returncode": 0, "stdout": show_bytes},
            }
        ),
    )

    result = GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert result == show_bytes
    subcmds = [d._subcmd_for(c) for c in d.calls]
    assert subcmds == ["archive", "clone", "fetch", "show"]


def test_git_archive_ambiguous_falls_back_to_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show_bytes = b'{"schema_version":"2.0","project":{}}'
    d = _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {"returncode": 128, "stderr": b"weird non-matching error"},
                "clone": {"returncode": 0},
                "fetch": {"returncode": 0},
                "show": {"returncode": 0, "stdout": show_bytes},
            }
        ),
    )

    assert GitArchiveFetcher().fetch_metadata_at(REPO, PIN) == show_bytes
    assert d.count("clone") == 1


def test_clone_fails_auth_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {"returncode": 128, "stderr": b"Operation not supported"},
                "clone": {"returncode": 128, "stderr": "fatal: Authentication failed"},
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE


def test_fetch_fails_pin_missing_maps_to_pin_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {"returncode": 128, "stderr": b"Operation not supported"},
                "clone": {"returncode": 0},
                "fetch": {
                    "returncode": 128,
                    "stderr": "fatal: Couldn't find remote ref abc",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.PIN_MISSING


def test_show_path_missing_maps_to_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {"returncode": 128, "stderr": b"Operation not supported"},
                "clone": {"returncode": 0},
                "fetch": {"returncode": 0},
                "show": {
                    "returncode": 128,
                    "stderr": b"fatal: path 'metadata.json' does not exist in 'abc'",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.METADATA_MISSING


def test_show_empty_stdout_maps_to_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {"returncode": 128, "stderr": b"Operation not supported"},
                "clone": {"returncode": 0},
                "fetch": {"returncode": 0},
                "show": {"returncode": 0, "stdout": b""},
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.METADATA_MISSING


def test_git_not_installed_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr("mintd._producer_git_ops.subprocess.run", boom)

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert "not installed" in ei.value.detail


def _timeout_raiser_for(target_subcmd: str, dispatcher: _Dispatcher) -> Callable[..., Any]:
    """Wrap a dispatcher so the first call matching `target_subcmd` raises TimeoutExpired."""

    def wrapper(argv: list[str], **kwargs: Any) -> Any:
        subcmd = dispatcher._subcmd_for(argv)
        if subcmd == target_subcmd:
            dispatcher.calls.append(argv)
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 60))
        return dispatcher(argv, **kwargs)

    return wrapper


def test_timeout_in_archive_maps_to_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    d = _Dispatcher({})
    monkeypatch.setattr(
        "mintd._producer_git_ops.subprocess.run", _timeout_raiser_for("archive", d)
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert "timeout" in ei.value.detail
    assert "archive" in ei.value.detail
    assert d.count("clone") == 0


def test_timeout_in_fallback_clone_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _Dispatcher(
        {
            "archive": {"returncode": 128, "stderr": b"Operation not supported"},
        }
    )
    monkeypatch.setattr(
        "mintd._producer_git_ops.subprocess.run", _timeout_raiser_for("clone", d)
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert "clone" in ei.value.detail
    assert d.count("fetch") == 0
    assert d.count("show") == 0


def test_timeout_in_fallback_fetch_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _Dispatcher(
        {
            "archive": {"returncode": 128, "stderr": b"Operation not supported"},
            "clone": {"returncode": 0},
        }
    )
    monkeypatch.setattr(
        "mintd._producer_git_ops.subprocess.run", _timeout_raiser_for("fetch", d)
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert "fetch" in ei.value.detail
    assert d.count("show") == 0


def test_timeout_in_fallback_show_maps_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _Dispatcher(
        {
            "archive": {"returncode": 128, "stderr": b"Operation not supported"},
            "clone": {"returncode": 0},
            "fetch": {"returncode": 0},
        }
    )
    monkeypatch.setattr(
        "mintd._producer_git_ops.subprocess.run", _timeout_raiser_for("show", d)
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.UNREACHABLE
    assert "show" in ei.value.detail


def test_stderr_bytes_decoded_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Dispatcher(
            {
                "archive": {
                    "returncode": 128,
                    "stderr": b"\xff\xfe noise unknown revision \xff",
                },
            }
        ),
    )

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(REPO, PIN)

    assert ei.value.reason == FetchError.Reason.PIN_MISSING


def test_single_subprocess_call_site() -> None:
    text = Path("src/mintd/_producer_git_ops.py").read_text(encoding="utf-8")
    assert text.count("subprocess.run(") == 1
