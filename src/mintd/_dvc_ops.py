"""DVC subprocess seam.

Only this module shells out to `dvc`. Mirrors `_registry_git_ops.py` for git/gh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

from ._config import Timeouts
from ._console import Reporter
from ._dvc_invoke import dvc_cmd
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


def _is_dvc_module_missing(stderr: str) -> bool:
    """`sys.executable -m dvc` exits 1 with this message when dvc isn't
    in mintd's env. We re-raise as DvcNotInstalled so users get the
    reinstall hint instead of a confusing operation-specific error."""
    return "No module named 'dvc'" in stderr or "No module named dvc" in stderr


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
        extra_args: list[str] | None = None,
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
        extra_args: list[str] | None = None,
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

    def __init__(
        self,
        *,
        timeouts: Timeouts,
        reporter: Optional[Reporter] = None,
        aws_profile_name: Optional[str] = None,
    ) -> None:
        self._timeouts = timeouts
        self._reporter = reporter
        self._aws_profile_name = aws_profile_name

    def _env(self) -> Optional[dict[str, str]]:
        """Subprocess env with AWS_PROFILE injected so dvc's boto3 picks
        up mintd's [mintd] credentials (no [default] profile required in
        ~/.aws/credentials). None means inherit parent env unchanged.

        Uses ``setdefault`` so an already-exported ``AWS_PROFILE``
        (per-invocation override, SSO session manager like aws-vault) wins
        over mintd's auto-detected default. Standard AWS precedence chain
        is preserved.
        """
        if not self._aws_profile_name:
            return None
        import os
        env = dict(os.environ)
        env.setdefault("AWS_PROFILE", self._aws_profile_name)
        return env

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        rev: str | None = None,
        force: bool = False,
        extra_args: list[str] | None = None,
    ) -> Path:
        cmd: list[str] = [*dvc_cmd(), "import", repo_url, path, "-o", str(dest)]
        if rev:
            cmd.extend(["--rev", rev])
        if force:
            cmd.append("--force")
        if extra_args:
            cmd.extend(extra_args)

        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None

        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
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
        cmd = [*dvc_cmd(), "push"]
        if remote:
            cmd.extend(["--remote", remote])
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcPushError(
                f"dvc push failed (exit {r.returncode}): {stderr.strip()}"
            )

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        cmd = [*dvc_cmd(), "pull"]
        if remote:
            cmd.extend(["--remote", remote])
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        if extra_args:
            cmd.extend(extra_args)
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcPullError(
                f"dvc pull failed (exit {r.returncode}): {stderr.strip()}"
            )

    def add(self, path: Path) -> Path:
        cmd = [*dvc_cmd(), "add", str(path)]
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcAddError(
                f"dvc add failed (exit {r.returncode}): {stderr.strip()}"
            )
        return path.parent / (path.name + ".dvc")

    def status(self, targets: list[str] | None = None) -> dict[str, str]:
        import json

        cmd = [*dvc_cmd(), "status", "--json"]
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, json_mode=True, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None

        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcStatusError(
                f"dvc status failed (exit {r.returncode}): {stderr.strip()}"
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
        cmd = [*dvc_cmd(), "remove", name]
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcRemoveError(
                f"dvc remove failed (exit {r.returncode}): {stderr.strip()}"
            )

    def checkout(self, *, targets: list[str] | None = None) -> None:
        cmd = [*dvc_cmd(), "checkout"]
        if targets:
            cmd.extend(targets)
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcCheckoutError(
                f"dvc checkout failed (exit {r.returncode}): {stderr.strip()}"
            )
