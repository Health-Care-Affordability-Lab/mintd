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
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO / ".github" / "workflows"
_WF: dict[str, Any] = {p.name: yaml.safe_load(p.read_text()) for p in sorted(_WORKFLOWS.glob("*.yml"))}
_JOBS = _WF["release.yml"]["jobs"]
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
    """A release runs only when started by hand; no merge tags or starts one."""
    assert _on(_WF["release.yml"]) == {"workflow_dispatch": {"inputs": {"bump": {
        "description": "On main: patch or minor. Ignored on a hotfix/vX.Y.Z branch.",
        "type": "choice", "options": ["patch", "minor"], "default": "patch"}}}}
    assert "tag.yml" not in _WF
    assert _on(_WF["test.yml"]) == {
        "push": {"branches": ["main"]}, "pull_request": {"branches": ["main"]}, "workflow_call": None,
    }
    for name, workflow in _WF.items():
        assert not {"pull_request_target", "workflow_run"} & set(_on(workflow)), name


def test_each_job_gets_only_the_permissions_it_needs() -> None:
    assert _WF["release.yml"]["permissions"] == {}
    assert {job: spec.get("permissions") for job, spec in _JOBS.items()} == {
        "version": {"contents": "read"},
        "test": {"contents": "read"},
        "build": {"contents": "read"},
        "prerelease": {"contents": "write"},
        "install": {"contents": "read"},
        "evidence": {"contents": "write", "pull-requests": "read"},
        "promote": {"contents": "write", "actions": "read"},
    }


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


#: `evidence` records a failed install too, so it runs whatever `install`
#: did, but only once CI passed and the prerelease exists.
_ALLOWED_IF = {"evidence": "always() && needs.prerelease.result == 'success'"}


def test_every_job_after_the_version_waits_for_ci() -> None:
    """CI runs on the very commit being released, once its version is chosen."""
    assert _JOBS["test"] == {"needs": "version", "uses": "./.github/workflows/test.yml", "permissions": {"contents": "read"}}
    assert "needs" not in _JOBS["version"]
    for job in set(_JOBS) - {"version", "test"}:
        assert "test" in _upstream(job), job
        assert _JOBS[job].get("if") == _ALLOWED_IF.get(job), f"{job} must not run when CI failed (`if: always()`)"


def test_only_the_approved_promote_job_marks_a_release_latest() -> None:
    assert _JOBS["promote"]["environment"] == "release"
    assert _upstream("promote") == set(_JOBS) - {"promote"}
    # `evidence` runs after a failed install too, so needing it alone would
    # let promote run after one.
    assert "install" in _JOBS["promote"]["needs"]
    # One line per command: join the backslash-continued lines.
    others = "\n".join(_runs(job) for job in _JOBS if job != "promote").replace("\\\n", " ")
    assert not re.search(r"--latest(?!=false)", others)
    assert "--prerelease=false" not in others
    create = re.search(r"gh release create[^\n]*", others)
    assert create and re.search(r"--prerelease(?![=\w-])", create[0])
    # The tag is made at publish, on this run's commit; it doesn't exist before.
    assert '--target "$GITHUB_SHA"' in create[0] and "--verify-tag" not in create[0]
    assert re.search(r"--draft(?![=\w-])", create[0])  # published only once its files are all up


def test_one_release_run_at_a_time() -> None:
    """A second press waits until the first is approved, rejected or fails, so two runs
    never claim one number. Mutation: drop the group -> red."""
    assert _WF["release.yml"]["concurrency"] == {"group": "release", "cancel-in-progress": False}
    assert all("concurrency" not in spec for spec in _JOBS.values())


# --- the version, run with a stub python3 and gh -------------------------

_STUB_PY_VERSION = """#!/bin/bash
echo "python3 $*" >> "$LOG"
[ "$2" = refs/heads/main ] && [ "$3" = abc ] && [ "$4" = minor ] || exit 9
[ "$REFUSE" = 1 ] && { echo "::error::abc doesn't come after v1.2.2" >&2; exit 1; }
echo v1.2.3
"""
_STUB_GH_VERSION = """#!/bin/bash
echo "gh $*" >> "$LOG"
case "$TAG_STATE" in
  absent) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  present) echo '{"ref":"refs/tags/v1.2.3"}' ;;
  *) echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1 ;;
esac
"""


@_BASH
@pytest.mark.parametrize(("case", "tag_state", "refuse", "made", "error"), [
    ("a new version", "absent", False, True, None),
    ("the script refuses", "absent", True, False, "doesn't come after"),
    ("the tag already exists", "present", False, False, "v1.2.3 already exists; a version is never reused"),
    ("the lookup fails", "error", False, False, "Bad Gateway"),
])
def test_the_version_is_new_or_the_run_stops(
    tmp_path: Path, case: str, tag_state: str, refuse: bool, made: bool, error: str | None,
) -> None:
    """Only a 404 means the tag is new. Mutations: skip the existence check, or read any
    lookup failure as "new" -> red."""
    out = tmp_path / "github_output"
    out.touch()
    env = {
        **_stubs(tmp_path, python3=_STUB_PY_VERSION, gh=_STUB_GH_VERSION), "GH_REPO": "o/r", "BUMP": "minor",
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "abc", "GITHUB_OUTPUT": str(out),
        "TAG_STATE": tag_state, "REFUSE": "1" if refuse else "0",
    }
    result = subprocess.run(["bash", "-c", _step("version", "Choose the version")["run"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is made, result.stdout + result.stderr
    assert out.read_text() == ("tag=v1.2.3\n" if made else "")
    if error:
        assert error in result.stdout + result.stderr


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
  # Named as setuptools-scm would: the pretend version when given, else a dev build.
  build) mkdir -p "$outdir"; echo "a wheel" > "$outdir/mintd-${BUILT:-${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MINTD:-0.1.dev1+gabc}}-py3-none-any.whl" ;;
esac
"""


def _build(tmp_path: Path, tag: str, built: str = "") -> tuple[subprocess.CompletedProcess[str], str]:
    env = {**_stubs(tmp_path, uv=_STUB_UV), "TAG": tag, "BUILT": built, "RUNNER_TEMP": str(tmp_path)}
    result = subprocess.run(
        ["bash", "-c", _step("build", "Build the release files")["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    return result, (tmp_path / "log").read_text()


@_BASH
@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3.post1"])
def test_the_build_writes_the_wheel_its_sha256_and_hashed_constraints(tmp_path: Path, tag: str) -> None:
    """No tag exists until publish, so the build names the version itself.
    Mutation: drop SETUPTOOLS_SCM_PRETEND_VERSION_FOR_MINTD -> a dev wheel, red."""
    result, log = _build(tmp_path, tag)
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


# --- stamping the install scripts ----------------------------------------

_PLACEHOLDERS = {"install.sh": 'MINTD_TAG=""', "install.ps1": '$MintdTag = ""'}


def _stamp(tmp_path: Path, scripts: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the one stamper, as the build does, on `scripts` in an empty directory. The scripts are written as the
    repo holds them, UTF-8 with their own line endings, whatever the platform's default encoding (cp1252 on Windows
    can't hold their em dash as UTF-8 does)."""
    for name, text in scripts.items():
        (tmp_path / name).write_bytes(text.encode("utf-8"))
    return subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "stamp_install_scripts.py"), "v1.2.3", "--out", "dist", *extra],
        cwd=tmp_path, capture_output=True, text=True,
    )


def test_the_build_stamps_with_the_one_stamper() -> None:
    """CI's install checks call the same script, so they test what users get."""
    assert _step("build", "Stamp the install scripts")["run"].strip() == 'python3 scripts/stamp_install_scripts.py "$TAG" --out dist'


@_BASH
def test_stamping_writes_the_tag_into_both_install_scripts(tmp_path: Path) -> None:
    scripts = {name: (_REPO / name).read_text(encoding="utf-8") for name in _PLACEHOLDERS}
    result = _stamp(tmp_path, scripts)
    assert result.returncode == 0, result.stderr
    for name, empty in _PLACEHOLDERS.items():
        stamped = empty.replace('""', '"v1.2.3"')
        expected = scripts[name].replace(f"\n{empty}\n", f"\n{stamped}\n")
        assert expected != scripts[name]
        assert (tmp_path / "dist" / name).read_text(encoding="utf-8") == expected
    assert subprocess.run(["bash", "-n", str(tmp_path / "dist" / "install.sh")]).returncode == 0


def test_only_ci_points_the_scripts_at_another_releases_url(tmp_path: Path) -> None:
    """The release build passes no --releases, so a release's scripts keep GitHub's URL."""
    scripts = {name: (_REPO / name).read_text(encoding="utf-8") for name in _PLACEHOLDERS}
    assert _stamp(tmp_path, scripts).returncode == 0
    assert 'RELEASES="https://github.com/Health-Care-Affordability-Lab/mintd/releases"' in (
        tmp_path / "dist" / "install.sh").read_text(encoding="utf-8")
    assert _stamp(tmp_path, scripts, "--releases", "http://127.0.0.1:8000").returncode == 0
    assert '\nRELEASES="http://127.0.0.1:8000"\n' in (tmp_path / "dist" / "install.sh").read_text(encoding="utf-8")
    assert '\n$Releases = "http://127.0.0.1:8000"\n' in (tmp_path / "dist" / "install.ps1").read_text(encoding="utf-8")
    assert "--releases" not in yaml.safe_dump(_JOBS)


@_BASH
@pytest.mark.parametrize("copies", [0, 2])
def test_stamping_stops_unless_each_script_has_exactly_one_placeholder(tmp_path: Path, copies: int) -> None:
    """An unstamped script on a release would install the newest release instead."""
    scripts = {name: (_REPO / name).read_text(encoding="utf-8") for name in _PLACEHOLDERS}
    empty = _PLACEHOLDERS["install.sh"]
    scripts["install.sh"] = scripts["install.sh"].replace(f"\n{empty}\n", f"\n{empty}\n" * copies or "\n")
    result = _stamp(tmp_path, scripts)
    assert result.returncode != 0
    assert "must appear exactly once" in result.stderr


# --- publishing, run with a stub gh --------------------------------------

_STUB_GH_PUBLISH = """#!/bin/bash
echo "gh $*" >> "$LOG"
case "$*" in
  # The tag's commit, as real gh answers: no tag yet (until a publish makes it, unless
  # $TAG_AT holds one) prints the error's JSON on stdout and the status on stderr.
  "api repos/o/r/commits/refs/tags/"*)
    [ "$LOOKUP_FAIL" = 1 ] && { echo "error connecting to api.github.com" >&2; exit 1; }
    [ -s "$TAG_AT" ] && { cat "$TAG_AT"; exit 0; }
    echo '{"message":"No commit found for SHA: v1.2.3","status":"422"}'
    echo "gh: No commit found for SHA: v1.2.3 (HTTP 422)" >&2; exit 1 ;;
  # The listing as `gh api --paginate --jq` prints it: one line per match, page
  # after page. Without --paginate only the first page comes back.
  # A new draft shows in the listing only after $LAG listings, as GitHub's list can lag a write.
  "api --paginate "*) [ "$LIST_FAIL" = 1 ] && exit 1
    if [ -s "$LAGGED" ]; then
      n=$(cat "$LAGGED")
      if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$LAGGED"; else cat "$NEW" >> "$PAGE1"; : > "$LAGGED"; fi
    fi
    cat "$PAGE1" "$PAGE2" ;;
  "api "*) [ "$LIST_FAIL" = 1 ] && exit 1; cat "$PAGE1" ;;
  "release view"*"--json assets"*) [ "$ASSETS_FAIL" = 1 ] && exit 1; cat "$HAVE" ;;
  # Real gh prints the new release's URL. $LISTED_URL lists another release in its place.
  "release create"*)
    echo "https://github.com/o/r/releases/tag/untagged-1"
    [ "$UNLISTED" = 1 ] || {
      echo "999 true true $GITHUB_SHA ${LISTED_URL:-https://github.com/o/r/releases/tag/untagged-1}" > "$NEW"
      echo "${LAG:-0}" > "$LAGGED"; } ;;
  # Publishing makes the tag, on the draft's commit unless $LANDS_ON says otherwise.
  "release edit"*"--draft=false"*) echo "${LANDS_ON:-$GITHUB_SHA}" > "$TAG_AT" ;;
  "release upload"*)
    name=${4##*/}
    [ "$name" = "$UPLOAD_FAIL" ] && exit 1
    if [ "$name" = "$UPLOAD_HALF" ]; then echo "$name open null" >> "$HAVE"
    else echo "$name uploaded sha256:$(sha256sum "$4" | cut -d' ' -f1)" >> "$HAVE"; fi ;;
esac
exit 0
"""

_SAME, _OTHER, _HALF = "same", "other", "half"
# A release as the listing prints it: `<id> <draft> <prerelease> <target> <url>`; this run's commit is abc.
_DRAFT, _NOT_PRE_DRAFT, _OTHER_DRAFT = "true true abc", "true false abc", "true true def"
_PUBLISHED, _PROMOTED = "false true abc", "false false abc"
_CREATE = "gh release create v1.2.3 --draft --prerelease --latest=false --target abc"
_PUBLISH = "gh release edit v1.2.3 --draft=false --prerelease --latest=false"
_BOTH = ["a.whl", "b.txt"]
_COMPLETE = {"a.whl": _SAME, "b.txt": _SAME}


@_BASH
@pytest.mark.parametrize(
    ("case", "pages", "have", "tag_at", "created", "uploaded", "published", "error"),
    [
        ("none yet", ([], []), {}, "", True, _BOTH, True, None),
        ("a complete draft", ([_DRAFT], []), _COMPLETE, "", False, [], True, None),
        ("a partial draft", ([_DRAFT], []), {"a.whl": _SAME}, "", False, ["b.txt"], True, None),
        # The draft's files are all checked before anything is uploaded.
        ("a draft with a half upload", ([_DRAFT], []), {"a.whl": _HALF}, "", False, [], False, "draft 100 has an incomplete upload of a.whl"),
        ("a draft with a different file", ([_DRAFT], []), {"b.txt": _OTHER}, "", False, [], False, "draft 100 already has a different b.txt"),
        ("a draft with a stray file", ([_DRAFT], []), {"c.txt": _OTHER}, "", False, [], False, "draft 100 has c.txt, which this build didn't make"),
        ("a draft that isn't a prerelease", ([_NOT_PRE_DRAFT], []), {}, "", False, [], False, "draft 100 is not a prerelease: delete it by its id"),
        # Its commit would become a permanent tag on the wrong commit.
        ("a draft on another commit", ([_OTHER_DRAFT], []), _COMPLETE, "", False, [], False, "draft 100 targets def, not this run's abc"),
        ("an empty dist/", ([], []), {}, "", False, [], False, "dist/ is empty"),
        ("two releases, on two pages", ([_PUBLISHED], [_DRAFT]), {}, "abc", False, [], False, "several releases for v1.2.3"),
        ("the listing fails", ([], []), {}, "", False, [], False, ""),
        ("the file lookup fails", ([_DRAFT], []), {}, "", False, [], False, ""),
        # "Re-run failed jobs" after this run published: nothing to do.
        ("a complete published prerelease on this commit", ([_PUBLISHED], []), _COMPLETE, "abc", False, [], False, None),
        ("a published prerelease on another commit", ([_PUBLISHED], []), _COMPLETE, "def", False, [], False, "published release is on another commit"),
        ("a published prerelease short of a file", ([_PUBLISHED], []), {"a.whl": _SAME}, "abc", False, [], False, "published release is missing b.txt"),
        ("a published prerelease with an extra file", ([_PUBLISHED], []), {**_COMPLETE, "c.txt": _OTHER}, "abc", False, [], False, "published release has c.txt, which this build didn't make"),
        ("a promoted release", ([_PROMOTED], []), _COMPLETE, "abc", False, [], False, "a promoted release is never changed"),
        ("an upload fails", ([], []), {}, "", True, ["a.whl"], False, ""),
        ("an upload lands incomplete", ([], []), {}, "", True, _BOTH, False, "draft 999 has an incomplete upload of a.whl"),
        ("a new draft that isn't listed", ([], []), {}, "", True, [], False, "new draft isn't listed as its only release"),
        # GitHub's list lags a new draft: the step waits while the list is empty.
        ("a new draft listed two tries late", ([], []), {}, "", True, _BOTH, True, None),
        # Never another release in its place: the one listed must be the draft this run made.
        ("another release listed after the create", ([], []), {}, "", True, [], False,
         "one listed release is https://github.com/o/r/releases/tag/untagged-2, not the draft this run made"),
        # Checked again seconds before publishing: publishing onto an existing tag would attach to its commit.
        ("the tag appears before the publish", ([_DRAFT], []), _COMPLETE, "def", False, [], False, "appeared since this run began"),
        # The tag is permanent once made: the run stops, and nothing after it runs.
        ("the tag lands on another commit", ([_DRAFT], []), _COMPLETE, "", False, [], True, "landed on def, not abc"),
        # No answer is never "no tag": the step stops before publishing.
        ("the tag lookup fails", ([_DRAFT], []), _COMPLETE, "", False, [], False, "couldn't read which commit v1.2.3 names"),
    ],
)
def test_the_prerelease_publishes_only_a_complete_draft(
    tmp_path: Path, case: str, pages: tuple[list[str], list[str]], have: dict[str, str], tag_at: str,
    created: bool, uploaded: list[str], published: bool, error: str | None,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    files = {} if case == "an empty dist/" else {"a.whl": b"wheel", "b.txt": b"pins"}
    for name, data in files.items():
        (dist / name).write_bytes(data)
    answer = {
        _SAME: lambda n: f"uploaded sha256:{hashlib.sha256(files[n]).hexdigest()}",
        _OTHER: lambda n: "uploaded sha256:" + "0" * 64,
        _HALF: lambda n: "open null",
    }
    (tmp_path / "have").write_text("".join(f"{n} {answer[kind](n)}\n" for n, kind in have.items()))
    (tmp_path / "tag_at").write_text(f"{tag_at}\n" if tag_at else "")
    for i, page in enumerate(pages, 1):
        lines = (f"{100 * i + j} {flags} https://github.com/o/r/releases/tag/v1.2.3\n" for j, flags in enumerate(page))
        (tmp_path / f"page{i}").write_text("".join(lines))
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_PUBLISH, sleep='#!/bin/sh\necho "sleep $*" >> "$LOG"\n'),
        "TAG": "v1.2.3", "GH_REPO": "o/r", "NEW": str(tmp_path / "new"), "LAGGED": str(tmp_path / "lagged"),
        "LAG": "2" if case == "a new draft listed two tries late" else "0",
        "LISTED_URL": "https://github.com/o/r/releases/tag/untagged-2" if case.startswith("another release") else "",
        "PAGE1": str(tmp_path / "page1"), "PAGE2": str(tmp_path / "page2"), "TAG_AT": str(tmp_path / "tag_at"),
        "LIST_FAIL": "1" if case == "the listing fails" else "0",
        "ASSETS_FAIL": "1" if case == "the file lookup fails" else "0",
        "UPLOAD_FAIL": "a.whl" if case == "an upload fails" else "",
        "UPLOAD_HALF": "a.whl" if case == "an upload lands incomplete" else "",
        "UNLISTED": "1" if case == "a new draft that isn't listed" else "0",
        "LANDS_ON": "def" if case == "the tag lands on another commit" else "",
        "LOOKUP_FAIL": "1" if case == "the tag lookup fails" else "0",
        "HAVE": str(tmp_path / "have"), "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "1", "GITHUB_SHA": "abc",
    }
    result = subprocess.run(
        ["bash", "-c", _step("prerelease", "Publish the prerelease")["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    log = (tmp_path / "log").read_text().splitlines()
    assert (result.returncode == 0) is (error is None), result.stdout + result.stderr
    if error:
        assert error in result.stdout
    assert any(line.startswith(_CREATE) for line in log) is created
    # Waits only while a new draft isn't listed yet, five at most.
    waits = {"a new draft listed two tries late": 2, "a new draft that isn't listed": 5}.get(case, 0)
    assert [line for line in log if line.startswith("sleep")] == ["sleep 5"] * waits
    assert [line.rsplit("/", 1)[1] for line in log if line.startswith("gh release upload")] == uploaded
    # The one edit is the publish, after every upload and the final check; then only the tag's commit is read.
    edits = [i for i, line in enumerate(log) if line.startswith("gh release edit")]
    assert [log[i] for i in edits] == ([_PUBLISH] if published else [])
    if published:
        assert all(i < edits[0] for i, line in enumerate(log) if line.startswith("gh release upload"))
        assert [line for line in log[edits[0] + 1:]] == ["gh api repos/o/r/commits/refs/tags/v1.2.3 --jq .sha"]


def test_the_listing_reads_the_tag_from_env_on_every_page() -> None:
    """The tag reaches the filter as gojq's `env.TAG`, never spliced into it, and
    `.[] |` streams one line per match whatever the page count (a `[...]` filter
    runs once per page)."""
    run = re.sub(r"\s*\\\n\s*", " ", _step("prerelease", "Publish the prerelease")["run"])
    assert "gh api --paginate \"repos/$GH_REPO/releases?per_page=100\" --jq '.[] | select(.tag_name == env.TAG) |" in run
    assert run.count("out=$(releases)") == 2  # before anything is made, and again for a new draft's id


# --- the evidence, run with a stub gh ------------------------------------

# The changelog script, stood in for: it has its own tests (tests/test_release_notes.py).
_STUB_RELEASE_NOTES = """#!/bin/bash
[ "$1" = scripts/release_notes.py ] && [ "$2" = "$TAG" ] || exit 9
[ "$NOTES_FAIL" = 1 ] && { echo "release_notes: could not read PR #7" >&2; exit 1; }
printf '## What changed since v1.2.2\n\n### data\n\n- something (#7)\n'
"""

_STUB_GH_EVIDENCE = """#!/bin/bash
echo "gh $1 $2" >> "$LOG"
case "$*" in
  "release view"*"--json isPrerelease"*) echo "$PRERELEASE" ;;
  "release view"*"--json body"*) cat "$BODY" ;;
  "release edit"*)
    prev=""
    for a in "$@"; do [ "$prev" = --notes ] && printf '%s' "$a" > "$NOTES_OUT"; prev="$a"; done ;;
esac
exit 0
"""
_PLACEHOLDER = "The release gate is running: https://example/run. The `evidence` job replaces this note with its results."
_FILLED = """commit: abc
gate run: https://example/run

- CI (test.yml) on this tag: passed
- clean install with this release's install.sh and install.ps1 on Linux (--with-schema), macOS and Windows PowerShell 5.1 (plain, then -WithSchema), and the story tests against the Linux install: failure
- a line someone added by hand

A prerelease."""


def _evidence(tmp_path: Path, body: str, prerelease: str = "true", notes_fail: bool = False) -> tuple[int, str | None, str]:
    (tmp_path / "body").write_text(body)
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_EVIDENCE, python3=_STUB_RELEASE_NOTES), "TAG": "v1.2.3", "INSTALL": "success",
        "PRERELEASE": prerelease, "NOTES_FAIL": "1" if notes_fail else "0",
        "BODY": str(tmp_path / "body"), "NOTES_OUT": str(tmp_path / "notes"),
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "1", "GITHUB_SHA": "abc",
    }
    result = subprocess.run(["bash", "-c", _runs("evidence")], env=env, capture_output=True, text=True)
    notes = (tmp_path / "notes").read_text() if (tmp_path / "notes").exists() else None
    return result.returncode, notes, result.stdout + result.stderr


@_BASH
def test_evidence_fills_the_notes_while_the_gate_placeholder_is_there(tmp_path: Path) -> None:
    rc, notes, out = _evidence(tmp_path, _PLACEHOLDER)
    assert rc == 0, out
    assert notes is not None and notes.startswith("## What changed since v1.2.2\n\n### data\n\n- something (#7)\n\n---\n\ncommit: abc\n")
    assert "and the story tests against the Linux install: success\n" in notes
    assert "gate run: https://github.com/o/r/actions/runs/1\n" in notes  # the one link to the run
    assert "live tier" not in notes and "canary" not in notes  # nothing left for people to fill in


@_BASH
def test_a_changelog_that_cannot_be_written_says_so_in_the_notes(tmp_path: Path) -> None:
    """Never left out silently. Mutation: drop the fallback -> the step fails, red."""
    rc, notes, out = _evidence(tmp_path, _PLACEHOLDER, notes_fail=True)
    assert rc == 0, out
    assert notes is not None and notes.startswith("(The changelog could not be written: see this run's evidence job.)\n")
    assert "::warning::the changelog could not be written" in out and "could not read PR #7" in out


@_BASH
def test_a_rerun_writes_a_changelog_that_failed_last_time(tmp_path: Path) -> None:
    """Mutation: a rerun keeps the failure line -> red."""
    failed = "(The changelog could not be written: see this run's evidence job.)"
    rc, notes, out = _evidence(tmp_path, failed + "\n\n---\n\n" + _FILLED)
    assert rc == 0, out
    assert notes is not None and notes.startswith("## What changed since v1.2.2\n\n### data\n\n- something (#7)\n\n---\n\ncommit: abc\n")
    assert "- a line someone added by hand" in notes and "against the Linux install: success" in notes


def test_ci_lints_the_release_scripts_too() -> None:
    """The release runs scripts/; every runner's lint covers it, Windows' included. Mutation: drop `scripts` from
    either step -> red."""
    steps = [step for job in _WF["test.yml"]["jobs"].values() for step in job.get("steps", [])]
    lints = [step["run"].split() for step in steps if " check src tests" in f" {step.get('run', '')}"]
    assert len(lints) == 2, lints  # Linux and macOS, and Windows
    assert all(lint[-3:] == ["src", "tests", "scripts"] for lint in lints), lints


def test_evidence_reads_the_history_back_to_the_last_promoted_release() -> None:
    checkout = next(s for s in _JOBS["evidence"]["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert 'changes=$(python3 scripts/release_notes.py "$TAG")' in _runs("evidence")


@_BASH
def test_evidence_on_a_rerun_replaces_only_the_install_line(tmp_path: Path) -> None:
    """A rerun keeps anything people wrote."""
    rc, notes, out = _evidence(tmp_path, _FILLED)
    assert rc == 0, out
    assert notes == _FILLED.replace("against the Linux install: failure", "against the Linux install: success")


@_BASH
def test_evidence_never_changes_a_release_that_is_not_a_prerelease(tmp_path: Path) -> None:
    rc, notes, out = _evidence(tmp_path, _FILLED, prerelease="false")
    assert rc != 0 and "is not a prerelease" in out
    assert notes is None


_PROMOTED_BODY = """## What changed since v0.1.0

### data

- something (#7)

---

commit: abc
gate run: https://example/run

- CI (test.yml) on this tag: passed

A prerelease: `releases/latest` skips it. It becomes the latest release only when the maintainer approves this run's `release` environment (RELEASING.md)."""


@_BASH
@pytest.mark.parametrize("body_fail", [False, True], ids=["notes-read", "notes-unreadable"])
def test_promote_takes_the_prerelease_line_out_of_the_notes_in_its_one_edit(tmp_path: Path, body_fail: bool) -> None:
    """The line is true only until promote. Mutations: keep it; edit the notes in a second call;
    blank the notes when they can't be read -> red."""
    (tmp_path / "body").write_text(_PROMOTED_BODY + "\n")
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_PROMOTE), "TAG": "v0.2.0", "GITHUB_SHA": "abc", "GH_REPO": "o/r",
        "REVIEWERS": "1", "TAG_SHA": "abc", "ASSETS": "install.sh install.ps1", "LATEST": "v0.1.0",
        "BODY": str(tmp_path / "body"), "NOTES_OUT": str(tmp_path / "notes"), "BODY_FAIL": "1" if body_fail else "0",
    }
    result = subprocess.run(["bash", "-c", _runs("promote")], env=env, capture_output=True, text=True)
    edits = [line for line in (tmp_path / "log").read_text().splitlines() if line.startswith("release edit")]
    if body_fail:
        assert result.returncode != 0 and edits == []
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(edits) == 1 and edits[0].startswith("release edit v0.2.0 --prerelease=false --latest --notes ")
    assert (tmp_path / "notes").read_text() == _PROMOTED_BODY.split("\n\nA prerelease: ")[0]


# --- promoting, run with a stub gh ---------------------------------------

# Answers promote's lookups from the environment. Only the full tag ref
# resolves: a bare name could also match a branch.
_STUB_GH_PROMOTE = r"""#!/usr/bin/env bash
echo "$*" >> "$LOG"
case "$*" in
  *environments/release*) echo "$REVIEWERS" ;;
  *commits/refs/tags/*) echo "$TAG_SHA" ;;
  "release view"*"--json body"*) [ "$BODY_FAIL" = 1 ] && exit 1; cat "$BODY" ;;
  "release view"*) printf '%s\n' $ASSETS ;;
  "release edit"*)
    prev=""
    for a in "$@"; do [ "$prev" = --notes ] && printf '%s' "$a" > "$NOTES_OUT"; prev="$a"; done ;;
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
    (tmp_path / "body").write_text("notes\n")
    env = {
        **_stubs(tmp_path, gh=_STUB_GH_PROMOTE), "TAG": tag, "GITHUB_SHA": "abc", "GH_REPO": "o/r",
        "REVIEWERS": reviewers, "TAG_SHA": tag_sha, "ASSETS": assets, "LATEST": latest,
        "BODY": str(tmp_path / "body"), "NOTES_OUT": str(tmp_path / "notes"), "BODY_FAIL": "0",
    }
    result = subprocess.run(["bash", "-c", _runs("promote")], env=env, capture_output=True, text=True)
    edited = any(line.startswith(f"release edit {tag} --prerelease=false --latest")
                 for line in (tmp_path / "log").read_text().splitlines())
    assert (result.returncode == 0, edited) == (promoted, promoted), result.stdout + result.stderr
