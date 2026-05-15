"""Producer-fetch subprocess seam.

Only this module shells out to `git` for producer-side operations (fetching
`metadata.json` at a pinned commit). Mirrors the single-seam pattern of
`_dvc_ops.py` and `_registry_git_ops.py`.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
import tempfile
from enum import StrEnum
from io import BytesIO
from typing import Literal, Protocol, overload


class FetchError(Exception):
    """Transport-level failure reaching the producer. Raised by Fetcher."""

    class Reason(StrEnum):
        UNREACHABLE = "unreachable"
        PIN_MISSING = "pin_missing"
        METADATA_MISSING = "metadata_missing"

    def __init__(
        self,
        reason: "FetchError.Reason",
        repo: str,
        pin: str,
        detail: str = "",
    ) -> None:
        super().__init__(f"{reason}: repo={repo} pin={pin} detail={detail}")
        self.reason = reason
        self.repo = repo
        self.pin = pin
        self.detail = detail

    @classmethod
    def unreachable(cls, repo: str, pin: str, detail: str = "") -> "FetchError":
        return cls(cls.Reason.UNREACHABLE, repo, pin, detail)

    @classmethod
    def pin_missing(cls, repo: str, pin: str, detail: str = "") -> "FetchError":
        return cls(cls.Reason.PIN_MISSING, repo, pin, detail)

    @classmethod
    def metadata_missing(cls, repo: str, pin: str, detail: str = "") -> "FetchError":
        return cls(cls.Reason.METADATA_MISSING, repo, pin, detail)


class Fetcher(Protocol):
    """Returns the raw bytes of metadata.json at `pin` for `repo`.

    Implementations raise `FetchError` for transport failures.
    """

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes: ...

    def fetch_metadata_at_head(self, repo: str) -> tuple[bytes, str]:
        """Resolve HEAD on the remote, fetch metadata at that SHA, return
        `(metadata_bytes, resolved_head_sha)`. The SHA is recorded by the
        consumer (`dvc import --rev <sha>`), so hiding it would force every
        caller to re-resolve.
        """
        ...


# ---------------------------------------------------------------------------
# Stderr classifiers — substring-based mapping from git error text to typed
# FetchError reasons. _classify_metadata_missing runs *before* _classify_stderr
# so a path-missing error isn't misclassified as a pin-missing one.
# ---------------------------------------------------------------------------


def _classify_metadata_missing(stderr: str) -> bool:
    return any(
        token in stderr
        for token in (
            "did not match any",
            "pathspec 'metadata.json'",
            "exists on disk, but not in",
            "does not exist in",
        )
    )


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _classify_stderr(stderr: str) -> FetchError.Reason | None:
    if any(
        token in stderr
        for token in (
            "Could not resolve revision",
            "Couldn't find remote ref",
            "unknown revision",
        )
    ):
        return FetchError.Reason.PIN_MISSING
    if any(
        token in stderr
        for token in (
            "Authentication failed",
            "Permission denied",
            "Could not resolve host",
            "unable to access",
            "timed out",
            "Connection refused",
        )
    ):
        return FetchError.Reason.UNREACHABLE
    return None


class GitArchiveFetcher:
    """Production Fetcher. Tries `git archive --remote`, falls back to a
    shallow clone if the remote rejects `upload-archive` (GitHub disables
    it by default).
    """

    def __init__(self, *, timeout: float = 60.0) -> None:
        self.timeout = timeout

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes:
        archive_argv = [
            "git",
            "archive",
            "--format=tar",
            "--remote",
            repo,
            pin,
            "metadata.json",
        ]
        result = _run(
            archive_argv,
            timeout=self.timeout,
            repo=repo,
            pin=pin,
            binary_stdout=True,
        )

        if result.returncode == 0:
            return _extract_metadata_bytes(result.stdout, repo=repo, pin=pin)

        stderr_text = result.stderr.decode("utf-8", errors="replace")

        if _classify_metadata_missing(stderr_text):
            raise FetchError.metadata_missing(repo, pin, detail=stderr_text.strip())

        classified = _classify_stderr(stderr_text)
        if classified is not None:
            raise FetchError(classified, repo, pin, detail=stderr_text.strip())

        return self._fallback_clone(repo, pin)

    def fetch_metadata_at_head(self, repo: str) -> tuple[bytes, str]:
        head_sha = _git_ls_remote_head(repo, timeout=self.timeout)
        raw = self.fetch_metadata_at(repo, head_sha)
        return raw, head_sha

    def _fallback_clone(self, repo: str, pin: str) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            self._run_clone(["git", "clone", "--depth=1", "--filter=blob:none", "--no-checkout", repo, tmp], repo=repo, pin=pin)
            self._run_clone(["git", "-C", tmp, "fetch", "--depth=1", "origin", pin], repo=repo, pin=pin)
            show = _run(
                ["git", "-C", tmp, "show", f"{pin}:metadata.json"],
                timeout=self.timeout,
                repo=repo,
                pin=pin,
                binary_stdout=True,
            )
            if show.returncode != 0:
                stderr_text = show.stderr.decode("utf-8", errors="replace")
                if _classify_metadata_missing(stderr_text):
                    raise FetchError.metadata_missing(repo, pin, detail=stderr_text.strip())
                classified = _classify_stderr(stderr_text)
                if classified is not None:
                    raise FetchError(classified, repo, pin, detail=stderr_text.strip())
                raise FetchError.unreachable(repo, pin, detail=stderr_text.strip())
            if not show.stdout:
                raise FetchError.metadata_missing(repo, pin, detail="show returned empty")
            return show.stdout

    def _run_clone(self, argv: list[str], *, repo: str, pin: str) -> None:
        result = _run(
            argv,
            timeout=self.timeout,
            repo=repo,
            pin=pin,
            binary_stdout=False,
        )
        if result.returncode != 0:
            stderr_text = result.stderr
            if _classify_metadata_missing(stderr_text):
                raise FetchError.metadata_missing(repo, pin, detail=stderr_text.strip())
            classified = _classify_stderr(stderr_text)
            if classified is not None:
                raise FetchError(classified, repo, pin, detail=stderr_text.strip())
            raise FetchError.unreachable(repo, pin, detail=stderr_text.strip())


def _extract_metadata_bytes(archive_bytes: bytes, *, repo: str, pin: str) -> bytes:
    try:
        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r|") as tar:
            for member in tar:
                if member.name != "metadata.json":
                    continue
                if member.issym() or member.islnk():
                    raise FetchError.metadata_missing(
                        repo, pin, detail="metadata.json is a symlink/hardlink in archive"
                    )
                handle = tar.extractfile(member)
                if handle is None:
                    raise FetchError.metadata_missing(repo, pin, detail="metadata.json empty in archive")
                data = handle.read()
                if not data:
                    raise FetchError.metadata_missing(repo, pin, detail="metadata.json empty in archive")
                return data
    except tarfile.TarError as e:
        raise FetchError.metadata_missing(repo, pin, detail=f"tar read failed: {e}") from e
    raise FetchError.metadata_missing(repo, pin, detail="metadata.json missing from archive")


@overload
def _run(
    argv: list[str],
    *,
    timeout: float,
    repo: str,
    pin: str,
    binary_stdout: Literal[True],
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def _run(
    argv: list[str],
    *,
    timeout: float,
    repo: str,
    pin: str,
    binary_stdout: Literal[False],
) -> subprocess.CompletedProcess[str]: ...


def _run(
    argv: list[str],
    *,
    timeout: float,
    repo: str,
    pin: str,
    binary_stdout: bool,
) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
    """The single subprocess chokepoint for this module.

    Translates FileNotFoundError and TimeoutExpired into typed FetchError so
    callers never see raw subprocess exceptions.
    """
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=not binary_stdout,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise FetchError.unreachable(repo, pin, detail=f"git not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        subcmd = _git_subcmd(argv)
        raise FetchError.unreachable(
            repo, pin, detail=f"timeout after {timeout}s ({subcmd})"
        ) from e


def _git_ls_remote_head(repo: str, *, timeout: float) -> str:
    """Resolve the remote's HEAD to a 40-char SHA.

    Uses `git ls-remote --symref <repo> HEAD`. Output is one or two lines:
    optionally `ref: refs/heads/<name>\\tHEAD` then `<sha>\\tHEAD`. Parse
    defensively — pick the line whose first whitespace-separated token is a
    40-char hex SHA. Empty / malformed stdout (empty repo, no HEAD ref)
    raises `FetchError.pin_missing`.
    """
    result = _run(
        ["git", "ls-remote", "--symref", repo, "HEAD"],
        timeout=timeout,
        repo=repo,
        pin="HEAD",
        binary_stdout=False,
    )
    if result.returncode != 0:
        stderr_text = result.stderr
        classified = _classify_stderr(stderr_text)
        if classified is not None:
            raise FetchError(classified, repo, "HEAD", detail=stderr_text.strip())
        raise FetchError.unreachable(repo, "HEAD", detail=stderr_text.strip())

    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        if _SHA_RE.match(parts[0]):
            return parts[0]

    raise FetchError.pin_missing(
        repo, "HEAD", detail="no HEAD ref on remote (empty repo?)"
    )


def _git_subcmd(argv: list[str]) -> str:
    """Return the git subcommand from an argv like ['git', '-C', dir, 'fetch', ...]."""
    skip_next = False
    for token in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "-C":
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return argv[0]
