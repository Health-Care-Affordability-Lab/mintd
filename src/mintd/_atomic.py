"""Atomic-write helpers shared by publish, config_ops, and fast-sync.

Imports only stdlib (os, pathlib) — safe to import from anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path


def _try_fsync_file(path: Path) -> None:
    """Best-effort fsync of a just-written file for pre-rename durability.

    The data is already written (``write_bytes`` / ``download_file``) before
    this call; the fsync only flushes it to stable storage ahead of the
    rename. On Windows, ``os.open(..., O_RDONLY)`` + ``os.fsync`` on a fresh
    regular file can raise ``OSError`` (``[Errno 9] Bad file descriptor``),
    so — like :func:`_try_fsync_parent_dir` — we swallow ``OSError`` and
    continue rather than crash the cache write on a durability refinement.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _try_fsync_parent_dir(path: Path) -> None:
    """Best-effort fsync of ``path.parent`` for rename durability.

    POSIX systems support opening a directory ``O_RDONLY`` and fsyncing
    its fd, which durably persists a prior rename. Windows and some
    other platforms reject either the open or the fsync; the durability
    step is a refinement (the rename has already happened) so we
    swallow ``OSError`` and continue.
    """
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
