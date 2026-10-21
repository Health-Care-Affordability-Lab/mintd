"""Tests for ``mintd data push`` success-line UX (slice 48).

Covers the Reporter-rendered summary that replaced the bare ``print("pushed")``:
human-mode line shape, ``--json`` payload, effective-remote precedence, the
nothing-to-push case, and graceful degradation when dvc's count can't be parsed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mintd import cli
from mintd._dvc_ops import DvcPushResult, _parse_push_output
from mintd.data_ops import PushSummary, data_push
from tests._dvc_fixtures import stage_undeliverable_primary, write_pointer
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
    dvc_ops = _FakeDvcOps()
    monkeypatch.setattr(
        "mintd.cli.Config.load", classmethod(lambda cls, path=None: cls())
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
        ) -> DvcPushResult:
            super().push(cwd=cwd, targets=targets, remote=remote, jobs=jobs)
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
        "remote", "pushed", "bytes", "elapsed_s", "up_to_date", "uncommitted_pointers",
    }
    assert payload["uncommitted_pointers"] == []
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

    Mutations: `if pointers:` -> `if False:` reddens run 1; warning only when
    `not summary.up_to_date` reddens run 2; dropping the JSON key reddens
    run 3.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    stage_undeliverable_primary(proj)
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
    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "pushed 1 object(s)" in err
    assert "data/final.dvc" in err and "commit and push" in err
    assert _git(["status", "--porcelain"], cwd=proj).strip()  # data push writes no git

    # A rerun with nothing new to upload: the pointer is still not committed.
    push_clients.push_result = DvcPushResult(pushed=0, up_to_date=True)
    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓" not in err
    assert "already up to date" in err and "data/final.dvc" in err

    assert cli.main(["--json", "data", "push", "--remote", "r"]) == 0
    assert json.loads(capsys.readouterr().out)["uncommitted_pointers"] == ["data/final.dvc"]

    _git(["commit", "-q", "-am", "pin"], cwd=proj)
    assert cli.main(["data", "push", "--remote", "r"]) == 0
    err = " ".join(capsys.readouterr().err.split())
    assert "✓ already up to date" in err
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
