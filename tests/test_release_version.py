"""scripts/release_version.py chooses a release run's version, or says why it makes none.

Each test runs the script as release.yml's `version` job does, in a real git repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_version.py"


def _repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """main: v1.9.0, then v1.10.0, then one more commit (HEAD); hotfix/v1.9.0: post1, then one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    shas = {}
    for name, tag in (("v1.9.0", "v1.9.0"), ("v1.10.0", "v1.10.0"), ("main", None)):
        git("commit", "-q", "--allow-empty", "-m", name)
        shas[name] = git("rev-parse", "HEAD")
        if tag:
            git("tag", "-a", tag, "-m", tag)  # annotated, as the old tag job made them
    git("switch", "-q", "-c", "hotfix/v1.9.0", "v1.9.0")
    git("commit", "-q", "--allow-empty", "-m", "the first fix")
    git("tag", "v1.9.0.post1")
    shas["post1"] = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "the second fix")
    shas["hotfix"] = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    return repo, shas


def _version(repo: Path, ref: str, sha: str, bump: str = "patch") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), ref, sha, bump], cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize(("bump", "want"), [("patch", "v1.10.1"), ("minor", "v1.11.0")])
def test_main_takes_the_next_version_after_the_latest_tag(tmp_path: Path, bump: str, want: str) -> None:
    """v1.10.0 is after v1.9.0 (numbers, not text), and hotfix tags don't count on main.
    Mutation: sort the tags as text -> v1.9.1, red."""
    repo, shas = _repo(tmp_path)
    result = _version(repo, "refs/heads/main", shas["main"], bump)
    assert (result.returncode, result.stdout) == (0, f"{want}\n"), result.stderr


def test_a_hotfix_branch_takes_the_next_post_number(tmp_path: Path) -> None:
    """Mutation: start from the line's release instead of its latest .postN -> v1.9.0.post1, red."""
    repo, shas = _repo(tmp_path)
    result = _version(repo, "refs/heads/hotfix/v1.9.0", shas["hotfix"], "minor")  # bump is ignored there
    assert (result.returncode, result.stdout) == (0, "v1.9.0.post2\n"), result.stderr


@pytest.mark.parametrize(("ref", "sha", "why"), [
    pytest.param("refs/heads/main", "v1.10.0", "is already released as v1.10.0", id="a-released-commit"),
    pytest.param("refs/heads/main", "v1.9.0", "doesn't come after v1.10.0", id="an-older-commit"),
    pytest.param("refs/heads/hotfix/v1.9.0", "post1", "is already released as v1.9.0.post1", id="a-released-hotfix"),
    pytest.param("refs/heads/feature", "main", "a release runs on main or a hotfix/vX.Y.Z branch", id="another-branch"),
    pytest.param("refs/tags/v1.10.0", "main", "a release runs on main or a hotfix/vX.Y.Z branch", id="a-tag"),
    pytest.param("refs/heads/hotfix/v2.0.0", "main", "which isn't a tag", id="a-hotfix-for-no-release"),
])
def test_a_run_that_would_misnumber_a_commit_makes_no_release(tmp_path: Path, ref: str, sha: str, why: str) -> None:
    """Mutations: drop the ancestry check (an older commit, e.g. "Re-run all jobs" on an old run,
    numbered higher); drop the ref allow-list -> red."""
    repo, shas = _repo(tmp_path)
    commit = shas.get(sha) or subprocess.run(["git", "rev-list", "-n1", sha], cwd=repo, capture_output=True, text=True).stdout.strip()
    result = _version(repo, ref, commit)
    assert result.returncode == 1 and result.stdout == ""
    assert why in result.stderr


def test_with_no_tag_yet_the_first_release_is_v0_1_0(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "first"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert _version(repo, "refs/heads/main", sha).stdout == "v0.1.0\n"
