"""Tests for the slice-23 ``_try_fsync_parent_dir`` durability helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mintd import _atomic


def test_try_fsync_parent_dir_succeeds_on_normal_path(tmp_path: Path) -> None:
    """Happy path: file exists, parent fsync works, helper returns silently."""
    target = tmp_path / "file.txt"
    target.write_text("hello", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)


def test_try_fsync_parent_dir_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``os.open`` itself raises (Windows behavior), the helper must
    not propagate — durability is best-effort."""

    def _raise(*_a: Any, **_kw: Any) -> int:
        raise OSError("simulated windows refusal")

    monkeypatch.setattr(_atomic.os, "open", _raise)
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)


def test_try_fsync_parent_dir_closes_fd_on_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If ``os.open`` succeeds but ``os.fsync`` raises, the fd must still
    be closed — fd-leak guard."""
    opened: list[int] = []
    closed: list[int] = []

    real_open = _atomic.os.open
    real_close = _atomic.os.close

    def _wrap_open(path: str, flags: int) -> int:
        fd = real_open(path, flags)
        opened.append(fd)
        return fd

    def _wrap_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _fsync_raises(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(_atomic.os, "open", _wrap_open)
    monkeypatch.setattr(_atomic.os, "close", _wrap_close)
    monkeypatch.setattr(_atomic.os, "fsync", _fsync_raises)

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)

    assert opened, "os.open should have been called"
    assert opened == closed, "every opened fd must be closed"


def test_try_fsync_file_succeeds_on_normal_path(tmp_path: Path) -> None:
    """Happy path: file exists, fsync works, helper returns silently."""
    target = tmp_path / "file.txt"
    target.write_text("hello", encoding="utf-8")
    _atomic._try_fsync_file(target)


def test_try_fsync_file_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When os.open/os.fsync raise (the Windows [Errno 9] Bad file
    descriptor case), the helper must not propagate — the fsync is a
    durability refinement, the write already happened."""

    def _raise(*_a: Any, **_kw: Any) -> int:
        raise OSError(9, "Bad file descriptor")

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(_atomic.os, "open", _raise)
    _atomic._try_fsync_file(target)  # must not raise


def test_try_fsync_file_closes_fd_on_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """os.open succeeds but os.fsync raises → fd still closed (no leak)."""
    opened: list[int] = []
    closed: list[int] = []
    real_open = _atomic.os.open
    real_close = _atomic.os.close

    def _wrap_open(path: str, flags: int) -> int:
        fd = real_open(path, flags)
        opened.append(fd)
        return fd

    def _wrap_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(_atomic.os, "open", _wrap_open)
    monkeypatch.setattr(_atomic.os, "close", _wrap_close)
    monkeypatch.setattr(_atomic.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("boom")))

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_file(target)
    assert opened == closed, "every opened fd must be closed"
