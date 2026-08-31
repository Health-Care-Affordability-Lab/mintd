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
from mintd.check import _git_error_summary

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


def test_failed_clone_reports_gits_fatal_line_not_the_progress_blob(
    tmp_path: Path,
) -> None:
    """A cold registry cache is a `clone`, and `run_streaming` captures stderr
    as newline-STRIPPED segments. Joined with `""`, git's five lines became one
    blob — `...does not appear to be a git repositoryfatal: Could not read from
    remote repository.Please make sure you have the correct access rightsand
    the repository exists.` — and `_git_error_summary`'s first-non-blank-line
    rule then returned that whole blob, led by a `Cloning into '<temp cache
    path>'...` progress line naming a directory the user never chose.

    That is what `mintd publish` printed to anyone who followed its own
    recovery hint and deleted the cache. `fetch` never showed it (it goes
    through `_git`/`capture_output`, which keeps real newlines), so the
    warm-cache path looked fine and no fake could see the difference: this
    needs the real `clone`.

    Mutations: `"\\n".join` -> `"".join` in `clone`, or dropping the
    `fatal:` preference in `_git_error_summary` -> this reddens.
    """
    with pytest.raises(GitOpError) as exc:
        SubprocessRegistryGitOps().clone(
            (tmp_path / "nope.git").as_uri(), tmp_path / "cache"
        )

    assert len(exc.value.stderr.splitlines()) > 1
    summary = _git_error_summary(exc.value)
    assert summary.startswith("fatal:")
    assert "Cloning into" not in summary


def test_git_env_pins_the_message_language() -> None:
    """`git_env()` is the whole fix, so pin its two keys and its inheritance.

    Mutation: drop either key from `_git_invoke.git_env` -> this test fails.
    """
    from mintd._git_invoke import git_env

    env = git_env()

    assert env["LC_ALL"] == "C"
    # gettext consults LANGUAGE ahead of LC_ALL, so pinning LC_ALL alone is
    # not enough on a machine that exports both.
    assert env["LANGUAGE"] == ""
    # It is the parent env PLUS those two, not a replacement: a git that
    # cannot see PATH, HOME or SSH_AUTH_SOCK cannot reach a remote at all.
    assert "PATH" in env


def test_git_speaks_english_to_mintd_even_when_the_user_does_not() -> None:
    """The defect, end to end, against the real git binary.

    `check`'s `_git_error_summary` picks git's `fatal:` line out of a failed
    clone's stderr so the user sees the cause rather than the
    `Cloning into '<temp cache path>'...` progress line naming a directory
    they never chose. git translates that word: under a German locale it is
    `Schwerwiegend:`, the prefix never matches, and the summary falls back to
    the progress line -- the exact symptom, reintroduced on any machine whose
    locale is not English, with a green suite everywhere else.

    Skipped rather than xfailed where the locale is unavailable: a runner
    without German generated has git answering in English regardless, so the
    assertion would pass for the wrong reason.

    Mutation: drop `env=git_env()` from `_registry_git_ops._git` (or revert
    `git_env` to `dict(os.environ)`) -> this test sees `Schwerwiegend:`.
    """
    import os
    import subprocess

    from mintd._git_invoke import git_env

    hostile = {**os.environ, "LC_ALL": "de_DE.UTF-8", "LANGUAGE": "de"}
    argv = ["git", "clone", "/nonexistent-repo-for-this-test.git", "/tmp/never-created"]

    speaks_german = subprocess.run(argv, capture_output=True, text=True, env=hostile)
    if "Schwerwiegend" not in speaks_german.stderr:
        pytest.skip("this machine's git has no German locale, so there is nothing to defeat")

    with_pin = subprocess.run(argv, capture_output=True, text=True, env={**hostile, **git_env()})

    first = next(ln for ln in with_pin.stderr.splitlines() if ln.strip())
    assert first.startswith("fatal:"), f"git answered mintd in the user's language: {first!r}"


def test_every_function_that_spawns_git_passes_an_explicit_env() -> None:
    """No git spawn in `src/mintd/` may inherit the ambient environment.

    Twin of `test_every_function_that_spawns_dvc_passes_an_explicit_env`, and
    for a sharper reason: dvc's inherited env leaked telemetry, git's leaks the
    user's LANGUAGE into output mintd then parses. `git_env()` only helps the
    call sites that pass it.

    Mutation: delete `env=git_env()` from any git spawn in `_init_ops.py`,
    `_registry_git_ops.py` or `_producer_git_ops.py`, or weaken one to
    `env=None` -- which is an `env` keyword and still inherits.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "mintd"
    spawners = {"run", "Popen", "run_streaming"}
    offenders: list[str] = []
    checked = 0

    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None)
                )
                if name not in spawners:
                    continue
                # Scanned per CALL on the argv itself, not per function: git is
                # spawned from three modules with no shared chokepoint, and the
                # argv is always a literal list starting with "git".
                argv = node.args[0] if node.args else None
                first = None
                if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                    head = argv.elts[0]
                    if isinstance(head, ast.Constant):
                        first = head.value
                if first != "git":
                    continue
                checked += 1
                env_kw = next((k for k in node.keywords if k.arg == "env"), None)
                value = env_kw.value if env_kw is not None else None
                builder = None
                if isinstance(value, ast.Call):
                    builder = (
                        value.func.attr
                        if isinstance(value.func, ast.Attribute)
                        else getattr(value.func, "id", None)
                    )
                if builder != "git_env":
                    offenders.append(
                        f"{path.name}:{node.lineno} in {fn.name}() — "
                        f"env={ast.unparse(value) if value is not None else 'ABSENT'}"
                    )

    assert offenders == [], f"git spawned with an inherited env: {offenders}"
    # Guard the scanner itself: a matcher that finds nothing passes vacuously.
    # 4 = `_init_ops.py`'s git_init / git_add / git_rm_cached / git_origin_url.
    # The other four sites build their argv into a local first (`cmd`, `argv`)
    # and are covered by the literal-argv sites plus mypy, not by this count;
    # a FALLING number means a spawn moved somewhere this scanner cannot see.
    assert checked == 4, f"literal-argv git spawn sites moved: {checked}"
