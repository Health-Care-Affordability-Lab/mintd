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


class InitNonInteractive(InitOpError):
    """``mintd init`` invoked without a TTY but classification not supplied
    via kwargs. Slice 30: init's classification prompt is interactive-only."""


class InitOps(Protocol):
    def git_init(self, target_dir: Path) -> None: ...
    def dvc_init(self, target_dir: Path) -> None: ...
    def dvc_remote_add(
        self, target_dir: Path, *,
        name: str, url: str, default: bool,
        endpoint: str | None, profile: str | None,
    ) -> None: ...


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

    def dvc_remote_add(
        self, target_dir: Path, *,
        name: str, url: str, default: bool,
        endpoint: str | None, profile: str | None,
    ) -> None:
        """Write a remote section to ``.dvc/config`` (per-project scope —
        no ``--local``/``--global``/``--system``, so the section lives in
        the tracked file and clones pick it up). Follows up with
        ``dvc remote modify <name> endpointurl <endpoint>`` and/or
        ``dvc remote modify <name> profile <profile>`` when set, so
        consumers running raw ``dvc pull`` (outside mintd) get the right
        AWS profile from the boto3 chain.
        """
        cmd = ["dvc", "remote", "add"]
        if default:
            cmd.append("-d")
        cmd.extend([name, url])
        try:
            result = subprocess.run(
                cmd,
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if result.returncode != 0:
            raise InitOpError(f"dvc remote add failed: {result.stderr.strip()}")
        if endpoint:
            result = subprocess.run(
                ["dvc", "remote", "modify", name, "endpointurl", endpoint],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            if result.returncode != 0:
                raise InitOpError(f"dvc remote modify endpoint failed: {result.stderr.strip()}")
        if profile:
            result = subprocess.run(
                ["dvc", "remote", "modify", name, "profile", profile],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            if result.returncode != 0:
                raise InitOpError(f"dvc remote modify profile failed: {result.stderr.strip()}")
