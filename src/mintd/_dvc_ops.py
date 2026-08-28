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
from ._dvc_invoke import dvc_cmd, dvc_env
from ._subprocess import StreamResult, run_streaming


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


class DvcRepoPathError(DvcOpError):
    """The directory a verb was aimed at is missing or is not a directory.

    Its own type because the alternative is a lie. Every verb translates
    `FileNotFoundError` from the spawn into `DvcNotInstalled` ("reinstall
    mintd"), which was sound only while `cwd` was always the process's own
    directory and therefore always existed. Once `cwd` became a parameter,
    `subprocess` began raising the same exception for a bad *working
    directory* — so a user who typo'd `--path` was told their install was
    broken, at exit 2. A typo is not a broken install.
    """

    def __init__(self, cwd: Path) -> None:
        super().__init__(f"not a directory: {cwd}")
        self.hint = f"check the path exists and is a project directory: {cwd}"


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


class DvcDestinationGitIgnored(DvcOpError):
    """dvc refused to write the pointer because its path is git-ignored.

    A `DvcOpError` like every sibling, so existing handlers already render it —
    but NOT a `DvcImportDestinationExists`, which is what it used to be
    reported as. That message ("already exists; remove the directory or pass
    force=True") named two remedies, both inert here, while dvc's own stderr —
    the one thing that says which ignore rule bit — was discarded.
    """


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
    stderr: str, *, op: str, exit_code: int, cwd: Path,
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
    base = cwd
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

    **`cwd` is required on every verb, and that is the point.** dvc resolves
    "which repo am I acting on" from its working directory, so a verb without
    a `cwd` acts on wherever the process happens to be standing. Seven of these
    eight used to work that way: `mintd enclave pull --manifest <path>` cached a
    producer's restricted bytes into whatever repo the user was in, exit 0, and
    `data.py` carried a process-global `os.chdir` to paper over the same hole on
    the clone lane. An optional `cwd: Path | None = None` would rebuild that trap
    with better typing, since ambient cwd stays the default. Required means a
    missed call site is a mypy failure, not a silent wrong-repo write.

    **The argument rule, stated once.** `Path`-typed arguments (`import_`'s
    `dest`, `add`'s `path`) are anchored to the *process* cwd — they are
    absolutized at this seam, so they keep meaning exactly what they mean today
    no matter what `cwd` is passed. `str`-typed arguments (`targets`, `name`) are
    *repo*-relative and are interpreted against `cwd`, which is the fix. Without
    the absolutizing half, passing a `cwd` would silently re-anchor a relative
    `-o` against the new directory: measured against real dvc 3.67.1, that turns
    the enclave's silent wrong-repo write into `stage working dir
    '.../outer/enclave/enclave/downloads/_staging' does not exist`.
    """

    def init(self, *, cwd: Path) -> None:
        """Run `dvc init` (bare, no remote). Tolerant of an already-init repo."""
        ...

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        cwd: Path,
        rev: str | None = None,
        force: bool = False,
        extra_args: list[str] | None = None,
    ) -> Path:
        """Run `dvc import` and return the path of the produced `.dvc` file."""
        ...

    def push(
        self,
        *,
        cwd: Path,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> DvcPushResult:
        """Run `dvc push`; report best-effort pushed count / up-to-date state."""
        ...

    def pull(
        self,
        *,
        cwd: Path,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        """Run `dvc pull`."""
        ...

    def add(self, path: Path, *, cwd: Path) -> Path:
        """Run `dvc add` and return the path of the produced `.dvc` file."""
        ...

    def status(self, targets: list[str] | None = None, *, cwd: Path) -> dict[str, str]:
        """Run `dvc status` and return a status map."""
        ...

    def remove(self, name: str, *, cwd: Path) -> None:
        """Run `dvc remove`."""
        ...

    def checkout(self, *, cwd: Path, targets: list[str] | None = None) -> None:
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

    def _env(self) -> dict[str, str]:
        """Subprocess env for dvc: ``dvc_env()`` plus AWS_PROFILE, so dvc's
        boto3 picks up mintd's [mintd] credentials (no [default] profile
        required in ~/.aws/credentials).

        Always a dict now, never ``None``. It used to inherit the parent env
        unchanged when no profile was configured, which also inherited dvc's
        telemetry default -- see ``dvc_env``.

        Uses ``setdefault`` so an already-exported ``AWS_PROFILE``
        (per-invocation override, SSO session manager like aws-vault) wins
        over mintd's auto-detected default. Standard AWS precedence chain
        is preserved.
        """
        env = dvc_env()
        if self._aws_profile_name:
            env.setdefault("AWS_PROFILE", self._aws_profile_name)
        return env

    def _spawn(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        wall_timeout: float | None,
        json_mode: bool = False,
    ) -> "StreamResult":
        """Run one dvc command in `cwd`, and answer "is dvc even installed?"
        in one place rather than eight.

        The `FileNotFoundError` translation is only sound when `cwd` is known
        good: `subprocess` raises it for a missing working directory exactly
        as readily as for a missing executable. Before `cwd` was a parameter
        that could not happen; now a single mistyped `--path` does it. So the
        directory is checked first, and only then is the exception the binary's.

        The stderr half of the same question lives here too — dvc exits 1 with
        "No module named 'dvc'" when it is missing from mintd's env — because
        splitting one question across two layers is how the two answers drift.
        """
        if not cwd.is_dir():
            raise DvcRepoPathError(cwd)
        try:
            r = run_streaming(
                cmd, wall_timeout=wall_timeout, reporter=self._reporter,
                cwd=cwd, json_mode=json_mode, env=self._env(),
            )
        except FileNotFoundError:
            # cwd was validated above, so this really is the binary.
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.") from None
        if r.returncode != 0 and _is_dvc_module_missing("".join(r.stderr_lines)):
            raise DvcNotInstalled("mintd's bundled dvc is missing — reinstall mintd.")
        return r

    def init(self, *, cwd: Path) -> None:
        cmd = [*dvc_cmd(), "init"]
        r = self._spawn(cmd, wall_timeout=self._timeouts.fast, cwd=cwd)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
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
        cwd: Path,
        rev: str | None = None,
        force: bool = False,
        extra_args: list[str] | None = None,
    ) -> Path:
        # `-o` is absolutized against the PROCESS cwd, not `cwd` -- see the
        # argument rule on `DvcOps`. The return value below stays computed from
        # the original `dest`, so this is invisible to callers.
        cmd: list[str] = [*dvc_cmd(), "import", repo_url, path, "-o", str(dest.absolute())]
        if rev:
            cmd.extend(["--rev", rev])
        if force:
            cmd.append("--force")
        if extra_args:
            cmd.extend(extra_args)

        r = self._spawn(cmd, wall_timeout=self._timeouts.transfer, cwd=cwd)

        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            if "not inside of a DVC repository" in stderr:
                raise DvcNotInRepoError(
                    f"not inside a DVC repository while importing into '{dest}'"
                )
            if "Does not exist" in stderr or "Unable to find" in stderr:
                raise DvcImportPathNotFound(
                    f"path '{path}' not found at rev '{rev or 'HEAD'}' in '{repo_url}'"
                )
            # `is git-ignored` means two different things and the string
            # cannot tell them apart — but the filesystem can. With the dest
            # PRESENT it is the container-nesting refusal below wearing a
            # different first-failing check (dvc wrote a `.gitignore` for the
            # earlier import, and the nested stage file lands under it). With
            # the dest ABSENT nothing is in the way and the ignore rule is the
            # whole story, so "already exists; remove the directory or pass
            # force=True" named two inert remedies and threw away the one fact
            # that would have fixed it.
            if "is git-ignored" in stderr and not dest.exists():
                raise DvcDestinationGitIgnored(
                    f"dvc will not write the pointer for '{dest}': that path "
                    f"is git-ignored. Un-ignore it — a mintd scaffold's "
                    f".gitignore already does, via '!/data/**/*.dvc' — or "
                    f"choose another location with --dest-root. dvc said: "
                    f"{stderr.strip()}"
                )
            if (
                "already exists" in stderr
                or "use --force" in stderr
                # `-o <existing-dir>` makes dvc nest the source basename
                # inside the directory, then refuse the overlap.
                or "overlap and are thus in the same tracked directory" in stderr
                or "is git-ignored" in stderr
            ):
                raise DvcImportDestinationExists(
                    f"destination '{dest}' already exists; remove the "
                    f"directory or pass force=True"
                )
            raise DvcOpError(
                f"dvc import failed (exit {r.returncode}): {stderr.strip()}"
            )

        return dest.parent / (dest.name + ".dvc")

    def push(
        self,
        *,
        cwd: Path,
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
        r = self._spawn(cmd, wall_timeout=self._timeouts.transfer, cwd=cwd, json_mode=True)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            raise DvcPushError(
                f"dvc push failed (exit {r.returncode}): {stderr.strip()}"
            )
        return _parse_push_output("\n".join(r.stdout_lines))

    def pull(
        self,
        *,
        cwd: Path,
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
        r = self._spawn(cmd, wall_timeout=self._timeouts.transfer, cwd=cwd)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            translated = _translate_storage_key_error(
                stderr, op="pull", exit_code=r.returncode, cwd=cwd,
            )
            if translated is not None:
                raise translated
            raise DvcPullError(
                f"dvc pull failed (exit {r.returncode}): {stderr.strip()}"
            )

    def add(self, path: Path, *, cwd: Path) -> Path:
        # Absolutized against the PROCESS cwd; the return value below stays
        # computed from the original `path`. See the rule on `DvcOps`.
        cmd = [*dvc_cmd(), "add", str(path.absolute())]
        r = self._spawn(cmd, wall_timeout=self._timeouts.fast, cwd=cwd)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            raise DvcAddError(
                f"dvc add failed (exit {r.returncode}): {stderr.strip()}"
            )
        return path.parent / (path.name + ".dvc")

    def status(self, targets: list[str] | None = None, *, cwd: Path) -> dict[str, str]:
        import json

        cmd = [*dvc_cmd(), "status", "--json"]
        if targets:
            cmd.extend(targets)
        r = self._spawn(cmd, wall_timeout=self._timeouts.fast, cwd=cwd, json_mode=True)

        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
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

    def remove(self, name: str, *, cwd: Path) -> None:
        cmd = [*dvc_cmd(), "remove", name]
        r = self._spawn(cmd, wall_timeout=self._timeouts.fast, cwd=cwd)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            raise DvcRemoveError(
                f"dvc remove failed (exit {r.returncode}): {stderr.strip()}"
            )

    def checkout(self, *, cwd: Path, targets: list[str] | None = None) -> None:
        cmd = [*dvc_cmd(), "checkout"]
        if targets:
            cmd.extend(targets)
        # transfer tier, NOT fast: checkout
        # materializes cache blobs into the workspace — tens of GB across
        # ~80 targets on a fresh clone. 0.6s on APFS reflink, but minutes
        # of real copying on non-reflink filesystems (the lab's Linux
        # boxes); the 30s fast tier SIGTERM'd dvc mid-materialization.
        r = self._spawn(cmd, wall_timeout=self._timeouts.transfer, cwd=cwd)
        if r.returncode != 0:
            stderr = "".join(r.stderr_lines)
            translated = _translate_storage_key_error(
                stderr, op="checkout", exit_code=r.returncode, cwd=cwd,
            )
            if translated is not None:
                raise translated
            raise DvcCheckoutError(
                f"dvc checkout failed (exit {r.returncode}): {stderr.strip()}"
            )
