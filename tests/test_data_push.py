"""Tests for ``mintd data push`` success-line UX (slice 48).

Covers the Reporter-rendered summary that replaced the bare ``print("pushed")``:
human-mode line shape, ``--json`` payload, effective-remote precedence, the
nothing-to-push case, and graceful degradation when dvc's count can't be parsed.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from mintd import cli
from mintd._dvc_ops import DvcPushResult, _parse_push_output
from mintd._fast_sync_ops import DvcFileEntry, ensure_dir_manifest
from mintd._registry_git_ops import GitOpError, SubprocessRegistryGitOps
from mintd.data_ops import PushSummary, commit_pushed_pointers, data_push
from tests._dvc_fixtures import (
    S3HoldsWhatThePointersSay,
    LOCK_MD5,
    MD5,
    stage_dvc_project,
    stage_undeliverable_primary,
    write_pipeline,
    write_pointer,
)
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._harness.git import _git


@pytest.fixture
def push_clients(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeDvcOps:
    """Inject a fake DvcOps and a defaulted Config so ``data push`` never
    touches the real environment. The cwd is an empty git repo in
    ``tmp_path``: the push's pointer check would otherwise read this checkout,
    which tracks ``.dvc`` / ``dvc.lock`` fixtures, and outside a repo it warns."""
    _git(["init", "-q"], cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "mintd._push_verify._create_s3_client",
        lambda cfg, profile, **_: S3HoldsWhatThePointersSay(tmp_path),
    )
    dvc_ops = _FakeDvcOps()
    monkeypatch.setattr(
        "mintd.cli.Config.load", classmethod(lambda cls, path=None: cls(cache_dir=tmp_path / "mintd-cache"))
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_dvc_ops",
        lambda cfg, reporter=None, **_: dvc_ops,
    )
    return dvc_ops


# --- _parse_push_output (the human-line scrape) -----------------------------


@pytest.mark.parametrize(
    "stdout, pushed, up_to_date",
    [
        ("1 file pushed", 1, False),
        ("12 files pushed", 12, False),
        ("Everything is up to date.", 0, True),
        ("0 files pushed", 0, True),
        ("", None, False),
        ("some unexpected dvc banner\nwith no summary", None, False),
    ],
)
def test_parse_push_output(stdout: str, pushed: int | None, up_to_date: bool) -> None:
    result = _parse_push_output(stdout)
    assert result.pushed == pushed
    assert result.up_to_date is up_to_date
    assert result.bytes is None  # dvc push never reports bytes


# --- effective-remote precedence (data_push level) --------------------------


def _write_dvc_config(project: Path, body: str) -> None:
    (project / ".dvc").mkdir(parents=True, exist_ok=True)
    (project / ".dvc" / "config").write_text(body, encoding="utf-8")


def test_remote_precedence_explicit_wins(tmp_path: Path) -> None:
    _write_dvc_config(tmp_path, "[core]\n    remote = from_config\n")
    summary = data_push(
        project_path=tmp_path, dvc_ops=_FakeDvcOps(), remote="explicit"
    )
    assert isinstance(summary, PushSummary)
    assert summary.remote == "explicit"


def test_remote_precedence_config_default(tmp_path: Path) -> None:
    _write_dvc_config(tmp_path, "[core]\n    remote = from_config\n")
    summary = data_push(project_path=tmp_path, dvc_ops=_FakeDvcOps())
    assert summary.remote == "from_config"


def test_remote_precedence_falls_back_to_origin(tmp_path: Path) -> None:
    summary = data_push(project_path=tmp_path, dvc_ops=_FakeDvcOps())
    assert summary.remote == "origin"


def test_data_push_returns_summary_with_counts(tmp_path: Path) -> None:
    dvc_ops = _FakeDvcOps()
    dvc_ops.push_result = DvcPushResult(pushed=4, up_to_date=False)
    summary = data_push(project_path=tmp_path, dvc_ops=dvc_ops, remote="r")
    assert summary.pushed == 4
    assert summary.up_to_date is False
    assert summary.elapsed_s >= 0.0


def test_data_push_never_mixes_dvc_files_and_bare_paths_in_one_argv(
    tmp_path: Path,
) -> None:
    """dvc collects push targets through the same ``index_from_targets`` fast
    path as pull, so a mixed argv uploads only the last ``.dvc`` target's outs
    and exits 0 (measured: 1 of 2 objects, under a ✓). One argv per shape, and
    the best-effort counts add up across them.

    Mutation: push ``targets`` in one call again -> one mixed argv, red.
    """
    dvc_ops = _FakeDvcOps()
    dvc_ops.push_result = DvcPushResult(pushed=2, up_to_date=False)

    summary = data_push(
        project_path=tmp_path,
        dvc_ops=dvc_ops,
        targets=["data/x.csv.dvc", "data/a.csv"],
    )

    assert [c.targets for c in dvc_ops.push_calls] == [
        ["data/x.csv.dvc"], ["data/a.csv"],
    ]
    assert summary.pushed == 4  # 2 per group, summed
    assert summary.up_to_date is False


def test_data_push_targets_none_still_pushes_everything_in_one_argv(
    tmp_path: Path,
) -> None:
    """The no-targets case must stay one ``dvc push`` with no target list —
    dvc's own "push everything tracked". Mutation: group ``None`` -> an empty
    argv list or two calls, red."""
    dvc_ops = _FakeDvcOps()
    data_push(project_path=tmp_path, dvc_ops=dvc_ops)
    assert [c.targets for c in dvc_ops.push_calls] == [None]


def test_data_push_is_up_to_date_only_when_every_group_is(tmp_path: Path) -> None:
    """A ✓ "already up to date" line must not be printed when one group really
    uploaded. Mutation: ``any(...)`` instead of ``all(...)`` -> red."""

    class _MixedPush(_FakeDvcOps):
        def push(
            self,
            *,
            cwd: Path,
            targets: list[str] | None = None,
            remote: str | None = None,
            jobs: int | None = None,
            env: dict[str, str] | None = None,
        ) -> DvcPushResult:
            super().push(cwd=cwd, targets=targets, remote=remote, jobs=jobs, env=env)
            first = len(self.push_calls) == 1
            return (
                DvcPushResult(pushed=0, up_to_date=True)
                if first
                else DvcPushResult(pushed=3, up_to_date=False)
            )

    dvc_ops = _MixedPush()
    summary = data_push(
        project_path=tmp_path, dvc_ops=dvc_ops, targets=["a.dvc", "data/b.csv"],
    )
    assert summary.up_to_date is False
    assert summary.pushed == 3


def test_data_push_real_dvc_lands_every_object_of_a_mixed_selection(
    tmp_path: Path, real_dvc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real dvc, because only real dvc has the bug: pushing a ``.dvc``-tracked
    file and a dvc.lock stage out in one selection must put BOTH objects on the
    remote. Unpatched this uploads 1 of 2 and still prints ✓ pushed, so the
    collaborator who pulls the other out is the one who finds out.

    Mutation: push ``targets`` in one call again -> 1 object on the remote, red.
    """
    from mintd._config import Timeouts
    from mintd._dvc_ops import SubprocessDvcOps
    from tests._harness.git import _git

    for key, value in real_dvc.env.items():
        monkeypatch.setenv(key, value)  # SubprocessDvcOps inherits the redirect
    ws = tmp_path / "ws"
    ws.mkdir()
    remote = tmp_path / "remote"
    _git(["init", "-b", "main", str(ws)])
    real_dvc(["init", "-q"], cwd=ws, check=True)
    real_dvc(["remote", "add", "-d", "storage", str(remote)], cwd=ws, check=True)
    (ws / "data").mkdir()
    for name in ("x.csv", "a.csv"):
        (ws / "data" / name).write_text(f"{name}\n")
    real_dvc(["add", "data/x.csv"], cwd=ws, check=True)
    (ws / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n      - data/a.csv\n")
    real_dvc(["commit", "-f"], cwd=ws, check=True)  # writes dvc.lock, never runs cmd

    summary = data_push(
        project_path=ws,
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()),
        targets=["data/x.csv.dvc", "data/a.csv"],
    )

    blobs = sorted(p.read_text() for p in remote.rglob("*") if p.is_file())
    assert blobs == ["a.csv\n", "x.csv\n"], blobs
    assert summary.up_to_date is False


# --- CLI human-mode line ----------------------------------------------------


def test_cli_push_human_line_shape(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=3, up_to_date=False)
    rc = cli.main(["data", "push", "--remote", "myremote"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "✓ pushed 3 object(s)" in captured.err
    assert "→ s3://myremote" in captured.err
    assert " in " in captured.err  # trailing duration clause
    # No bare token leaks onto stdout.
    assert "pushed" not in captured.out


def test_cli_push_human_line_with_bytes(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    # bytes is never produced by the real scrape, but the renderer must show a
    # size clause if a summary ever carries one.
    push_clients.push_result = DvcPushResult(pushed=2, bytes=2048, up_to_date=False)
    cli.main(["data", "push", "--remote", "r"])
    err = capsys.readouterr().err
    assert "✓ pushed 2 object(s) (2 KB) → s3://r" in err


def test_cli_push_up_to_date_distinct_line(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=0, up_to_date=True)
    rc = cli.main(["data", "push", "--remote", "r"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "✓ already up to date" in err
    assert "→ s3://r" in err
    assert "object(s)" not in err  # not a "pushed 0 object(s)" line


def test_cli_push_unparseable_counts_still_succeeds(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=None, up_to_date=False)
    rc = cli.main(["data", "push", "--remote", "r"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "✓ pushed → s3://r" in err  # no count clause
    assert "object(s)" not in err


# --- CLI --json mode --------------------------------------------------------


def test_cli_push_json_payload(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=5, up_to_date=False)
    rc = cli.main(["--json", "data", "push", "--remote", "r"])
    captured = capsys.readouterr()
    assert rc == 0
    # Exactly one JSON object on stdout, no stray token.
    out = captured.out.strip()
    payload = json.loads(out)
    assert set(payload) == {
        "remote", "pushed", "bytes", "elapsed_s", "up_to_date", "uncommitted_pointers", "git",
    }
    assert payload["uncommitted_pointers"] == []
    assert payload["git"]["state"] == "clean"
    assert payload["remote"] == "r"
    assert payload["pushed"] == 5
    assert payload["bytes"] is None
    assert payload["up_to_date"] is False
    # success() is a no-op under json_mode: the human line must not leak.
    assert "✓ pushed" not in captured.err


def test_cli_push_json_up_to_date(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=0, up_to_date=True)
    cli.main(["--json", "data", "push", "--remote", "r"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["up_to_date"] is True
    assert payload["pushed"] == 0


def test_cli_push_json_unparseable_counts(
    push_clients: _FakeDvcOps, capsys: pytest.CaptureFixture[str]
) -> None:
    push_clients.push_result = DvcPushResult(pushed=None, up_to_date=False)
    rc = cli.main(["--json", "data", "push", "--remote", "r"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert payload["pushed"] is None


# --- pointers the push left uncommitted --------------------------------------


def test_cli_push_names_uncommitted_pointers_instead_of_a_check(
    push_clients: _FakeDvcOps,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A version-aware push writes S3 version IDs into the pointer and leaves
    it uncommitted. Importers read the commit, so `data push` names the file
    and drops the ✓; it still exits 0, because the upload worked.

    A double stands in for dvc's rewrite: `version_aware` applies only to
    cloud remotes, moto's `mock_aws` is in-process and cannot reach a dvc
    subprocess, and moto's server mode needs flask, which is not installed.
    mintd only reads the pointer bytes on disk after the push, which is
    exactly what the double changes; git is real.

    `--no-commit` leaves this commit to the user: this is #61's warning,
    unchanged. The success line is the post-push check's.

    Mutations: `if pointers:` -> `if False:` reddens run 1; warning only when
    `not summary.up_to_date` reddens run 2; dropping the JSON key reddens
    run 3.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    stage_undeliverable_primary(proj)
    # The tree `dvc add` saved in the cache, which a push uploads from.
    tree = ensure_dir_manifest(proj / ".dvc" / "cache", [DvcFileEntry(MD5, "a.parquet")])
    write_pointer(proj, "data/final", is_dir=True, md5=tree.removesuffix(".dir"), version_id=None)
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "track"], cwd=proj)
    monkeypatch.chdir(proj)
    real_push = push_clients.push

    def push_and_pin(**kw):
        result = real_push(**kw)
        write_pointer(kw["cwd"], "data/final", files={"a.parquet": "v-1"})
        return result

    monkeypatch.setattr(push_clients, "push", push_and_pin)

    push_clients.push_result = DvcPushResult(pushed=1, up_to_date=False)
    assert cli.main(["data", "push", "--no-commit"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "1 file(s) verified on" in err
    assert "data/final.dvc" in err and "commit and push" in err
    assert _git(["status", "--porcelain"], cwd=proj).strip()  # data push writes no git

    # A rerun with nothing new to upload: the pointer is still not committed.
    push_clients.push_result = DvcPushResult(pushed=0, up_to_date=True)
    assert cli.main(["data", "push", "--no-commit"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "verified on" in err and "data/final.dvc" in err

    assert cli.main(["--json", "data", "push", "--no-commit"]) == 0
    assert json.loads(capsys.readouterr().out)["uncommitted_pointers"] == ["data/final.dvc"]

    _git(["commit", "-q", "-am", "pin"], cwd=proj)
    _git(["init", "-q", "--bare", "-b", "main", str(tmp_path / "origin.git")])
    _git(["remote", "add", "origin", str(tmp_path / "origin.git")], cwd=proj)
    _git(["push", "-q", "-u", "origin", "main"], cwd=proj)  # a ✓ needs the commit on a remote
    assert cli.main(["data", "push", "--no-commit"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓ 1 file(s) verified on" in err
    assert "data/final.dvc" not in err


def test_cli_push_still_succeeds_when_git_cannot_answer(
    push_clients: _FakeDvcOps,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No git on PATH after an upload that worked: a warning saying the
    pointers could not be checked, the upload's line, exit 0, no traceback.
    Under --json the warning is silent, so the key is null, not the `[]` of
    a clean tree. Mutations: delete the `except GitOpError` arm ->
    traceback; set `[]` there -> the JSON arm reddens; print the ✓ when git
    could not answer -> the ✓ assertion reddens."""
    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "could not check .dvc / dvc.lock files against HEAD" in err
    assert "pushed 1 object(s)" in err
    assert "✓" not in err  # nobody checked the pointers
    assert "Traceback" not in err

    assert cli.main(["--json", "data", "push", "--remote", "r"]) == 0
    assert json.loads(capsys.readouterr().out)["uncommitted_pointers"] is None


def test_cli_push_from_a_subdirectory_names_root_pointers_and_only_pointers(
    push_clients: _FakeDvcOps,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`dvc push` uploads the whole repo from any subdirectory (the scaffold's
    stages run from `code/`), so the check covers the root's `dvc.lock` too;
    and it names pointer files only, not an uncommitted note.

    Mutations: drop `top` from the pointer pathspecs -> a ✓ from `code/`;
    report every uncommitted file -> `notes.txt` is named.
    """
    proj = tmp_path / "proj"
    (proj / "code").mkdir(parents=True)
    (proj / "dvc.lock").write_text("v1\n")
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "seed"], cwd=proj)
    (proj / "dvc.lock").write_text("v2\n")
    (proj / "notes.txt").write_text("not a pointer\n")
    monkeypatch.chdir(proj / "code")

    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "differ from the last commit: dvc.lock" in err
    assert "notes.txt" not in err


def test_cli_push_from_a_nested_repo_checks_the_dvc_project(
    push_clients: _FakeDvcOps,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dvc finds its project by walking up to `.dvc/`; git stops at the
    nearest `.git`. From inside a submodule or a nested clone the push uploads
    the outer project, so its pointers are the ones to check.

    Mutation: query git at the cwd -> a ✓ over the outer `dvc.lock`.
    """
    proj = tmp_path / "proj"
    (proj / ".dvc").mkdir(parents=True)
    (proj / "dvc.lock").write_text("v1\n")
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "seed"], cwd=proj)
    (proj / "dvc.lock").write_text("v2\n")
    (proj / "code").mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj / "code")
    monkeypatch.chdir(proj / "code")

    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "differ from the last commit: dvc.lock" in err


# --- the commit lane: push commits and pushes the pointers it can show are importable ---


def _identity(repo: Path) -> None:
    """Production commits with the repository's own identity, and `real_dvc`
    replaces HOME, so a test repo carries one."""
    for key, value in (("user.email", "test@mintd"), ("user.name", "test"), ("commit.gpgsign", "false")):
        _git(["config", key, value], cwd=repo)


def _upstream(tmp_path: Path, repo: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)])
    _git(["remote", "add", "origin", str(origin)], cwd=repo)
    _git(["push", "-q", "-u", "origin", "main"], cwd=repo)
    return origin


def _producer(tmp_path: Path, *, version_aware: bool = True, commit: bool = True) -> Path:
    """`data/final/` (x.csv, y.csv) tracked by a `.dvc` pointer with no S3
    version ID, its tree saved in the cache the way `dvc add` saves it; with
    `commit`, committed and pushed to a bare `origin.git` upstream."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _identity(proj)
    stage_dvc_project(proj, version_aware=version_aware)
    (proj / ".gitignore").write_text("/data/final/\n/.dvc/cache/\n")
    (proj / "data" / "final").mkdir(parents=True)
    for name in ("x.csv", "y.csv"):
        (proj / "data" / "final" / name).write_text(f"{name}\n")
    tree = ensure_dir_manifest(proj / ".dvc" / "cache", [DvcFileEntry(MD5, "x.csv"), DvcFileEntry(MD5, "y.csv")])
    write_pointer(proj, "data/final", is_dir=True, md5=tree.removesuffix(".dir"), version_id=None)
    if commit:
        _git(["add", "-A"], cwd=proj)
        _git(["commit", "-q", "-m", "track"], cwd=proj)
        _upstream(tmp_path, proj)
    return proj


def _commit_all(proj: Path, message: str) -> None:
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", message], cwd=proj)
    _git(["push", "-q"], cwd=proj)


def _pin_stage(root: Path, stage: str) -> None:
    lock = yaml.safe_load((root / "dvc.lock").read_text())
    for out in lock["stages"][stage]["outs"]:
        out["cloud"] = {"storage": {"version_id": f"v-{stage}"}}
    (root / "dvc.lock").write_text(yaml.safe_dump(lock, sort_keys=False))


def _rewrites_like_dvc(push_clients: _FakeDvcOps, monkeypatch: pytest.MonkeyPatch, *, files=None) -> None:
    """dvc's version-aware rewrite, as a push leaves it on disk. `.dvc` or an
    untargeted push: the whole folder, each file with a version ID (or
    `files`); the bare `data/final/x.csv`: that file only, which is what real
    dvc does; a `dvc.yaml:<stage>` key: that stage's outs. A double because
    `version_aware` needs a cloud remote (see the test above)."""
    real_push = push_clients.push

    def push(**kw):
        result = real_push(**kw)
        root, targets = kw["cwd"], kw.get("targets")
        if targets is None or "data/final.dvc" in targets:
            write_pointer(root, "data/final", files=files if files is not None else {"x.csv": "v-1", "y.csv": "v-2"})
        elif "data/final/x.csv" in targets:
            write_pointer(root, "data/final", files={"x.csv": "v-1"})
        stages = [t.split(":", 1)[1] for t in targets or [] if t.startswith("dvc.yaml:")]
        if targets is None and (root / "dvc.lock").is_file():
            stages = list(yaml.safe_load((root / "dvc.lock").read_text())["stages"])
        for stage in stages:
            _pin_stage(root, stage)
        return result

    monkeypatch.setattr(push_clients, "push", push)


def _err(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return " ".join((captured.err + captured.out).split())


def _head(repo: Path, rev: str = "HEAD") -> str:
    return _git(["rev-parse", rev], cwd=repo).strip()


@pytest.mark.parametrize("kind", ["dvc-file", "lock-stage"])
def test_push_commits_and_pushes_only_the_pointer_files(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    """The version IDs a push writes reach importers only once committed and
    pushed, so `data push` does both, for the pointer files alone: an
    untracked notebook beside them stays untracked.

    Mutation: skip `commit_pushed_pointers` -> no commit, red; commit with
    `git add -A` -> `scratch.ipynb` is committed, red.
    """
    proj = _producer(tmp_path)
    if kind == "lock-stage":
        write_pipeline(proj, "data/out.csv")
        _commit_all(proj, "pipeline")
    target, pointer = ("data/final", "data/final.dvc") if kind == "dvc-file" else ("data/out.csv", "dvc.lock")
    (proj / "scratch.ipynb").write_text("{}\n")
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", target]) == 0
    err = _err(capsys)
    assert "✓" in err and "committed" in err and "pushed it to origin/main" in err
    assert _git(["show", "--name-only", "--format=", "HEAD"], cwd=proj).split() == [pointer]
    assert _head(tmp_path / "origin.git", "main") == _head(proj)
    assert "?? scratch.ipynb" in _git(["status", "--porcelain"], cwd=proj)


def test_push_of_one_file_in_a_tracked_folder_commits_the_whole_folder(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pushed bare, one file of a tracked folder uploads alone and dvc
    rewrites the folder's pointer to list only it. The target resolves to
    the folder's pointer, so the committed pointer lists both files.

    Mutation: push the target as typed -> the pointer lists x.csv only, a
    tree the cache never held, exit 1.
    """
    proj = _producer(tmp_path)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/final/x.csv"]) == 0, _err(capsys)
    assert [c.targets for c in push_clients.push_calls] == [["data/final.dvc"]]
    committed = _git(["show", "HEAD:data/final.dvc"], cwd=proj)
    assert "relpath: x.csv" in committed and "relpath: y.csv" in committed
    assert committed.count("version_id") == 2


def _two_stage_pipeline(proj: Path) -> None:
    (proj / "dvc.yaml").write_text(
        "stages:\n  a:\n    cmd: run\n    outs:\n    - data/a.csv\n"
        "  b:\n    cmd: run\n    outs:\n    - data/b.csv\n"
    )
    out = "    outs:\n    - path: data/{0}.csv\n      md5: " + LOCK_MD5 + "\n      size: 12\n"
    (proj / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  a:\n    cmd: run\n" + out.format("a") + "  b:\n    cmd: run\n" + out.format("b")
    )


def test_push_refuses_to_commit_a_lock_stage_not_on_s3(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`dvc.lock` is one file for every stage, so a push of stage a commits
    whatever else changed in it. The post-push check judges what the commit
    would carry: b re-run and not pushed fails it, named as not in this push,
    and nothing is committed.

    Mutation: check only the stages this push targeted -> phase 2 commits
    b's unpinned entry, red.
    """
    proj = _producer(tmp_path)
    _two_stage_pipeline(proj)
    _commit_all(proj, "pipeline")
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/a.csv"]) == 0, _err(capsys)
    capsys.readouterr()
    first = _head(proj)

    lock = yaml.safe_load((proj / "dvc.lock").read_text())
    lock["stages"]["b"]["outs"][0]["md5"] = "c" * 32  # `dvc repro b`, not pushed
    (proj / "dvc.lock").write_text(yaml.safe_dump(lock, sort_keys=False))
    assert cli.main(["data", "push", "data/a.csv"]) == 1
    err = _err(capsys)
    assert "not in this push: data/b.csv" in err and "no S3 version ID" in err
    assert "✓" not in err
    assert _head(proj) == first


@pytest.mark.parametrize("other", ["code/clean.py", "dvc.yaml", "metadata.json", "notes.txt"])
def test_push_leaves_the_commit_to_you_when_anything_else_changed(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], other: str,
) -> None:
    """Someone mid-edit decides what goes into the commit: any other tracked
    change, a staged new file included, leaves the pointers for them, with
    #61's warning and the reason.

    Mutation: drop the tracked-changes gate -> a commit, red.
    """
    proj = _producer(tmp_path)
    (proj / "code").mkdir()
    (proj / "code" / "clean.py").write_text("v1\n")
    (proj / "dvc.yaml").write_text("stages: {}\n")
    _commit_all(proj, "code")
    if other == "notes.txt":
        (proj / other).write_text("draft\n")
        _git(["add", other], cwd=proj)
    else:
        with (proj / other).open("a") as f:
            f.write("\n")
    before = _head(proj)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/final"]) == 0
    err = _err(capsys)
    assert "commit and push them" in err
    assert f"not committed: {other} also differs from the last commit" in err
    assert _head(proj) == before


@pytest.mark.parametrize("how", ["rejected", "no-upstream"])
def test_push_says_the_commit_is_local_when_git_push_fails(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], how: str,
) -> None:
    """The data is on S3 and the pointer commit is local: exit 1, say both,
    give the command. mintd never pulls, rebases or forces.

    Mutation: report a failed push as success -> exit 0, red; force-push ->
    the rival commit is gone from origin, red.
    """
    proj = _producer(tmp_path)
    origin = tmp_path / "origin.git"
    if how == "rejected":
        rival = tmp_path / "rival"
        _git(["clone", "-q", str(origin), str(rival)])
        (rival / "rival.txt").write_text("r\n")
        _git(["add", "rival.txt"], cwd=rival)
        _git(["commit", "-q", "-m", "rival"], cwd=rival)
        _git(["push", "-q", "origin", "main"], cwd=rival)
    else:
        _git(["branch", "--unset-upstream"], cwd=proj)
    on_origin = _head(origin, "main")
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/final"]) == 1
    err = _err(capsys)
    assert "on S3" in err and "committed locally" in err
    assert ("git pull --rebase" if how == "rejected" else "git push -u origin main") in err
    assert _git(["show", "--name-only", "--format=", "HEAD"], cwd=proj).split() == ["data/final.dvc"]
    assert _head(origin, "main") == on_origin


def test_no_commit_uploads_only(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--no-commit` leaves git alone: #61's warning, exit 0.
    Mutation: ignore the flag -> a commit, red."""
    proj = _producer(tmp_path)
    before = _head(proj)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "--no-commit"]) == 0
    assert "commit and push them" in _err(capsys)
    assert _head(proj) == before
    assert "data/final.dvc" in _git(["status", "--porcelain"], cwd=proj)


@pytest.mark.parametrize("committed", [True, False], ids=["against-the-last-commit", "against-the-folder"])
def test_push_refuses_a_folder_rewritten_to_an_empty_list(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], committed: bool,
) -> None:
    """An emptied listing passes a version-ID check (every listed file has
    one) but imports nothing. Refused while the last commit's pointer, or
    the folder on disk, lists files, and still refused on a rerun.

    Mutation: drop the empty-listing check -> the empty pointer is
    committed, red.
    """
    proj = _producer(tmp_path)
    if not committed:
        _git(["rm", "-q", "--cached", "data/final.dvc"], cwd=proj)
        _git(["commit", "-q", "-m", "untrack"], cwd=proj)
    before = _head(proj)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch, files={})

    for _ in range(2):  # the rerun finds the emptied pointer still on disk
        assert cli.main(["data", "push", "data/final"]) == 1
        err = _err(capsys)
        assert "data/final" in err and "lists no files" in err
        assert _head(proj) == before


def test_push_to_a_non_default_remote_commits_nothing(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Importers pull from the default remote, and on a version-aware remote
    DVC records versions read from it whatever `--remote` says: refused
    before anything uploads. Mutation: drop the refusal -> uploaded, red."""
    proj = _producer(tmp_path)
    before = _head(proj)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "--remote", "other", "data/final"]) == 1
    assert "--remote other isn't the default remote storage" in _err(capsys)
    assert push_clients.push_calls == []
    assert _head(proj) == before


@pytest.mark.parametrize(
    "shape",
    [
        "detached", "no-commit-yet", "deleted-pointer", "nested-project", "nested-clone",
        "branch-tracks-another-name", "config-local-default", "config-local-url",
        "config-not-committed", "dvc-yaml-not-committed", "staged-deletion", "no-default-remote",
    ],
)
def test_push_leaves_the_commit_when_git_state_is_unusual(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], shape: str,
) -> None:
    """Each state where committing is not mintd's call falls back to #61's
    warning with the reason, exit 0. The nested case matters most: a DVC
    project below its git repository's top level must not commit the
    enclosing repo.

    Mutation: drop the matching gate -> a commit (or an exit 1), red.
    """
    reason = {
        "detached": "HEAD is detached",
        "no-commit-yet": "the repository has no commit yet",
        "deleted-pointer": "data/final.dvc was deleted",
        "nested-project": "the DVC project is not the root of its git repository",
        "nested-clone": "you are in another git repository than the DVC project's",
        "branch-tracks-another-name": "wip tracks origin/main, a branch of another name",
        "config-local-default": ".dvc/config.local sends pushes to scratch",
        "config-local-url": ".dvc/config.local overrides storage's url",
        "config-not-committed": ".dvc/config is not committed",
        "dvc-yaml-not-committed": "dvc.yaml is not committed",
        "staged-deletion": "data/final.dvc is staged for deletion",
        "no-default-remote": "no default DVC remote in .dvc/config",
    }[shape]
    if shape == "nested-project":
        outer = tmp_path / "outer"
        outer.mkdir()
        _git(["init", "-q", "-b", "main"], cwd=outer)
        _identity(outer)
        proj = _producer(outer, commit=False)
        shutil.rmtree(proj / ".git")  # one repository, the DVC project a folder in it
        _git(["add", "-A"], cwd=outer)
        _git(["commit", "-q", "-m", "track"], cwd=outer)
        repo = outer
    else:
        proj = repo = _producer(tmp_path, commit=shape != "no-commit-yet")
    if shape == "detached":
        _git(["checkout", "-q", "--detach"], cwd=proj)
    if shape == "branch-tracks-another-name":
        # `git checkout -b wip origin/main` makes origin/main wip's upstream;
        # pushing wip's commits there is what git's own default refuses.
        _git(["checkout", "-q", "-b", "wip", "origin/main"], cwd=proj)
        (proj / "experiment.txt").write_text("wip\n")
        _git(["add", "experiment.txt"], cwd=proj)
        _git(["commit", "-q", "-m", "WIP experiment"], cwd=proj)
    if shape == "config-local-default":
        (proj / ".dvc" / "config.local").write_text("[core]\n    remote = scratch\n")
    if shape == "config-local-url":
        (proj / ".dvc" / "config.local").write_text("['remote \"storage\"']\n    url = s3://scratch/x\n")
    if shape == "config-not-committed":
        _git(["rm", "-q", "--cached", ".dvc/config"], cwd=proj)
        _git(["commit", "-q", "-m", "untrack the config"], cwd=proj)
    if shape == "dvc-yaml-not-committed":
        write_pipeline(proj, "data/out.csv")
        _git(["add", "dvc.lock"], cwd=proj)
        _git(["commit", "-q", "-m", "lock only"], cwd=proj)
    if shape == "staged-deletion":
        _git(["rm", "-q", "--cached", "data/final.dvc"], cwd=proj)
    if shape == "no-default-remote":
        (proj / ".dvc" / "config").write_text("")
        _git(["commit", "-q", "-am", "no remote"], cwd=proj)
    before = _git(["rev-list", "--all"], cwd=repo)
    if shape == "nested-clone":
        (proj / "notebooks").mkdir()
        _git(["init", "-q", "-b", "main"], cwd=proj / "notebooks")  # a separate repository inside the project
        monkeypatch.chdir(proj / "notebooks")
    else:
        monkeypatch.chdir(proj)
    if shape == "deleted-pointer":
        (proj / "data" / "final.dvc").unlink()  # and the plain double rewrites nothing
    else:
        _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push"]) == 0
    err = _err(capsys)
    assert "commit and push them" in err and f"not committed: {reason}" in err
    assert _git(["rev-list", "--all"], cwd=repo) == before
    if shape == "branch-tracks-another-name":
        assert "WIP experiment" not in _git(["log", "main"], cwd=tmp_path / "origin.git")


def test_md5_remote_targeted_push_does_not_commit_an_unpushed_pointer(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """On an md5-layout remote nothing in a pointer shows its data was pushed,
    so a targeted push commits only what it uploaded: a changed pointer it
    did not push stays for the user.

    Mutation: drop the selection check -> data/other.dvc is committed, red.
    """
    proj = _producer(tmp_path, version_aware=False)
    write_pointer(proj, "data/other", md5="c" * 32, version_id=None)
    _commit_all(proj, "other")
    write_pointer(proj, "data/other", md5="d" * 32, version_id=None)  # re-added, not pushed
    before = _head(proj)
    monkeypatch.chdir(proj)  # an md5-layout push rewrites no pointer: the plain double

    assert cli.main(["data", "push", "data/final/x.csv"]) == 0
    assert "not committed: on an md5-layout remote nothing shows data/other.dvc reached S3" in _err(capsys)
    assert _head(proj) == before


def test_failing_pre_commit_hook_leaves_nothing_committed(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hook that refuses the commit: exit 1, the data is on S3, nothing
    committed. Mutation: treat a commit error as success -> exit 0, red."""
    proj = _producer(tmp_path)
    hook = proj / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(b"#!/bin/sh\nexit 1\n")  # LF, so Git for Windows' sh runs it
    hook.chmod(0o755)
    before = _head(proj)
    monkeypatch.setenv("COLUMNS", "1000")  # the hint is checked whole, not wrapped
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/final"]) == 1
    err = _err(capsys)
    assert "on S3" in err and "git commit failed: git printed no reason" in err
    # The hint runs from any directory: `git -C <root>`, quoted paths.
    assert re.search(r"git -C \S+ commit -m 'Record pushed data versions' -- data/final\.dvc && git -C \S+ push", err)
    assert _head(proj) == before


def test_push_from_a_feature_branch_says_when_importers_see_it(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Importers read the remote's default branch: a feature branch's commit
    reaches them when it merges. Mutation: drop the note -> red."""
    proj = _producer(tmp_path)
    _git(["checkout", "-q", "-b", "feature"], cwd=proj)
    _git(["push", "-q", "-u", "origin", "feature"], cwd=proj)
    monkeypatch.chdir(proj)
    _rewrites_like_dvc(push_clients, monkeypatch)

    assert cli.main(["data", "push", "data/final"]) == 0
    err = _err(capsys)
    assert "pushed it to origin/feature" in err and "once feature merges into main" in err


@pytest.mark.parametrize("upstream", [True, False], ids=["ahead-of-upstream", "no-upstream"])
def test_a_rerun_after_a_failed_git_push_says_the_commit_is_still_local(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], upstream: bool,
) -> None:
    """After a failed push the pointers are committed, so a rerun finds
    nothing to commit; importers still see nothing until the commit is
    pushed, so no ✓ while the branch is ahead of its upstream, or has commits
    on no remote at all.

    Mutation: drop the ahead check -> a ✓, red.
    """
    proj = _producer(tmp_path)
    if not upstream:
        _git(["branch", "--unset-upstream"], cwd=proj)
    write_pointer(proj, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})
    _git(["commit", "-q", "-am", "pin, not pushed"], cwd=proj)
    monkeypatch.chdir(proj)

    assert cli.main(["data", "push"]) == 0
    err = _err(capsys)
    where = "origin/main" if upstream else "any remote"
    assert "✓" not in err and f"1 commit(s) on this branch are not on {where} yet" in err
    assert cli.main(["--json", "data", "push"]) == 0
    assert json.loads(capsys.readouterr().out)["git"]["state"] == "unpushed"


@pytest.mark.parametrize("pushed", [True, False], ids=["on-origin-wip", "on-no-remote"])
def test_a_branch_tracking_another_name_is_judged_by_any_remote(
    push_clients: _FakeDvcOps, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], pushed: bool,
) -> None:
    """`git checkout -b wip origin/main` tracks main, but `git push` sends wip
    to origin/wip: whether its commits reached a remote is the question, not
    whether main has them. Mutation: count `@{u}..HEAD` here -> no ✓ for a
    pushed branch, and a `git push` hint git refuses, red."""
    proj = _producer(tmp_path)
    write_pointer(proj, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})  # pinned and pushed
    _commit_all(proj, "pinned")
    _git(["checkout", "-q", "-b", "wip", "origin/main"], cwd=proj)
    (proj / "experiment.txt").write_text("wip\n")
    _git(["add", "experiment.txt"], cwd=proj)
    _git(["commit", "-q", "-m", "WIP experiment"], cwd=proj)
    if pushed:
        _git(["push", "-q", "origin", "wip"], cwd=proj)
    monkeypatch.chdir(proj)

    assert cli.main(["data", "push"]) == 0
    err = _err(capsys)
    if pushed:
        assert "✓" in err and "not on" not in err
    else:
        assert "✓" not in err and "1 commit(s) on this branch are not on any remote yet" in err


def test_commit_that_landed_despite_a_git_error_is_still_pushed(tmp_path: Path) -> None:
    """A hook or the timeout can kill git after the commit landed: HEAD moved,
    so the commit is pushed rather than reported as failed.
    Mutation: return commit_failed on any commit error -> red."""
    proj = _producer(tmp_path)
    write_pointer(proj, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})

    class _KilledAfterCommit(SubprocessRegistryGitOps):
        def commit_paths(self, work_dir: Path, paths: list[str], message: str) -> None:
            super().commit_paths(work_dir, paths, message)
            raise GitOpError(["git", "commit"], "timed out")

    outcome = commit_pushed_pointers(
        proj, ["data/final.dvc"], git_ops=_KilledAfterCommit(), remote=None, selection=None,
    )
    assert outcome.state == "pushed"
    assert _head(tmp_path / "origin.git", "main") == _head(proj)


def test_real_dvc_push_of_one_file_commits_the_whole_folder(
    push_clients: _FakeDvcOps, tmp_path: Path, real_dvc, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real dvc and real git on an md5 local remote: re-adding a changed folder
    and pushing one file of it uploads the whole folder and commits only its
    pointer. Pushed bare, dvc uploads x.csv and the manifest, never y.csv,
    and nothing shows the pointer was pushed, so nothing is committed.

    Mutation: push the target as typed -> y.csv missing and no commit, red.
    """
    from mintd._config import Timeouts
    from mintd._dvc_ops import SubprocessDvcOps

    for key, value in real_dvc.env.items():
        monkeypatch.setenv(key, value)  # SubprocessDvcOps inherits the redirect
    ws = tmp_path / "ws"
    ws.mkdir()
    store = tmp_path / "store"
    _git(["init", "-q", "-b", "main"], cwd=ws)
    _identity(ws)
    real_dvc(["init", "-q"], cwd=ws, check=True)
    real_dvc(["remote", "add", "-d", "storage", str(store)], cwd=ws, check=True)
    (ws / "data" / "final").mkdir(parents=True)
    for name in ("x.csv", "y.csv"):
        (ws / "data" / "final" / name).write_text(f"{name}\n")
    real_dvc(["add", "data/final"], cwd=ws, check=True)
    _git(["add", "-A"], cwd=ws)
    _git(["commit", "-q", "-m", "track"], cwd=ws)
    origin = _upstream(tmp_path, ws)
    (ws / "data" / "final" / "x.csv").write_text("x2\n")
    real_dvc(["add", "data/final"], cwd=ws, check=True)
    monkeypatch.setattr(push_clients, "push", SubprocessDvcOps(timeouts=Timeouts()).push)
    monkeypatch.chdir(ws)

    assert cli.main(["data", "push", "data/final/x.csv"]) == 0, _err(capsys)
    blobs = sorted(p.read_text() for p in store.rglob("*") if p.is_file() and not p.name.endswith(".dir"))
    assert blobs == ["x2\n", "y.csv\n"], blobs
    assert any(p.name.endswith(".dir") for p in store.rglob("*"))  # and the folder's manifest
    assert _git(["log", "-1", "--name-only", "--format="], cwd=origin).split() == ["data/final.dvc"]


# --- the post-push check, against moto ---------------------------------------
#
# S3 is moto. DVC is the fake, whose on_push does to S3 and to the pointer
# files what a dvc push would: `_dvc_uploads` uploads each file that has no
# version ID and records the version S3 returns, and `_dvc_pins_what_is_there`
# reproduces DVC's post-push rebuild, which pins whatever object is at the path
# without checking its bytes.

import hashlib  # noqa: E402

from mintd._fast_sync_ops import cache_path_for  # noqa: E402

KEY = "lab/p/data/x.csv"


def _bytes_md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _x_pointer(proj: Path, data: bytes, cloud: dict | None = None) -> None:
    rec: dict = {"md5": _bytes_md5(data), "size": len(data), "hash": "md5", "path": "x.csv"}
    if cloud is not None:
        rec["cloud"] = cloud
    (proj / "data" / "x.csv.dvc").write_text(yaml.safe_dump({"outs": [rec]}, sort_keys=False))


def _moto_producer(tmp_path: Path, s3, *, data: bytes = b"x-data\n", cache: bool = True,
                   cloud: dict | None = None) -> Path:
    """`data/x.csv` tracked by `data/x.csv.dvc`, committed and pushed to a
    bare upstream; its bytes in the cache unless `cache=False`."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _identity(proj)
    stage_dvc_project(proj)
    (proj / ".gitignore").write_text("/data/x.csv\n/.dvc/cache/\n")
    (proj / "data").mkdir()
    (proj / "data" / "x.csv").write_bytes(data)
    if cache:
        obj = cache_path_for(proj / ".dvc" / "cache", _bytes_md5(data))
        obj.parent.mkdir(parents=True)
        obj.write_bytes(data)
    _x_pointer(proj, data, cloud)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "track"], cwd=proj)
    _upstream(tmp_path, proj)
    return proj


@pytest.fixture
def moto_push(push_clients: _FakeDvcOps, s3_versioned, monkeypatch: pytest.MonkeyPatch):
    s3, bucket = s3_versioned
    monkeypatch.setattr("mintd._push_verify._create_s3_client", lambda cfg, profile, **_: s3)
    return push_clients, s3, bucket


def _records_of(proj: Path):
    doc = yaml.safe_load((proj / "data" / "x.csv.dvc").read_text())
    return doc, doc["outs"][0]


def _dvc_uploads(s3, bucket: str, proj: Path, *, body: bytes | None = None):
    def on_push(cwd, targets):
        doc, rec = _records_of(proj)
        if "storage" in (rec.get("cloud") or {}):
            return
        data = body if body is not None else cache_path_for(proj / ".dvc" / "cache", rec["md5"]).read_bytes()
        put = s3.put_object(Bucket=bucket, Key=KEY, Body=data)
        rec["cloud"] = {"storage": {"etag": put["ETag"].strip('"'), "version_id": put["VersionId"]}}
        (proj / "data" / "x.csv.dvc").write_text(yaml.safe_dump(doc, sort_keys=False))
    return on_push


def _dvc_pins_what_is_there(s3, bucket: str, proj: Path):
    def on_push(cwd, targets):
        doc, rec = _records_of(proj)
        if "storage" in (rec.get("cloud") or {}):
            return
        head = s3.head_object(Bucket=bucket, Key=KEY)
        rec["cloud"] = {"storage": {"etag": head["ETag"].strip('"'), "version_id": head["VersionId"]}}
        (proj / "data" / "x.csv.dvc").write_text(yaml.safe_dump(doc, sort_keys=False))
    return on_push


def test_check_a_push_that_uploads_is_verified_committed_and_pushed(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    assert cli.main(["data", "push"]) == 0, _err(capsys)
    err = _err(capsys)
    assert "✓ 1 file(s) verified on s3://test-bucket/lab/p (1 uploaded and verified)" in err
    assert "pushed it to origin/main" in err
    # Every dvc push of the command ran with one throwaway site cache, gone after.
    site = {c.env["DVC_SITE_CACHE_DIR"] for c in dvc.push_calls}
    assert len(site) == 1 and not Path(site.pop()).exists()


def test_check_dvc_pinning_other_bytes_fails_and_commits_nothing(moto_push, tmp_path, monkeypatch, capsys) -> None:
    """DVC's rebuild pins the object at the path whatever it holds. The offline
    judge passes (an ID is present); only the content proof catches it."""
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    head = _head(proj)
    s3.put_object(Bucket=bucket, Key=KEY, Body=b"someone else's bytes")
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_pins_what_is_there(s3, bucket, proj)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "the pinned version holds other bytes: data/x.csv" in err and "✓" not in err
    assert _head(proj) == head


def test_check_ids_only_under_another_prefix_with_a_push_that_does_nothing_fail(
    moto_push, tmp_path, monkeypatch, capsys,
) -> None:
    """cms-pdc: the recorded IDs exist only under the old prefix, the file is
    in the cache, and DVC uploads nothing. Repair strips the stale ID; the
    check finds no ID after the push and says DVC uploaded nothing."""
    dvc, s3, bucket = moto_push
    old = s3.put_object(Bucket=bucket, Key="old-prefix/data/x.csv", Body=b"x-data\n")["VersionId"]
    proj = _moto_producer(tmp_path, s3, cloud={"storage": {"etag": _bytes_md5(b"x-data\n"), "version_id": old}})
    monkeypatch.chdir(proj)
    dvc.push_result = DvcPushResult(pushed=0, up_to_date=True)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "re-uploading data/x.csv (stale ID)" in err
    assert "the remote has probably moved" in err
    assert "no S3 version ID recorded: data/x.csv" in err and "✓" not in err


def test_check_a_stale_id_with_no_object_and_no_cache_stops_before_anything(
    moto_push, tmp_path, monkeypatch, capsys,
) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3, cache=False,
                          cloud={"storage": {"etag": _bytes_md5(b"x-data\n"), "version_id": "gone"}})
    # A second, fixable pointer: its stale ID would be stripped, but nothing
    # is written while another file can't be fixed.
    y = b"y-data\n"
    obj = cache_path_for(proj / ".dvc" / "cache", _bytes_md5(y))
    obj.parent.mkdir(parents=True)
    obj.write_bytes(y)
    (proj / "data" / "y.csv.dvc").write_text(yaml.safe_dump({"outs": [{
        "md5": _bytes_md5(y), "size": len(y), "hash": "md5", "path": "y.csv",
        "cloud": {"storage": {"etag": _bytes_md5(y), "version_id": "gone"}}}]}, sort_keys=False))
    before = {p: (proj / "data" / p).read_bytes() for p in ("x.csv.dvc", "y.csv.dvc")}
    monkeypatch.chdir(proj)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "nothing was written or pushed" in err and "data/x.csv" in err and "data/y.csv" not in err
    assert "mintd data pull" in err
    assert dvc.push_calls == []
    assert {p: (proj / "data" / p).read_bytes() for p in before} == before


def test_check_dvc_commit_f_is_reattached_with_no_upload(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    v = s3.put_object(Bucket=bucket, Key=KEY, Body=b"x-data\n")["VersionId"]
    proj = _moto_producer(tmp_path, s3)  # committed with no ID, as `dvc commit -f` leaves it
    monkeypatch.chdir(proj)
    puts: list[str] = []
    s3.meta.events.register("before-send.s3.PutObject", lambda request, **_: puts.append(request.url))
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    assert cli.main(["data", "push"]) == 0, _err(capsys)
    err = _err(capsys)
    assert "re-attached data/x.csv (no ID)" in err
    assert "`dvc add` or `dvc commit` rewrote these .dvc files" in err
    assert puts == []
    assert _records_of(proj)[1]["cloud"]["storage"]["version_id"] == v


def test_check_refuses_a_non_default_remote_before_uploading(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    monkeypatch.chdir(proj)
    assert cli.main(["data", "push", "--remote", "backup"]) == 1
    assert "isn't the default remote storage" in _err(capsys)
    assert dvc.push_calls == []


def test_check_refuses_a_remote_with_its_own_access_keys(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    with open(proj / ".dvc" / "config.local", "w") as f:
        f.write("['remote \"storage\"']\n    access_key_id = AKIA\n    secret_access_key = s\n")
    monkeypatch.chdir(proj)
    assert cli.main(["data", "push"]) == 1
    assert "carries its own access keys" in _err(capsys)
    assert dvc.push_calls == []


def test_check_needs_the_pointer_list_from_git(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    shutil.rmtree(proj / ".git")
    (tmp_path / ".git").rename(tmp_path / ".git-moved")  # no repo above it either
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "git couldn't list the pointer files" in err and "✓" not in err


def test_check_a_failed_second_group_says_the_tree_holds_repinned_pointers(
    moto_push, tmp_path, monkeypatch, capsys,
) -> None:
    from mintd._dvc_ops import DvcPushError

    dvc, s3, bucket = moto_push
    s3.put_object(Bucket=bucket, Key=KEY, Body=b"x-data\n")
    proj = _moto_producer(tmp_path, s3)
    head = _head(proj)
    monkeypatch.chdir(proj)
    calls: list[int] = []

    def second_fails(cwd, targets):
        calls.append(1)
        if len(calls) == 2:
            raise DvcPushError("dvc push failed (exit 1): boom")

    dvc.on_push = second_fails
    assert cli.main(["data", "push", "data/x.csv.dvc", "dvc.yaml:nope"]) == 1
    err = _err(capsys)
    assert "re-pinned pointer files (data/x.csv.dvc); re-run mintd data push" in err
    assert _head(proj) == head


def test_check_a_pointer_rewritten_while_being_checked_is_not_committed(
    moto_push, tmp_path, monkeypatch, capsys,
) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    head = _head(proj)
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    armed = {"on": False}
    real_upload = dvc.on_push

    def upload_then_arm(cwd, targets):
        real_upload(cwd, targets)
        armed["on"] = True

    def rewrite_during_check(request, **_):
        if armed["on"] and "versionId=" in request.url:
            armed["on"] = False
            with open(proj / "data" / "x.csv.dvc", "a") as f:
                f.write("# edited mid-check\n")

    dvc.on_push = upload_then_arm
    s3.meta.events.register_first("before-send.s3.HeadObject", rewrite_during_check)
    cli.main(["data", "push"])
    err = _err(capsys)
    assert "data/x.csv.dvc changed after the push was checked" in err and "✓" not in err
    assert _head(proj) == head

    # --no-commit: the re-check before the ✓ catches it too.
    _git(["checkout", "-q", "--", "data/x.csv.dvc"], cwd=proj)
    s3.put_object(Bucket=bucket, Key=KEY, Body=b"other bytes")  # so the file is uploaded and checked
    _x_pointer(proj, b"x-data\n")
    dvc.on_push = upload_then_arm
    assert cli.main(["data", "push", "--no-commit"]) == 1
    err = _err(capsys)
    assert "changed while the push was being checked" in err and "✓" not in err


def test_check_a_commit_hook_that_rewrites_a_pointer_keeps_the_commit_local(
    moto_push, tmp_path, monkeypatch, capsys,
) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    hook = proj / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf '# hook\\n' >> data/x.csv.dvc && git add data/x.csv.dvc\n")
    hook.chmod(0o755)
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "doesn't hold exactly the checked pointer files" in err and "stays local" in err
    assert _head(tmp_path / "origin.git", "main") != _head(proj)  # never pushed


def _moto_pipeline(tmp_path: Path, s3, bucket: str) -> Path:
    """Stage a writes data/a.csv from the data/raw folder; stage b's
    data/b.csv is pinned to a version already on S3. Committed and pushed."""
    proj = _moto_producer(tmp_path, s3)
    b = s3.put_object(Bucket=bucket, Key="lab/p/data/b.csv", Body=b"b\n")
    (proj / "dvc.yaml").write_text(
        "stages:\n"
        "  a:\n    cmd: make a\n    deps: [data/raw]\n    outs: [data/a.csv]\n"
        "  b:\n    cmd: make b\n    outs: [data/b.csv]\n"
    )
    a_bytes = b"a\n"
    obj = cache_path_for(proj / ".dvc" / "cache", _bytes_md5(a_bytes))
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(a_bytes)
    lock = {"schema": "2.0", "stages": {
        "a": {"cmd": "make a",
              "deps": [{"path": "data/raw", "hash": "md5", "md5": "d" * 32 + ".dir", "size": 3, "nfiles": 1}],
              "outs": [{"path": "data/a.csv", "hash": "md5", "md5": _bytes_md5(a_bytes), "size": 2}]},
        "b": {"cmd": "make b",
              "outs": [{"path": "data/b.csv", "hash": "md5", "md5": _bytes_md5(b"b\n"), "size": 2,
                        "cloud": {"storage": {"etag": b["ETag"].strip('"'), "version_id": b["VersionId"]}}}]},
    }}
    (proj / "dvc.lock").write_text(yaml.safe_dump(lock, sort_keys=False))
    _commit_all(proj, "pipeline")
    return proj


def test_check_records_dvc_wasnt_asked_to_change_are_put_back(moto_push, tmp_path, monkeypatch, capsys) -> None:
    """DVC re-dumps whole stages after a push: stage a's folder dep can come
    back bare (no md5) and an out of another stage degraded. Push doesn't
    change the workspace, so the records from before it are put back."""
    dvc, s3, bucket = moto_push
    proj = _moto_pipeline(tmp_path, s3, bucket)
    before = yaml.safe_load((proj / "dvc.lock").read_text())
    monkeypatch.chdir(proj)

    def dvc_push_of_a(cwd, targets):
        lock = yaml.safe_load((proj / "dvc.lock").read_text())
        out = lock["stages"]["a"]["outs"][0]
        put = s3.put_object(Bucket=bucket, Key="lab/p/data/a.csv", Body=b"a\n")
        out["cloud"] = {"storage": {"etag": put["ETag"].strip('"'), "version_id": put["VersionId"]}}
        del lock["stages"]["a"]["deps"][0]["md5"]          # the bare dep
        del lock["stages"]["b"]["outs"][0]["cloud"]        # a sibling stage degraded
        (proj / "dvc.lock").write_text(yaml.safe_dump(lock, sort_keys=False))

    dvc.on_push = dvc_push_of_a
    assert cli.main(["data", "push", "data/a.csv"]) == 0, _err(capsys)
    after = yaml.safe_load((proj / "dvc.lock").read_text())
    assert after["stages"]["a"]["deps"] == before["stages"]["a"]["deps"]
    assert after["stages"]["b"] == before["stages"]["b"]
    assert "storage" in after["stages"]["a"]["outs"][0]["cloud"]


def test_check_an_untouched_pin_holding_other_bytes_fails(moto_push, tmp_path, monkeypatch, capsys) -> None:
    """S3 didn't answer for the pin before the push, so repair left it; the
    check after the push finds it holds other bytes."""
    from botocore.awsrequest import AWSResponse

    dvc, s3, bucket = moto_push
    bad = s3.put_object(Bucket=bucket, Key=KEY, Body=b"other bytes")["VersionId"]
    proj = _moto_producer(tmp_path, s3, cloud={"storage": {"etag": _bytes_md5(b"x-data\n"), "version_id": bad}})
    monkeypatch.chdir(proj)
    first = {"done": False}

    class _Empty:
        def stream(self, **_):
            yield b""

        def read(self, *_):
            return b""

    def no_answer_once(request, **_):
        if not first["done"] and "versionId=" in request.url:
            first["done"] = True
            # 409: unmapped, so inconclusive (botocore re-sends a HEAD's 400 as a region redirect).
            return AWSResponse(request.url, 409, {}, _Empty())
        return None

    s3.meta.events.register_first("before-send.s3.HeadObject", no_answer_once)
    assert cli.main(["data", "push"]) == 1
    err = _err(capsys)
    assert "the pinned version holds other bytes: data/x.csv" in err and "✓" not in err


def test_check_an_endpoint_override_in_config_local_is_not_committed(moto_push, tmp_path, monkeypatch, capsys) -> None:
    dvc, s3, bucket = moto_push
    proj = _moto_producer(tmp_path, s3)
    head = _head(proj)
    (proj / ".dvc" / "config.local").write_text("['remote \"storage\"']\n    endpointurl = https://elsewhere\n")
    monkeypatch.chdir(proj)
    dvc.on_push = _dvc_uploads(s3, bucket, proj)
    cli.main(["data", "push"])
    err = _err(capsys)
    assert "config.local overrides storage's endpointurl" in err and "✓" not in err
    assert _head(proj) == head
