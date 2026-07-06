"""DVC subprocess seam.

Only this module shells out to `dvc`. Mirrors `_registry_git_ops.py` for git/gh.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from ._config import Timeouts
from ._console import Reporter
from ._dvc_invoke import dvc_cmd
from ._subprocess import run_streaming


class DvcOpError(Exception):
    """Generic non-zero exit from a `dvc` invocation.

    ``hint`` is an optional actionable recovery command for the CLI's error
    renderer; ``None`` on every subclass except the ones that populate it
    (currently ``DvcStorageKeyError``).
    """

    hint: str | None = None


def pull_retry_hint(target: str | None) -> str:
    """The canonical targeted-retry hint for a target mintd could not pull.

    One composition site so a wording change (or a future flag the retry
    must carry) doesn't need coordinated edits across the error surfaces.
    ``None`` means the owning ``.dvc`` target could not be resolved.
    """
    if target is None:
        return "retry the .dvc target that tracks this path: mintd data pull <target>.dvc"
    return f"retry just this target: mintd data pull {target}"


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


class DvcStorageKeyError(DvcOpError):
    """dvc crashed with dvc_data's opaque StorageKeyError tuple.

    Raw stderr looks like ``ERROR: unexpected error - ('data', 'final',
    'aha_ccn_xw', 'crosswalk_aha_pos.dta')`` (exit 255): the tuple is the
    path components of a workspace file dvc's checkout phase could not map
    to a cache entry — the rehash-on-pull pathology plain `dvc pull` hits
    on version-aware remotes (see the fallback-scope comments in
    data_ops.py). Carries the owning ``.dvc`` target when it can be found
    on disk plus a targeted mintd retry ``hint`` so the CLI renders an
    actionable error instead of the bare tuple.
    """

    def __init__(self, message: str, *, target: str | None, hint: str) -> None:
        super().__init__(message)
        self.target = target
        self.hint = hint


class DvcNotInRepoError(DvcOpError):
    """A `dvc` command ran outside a DVC repository (no `.dvc/` scaffold).

    Distinct from a pin/repo problem: the consumer-side fix is `dvc init`
    (which `enclave_pull` now does lazily), not checking the producer's pin.
    """


class DvcImportPathNotFound(DvcOpError):
    """`dvc import` reports the requested path doesn't exist at the given rev."""


class DvcImportDestinationExists(DvcOpError):
    """`dvc import` refused because the destination `.dvc` already exists.

    The consumer-side fix is to remove it or pass `force=True` (which maps to
    `dvc import --force`).
    """


@dataclass
class DvcPushResult:
    """What `dvc push` reported, best-effort.

    `dvc push` has no `--json` mode (unlike `dvc status`), so the count is
    scraped from its human summary line (`N file(s) pushed` /
    `Everything is up to date.`). When that line can't be parsed across dvc
    versions, `pushed` stays `None` and the caller still succeeds. `bytes` is
    never reported by `dvc push`; it exists for summary symmetry and stays
    `None`.
    """

    pushed: int | None = None
    bytes: int | None = None
    up_to_date: bool = False


def _parse_push_output(stdout: str) -> DvcPushResult:
    """Best-effort scrape of `dvc push`'s human summary.

    dvc emits `Everything is up to date.` when there's nothing to upload, or
    `N file(s) pushed` after a real transfer. Never raises: unrecognized
    output yields `pushed=None`, and the caller still reports success.
    """
    import re

    if "Everything is up to date." in stdout:
        return DvcPushResult(pushed=0, up_to_date=True)
    m = re.search(r"(\d+)\s+files?\s+pushed", stdout)
    if m:
        n = int(m.group(1))
        return DvcPushResult(pushed=n, up_to_date=(n == 0))
    return DvcPushResult(pushed=None)


# dvc renders an uncaught dvc_data StorageKeyError as
# "ERROR: unexpected error - ('data', 'final', ..., 'file.dta')".
_STORAGE_KEY_TUPLE_RE = re.compile(r"unexpected error\s*-\s*(\([^()]*\))")


def _translate_storage_key_error(
    stderr: str, *, op: str, exit_code: int, cwd: Path | None = None,
) -> DvcStorageKeyError | None:
    """Translate dvc's StorageKeyError tuple crash into an actionable error.

    The tuple's elements are the path components of the workspace file dvc's
    checkout phase failed on. Join them back into a path, then walk prefixes
    (longest first, relative to ``cwd`` — the dir the dvc subprocess ran in)
    looking for the owning ``<prefix>.dvc`` target so the user gets a
    concrete `mintd data pull <target>` recovery command. Returns ``None``
    when stderr carries no such tuple (caller raises its generic error).
    """
    m = _STORAGE_KEY_TUPLE_RE.search(stderr)
    if not m:
        return None
    try:
        parts = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None
    if not (
        isinstance(parts, tuple)
        and parts
        and all(isinstance(p, str) for p in parts)
    ):
        return None
    rel = "/".join(parts)
    base = cwd if cwd is not None else Path.cwd()
    target: str | None = None
    for i in range(len(parts), 0, -1):
        candidate = "/".join(parts[:i])
        try:
            if (base / f"{candidate}.dvc").is_file():
                target = f"{candidate}.dvc"
                break
        except OSError:
            break
    if target is not None:
        return DvcStorageKeyError(
            f"dvc {op} failed (exit {exit_code}): storage key error on "
            f"'{rel}' (target {target}) — plain dvc cannot serve this "
            "version-aware output",
            target=target,
            hint=pull_retry_hint(target),
        )
    return DvcStorageKeyError(
        f"dvc {op} failed (exit {exit_code}): storage key error on '{rel}'",
        target=None,
        hint=pull_retry_hint(None),
    )


def _is_dvc_module_missing(stderr: str) -> bool:
    """`sys.executable -m dvc` exits 1 with this message when dvc isn't
    in mintd's env. We re-raise as DvcNotInstalled so users get the
    reinstall hint instead of a confusing operation-specific error."""
    return "No module named 'dvc'" in stderr or "No module named dvc" in stderr


class DvcOps(Protocol):
    """Surface used by the rest of mintd to talk to dvc.

    Tests pass a fake; production passes `SubprocessDvcOps`.
    """

    def init(self, *, cwd: Path | None = None) -> None:
        """Run `dvc init` (bare, no remote). Tolerant of an already-init repo."""
        ...

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

    def push(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> DvcPushResult:
        """Run `dvc push`; report best-effort pushed count / up-to-date state."""
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

    def init(self, *, cwd: Path | None = None) -> None:
        cmd = [*dvc_cmd(), "init"]
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.fast, reporter=self._reporter, cwd=cwd, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            # Tolerate an already-initialized repo so callers can init
            # unconditionally and repeated pulls stay idempotent. `dvc init`
            # exits non-zero with "'.dvc' exists. Use `-f` to force." in that case.
            if ".dvc' exists" in stderr or "already initialized" in stderr:
                return
            raise DvcOpError(
                f"dvc init failed (exit {r.returncode}): {stderr.strip()}"
            )

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
            if "not inside of a DVC repository" in stderr:
                raise DvcNotInRepoError(
                    f"not inside a DVC repository while importing into '{dest}'"
                )
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

    def push(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> DvcPushResult:
        cmd = [*dvc_cmd(), "push"]
        if remote:
            cmd.extend(["--remote", remote])
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        if targets:
            cmd.extend(targets)
        # json_mode suppresses dvc's stdout summary token ("1 file pushed" /
        # "Everything is up to date.") from leaking to the terminal/stdout —
        # we render our own summary instead, and JSON consumers must not see
        # it. Live transfer progress is on stderr and is unaffected, so the
        # spinner still ticks during the upload.
        try:
            r = run_streaming(
                cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter,
                json_mode=True, env=self._env(),
            )
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            raise DvcPushError(
                f"dvc push failed (exit {r.returncode}): {stderr.strip()}"
            )
        return _parse_push_output("\n".join(r.stdout_lines))

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
            translated = _translate_storage_key_error(
                stderr, op="pull", exit_code=r.returncode,
            )
            if translated is not None:
                raise translated
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
        # transfer tier, NOT fast: checkout
        # materializes cache blobs into the workspace — tens of GB across
        # ~80 targets on a fresh clone. 0.6s on APFS reflink, but minutes
        # of real copying on non-reflink filesystems (the lab's Linux
        # boxes); the 30s fast tier SIGTERM'd dvc mid-materialization.
        try:
            r = run_streaming(cmd, wall_timeout=self._timeouts.transfer, reporter=self._reporter, env=self._env())
        except FileNotFoundError:
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if _is_dvc_module_missing(stderr):
                raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
            translated = _translate_storage_key_error(
                stderr, op="checkout", exit_code=r.returncode,
            )
            if translated is not None:
                raise translated
            raise DvcCheckoutError(
                f"dvc checkout failed (exit {r.returncode}): {stderr.strip()}"
            )
