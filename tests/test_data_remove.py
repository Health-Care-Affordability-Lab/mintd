"""`mintd data remove` stops tracking with dvc and leaves the data on disk.

It runs `dvc remove` without `--outs`: the pointer and its `.gitignore` line go,
the files stay. The command says so, names what stayed (never its contents),
and warns when git now sees those files as untracked.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from mintd import cli
from mintd.data_ops import data_remove
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.git import _git

SCAFFOLD_IGNORE = "/data/**\n!/data/**/\n!/data/**/.gitkeep\n!/data/**/*.dvc\n"  # mintd init's rule for data/


def test_data_remove_help_says_it_stops_tracking_and_the_data_stays() -> None:
    parser = cli._build_parser()
    data = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["data"]
    text = " ".join(data.format_help().split())
    assert "remove Stop tracking a file or folder with DVC; the data stays on disk" in text


def test_data_remove_returns_the_outs_left_on_disk(tmp_path: Path) -> None:
    (tmp_path / "data" / "final").mkdir(parents=True)
    (tmp_path / "data" / "final" / "x.csv").write_text("x")
    (tmp_path / "data" / "final.dvc").write_text(
        "outs:\n- md5: d751713988987e9331980363e24189ce.dir\n  size: 0\n  nfiles: 0\n  hash: md5\n  path: final\n")
    fake = _FakeDvcOps()
    assert data_remove("data/final.dvc", project_path=tmp_path, dvc_ops=fake) == ["data/final"]
    assert fake.remove_calls[0].name == "data/final.dvc"


@pytest.fixture
def tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dvc):  # noqa: F811
    """A git repo with dvc, data/final/x.csv tracked by data/final.dvc and
    committed; mintd's own dvc runs with the harness's dvc dirs."""
    for k in ("HOME", "USERPROFILE", "DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR", "DVC_SITE_CACHE_DIR"):
        monkeypatch.setenv(k, real_dvc.env[k])
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["config", "user.email", "t@t"], cwd=proj)
    _git(["config", "user.name", "T"], cwd=proj)
    real_dvc(["init", "-q"], cwd=proj, check=True)
    monkeypatch.chdir(proj)

    def track(*, scaffold_ignore: bool) -> Path:
        if scaffold_ignore:
            (proj / ".gitignore").write_text(SCAFFOLD_IGNORE)
        (proj / "data" / "final").mkdir(parents=True)
        (proj / "data" / "final" / "x.csv").write_text("A-ROW-THAT-MUST-NOT-PRINT\n")
        real_dvc(["add", "-q", "data/final"], cwd=proj, check=True)
        _git(["add", "-A"], cwd=proj)
        _git(["commit", "-q", "-m", "track"], cwd=proj)
        return proj

    return track


def test_remove_names_what_stays_and_warns_when_git_now_sees_it(tracked, capsys) -> None:
    """Real dvc: with no other ignore rule, `dvc remove` drops the pointer's
    `.gitignore` line, so git now lists the data as untracked. The command
    names the folder left on disk and says how to keep it out of a commit,
    and never prints a file's contents.

    Mutation: skip the git check -> no warning, red."""
    proj = tracked(scaffold_ignore=False)
    assert cli.main(["data", "remove", "data/final.dvc"]) == 0
    out, err = capsys.readouterr()
    said = " ".join((out + err).split())
    assert "removed: data/final.dvc" in said
    assert "DVC no longer tracks it; the data stays on disk: data/final/" in said
    assert "warning: git now sees 1 file(s) in data/final/ as untracked: a `git add` would commit them" in said
    assert "warning: to keep them out: delete them, or add data/final/ to .gitignore" in said
    assert "A-ROW-THAT-MUST-NOT-PRINT" not in said
    assert (proj / "data" / "final" / "x.csv").is_file() and not (proj / "data" / "final.dvc").exists()
    assert "data/final/x.csv" in _git(["status", "--porcelain", "--untracked-files=all"], cwd=proj)


def test_remove_under_the_scaffold_ignore_rule_names_what_stays_without_a_warning(tracked, capsys) -> None:
    """Under `mintd init`'s rule for data/ (`/data/**`), git still ignores the
    files after the pointer's own line goes: they stay on disk, unseen."""
    proj = tracked(scaffold_ignore=True)
    assert cli.main(["data", "remove", "data/final.dvc"]) == 0
    said = " ".join("".join(capsys.readouterr()).split())
    assert "DVC no longer tracks it; the data stays on disk: data/final/" in said
    assert "as untracked" not in said
    assert _git(["status", "--porcelain", "--untracked-files=all", "--", "data/final"], cwd=proj) == ""
