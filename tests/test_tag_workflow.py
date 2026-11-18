"""The tag job's script (.github/workflows/tag.yml) counts only plain vX.Y.Z tags, never hotfixes like v0.4.0.post1."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="the tag job's script is bash")

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tag.yml"
# Hermetic: no global or system git config (signing, hooks), a fixed identity.
_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, tags_per_commit: list[list[str]]) -> tuple[Path, list[str]]:
    """A clone of a bare "origin" with one commit per entry, carrying those tags."""
    _git(tmp_path, "init", "-q", "--bare", "origin.git")
    _git(tmp_path, "clone", "-q", "origin.git", "work")
    work, shas = tmp_path / "work", []
    for tags in tags_per_commit:
        _git(work, "commit", "-q", "--allow-empty", "-m", "c")
        shas.append(_git(work, "rev-parse", "HEAD"))
        for tag in tags:
            _git(work, "tag", tag)
    return work, shas


def _run_tag_step(work: Path, sha: str, minor: str = "false") -> str:
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["tag"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Tag the merge commit")
    env = {**_ENV, "SHA": sha, "PR": "1", "MINOR": minor}
    result = subprocess.run(["bash", "-c", script], cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("post1_newer", [False, True])
@pytest.mark.parametrize(("minor", "expected"), [("false", "v0.4.1"), ("true", "v0.5.0")])
def test_next_tag_ignores_hotfix_tags(tmp_path: Path, post1_newer: bool, minor: str, expected: str) -> None:
    tagged = [["v0.4.0"], ["v0.4.0.post1"]]
    work, shas = _repo(tmp_path, [*(tagged if post1_newer else tagged[::-1]), []])
    assert _run_tag_step(work, shas[-1], minor) == f"tagged {shas[-1]} as {expected}\n"
    assert _git(tmp_path / "origin.git", "tag", "--points-at", shas[-1]) == expected


def test_commit_with_only_a_hotfix_tag_still_gets_a_plain_tag(tmp_path: Path) -> None:
    work, shas = _repo(tmp_path, [["v0.4.0"], ["v0.4.0.post1"]])
    assert _run_tag_step(work, shas[-1]) == f"tagged {shas[-1]} as v0.4.1\n"


def test_plain_tagged_commit_is_skipped(tmp_path: Path) -> None:
    work, shas = _repo(tmp_path, [["v0.4.0", "v0.4.0.post1"]])
    assert _run_tag_step(work, shas[0]) == f"{shas[0]} is already tagged\n"
    assert _git(work, "tag", "--list") == "v0.4.0\nv0.4.0.post1"


def test_first_tag_is_v0_1_0(tmp_path: Path) -> None:
    work, shas = _repo(tmp_path, [[]])
    assert _run_tag_step(work, shas[0]) == f"tagged {shas[0]} as v0.1.0\n"
