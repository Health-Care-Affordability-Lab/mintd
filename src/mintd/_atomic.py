"""Atomic-write helpers shared by publish, config_ops, and fast-sync.

Imports only stdlib (os, pathlib) — safe to import from anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path


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
