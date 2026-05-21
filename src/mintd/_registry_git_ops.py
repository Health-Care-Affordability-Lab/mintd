"""The only module in mintd that shells out to `git` or `gh`.

Every other module that needs to interact with the registry repo (the cache,
GitCatalogClient) goes through the `RegistryGitOps` Protocol. Production code
uses `SubprocessRegistryGitOps`; tests inject a fake.

This is the single seam that:
  - Normalizes subprocess failures into typed exceptions.
  - Lets us swap implementations without touching callers.
  - Bounds the surface area of code that depends on `git` / `gh` being on PATH.

If you ever find yourself reaching for `subprocess` outside this module, stop.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class GitOpError(Exception):
    """A `git` invocation failed (non-zero exit). Carries stderr for diagnostics."""

    def __init__(self, command: list[str], stderr: str) -> None:
        super().__init__(f"git command failed: {' '.join(command)}\n{stderr}")
        self.command = command
        self.stderr = stderr


class GitTagError(GitOpError):
    """`git tag` failed."""


class GitTagAlreadyExists(GitTagError):
    """The tag already exists."""

    def __init__(self, name: str, work_dir: str) -> None:
        super().__init__(["git", "tag", name], f"tag {name} already exists")
        self.name = name
        self.work_dir = work_dir


class GhAuthError(Exception):
    """`gh` reports the user is not authenticated. Caller should prompt
    `gh auth login` and retry."""


class GhNotInstalled(Exception):
    """The `gh` binary is not on PATH."""


class PRConflictError(Exception):
    """`gh pr create` reports an existing PR on the same branch."""

    def __init__(self, branch: str, existing_pr: int | None = None) -> None:
        super().__init__(
            f"PR already exists for branch {branch!r}"
            + (f" (#{existing_pr})" if existing_pr else "")
        )
        self.branch = branch
        self.existing_pr = existing_pr


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class RegistryGitOps(Protocol):
    """Every git/gh subprocess in mintd goes through one of these methods.

    Implementations must:
      - Raise `GitOpError` / `GhAuthError` / `GhNotInstalled` / `PRConflictError`
        on the expected failure modes — no bare `CalledProcessError` leaks.
      - Be re-entrant (clone-then-fetch-then-reset is a normal sequence).
      - Not maintain hidden state outside the filesystem under `repo_dir`.
    """

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
    ) -> None: ...
    def fetch(self, repo_dir: Path) -> None: ...
    def reset_hard(self, repo_dir: Path, ref: str) -> None: ...
    def checkout(self, repo_dir: Path, ref: str, *, force: bool = False) -> None: ...
    def checkout_new_branch(self, repo_dir: Path, branch: str) -> None: ...
    def commit_all(self, repo_dir: Path, message: str) -> None: ...
    def push_branch(self, repo_dir: Path, branch: str) -> None: ...
    def tag(self, work_dir: Path, name: str, message: str) -> None: ...
    def is_working_tree_clean(self, work_dir: Path) -> bool: ...
    def current_commit(self, work_dir: Path) -> str: ...
    def open_pr(
        self,
        repo_dir: Path,
        *,
        title: str,
        body: str,
        base: str = "main",
        head: str | None = None,
    ) -> int: ...
    def pr_exists_for_branch(self, repo_dir: Path, branch: str) -> int | None: ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class SubprocessRegistryGitOps:
    """Production: shells out to `git` and `gh`.

    Each method runs a single command and surfaces non-zero exits as the typed
    exceptions defined above. Stdout/stderr capture is on by default for both
    diagnostics (in errors) and parsing (for `gh pr list`).
    """

    def __init__(
        self,
        *,
        timeouts: Any = None,
        reporter: Any = None,
    ) -> None:
        self._timeouts = timeouts
        self._reporter = reporter

    @property
    def _fast_timeout(self) -> float | None:
        """Wall-clock cap for fast git/gh ops (fetch, commit, tag, gh pr list).
        Falls back to a sensible 30s when no Timeouts is supplied."""
        return self._timeouts.fast if self._timeouts is not None else 30.0

    # ------------------------------------------------------------------
    # git
    # ------------------------------------------------------------------

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
    ) -> None:
        # Slice 25: clone uses run_streaming so git's --progress output
        # reaches the terminal live. run_streaming reads chunks (not lines)
        # so \r-based progress overwrites correctly in place — single
        # updating line per phase, not a wall of text.
        # http.lowSpeedTime gives git its own dead-transfer abort.
        from ._subprocess import run_streaming

        argv: list[str] = [
            "git",
            "-c", "http.lowSpeedLimit=1000",
            "-c", "http.lowSpeedTime=300",
            "clone", "--progress",
        ]
        if shallow:
            argv.append("--depth=1")
        if branch:
            argv.extend(["--branch", branch])
        argv.extend([url, str(dest)])

        # Pick wall_timeout from the structured Timeouts object if present;
        # WallTimeoutExceeded propagates unwrapped per slice 25 spec.
        wall_timeout = (
            self._timeouts.transfer if self._timeouts is not None else None
        )
        try:
            r = run_streaming(
                argv,
                wall_timeout=wall_timeout,
                reporter=self._reporter,
            )
        except FileNotFoundError as e:
            raise GitOpError(argv, "git not installed") from e
        if r.returncode != 0:
            raise GitOpError(argv, "".join(r.stderr_lines) or "")

    def fetch(self, repo_dir: Path) -> None:
        self._git(["fetch", "origin"], cwd=repo_dir)

    def reset_hard(self, repo_dir: Path, ref: str) -> None:
        self._git(["reset", "--hard", ref], cwd=repo_dir)

    def checkout(self, repo_dir: Path, ref: str, *, force: bool = False) -> None:
        args = ["checkout"]
        if force:
            args.append("-f")
        args.append(ref)
        self._git(args, cwd=repo_dir)

    def checkout_new_branch(self, repo_dir: Path, branch: str) -> None:
        # Use ``-B`` (force-create-or-reset) so retries from a stuck
        # ``register/<name>`` state don't crash with "branch already
        # exists". Safe because the caller has just reset to a clean
        # origin/main baseline.
        self._git(["checkout", "-B", branch], cwd=repo_dir)

    def commit_all(self, repo_dir: Path, message: str) -> None:
        self._git(["add", "-A"], cwd=repo_dir)
        self._git(["commit", "-m", message], cwd=repo_dir)

    def push_branch(self, repo_dir: Path, branch: str) -> None:
        self._git(["push", "-u", "origin", branch], cwd=repo_dir)

    def tag(self, work_dir: Path, name: str, message: str) -> None:
        try:
            self._git(["tag", "-a", name, "-m", message], cwd=work_dir)
        except GitOpError as e:
            if "already exists" in e.stderr:
                raise GitTagAlreadyExists(name, str(work_dir)) from e
            raise GitTagError(e.command, e.stderr) from e

    def is_working_tree_clean(self, work_dir: Path) -> bool:
        stdout = self._git(["status", "--porcelain"], cwd=work_dir)
        return stdout.strip() == ""

    def current_commit(self, work_dir: Path) -> str:
        return self._git(["rev-parse", "--short=7", "HEAD"], cwd=work_dir).strip()

    # ------------------------------------------------------------------
    # gh
    # ------------------------------------------------------------------

    def open_pr(
        self,
        repo_dir: Path,
        *,
        title: str,
        body: str,
        base: str = "main",
        head: str | None = None,
    ) -> int:
        # Pass ``--head <branch>`` explicitly when known. gh CLI's
        # auto-detect uses the current branch's upstream tracking, which
        # can be missing (e.g. when push -u set tracking but a different
        # remote is what gh resolves the repo to). Surfacing head
        # explicitly removes the ambiguity that caused the
        # "must first push the current branch" error.
        args = ["pr", "create", "--title", title, "--body", body, "--base", base]
        if head is not None:
            args.extend(["--head", head])
        result = self._gh(args, cwd=repo_dir)
        # `gh pr create` prints the PR URL on success. Extract the trailing integer.
        url = result.strip().splitlines()[-1]
        try:
            return int(url.rsplit("/", 1)[-1])
        except (IndexError, ValueError) as e:
            raise GitOpError(["gh", "pr", "create"], f"could not parse PR number from: {url}") from e

    def pr_exists_for_branch(self, repo_dir: Path, branch: str) -> int | None:
        result = self._gh(
            ["pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            cwd=repo_dir,
        )
        items = json.loads(result.strip() or "[]")
        if not items:
            return None
        return int(items[0]["number"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(self, args: list[str], *, cwd: Path | None) -> str:
        cmd = ["git", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                text=True,
                timeout=self._fast_timeout,
                check=True,
            )
        except FileNotFoundError as e:
            raise GitOpError(cmd, "git not installed") from e
        except subprocess.CalledProcessError as e:
            raise GitOpError(cmd, e.stderr or e.stdout or "")
        return result.stdout

    def _gh(self, args: list[str], *, cwd: Path) -> str:
        cmd = ["gh", *args]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._fast_timeout,
                check=True,
            )
        except FileNotFoundError as e:
            raise GhNotInstalled(str(e)) from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "") + (e.stdout or "")
            if "not authenticated" in stderr.lower() or "authentication" in stderr.lower():
                raise GhAuthError(stderr) from e
            if "already exists" in stderr.lower():
                raise PRConflictError(branch="(unknown)") from e
            raise GitOpError(cmd, stderr)
        return result.stdout
