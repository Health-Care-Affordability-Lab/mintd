"""Subprocess seam for git/DVC init operations.

Only this module shells out to `git init` and `dvc init`. Mirrors the
single-seam pattern of `_dvc_ops.py` and `_registry_git_ops.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class InitOpError(Exception):
    """Non-zero exit from `git init` or `dvc init`."""


class GitNotInstalled(InitOpError):
    """`git` binary not on PATH."""


class DvcNotInstalled(InitOpError):
    """`dvc` binary not on PATH."""


class InitOps(Protocol):
    def git_init(self, target_dir: Path) -> None: ...
    def dvc_init(self, target_dir: Path) -> None: ...


class SubprocessInitOps:
    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def git_init(self, target_dir: Path) -> None:
        try:
            result = subprocess.run(
                ["git", "init"],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise GitNotInstalled("`git` binary not found on PATH.") from None
        if result.returncode != 0:
            raise InitOpError(f"git init failed: {result.stderr.strip()}")

    def dvc_init(self, target_dir: Path) -> None:
        try:
            result = subprocess.run(
                ["dvc", "init"],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if result.returncode != 0:
            raise InitOpError(f"dvc init failed: {result.stderr.strip()}")
