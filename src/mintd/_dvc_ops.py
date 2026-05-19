"""DVC subprocess seam.

Only this module shells out to `dvc`. Mirrors `_registry_git_ops.py` for git/gh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from ._config import Timeouts
from ._console import Reporter
from ._subprocess import run_streaming


class DvcOpError(Exception):
    """Generic non-zero exit from a `dvc` invocation."""


class DvcNotInstalled(DvcOpError):
    """The `dvc` binary is not on PATH."""


class DvcPushError(DvcOpError):
    """`dvc push` exited non-zero."""


class DvcPullError(DvcOpError):
    """`dvc pull` exited non-zero."""


class DvcAddError(DvcOpError):
    """`dvc add` exited non-zero."""


class DvcStatusError(DvcOpError):
    """`dvc status` exited non-zero."""


class DvcRemoveError(DvcOpError):
    """`dvc remove` exited non-zero."""


class DvcCheckoutError(DvcOpError):
    """`dvc checkout` exited non-zero."""


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

    def push(self, *, remote: str | None = None, jobs: int | None = None) -> None:
        """Run `dvc push`."""
        ...

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> None:
        """Run `dvc pull`."""
        ...

    def add(self, path: Path) -> Path:
        """Run `dvc add` and return the path of the produced `.dvc` file."""
        ...

    def status(self, targets: list[str] | None = None) -> dict[str, str]:
        """Run `dvc status` and return a status map."""
        ...

    def remove(self, name: str) -> None:
        """Run `dvc remove`."""
        ...

    def checkout(self, *, targets: list[str] | None = None) -> None:
        """Run `dvc checkout`."""
        ...


class SubprocessDvcOps:
    """Production: shells out to `dvc` commands."""

    def __init__(self, *, timeouts: Timeouts, reporter: Optional[Reporter] = None) -> None:
        self._timeouts = timeouts
        self._reporter = reporter

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
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None

        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if "Does not exist" in stderr or "Unable to find" in stderr:
                raise DvcImportPathNotFound(
                    f"path '{path}' not found at rev '{rev or 'HEAD'}' in '{repo_url}'"
                )
            if "already exists" in stderr or "use --force" in stderr:
                raise DvcImportDestinationExists(
                    f"destination for '{dest}.dvc' already exists; pass force=True"
                )
            raise DvcOpError(
                f"dvc import failed (exit {r.returncode}): {stderr.strip()}"
            )

        return dest.parent / (dest.name + ".dvc")

    def push(self, *, remote: str | None = None, jobs: int | None = None) -> None:
        cmd = ["dvc", "push"]
        if remote:
            cmd.extend(["--remote", remote])
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if r.returncode != 0:
            raise DvcPushError(
                f"dvc push failed (exit {r.returncode}): {''.join(r.stderr_lines).strip()}"
            )

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> None:
        cmd = ["dvc", "pull"]
        if remote:
            cmd.extend(["--remote", remote])
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if r.returncode != 0:
            raise DvcPullError(
                f"dvc pull failed (exit {r.returncode}): {''.join(r.stderr_lines).strip()}"
            )

    def add(self, path: Path) -> Path:
        cmd = ["dvc", "add", str(path)]
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if r.returncode != 0:
            raise DvcAddError(
                f"dvc add failed (exit {r.returncode}): {''.join(r.stderr_lines).strip()}"
            )
        return path.parent / (path.name + ".dvc")

    def status(self, targets: list[str] | None = None) -> dict[str, str]:
        import json

        cmd = ["dvc", "status", "--json"]
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, json_mode=True)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None

        if r.returncode != 0:
            raise DvcStatusError(
                f"dvc status failed (exit {r.returncode}): "
                f"{''.join(r.stderr_lines).strip()}"
            )
        stdout = "".join(r.stdout_lines).strip()
        if not stdout:
            return {}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise DvcStatusError(f"dvc status failed to parse json: {exc}") from exc

        status_map = {}
        for path, status in data.items():
            if isinstance(status, list):
                status_map[path] = status[0]
            elif isinstance(status, dict):
                status_map[path] = next(iter(status.values()))
            else:
                status_map[path] = status
        return status_map

    def remove(self, name: str) -> None:
        cmd = ["dvc", "remove", name]
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if r.returncode != 0:
            raise DvcRemoveError(
                f"dvc remove failed (exit {r.returncode}): {''.join(r.stderr_lines).strip()}"
            )

    def checkout(self, *, targets: list[str] | None = None) -> None:
        cmd = ["dvc", "checkout"]
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter)
        except FileNotFoundError:
            raise DvcNotInstalled("`dvc` binary not found on PATH.") from None
        if r.returncode != 0:
            raise DvcCheckoutError(
                f"dvc checkout failed (exit {r.returncode}): {''.join(r.stderr_lines).strip()}"
            )
