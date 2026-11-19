"""release.yml only releases a tag CI passed on, and only `promote` makes a release latest."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_JOBS = yaml.safe_load((_WORKFLOWS / "release.yml").read_text())["jobs"]


def _upstream(job: str) -> set[str]:
    needs = _JOBS[job].get("needs", [])
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_upstream(n) for n in direct))


def _runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _JOBS[job].get("steps", []))


def test_every_job_waits_for_ci_on_a_tag() -> None:
    assert _JOBS["test"]["if"] == "github.ref_type == 'tag'"
    for job in set(_JOBS) - {"test"}:
        assert "test" in _upstream(job), job
        assert "if" not in _JOBS[job], f"{job} must not run when CI failed (`if: always()`)"


def test_only_the_approved_promote_job_marks_a_release_latest() -> None:
    assert _JOBS["promote"]["environment"] == "release"
    assert _upstream("promote") == set(_JOBS) - {"promote"}
    # One line per command: join the backslash-continued lines.
    others = "\n".join(_runs(job) for job in _JOBS if job != "promote").replace("\\\n", " ")
    assert not re.search(r"--latest(?!=false)", others)
    create = re.search(r"gh release create[^\n]*", others)
    assert create and re.search(r"--prerelease(?![=\w-])", create[0])


def test_the_build_runs_in_a_job_without_write_access() -> None:
    """The build runs setuptools-scm fresh from PyPI. Steps of one job share a
    runner, so only a separate read-only job keeps the write token from it."""
    assert _JOBS["build"]["permissions"] == {"contents": "read"}
    assert "Build the release files" in [step.get("name") for step in _JOBS["build"]["steps"]]
    assert "token" not in yaml.safe_dump(_JOBS["build"])
    assert all("run" not in step or "uv " not in step["run"] for step in _JOBS["prerelease"]["steps"])


def test_workflows_pin_actions_and_never_run_pr_code_with_write_access() -> None:
    for path in _WORKFLOWS.glob("*.yml"):
        text = path.read_text()
        assert "pull_request_target" not in text, path.name
        for uses in re.findall(r"uses:\s*(\S+)", text):
            assert uses.startswith("./") or re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), (path.name, uses)


# A `gh` that logs its arguments and answers promote's lookups from the environment.
_STUB_GH = r"""#!/usr/bin/env bash
echo "$*" >> "$LOG"
case "$*" in
  *environments/release*) echo "$REVIEWERS" ;;
  *commits/*) echo "$TAG_SHA" ;;
  "release view"*) printf '%s\n' $ASSETS ;;
  *releases/latest*)
    # As real gh does on an HTTP error: the body on stdout, the status on stderr.
    if grep -q '^release edit' "$LOG"; then echo "$TAG"
    elif [ "$LATEST" = 502 ]; then echo '{"message":"Bad Gateway"}'; echo 'gh: Bad Gateway (HTTP 502)' >&2; exit 1
    elif [ -n "$LATEST" ]; then echo "$LATEST"
    else echo '{"message":"Not Found","status":"404"}'; echo 'gh: Not Found (HTTP 404)' >&2; exit 1; fi ;;
esac
"""


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="promote's script is bash")
@pytest.mark.parametrize(
    ("tag", "reviewers", "tag_sha", "assets", "latest", "promoted"),
    [
        ("v0.2.0", "1", "abc", "install.sh install.ps1 x.whl", "v0.1.0", True),
        ("v0.2.0", "1", "abc", "install.sh install.ps1", "", True),  # the first release
        ("v0.2.0.post1", "1", "abc", "install.sh install.ps1", "v0.2.0", True),
        ("v0.2.1", "1", "abc", "install.sh install.ps1", "v0.2.0.post1", True),
        ("v0.2.0", "0", "abc", "install.sh install.ps1", "v0.1.0", False),  # unprotected environment
        ("v0.2.0", "1", "def", "install.sh install.ps1", "v0.1.0", False),  # the tag moved
        ("v0.2.0", "1", "abc", "install.sh x.whl", "v0.1.0", False),  # no install.ps1
        ("v0.2.0", "1", "abc", "install.sh install.ps1", "v0.3.0", False),  # a stale run
        ("v0.2.0", "1", "abc", "install.sh install.ps1", "502", False),  # latest unknown
    ],
)
def test_promote_refuses_unless_every_check_passes(
    tmp_path: Path, tag: str, reviewers: str, tag_sha: str, assets: str, latest: str, promoted: bool
) -> None:
    gh = tmp_path / "gh"
    gh.write_text(_STUB_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()
    env = {
        **os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "LOG": str(log),
        "TAG": tag, "GITHUB_SHA": "abc", "GH_REPO": "o/r",
        "REVIEWERS": reviewers, "TAG_SHA": tag_sha, "ASSETS": assets, "LATEST": latest,
    }
    result = subprocess.run(["bash", "-c", _runs("promote")], env=env, capture_output=True, text=True)
    edited = f"release edit {tag} --prerelease=false --latest" in log.read_text().splitlines()
    assert (result.returncode == 0, edited) == (promoted, promoted), result.stdout + result.stderr
