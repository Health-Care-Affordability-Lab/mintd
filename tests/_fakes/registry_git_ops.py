"""Fake `RegistryGitOps` for tests.

Strategy: real local `git` for everything that's fast and authentic on the
local machine (clone, fetch, reset, checkout, commit, push). Stubs only the
`gh` operations (`open_pr`, `pr_exists_for_branch`).

A test fixture sets up a local bare repo as the "remote", and `push_branch`
pushes for real into that bare repo. `open_pr`:

  1. Records the PR (number, branch, title, body).
  2. **Simulates an immediate merge** by fast-forwarding the bare remote's
     `main` to the branch's tip. Production has a human reviewer + merge;
     tests want read-after-write semantics.

This means: after `client.register(m)` against a fake-backed
`GitCatalogClient`, a subsequent `client.fetch(m.project.name)` succeeds
(the entry is on main). In production, the entry would not be visible
until the human merged the PR.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakePR:
    number: int
    branch: str
    title: str
    body: str
    base: str


@dataclass
class _FakeRegistryGitOps:
    """Real local-git + stubbed gh. See module docstring."""

    open_prs: dict[str, FakePR] = field(default_factory=dict)            # branch -> PR
    _next_pr: int = 100

    # ------------------------------------------------------------------
    # git (real)
    # ------------------------------------------------------------------

    def clone(self, url: str, dest: Path) -> None:
        self._git(["clone", url, str(dest)], cwd=None)

    def fetch(self, repo_dir: Path) -> None:
        self._git(["fetch", "origin"], cwd=repo_dir)

    def reset_hard(self, repo_dir: Path, ref: str) -> None:
        self._git(["reset", "--hard", ref], cwd=repo_dir)

    def checkout_new_branch(self, repo_dir: Path, branch: str) -> None:
        self._git(["checkout", "-b", branch], cwd=repo_dir)

    def commit_all(self, repo_dir: Path, message: str) -> None:
        self._git(["add", "-A"], cwd=repo_dir)
        # Configure local user.email/user.name so commits don't require a global config.
        self._git(["-c", "user.email=test@mintd", "-c", "user.name=test", "commit", "-m", message],
                  cwd=repo_dir)

    def push_branch(self, repo_dir: Path, branch: str) -> None:
        self._git(["push", "-u", "origin", branch], cwd=repo_dir)

    # ------------------------------------------------------------------
    # gh (stubbed)
    # ------------------------------------------------------------------

    def open_pr(self, repo_dir: Path, *, title: str, body: str, base: str = "main") -> int:
        # Branch is the currently checked-out one.
        branch = self._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir).strip()
        if branch in self.open_prs:
            from mintd._registry_git_ops import PRConflictError
            raise PRConflictError(branch=branch, existing_pr=self.open_prs[branch].number)
        pr = FakePR(number=self._next_pr, branch=branch, title=title, body=body, base=base)
        self.open_prs[branch] = pr
        self._next_pr += 1
        # Auto-merge: fast-forward the remote's `base` to this branch's tip
        # so subsequent `ensure_fresh()` calls see the new content.
        self._git(["push", "origin", f"{branch}:{base}"], cwd=repo_dir)
        return pr.number

    def pr_exists_for_branch(self, repo_dir: Path, branch: str) -> int | None:
        pr = self.open_prs.get(branch)
        return pr.number if pr else None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(self, args: list[str], *, cwd: Path | None) -> str:
        cmd = ["git", *args]
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
