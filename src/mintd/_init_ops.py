"""Subprocess seam for git/DVC init operations.

Only this module shells out to `git init` and `dvc init`. Mirrors the
single-seam pattern of `_dvc_ops.py` and `_registry_git_ops.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol
from ._dvc_invoke import dvc_cmd


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
    def git_add(self, target_dir: Path, paths: list[str]) -> None: ...
    def git_unstage(self, target_dir: Path, paths: list[str]) -> None: ...
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

    def git_add(self, target_dir: Path, paths: list[str]) -> None:
        """Stage ``paths`` (``git add -- <paths>``). Raises ``InitOpError``
        on non-zero exit, mirroring ``git_init``. Used to restage
        ``.dvc/config`` after ``dvc init``/``dvc remote add`` rewrite it —
        so a teammate cloning the repo gets the config (with the remote)
        rather than a half-staged ``AM`` entry."""
        try:
            result = subprocess.run(
                ["git", "add", "--", *paths],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise GitNotInstalled("`git` binary not found on PATH.") from None
        if result.returncode != 0:
            raise InitOpError(f"git add failed: {result.stderr.strip()}")

    def git_unstage(self, target_dir: Path, paths: list[str]) -> None:
        """Best-effort ``git rm -r --cached --ignore-unmatch -- <paths>``.
        Never raises — this is rollback cleanup after ``.dvc/`` was
        rmtree'd, so a missing binary or non-zero exit must not mask the
        original failure (precedent: best-effort fsync, commit 2bccf43)."""
        try:
            subprocess.run(
                ["git", "rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", *paths],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def dvc_init(self, target_dir: Path) -> None:
        try:
            result = subprocess.run(
                [*dvc_cmd(), "init"],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if result.returncode != 0:
            if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise InitOpError(f"dvc init failed: {result.stderr.strip()}")
        # Slice 30 polish: set the cache.type fallback chain so checkout
        # uses reflink/hardlink/symlink before falling back to copy. DVC
        # defaults are filesystem-dependent — on Linux ext4 with no
        # explicit config the fallback is `copy`, which duplicates the
        # bytes from .dvc/cache into the working tree on every pull
        # (slow + 2x disk usage). Written to .dvc/config (per-project,
        # no --local/--global) so consumers cloning the repo inherit it.
        result = subprocess.run(
            [*dvc_cmd(), "config", "cache.type", "reflink,hardlink,symlink,copy"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if result.returncode != 0:
            raise InitOpError(
                f"dvc config cache.type failed: {result.stderr.strip()}"
            )

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
        AWS profile from the boto3 chain. Always concludes with
        ``dvc remote modify <name> version_aware true`` so the S3 key is
        the file's real path (mintd's mental model; matches what
        ``metadata.storage.versioning = True`` already declares).
        """
        cmd = [*dvc_cmd(), "remote", "add"]
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
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if result.returncode != 0:
            if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise InitOpError(f"dvc remote add failed: {result.stderr.strip()}")
        if endpoint:
            result = subprocess.run(
                [*dvc_cmd(), "remote", "modify", name, "endpointurl", endpoint],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            if result.returncode != 0:
                if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
                    raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
                raise InitOpError(f"dvc remote modify endpoint failed: {result.stderr.strip()}")
        if profile:
            result = subprocess.run(
                [*dvc_cmd(), "remote", "modify", name, "profile", profile],
                cwd=target_dir,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
            if result.returncode != 0:
                if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
                    raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
                raise InitOpError(f"dvc remote modify profile failed: {result.stderr.strip()}")
        result = subprocess.run(
            [*dvc_cmd(), "remote", "modify", name, "version_aware", "true"],
            cwd=target_dir,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if result.returncode != 0:
            if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise InitOpError(
                f"dvc remote modify version_aware failed: {result.stderr.strip()}"
            )
