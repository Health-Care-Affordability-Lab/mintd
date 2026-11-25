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


EMPTY_DIR = "md5: d751713988987e9331980363e24189ce.dir\n  size: 0\n  nfiles: 0\n  hash: md5"


@pytest.mark.parametrize("on_disk", [True, False], ids=["still-there", "already-gone"])
def test_data_remove_returns_the_outs_left_on_disk(tmp_path: Path, on_disk: bool) -> None:
    """Only an out still on disk is named; one already gone isn't.

    Mutation: drop the on-disk filter -> already-gone red."""
    if on_disk:
        (tmp_path / "data" / "final").mkdir(parents=True)
        (tmp_path / "data" / "final" / "x.csv").write_text("x")
    else:
        (tmp_path / "data").mkdir()
    (tmp_path / "data" / "final.dvc").write_text(f"outs:\n- {EMPTY_DIR}\n  path: final\n")
    fake = _FakeDvcOps()
    assert data_remove("data/final.dvc", project_path=tmp_path, dvc_ops=fake) == (["data/final"] if on_disk else [])
    assert fake.remove_calls[0].name == "data/final.dvc"


@pytest.mark.parametrize("out", ["../../outside", "/etc/hosts"], ids=["dotdot", "absolute"])
def test_an_out_outside_the_project_is_neither_named_nor_probed(tmp_path: Path, out: str) -> None:
    """`cache: false` lets an out point outside the project: it isn't named,
    whether or not something exists there.

    Mutation: drop the inside-the-project filter -> red."""
    proj = tmp_path / "a" / "proj"
    (proj / "data").mkdir(parents=True)
    (tmp_path / "a" / "outside").mkdir()  # where data/../../outside lands; /etc/hosts exists too
    (proj / "data" / "x.dvc").write_text(f"outs:\n- md5: {'ab' * 16}\n  size: 1\n  hash: md5\n  cache: false\n  path: {out}\n")
    assert data_remove("data/x.dvc", project_path=proj, dvc_ops=_FakeDvcOps()) == []


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


def test_removing_a_stage_names_its_out_and_warns_when_git_now_sees_it(tmp_path: Path, monkeypatch, real_dvc,  # noqa: F811
                                                                        capsys) -> None:
    """Real dvc: `mintd data remove <stage>` drops the stage and its outs'
    .gitignore lines, and the outs stay. Outside data/, git sees them even
    under the scaffold's rule, so the same lines are said.

    Mutation: read no outs for a stage -> silent, red."""
    for k in ("HOME", "USERPROFILE", "DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR", "DVC_SITE_CACHE_DIR"):
        monkeypatch.setenv(k, real_dvc.env[k])
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["config", "user.email", "t@t"], cwd=proj)
    _git(["config", "user.name", "T"], cwd=proj)
    real_dvc(["init", "-q"], cwd=proj, check=True)
    (proj / ".gitignore").write_text(SCAFFOLD_IGNORE)
    write = "import pathlib; p = pathlib.Path('build'); p.mkdir(exist_ok=True); (p / 'out.txt').write_text('built')"
    (proj / "dvc.yaml").write_text(f'stages:\n  make:\n    cmd: python -c "{write}"\n    outs:\n    - build/out.txt\n')
    real_dvc(["repro", "-q"], cwd=proj, check=True)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "stage"], cwd=proj)
    monkeypatch.chdir(proj)

    assert cli.main(["data", "remove", "make"]) == 0
    said = " ".join("".join(capsys.readouterr()).split())
    assert "DVC no longer tracks it; the data stays on disk: build/out.txt" in said
    assert "warning: git now sees 1 file(s) in build/out.txt as untracked" in said
    assert "built" not in said.replace("build/", "")
    assert (proj / "build" / "out.txt").is_file()
