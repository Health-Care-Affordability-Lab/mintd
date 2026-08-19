"""Tests for `SubprocessRegistryGitOps` against real local git — no `gh`.

These exist because `_FakeRegistryGitOps` clones WITHOUT `--depth`, so every
fake-backed test would still pass if `checkout_remote_branch` dropped its
explicit refspec and went back to a plain `fetch origin`. The registry cache is
a `--depth=1` clone (which implies `--single-branch`), and that blindness to
remote branches is exactly what made `mintd publish` crash on a second publish.

Two rules for every clone here:
  - Build the URL with `Path.as_uri()`. `--depth` is silently ignored for
    plain-path local clones, so the URL form is load-bearing — and an f-string
    over a `WindowsPath` yields `file://C:\\...`, which git rejects (the
    windows-test CI job runs this file).
  - Set `user.email` / `user.name` in the clone: production `commit_all`
    injects no identity, unlike the fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mintd._registry_git_ops import (
    GitOpError,
    PRConflictError,
    RegistryBranchExists,
    SubprocessRegistryGitOps,
)

from tests._harness.git import _git


def _seed_update_branch(remote: Path, work: Path, *, with_pending: bool) -> None:
    """Push an `update/x` branch to `remote`, then advance `main` past it so
    the two histories genuinely diverge (the production shape: an open catalog
    PR while other entries keep landing on main)."""
    seed = work / "seed"
    _git(["clone", str(remote), str(seed)])
    _git(["checkout", "-b", "update/x"], cwd=seed)
    (seed / "entry.yaml").write_text("v1\n", encoding="utf-8")
    if with_pending:
        (seed / ".mintd_pending.json").write_text("remote-copy\n", encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "Update x"], cwd=seed)
    _git(["push", "origin", "update/x"], cwd=seed)

    _git(["checkout", "main"], cwd=seed)
    (seed / "moved.txt").write_text("main moved on\n", encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "unrelated entry"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)


def _shallow_cache(remote: Path, dest: Path) -> Path:
    _git(["clone", "--depth=1", remote.as_uri(), str(dest)])
    _git(["config", "user.email", "test@mintd"], cwd=dest)
    _git(["config", "user.name", "test"], cwd=dest)
    return dest


def test_checkout_remote_branch_sees_branch_in_shallow_clone(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """The load-bearing one: a `--depth=1` clone cannot see `update/x` via a
    plain `fetch origin`, and the explicit refspec is what makes the
    subsequent push a fast-forward instead of a rejection."""
    _seed_update_branch(remote_registry_empty, tmp_path, with_pending=False)
    cache = _shallow_cache(remote_registry_empty, tmp_path / "cache")
    ops = SubprocessRegistryGitOps()

    ops.fetch(cache)
    assert "origin/update/x" not in _git(["branch", "-r"], cwd=cache)

    ops.checkout_remote_branch(cache, "update/x")
    assert (cache / "entry.yaml").read_text(encoding="utf-8") == "v1\n"

    (cache / "entry.yaml").write_text("v2\n", encoding="utf-8")
    ops.commit_all(cache, "Update x")
    ops.push_branch(cache, "update/x")  # fast-forwards; must not raise

    assert "v2" in _git(
        ["--git-dir=" + str(remote_registry_empty), "show", "update/x:entry.yaml"],
    )


def test_checkout_remote_branch_overwrites_untracked_pending_file(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """`.mintd_pending.json` is untracked in the cache but tracked on the
    branch. `checkout -B <branch> FETCH_HEAD` aborts in exactly this state
    ("would be overwritten by checkout"); `reset --hard` clobbers it."""
    _seed_update_branch(remote_registry_empty, tmp_path, with_pending=True)
    cache = _shallow_cache(remote_registry_empty, tmp_path / "cache")
    (cache / ".mintd_pending.json").write_text("local-copy\n", encoding="utf-8")

    SubprocessRegistryGitOps().checkout_remote_branch(cache, "update/x")

    assert (cache / ".mintd_pending.json").read_text(encoding="utf-8") == "remote-copy\n"


def test_push_branch_raises_registry_branch_exists_on_non_fast_forward(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """Shallow clones say "(fetch first)" where full clones say
    "(non-fast-forward)". Both must retype."""
    _seed_update_branch(remote_registry_empty, tmp_path, with_pending=False)
    cache = _shallow_cache(remote_registry_empty, tmp_path / "cache")
    ops = SubprocessRegistryGitOps()

    # Branch off main, the way the unfixed code did — a rival history.
    ops.checkout_new_branch(cache, "update/x")
    (cache / "entry.yaml").write_text("rival\n", encoding="utf-8")
    ops.commit_all(cache, "Update x")

    with pytest.raises(RegistryBranchExists) as exc:
        ops.push_branch(cache, "update/x")
    assert exc.value.branch == "update/x"
    assert isinstance(exc.value, GitOpError)


def test_push_branch_leaves_other_failures_as_plain_git_op_error(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """The retype is deliberately narrow — a push to a remote that isn't
    there is not a branch collision."""
    cache = _shallow_cache(remote_registry_empty, tmp_path / "cache")
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=cache)
    ops = SubprocessRegistryGitOps()
    ops.checkout_new_branch(cache, "update/x")

    with pytest.raises(GitOpError) as exc:
        ops.push_branch(cache, "update/x")
    assert not isinstance(exc.value, RegistryBranchExists)


def test_open_pr_conflict_carries_real_branch(tmp_path: Path) -> None:
    """`_gh` is generic and raises with a "(unknown)" placeholder; `open_pr`
    knows the branch and must re-raise with it."""

    class _ConflictingOps(SubprocessRegistryGitOps):
        def _gh(self, args: list[str], *, cwd: Path) -> str:
            raise PRConflictError(branch="(unknown)")

    with pytest.raises(PRConflictError) as exc:
        _ConflictingOps().open_pr(tmp_path, title="t", body="b", head="update/x")
    assert exc.value.branch == "update/x"


@pytest.mark.parametrize("call", [
    lambda ops, d: ops.remote_branch_exists(d, "update/x"),
    lambda ops, d: ops.pr_exists_for_branch(d, "update/x"),
])
def test_timeouts_surface_as_git_op_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call,
) -> None:
    """`_git`/`_gh` run under a wall timeout. `subprocess.TimeoutExpired` is
    neither FileNotFoundError nor CalledProcessError, so without an explicit
    arm it escapes untyped — a raw traceback out of publish step 5, after the
    DVC push, the version commit, and the tag have all landed."""
    def _timeout(*a: object, **kw: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=30.0)

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(GitOpError) as exc:
        call(SubprocessRegistryGitOps(), tmp_path)
    assert "timed out" in exc.value.stderr
