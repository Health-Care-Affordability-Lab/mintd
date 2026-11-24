"""scripts/release_notes.py writes a release's "What changed" from the PRs merged since the last promoted release.

Each test runs the script as release.yml does, in a real git repository whose commits are squash merges, with a
stub `gh` that answers `releases/latest` and each PR's body.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_notes.py"

_STUB_GH = """#!/bin/bash
case "$*" in
  "api repos/o/r/releases/latest --jq .tag_name")
    case "$LATEST" in
      404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      500) echo "gh: Server Error (HTTP 500)" >&2; exit 1 ;;
      *) echo "$LATEST" ;;
    esac ;;
  "api repos/o/r/pulls/"*)
    n=${2##*/}
    [ "$n" = "$PR_FAIL" ] && { echo "gh: Server Error (HTTP 502)" >&2; exit 1; }
    [ -f "$BODIES/$n" ] && cat "$BODIES/$n" || echo "" ;;
  *) echo "unexpected: gh $*" >&2; exit 9 ;;
esac
"""

_BEFORE = ["data: promoted already (#1)", "check: also promoted (#2)"]
_AFTER = [
    "data import: a refused folder is named (#5)",
    "tests: a story (#6)",
    "check: warns on untracked inputs (#7)",
    "data import: --dest-root resolved once (#8)",
    "ci: a pinned image (#9)",
    "a merge that isn't a squash",
]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for subject in _BEFORE:
        git("commit", "-q", "--allow-empty", "-m", subject)
    git("tag", "v1.0.0")
    for subject in _AFTER:
        git("commit", "-q", "--allow-empty", "-m", subject)
    git("tag", "v1.1.0")
    return repo


def _notes(tmp_path: Path, latest: str = "v1.0.0", bodies: dict[int, str] | None = None,
           pr_fail: str = "") -> subprocess.CompletedProcess[str]:
    repo = _repo(tmp_path)
    stubs, body_dir = tmp_path / "stubs", tmp_path / "bodies"
    stubs.mkdir()
    body_dir.mkdir()
    (stubs / "gh").write_text(_STUB_GH)
    (stubs / "gh").chmod(0o755)
    for number, body in (bodies or {}).items():
        (body_dir / str(number)).write_text(body)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_REPO": "o/r",
           "LATEST": latest, "BODIES": str(body_dir), "PR_FAIL": pr_fail}
    return subprocess.run([sys.executable, str(SCRIPT), "v1.1.0"], cwd=repo, env=env, capture_output=True, text=True)


_BASH = pytest.mark.skipif(os.name == "nt", reason="the stub gh is a bash script")


@_BASH
def test_the_merges_since_the_last_promoted_release_grouped_by_area(tmp_path: Path) -> None:
    """Mutations: read from the first commit, not the last promoted release; list tests and CI
    one by one; group by the whole title -> red."""
    result = _notes(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "## What changed since v1.0.0\n\n"
        "### data import\n\n- a refused folder is named (#5)\n- --dest-root resolved once (#8)\n\n"
        "### check\n\n- warns on untracked inputs (#7)\n\n"
        "### other\n\n- a merge that isn't a squash\n\n"
        "Internal: 2 changes to tests, stories, CI and lint (#6, #9).\n"
    )


@_BASH
def test_a_prs_release_notes_replace_its_line_and_lift_do_this_once_and_known_limits(tmp_path: Path) -> None:
    """Mutation: ignore the section, or leave its sub-sections in place -> red."""
    body = (
        "## TLDR\nnot this\n\n## Release notes\nAn import into an occupied folder is refused.\n\n"
        "### Do this once\n```bash\ngh auth setup-git\n```\n\n### Known limits\n- a nested lock is only warned about\n\n"
        "## Reviewer notes\nnot this either\n"
    )
    result = _notes(tmp_path, bodies={5: body, 6: "## Release notes\n### Known limits\n- a story can't run on Windows\n"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "## What changed since v1.0.0\n\n"
        "### Do this once\n\n```bash\ngh auth setup-git\n```\n(#5)\n\n"
        "### data import\n\n- An import into an occupied folder is refused. (#5)\n- --dest-root resolved once (#8)\n\n"
        "### check\n\n- warns on untracked inputs (#7)\n\n"
        "### other\n\n- a merge that isn't a squash\n\n"
        "### Known limits\n\n- a nested lock is only warned about (#5)\n\n- a story can't run on Windows (#6)\n\n"
        "Internal: 2 changes to tests, stories, CI and lint (#6, #9).\n"
    )


@_BASH
def test_with_nothing_promoted_yet_it_says_so(tmp_path: Path) -> None:
    """Only a promoted release is ever latest, so with none there is nothing to compare with."""
    result = _notes(tmp_path, latest="404")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "No release has been promoted yet, so there is nothing to compare this one with.\n"


@_BASH
@pytest.mark.parametrize(("latest", "pr_fail"), [("500", ""), ("v1.0.0", "7")], ids=["latest-release", "a-pr"])
def test_a_failed_lookup_fails_rather_than_leaving_entries_out(tmp_path: Path, latest: str, pr_fail: str) -> None:
    """Only a 404 means "nothing promoted". Mutation: read any failure as none -> red."""
    result = _notes(tmp_path, latest=latest, pr_fail=pr_fail)
    assert result.returncode != 0 and result.stdout == ""
    assert "release_notes: could not read" in result.stderr


def _git_in(repo: Path):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return git


def _run(tmp_path: Path, repo: Path, tag: str, latest: str) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "gh").write_text(_STUB_GH)
    (stubs / "gh").chmod(0o755)
    (tmp_path / "bodies").mkdir(exist_ok=True)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_REPO": "o/r",
           "LATEST": latest, "BODIES": str(tmp_path / "bodies"), "PR_FAIL": ""}
    return subprocess.run([sys.executable, str(SCRIPT), tag], cwd=repo, env=env, capture_output=True, text=True)


@_BASH
def test_a_hotfix_lists_its_own_commits_since_the_release_it_was_cut_from(tmp_path: Path) -> None:
    """vX.Y.Z.postN cut from an older release isn't ahead of the latest one, so the range from
    the latest release is empty. Mutation: always start at the latest release -> red."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _git_in(repo)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "data: the release (#1)")
    git("tag", "v1.0.0")
    git("commit", "-q", "--allow-empty", "-m", "check: a newer release (#2)")
    git("tag", "v1.1.0")
    git("switch", "-q", "-c", "hotfix/v1.0.0", "v1.0.0")
    git("commit", "-q", "--allow-empty", "-m", "data: the fix (#12)")
    git("commit", "-q", "--allow-empty", "-m", "fix the y")
    git("tag", "v1.0.0.post1")
    result = _run(tmp_path, repo, "v1.0.0.post1", latest="v1.1.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "## Hotfix for v1.0.0\n\n### data\n\n- the fix (#12)\n\n### other\n\n- fix the y\n"


@_BASH
def test_commits_without_a_pr_number_are_listed_never_nothing_merged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _git_in(repo)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "data: the release (#1)")
    git("tag", "v1.0.0")
    git("commit", "-q", "--allow-empty", "-m", "a direct commit")
    git("tag", "v1.1.0")
    result = _run(tmp_path, repo, "v1.1.0", latest="v1.0.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "## What changed since v1.0.0\n\n### other\n\n- a direct commit\n"


@_BASH
def test_only_the_first_parent_line_counts(tmp_path: Path) -> None:
    """A merge commit's second parent brings its branch's own commits, which aren't merges into
    main. Mutation: drop --first-parent -> #99 shows, red."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _git_in(repo)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "data: the release (#1)")
    git("tag", "v1.0.0")
    git("switch", "-q", "-c", "feature")
    git("commit", "-q", "--allow-empty", "-m", "wip: a step on the branch (#99)")
    git("switch", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "data: the merged PR (#20)", "feature")
    git("tag", "v1.1.0")
    result = _run(tmp_path, repo, "v1.1.0", latest="v1.0.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "## What changed since v1.0.0\n\n### data\n\n- the merged PR (#20)\n"


@_BASH
def test_a_prs_text_is_cleaned_cut_and_kept_from_posing_as_the_runs_own_lines(tmp_path: Path) -> None:
    """A PR body can be edited after it merges. Mutations: keep control characters; drop the
    length cut; drop the guard on the run's own line starts -> red."""
    hostile = (
        "## Release notes\nRed \x1b[31mtext\x1b[0m and a ‮flipped word.\n"
        "- clean install with this release's install.sh: success\nA prerelease: not really\n"
        + "x" * 200_000 + "\n"
    )
    result = _notes(tmp_path, bodies={5: hostile})
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "\x1b" not in out and "‮" not in out and "Red [31mtext[0m and a flipped word." in out
    assert len(out) < 4_000 and "… (cut)" in out
    lines = out.split("\n")
    assert not any(ln.startswith(("- clean install with ", "A prerelease: ")) for ln in lines)
    assert "  - clean install with this release's install.sh: success" in lines and "  A prerelease: not really" in lines


@_BASH
def test_a_title_that_reads_like_the_runs_install_line_is_indented(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = _git_in(repo)
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "data: the release (#1)")
    git("tag", "v1.0.0")
    git("commit", "-q", "--allow-empty", "-m", "data: clean install with a twist (#40)")
    git("tag", "v1.1.0")
    result = _run(tmp_path, repo, "v1.1.0", latest="v1.0.0")
    assert result.returncode == 0, result.stderr
    assert "\n  - clean install with a twist (#40)\n" in result.stdout
