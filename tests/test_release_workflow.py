"""The release workflows do what RELEASING.md says, and nothing a reviewer would have to re-check by eye.

release.yml only releases a tag CI passed on, builds it once without write
access, never replaces a release's files, and only its approved `promote` job
makes a release latest. Each property is asserted on the parsed YAML, or by
running the step's own script with stub `uv` and `gh`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_WF: dict[str, Any] = {p.name: yaml.safe_load(p.read_text()) for p in sorted(_WORKFLOWS.glob("*.yml"))}
_JOBS = _WF["release.yml"]["jobs"]
_TAG_JOB = _WF["tag.yml"]["jobs"]["tag"]
_BASH = pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="the step's script is bash")


def _on(workflow: dict[str, Any]) -> Any:
    return workflow.get("on", workflow.get(True))  # PyYAML reads the key `on` as True


def _upstream(job: str) -> set[str]:
    needs = _JOBS[job].get("needs", [])
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_upstream(n) for n in direct))


def _runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in _JOBS[job].get("steps", []))


def _step(job: str, name: str) -> dict[str, Any]:
    return next(step for step in _JOBS[job]["steps"] if step.get("name") == name)


def _steps_using(action: str) -> list[dict[str, Any]]:
    return [s for job in _JOBS.values() for s in job.get("steps", []) if s.get("uses", "").startswith(action + "@")]


def _stubs(tmp_path: Path, **scripts: str) -> dict[str, str]:
    """Stub commands first on PATH, plus a `sha256sum` where the system has none."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    if shutil.which("sha256sum") is None:
        scripts.setdefault("sha256sum", '#!/bin/sh\nexec shasum -a 256 "$@"\n')
    for name, text in scripts.items():
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    (tmp_path / "log").touch()
    return {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "LOG": str(tmp_path / "log")}


# --- triggers, permissions, and what runs where ---------------------------


def test_triggers_are_exactly_these() -> None:
    assert _on(_WF["release.yml"]) == {"workflow_dispatch": None}
    assert _on(_WF["tag.yml"]) == {"pull_request": {"types": ["closed"], "branches": ["main"]}}
    assert _on(_WF["test.yml"]) == {
        "push": {"branches": ["main"]}, "pull_request": {"branches": ["main"]}, "workflow_call": None,
    }
    for name, workflow in _WF.items():
        assert not {"pull_request_target", "workflow_run"} & set(_on(workflow)), name


def test_each_job_gets_only_the_permissions_it_needs() -> None:
    assert _WF["release.yml"]["permissions"] == {}
    assert {job: spec.get("permissions") for job, spec in _JOBS.items()} == {
        "test": {"contents": "read"},
        "build": {"contents": "read"},
        "prerelease": {"contents": "write"},
        "promote": {"contents": "write", "actions": "read"},
    }
    assert _WF["tag.yml"]["permissions"] == {"contents": "write", "actions": "write"}


def test_no_expression_is_pasted_into_a_script() -> None:
    """Values reach `run:` through `env:`, so none can inject shell code."""
    for name, workflow in _WF.items():
        for job, spec in workflow["jobs"].items():
            for step in spec.get("steps", []):
                assert "${{" not in step.get("run", ""), (name, job, step.get("name"))


def test_no_job_or_step_may_fail_and_pass() -> None:
    for name, workflow in _WF.items():
        for job, spec in workflow["jobs"].items():
            assert "continue-on-error" not in spec, (name, job)
            for step in spec.get("steps", []):
                assert "continue-on-error" not in step, (name, job, step.get("name"))


def test_workflows_pin_actions_by_sha() -> None:
    for path in _WORKFLOWS.glob("*.yml"):
        for uses in re.findall(r"uses:\s*(\S+)", path.read_text()):
            assert uses.startswith("./") or re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), (path.name, uses)


def test_release_checkouts_keep_no_token_and_uv_uses_no_cache() -> None:
    checkouts, uvs = _steps_using("actions/checkout"), _steps_using("astral-sh/setup-uv")
    assert checkouts and all(step["with"]["persist-credentials"] is False for step in checkouts)
    assert uvs and all(step["with"]["enable-cache"] is False for step in uvs)


def test_the_build_runs_in_a_job_without_write_access() -> None:
    """The build runs setuptools-scm fresh from PyPI. Steps of one job share a
    runner, so only a separate read-only job keeps the write token from it."""
    assert "Build the release files" in [step.get("name") for step in _JOBS["build"]["steps"]]
    assert "token" not in yaml.safe_dump(_JOBS["build"])
    assert all("uv " not in step.get("run", "") for step in _JOBS["prerelease"]["steps"])


def test_every_job_waits_for_ci_on_a_tag() -> None:
    assert _JOBS["test"]["if"] == "github.ref_type == 'tag'"
    assert _JOBS["test"]["uses"] == "./.github/workflows/test.yml"
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
    assert create and re.search(r"--prerelease(?![=\w-])", create[0]) and "--verify-tag" in create[0]


def test_the_tag_job_runs_only_on_a_merge_one_at_a_time() -> None:
    assert _TAG_JOB["if"] == "github.event.pull_request.merged == true"
    # On the job: a workflow-level group would let a PR closed unmerged queue
    # there and cancel a merge's pending run.
    assert _TAG_JOB["concurrency"] == {"group": "tag", "cancel-in-progress": False}
    assert "concurrency" not in _WF["tag.yml"]


# --- the build step, run with a stub uv ----------------------------------

_STUB_UV = """#!/bin/bash
echo "uv $*" >> "$LOG"
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  [ "$prev" = "--out-dir" ] && outdir="$a"
  prev="$a"
done
case "$1" in
  export) echo "pins" > "$out" ;;
  build) mkdir -p "$outdir"; echo "a wheel" > "$outdir/mintd-$BUILT-py3-none-any.whl" ;;
esac
"""


def _build(tmp_path: Path, tag: str, built: str) -> tuple[subprocess.CompletedProcess[str], str]:
    env = {**_stubs(tmp_path, uv=_STUB_UV), "TAG": tag, "BUILT": built, "RUNNER_TEMP": str(tmp_path)}
    result = subprocess.run(
        ["bash", "-c", _step("build", "Build the release files")["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    return result, (tmp_path / "log").read_text()


@_BASH
@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3.post1"])
def test_the_build_writes_the_wheel_its_sha256_and_hashed_constraints(tmp_path: Path, tag: str) -> None:
    result, log = _build(tmp_path, tag, tag[1:])
    assert result.returncode == 0, result.stderr
    wheel = f"mintd-{tag[1:]}-py3-none-any.whl"
    digest = hashlib.sha256((tmp_path / "dist" / wheel).read_bytes()).hexdigest()
    assert (tmp_path / "dist" / f"{wheel}.sha256").read_text() == f"{digest}  {wheel}\n"
    export = next(line for line in log.splitlines() if "-o dist/constraints.txt" in line)
    assert "--no-hashes" not in export and "--extra schema" in export and "--locked" in export


@_BASH
@pytest.mark.parametrize("tag", ["v1.2.3-rc1", "v1.2", "1.2.3", "v1.2.3.dev1"])
def test_the_build_refuses_a_tag_that_is_not_a_release(tmp_path: Path, tag: str) -> None:
    result, log = _build(tmp_path, tag, tag.lstrip("v"))
    assert result.returncode != 0
    assert "is not vX.Y.Z or vX.Y.Z.postN" in result.stdout
    assert log == ""  # nothing was built


@_BASH
def test_the_build_refuses_a_wheel_whose_version_is_not_the_tag(tmp_path: Path) -> None:
    result, _ = _build(tmp_path, "v1.2.3", "1.2.4.dev1+g0123abc")
    assert result.returncode != 0
    assert "expected mintd-1.2.3-py3-none-any.whl" in result.stdout
    assert not list((tmp_path / "dist").glob("*.sha256"))


# --- publishing, run with a stub gh --------------------------------------

_STUB_GH_PUBLISH = """#!/bin/bash
echo "gh $*" >> "$LOG"
case "$*" in
  "release view"*"--json tagName"*) [ "$EXISTS" = 1 ] || { echo "release not found" >&2; exit 1; } ;;
  "release view"*"--json assets"*) cat "$HAVE" ;;
esac
"""


@_BASH
@pytest.mark.parametrize(
    ("exists", "have", "created", "uploaded", "ok"),
    [
        (False, {}, True, ["a.whl", "b.txt"], True),  # a new release
        (True, {"a.whl": "same"}, False, ["b.txt"], True),  # a rerun finishes it
        (True, {"a.whl": "same", "b.txt": "same"}, False, [], True),  # nothing left to do
        (True, {"a.whl": "other"}, False, [], False),  # a file differs: never replaced
    ],
)
def test_publishing_never_replaces_a_release_file(
    tmp_path: Path, exists: bool, have: dict[str, str], created: bool, uploaded: list[str], ok: bool
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    files = {"a.whl": b"wheel", "b.txt": b"pins"}
    for name, data in files.items():
        (dist / name).write_bytes(data)
    digest = {name: f"sha256:{hashlib.sha256(data).hexdigest()}" for name, data in files.items()}
    lines = [f"{name} {digest[name] if kind == 'same' else 'sha256:' + '0' * 64}" for name, kind in have.items()]
    (tmp_path / "have").write_text("".join(f"{line}\n" for line in lines))
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_PUBLISH), "TAG": "v1.2.3", "EXISTS": "1" if exists else "0",
        "HAVE": str(tmp_path / "have"), "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1", "GITHUB_SHA": "abc",
    }
    result = subprocess.run(
        ["bash", "-c", _step("prerelease", "Publish the prerelease")["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    log = (tmp_path / "log").read_text().splitlines()
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert any(line.startswith("gh release create v1.2.3 --verify-tag --prerelease --latest=false") for line in log) is created
    assert [line.rsplit("/", 1)[1] for line in log if line.startswith("gh release upload")] == uploaded


# --- promoting, run with a stub gh ---------------------------------------

# Answers promote's lookups from the environment. Only the full tag ref
# resolves: a bare name could also match a branch.
_STUB_GH_PROMOTE = r"""#!/usr/bin/env bash
echo "$*" >> "$LOG"
case "$*" in
  *environments/release*) echo "$REVIEWERS" ;;
  *commits/refs/tags/*) echo "$TAG_SHA" ;;
  "release view"*) printf '%s\n' $ASSETS ;;
  *releases/latest*)
    # As real gh does on an HTTP error: the body on stdout, the status on stderr.
    if grep -q '^release edit' "$LOG"; then echo "$TAG"
    elif [ "$LATEST" = 502 ]; then echo '{"message":"Bad Gateway"}'; echo 'gh: Bad Gateway (HTTP 502)' >&2; exit 1
    elif [ -n "$LATEST" ]; then echo "$LATEST"
    else echo '{"message":"Not Found","status":"404"}'; echo 'gh: Not Found (HTTP 404)' >&2; exit 1; fi ;;
esac
"""


@_BASH
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
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_PROMOTE), "TAG": tag, "GITHUB_SHA": "abc", "GH_REPO": "o/r",
        "REVIEWERS": reviewers, "TAG_SHA": tag_sha, "ASSETS": assets, "LATEST": latest,
    }
    result = subprocess.run(["bash", "-c", _runs("promote")], env=env, capture_output=True, text=True)
    edited = f"release edit {tag} --prerelease=false --latest" in (tmp_path / "log").read_text().splitlines()
    assert (result.returncode == 0, edited) == (promoted, promoted), result.stdout + result.stderr
