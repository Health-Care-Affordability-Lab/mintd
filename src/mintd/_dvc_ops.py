"""DVC subprocess seam.

Only this module shells out to `dvc`. Mirrors `_registry_git_ops.py` for git/gh.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class DvcOpError(Exception):
    """Generic non-zero exit from a `dvc` invocation."""


class DvcNotInstalled(DvcOpError):
    """The `dvc` binary is not on PATH."""


class DvcImportPathNotFound(DvcOpError):
    """`dvc import` reports the requested path doesn't exist at the given rev."""


class DvcImportDestinationExists(DvcOpError):
    """`dvc import` refused because the destination `.dvc` already exists.

    The consumer-side fix is to remove it or pass `force=True` (which maps to
    `dvc import --force`).
    """


class DvcOps(Protocol):
    """Surface used by the rest of mintd to talk to dvc.

    Tests pass a fake; production passes `SubprocessDvcOps`.
    """

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        rev: str | None = None,
        force: bool = False,
    ) -> Path:
        """Run `dvc import` and return the path of the produced `.dvc` file."""
        ...


class SubprocessDvcOps:
    """Production: shells out to `dvc import [--rev R] [--force] <repo_url> <path> -o <dest>`."""

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        rev: str | None = None,
        force: bool = False,
    ) -> Path:
        cmd: list[str] = ["dvc", "import", repo_url, path, "-o", str(dest)]
        if rev:
            cmd.extend(["--rev", rev])
        if force:
            cmd.append("--force")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None

        if result.returncode != 0:
            stderr = result.stderr or ""
            if "Does not exist" in stderr or "Unable to find" in stderr:
                raise DvcImportPathNotFound(
                    f"path '{path}' not found at rev '{rev or 'HEAD'}' in '{repo_url}'"
                )
            if "already exists" in stderr or "use --force" in stderr:
                raise DvcImportDestinationExists(
                    f"destination for '{dest}.dvc' already exists; pass force=True"
                )
            raise DvcOpError(f"dvc import failed (exit {result.returncode}): {stderr.strip()}")

        return dest.parent / (dest.name + ".dvc")
