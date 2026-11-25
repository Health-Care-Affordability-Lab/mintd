"""`mintd check` warns when a `dvc.yaml` input holds files git ignores and DVC
doesn't track: the scaffold's `data/raw/` once a CSV lands in it. `git status`
never lists them and `dvc repro` records them only as inputs, so a fresh clone
can't rerun the stage. A warning only: check moves no data and the exit code
is unchanged.

Real git repos; no dvc subprocess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import mintd
from mintd import cli
from mintd._registry_git_ops import GitOpError, SubprocessRegistryGitOps
from mintd.check import check_project
from tests._dvc_fixtures import stage_dvc_project, write_pointer

GITIGNORE = (Path(mintd.__file__).parent / "files" / "gitignore.txt").read_text(encoding="utf-8")

SCAFFOLD = """\
stages:
  ingest:
    wdir: code
    cmd: python ingest.py
    deps:
    - ingest.py
    - ../data/raw/
    outs:
    - ../data/intermediate/
  validate:
    wdir: code
    cmd: python validate.py
    deps:
    - validate.py
    - ../data/intermediate/
    outs:
    - ../data/final/
"""

UNTRACKED = "tracked by neither git nor DVC"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _project(tmp_path: Path, dvc_yaml: str = SCAFFOLD, *, below_repo_root: bool = False) -> Path:
    """A scaffold-shaped data product in a git repo with nothing committed, as
    `mintd init` leaves it: the scaffold's .gitignore, an unstaged
    code/ingest.py, and an empty data/raw/ holding its .gitkeep."""
    top = tmp_path / "repo"
    top.mkdir()
    _git(top, "init", "-q")
    proj = top / "proj" if below_repo_root else top
    proj.mkdir(exist_ok=True)
    stage_dvc_project(proj)
    (proj / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (proj / "dvc.yaml").write_text(dvc_yaml, encoding="utf-8")
    (proj / "code").mkdir()
    (proj / "code" / "ingest.py").write_text("", encoding="utf-8")
    (proj / "data" / "raw").mkdir(parents=True)
    (proj / "data" / "raw" / ".gitkeep").write_text("", encoding="utf-8")
    return proj


def _put(proj: Path, rel: str, text: str = "x") -> None:
    (proj / rel).parent.mkdir(parents=True, exist_ok=True)
    (proj / rel).write_text(text, encoding="utf-8")


RAW_HINT = [
    "to track it: mintd data add data/raw",
    "then mintd data push uploads it",
    "or let a pipeline stage download it, like the scaffold's fetch stage",
    "whether raw data belongs in lab storage (licence, size) is your call",
]
COPY_IN = "copy it into the project (e.g. data/raw/), and point the dep there"


def _hint(proj: Path) -> list[str]:
    """The lines of the one pipeline_input_untracked finding's hint."""
    [hint] = [f.hint for f in check_project(proj) if f.kind == "pipeline_input_untracked"]
    return hint.splitlines()


def _found(proj: Path, **kw) -> list[tuple[str, str, str]]:
    return [(f.kind, f.severity, f.message) for f in check_project(proj, **kw)
            if f.kind in ("pipeline_input_untracked", "pipeline_input_unchecked")]


def test_a_raw_file_no_one_tracks_is_named(tmp_path: Path) -> None:
    """The scaffold's case. code/ingest.py is a dep too, untracked but not
    ignored, so `git status` already lists it: no warning for it.

    Mutations: return [] -> red; drop the wdir join -> red;
    drop `--ignored --exclude-standard` -> ingest.py is named, red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    found = check_project(proj)
    warned = [f for f in found if f.kind == "pipeline_input_untracked"]
    assert [(f.severity, f.message, f.source) for f in warned] == [(
        "warning",
        f"pipeline input 'data/raw' (dvc.yaml stage 'ingest') is {UNTRACKED}: data/raw/county.csv; "
        "a fresh clone can't rerun the stage",
        proj / "dvc.yaml",
    )]
    assert warned[0].hint.splitlines() == RAW_HINT


def test_it_is_a_warning_and_json_output_stays_clean(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Exit 0 with only the warning, and git's output never reaches stdout:
    `--json` still parses."""
    proj = _project(tmp_path)
    meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    meta["project"]["type"] = "code"
    meta["data_products"] = {"primary": None, "outputs": []}
    (proj / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    _put(proj, "data/raw/county.csv")
    capsys.readouterr()
    assert cli.main(["--json", "check", str(proj)]) == 0
    kinds = [json.loads(line)["kind"] for line in capsys.readouterr().out.splitlines()]
    assert "pipeline_input_untracked" in kinds


def test_a_raw_folder_dvc_tracks_is_not_named(tmp_path: Path) -> None:
    """Mutation: drop the DVC-covered skip -> red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    write_pointer(proj, "data/raw", is_dir=True)
    assert _found(proj) == []


def test_a_raw_file_git_tracks_is_not_named(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    _git(proj, "add", "-f", "data/raw/county.csv")
    assert _found(proj) == []


@pytest.mark.parametrize("out", ["- ../data/intermediate/", "- ../data/intermediate/:\n        cache: false"])
def test_an_input_another_stage_writes_is_not_named(tmp_path: Path, out: str) -> None:
    """data/intermediate/ holds files before any dvc.lock: the ingest stage
    declares it, as a string or as a `{path: options}` mapping.

    Mutations: drop the declared-outs skip -> red; read only string-form
    outs -> the mapping case is red."""
    proj = _project(tmp_path, SCAFFOLD.replace("- ../data/intermediate/\n  validate", f"{out}\n  validate"))
    _put(proj, "data/intermediate/part.parquet")
    assert _found(proj) == []


def test_an_unstaged_script_is_not_named(tmp_path: Path) -> None:
    """Untracked but not ignored: `git status` lists it already.

    Mutation: drop `--ignored --exclude-standard` -> red."""
    proj = _project(tmp_path, SCAFFOLD.replace("- ingest.py", "- ingest.py\n    - new.py"))
    _put(proj, "code/new.py")
    assert _found(proj) == []


def test_only_the_files_no_pointer_covers_are_named(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _put(proj, "data/raw/a.csv")
    _put(proj, "data/raw/b.csv")
    write_pointer(proj, "data/raw/a.csv")
    assert [m for _, _, m in _found(proj)] == [
        f"pipeline input 'data/raw' (dvc.yaml stage 'ingest') is {UNTRACKED}: data/raw/b.csv; "
        "a fresh clone can't rerun the stage"]


def test_files_dvc_doesnt_hash_are_not_named(tmp_path: Path) -> None:
    """git lists `.DS_Store` and a `.dvcignore`d file as ignored; DVC never
    hashes them, so they are no input.

    Mutation: drop the intersection with what DVC sees -> red."""
    proj = _project(tmp_path, SCAFFOLD.replace("- ../data/raw/", "- ../data/raw/\n    - ../data/raw/notes.tmp"))
    (proj / ".dvcignore").write_text("*.tmp\n", encoding="utf-8")
    _put(proj, "data/raw/.DS_Store")
    _put(proj, "data/raw/notes.tmp")
    assert _found(proj) == []


def test_a_foreach_stage_input_is_named(tmp_path: Path) -> None:
    proj = _project(tmp_path, "stages:\n  build:\n    foreach: [a, b]\n    do:\n      wdir: code\n"
                              "      cmd: run ${item}\n      deps:\n      - ../data/raw/\n")
    _put(proj, "data/raw/county.csv")
    assert [m.split(" is ")[0] for _, _, m in _found(proj)] == ["pipeline input 'data/raw' (dvc.yaml stage 'build')"]


def test_a_nested_pipeline_input_resolves_from_its_own_folder(tmp_path: Path) -> None:
    """`../data/raw/` in sub/dvc.yaml is data/raw: the folder is joined
    before the escape test.

    Mutation: test for escape before joining the dvc.yaml's folder -> red."""
    proj = _project(tmp_path, "stages: {}\n")
    _put(proj, "sub/dvc.yaml", "stages:\n  s:\n    cmd: run\n    deps:\n    - ../data/raw/\n")
    _put(proj, "data/raw/county.csv")
    assert _found(proj) == [("pipeline_input_untracked", "warning",
                             f"pipeline input 'data/raw' (sub/dvc.yaml stage 's') is {UNTRACKED}: "
                             "data/raw/county.csv; a fresh clone can't rerun the stage")]


def test_an_input_a_nested_lock_records_is_not_named(tmp_path: Path) -> None:
    """A templated out resolves only in the lock: sub/dvc.lock's `a.csv`
    covers the root stage's dep.

    Mutation: drop `_nested_pipeline_outs` from what covers -> red."""
    proj = _project(tmp_path, "stages:\n  use:\n    cmd: run\n    deps:\n    - sub/a.csv\n")
    _put(proj, "sub/dvc.yaml", "stages:\n  s:\n    foreach: [a]\n    do:\n      cmd: run\n"
                               "      outs:\n      - ${item}.csv\n")
    _put(proj, "sub/dvc.lock", "schema: '2.0'\nstages:\n  s@a:\n    cmd: run\n    outs:\n    - path: a.csv\n"
                               "      hash: md5\n      md5: 0cc175b9c0f1b6a831c399e269772661\n      size: 1\n")
    _put(proj, "sub/.gitignore", "/a.csv\n")
    _put(proj, "sub/a.csv")
    assert _found(proj) == []


def test_a_subrepo_pipeline_is_left_to_its_own_check(tmp_path: Path) -> None:
    proj = _project(tmp_path, "stages: {}\n")
    (proj / "inner" / ".dvc").mkdir(parents=True)
    _put(proj, "inner/dvc.yaml", "stages:\n  s:\n    cmd: run\n    deps:\n    - raw/\n")
    _put(proj, "inner/.gitignore", "/raw/\n")
    _put(proj, "inner/raw/x.csv")
    assert _found(proj) == []


def test_a_project_below_its_repo_root_gets_project_relative_paths(tmp_path: Path) -> None:
    proj = _project(tmp_path, below_repo_root=True)
    _put(proj, "data/raw/county.csv")
    assert [m.split(": ")[1] for _, _, m in _found(proj)] == ["data/raw/county.csv; a fresh clone can't rerun the stage"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't hold a newline in a file name")
def test_a_file_name_holding_a_newline_survives(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _put(proj, "data/raw/a\nb.csv")
    assert [m for _, _, m in _found(proj)] == [
        f"pipeline input 'data/raw' (dvc.yaml stage 'ingest') is {UNTRACKED}: data/raw/a\nb.csv; "
        "a fresh clone can't rerun the stage"]


@pytest.mark.parametrize("raw, why", [
    (b"<<<<<<< HEAD\nstages: {}\n=======\n", "could not read sub/dvc.yaml ("),
    (b"stages:\n  s:\n    cmd: \xff\n", "could not read sub/dvc.yaml ("),
    (b"stages: [a]\n", "sub/dvc.yaml isn't in the shape dvc writes"),
])
def test_an_unreadable_nested_pipeline_is_named(tmp_path: Path, raw: bytes, why: str) -> None:
    """The root dvc.yaml's screen is `dvc_state_unreadable`; a nested one
    gets a warning here, not silence.

    Mutation: skip an unreadable nested dvc.yaml silently -> red."""
    proj = _project(tmp_path)
    (proj / "sub").mkdir()
    (proj / "sub" / "dvc.yaml").write_bytes(raw)
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert found[0][2].startswith(why)


def test_a_damaged_nested_lock_is_named_not_raised(tmp_path: Path) -> None:
    """Mutation: let `PointerCutShort` out of `_nested_pipeline_outs` -> check raises, red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    _put(proj, "sub/dvc.yaml", "stages:\n  s:\n    cmd: run\n    outs:\n    - out\n")
    _put(proj, "sub/dvc.lock", "schema: '2.0'\nstages:\n  s:\n    cmd: run\n    outs:\n    - path: out\n")
    assert _found(proj) == [("pipeline_input_unchecked", "warning", "could not read sub/dvc.lock (out records no "
                             "hash), so pipeline inputs aren't checked")]


@pytest.mark.parametrize("stage", [
    "    deps:\n    - ${item}/raw.csv\n",
    "    wdir: ${dir}\n    deps:\n    - raw.csv\n",
])
def test_a_templated_input_is_named_unchecked(tmp_path: Path, stage: str) -> None:
    """Mutation: skip templated deps silently -> red."""
    proj = _project(tmp_path, "stages:\n  s:\n    cmd: run\n" + stage)
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert found[0][2].startswith("dvc.yaml stage 's' names inputs check can't resolve: ")


@pytest.mark.parametrize("stage, dep", [
    ("    deps:\n    - /scratch/raw.csv\n", "/scratch/raw.csv"),
    ("    deps:\n    - C:/scratch/raw.csv\n", "C:/scratch/raw.csv"),
    ("    wdir: /scratch\n    deps:\n    - raw.csv\n", "raw.csv"),
    ("    deps:\n    - ../../outside/raw.csv\n", "../../outside/raw.csv"),
])
def test_an_input_outside_the_project_is_named(tmp_path: Path, stage: str, dep: str) -> None:
    """Mutation: skip outside deps silently -> red."""
    proj = _project(tmp_path, "stages:\n  s:\n    cmd: run\n" + stage)
    assert _found(proj) == [("pipeline_input_untracked", "warning",
                             f"pipeline input {dep!r} (dvc.yaml stage 's') is outside the project, so a fresh "
                             "clone has it only if that path exists on its machine")]
    assert _hint(proj) == [COPY_IN, *RAW_HINT]


@pytest.mark.parametrize("deps", ["3", "[3, {a: b}]"])
def test_deps_that_are_not_paths_are_named_unchecked(tmp_path: Path, deps: str) -> None:
    proj = _project(tmp_path, f"stages:\n  s:\n    cmd: run\n    deps: {deps}\n")
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert found[0][2].startswith("dvc.yaml stage 's' lists deps check can't read (")


def test_a_url_input_is_left_to_dvc(tmp_path: Path) -> None:
    proj = _project(tmp_path, "stages:\n  s:\n    cmd: run\n    deps:\n    - s3://bucket/raw.csv\n")
    assert _found(proj) == []


class _GitFails:
    def listed_files(self, work_dir: Path, paths: list[str]) -> list[str]:
        raise GitOpError(["git", "ls-files"], "fatal: unable to read index")

    ignored_files = listed_files


def test_git_failing_is_named_not_silent(tmp_path: Path) -> None:
    """Mutation: swallow GitOpError -> red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    assert _found(proj, git_ops=_GitFails()) == [(
        "pipeline_input_unchecked", "warning",
        "could not ask git which pipeline inputs it ignores: fatal: unable to read index")]


def test_a_project_outside_git_is_named_unchecked(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    shutil.rmtree(proj / ".git")
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert "not a git repository" in found[0][2]


def test_ignored_files_with_no_paths_lists_nothing(tmp_path: Path) -> None:
    """With no pathspec git would list every file in the repo.

    Mutation: drop either method's empty-paths guard -> red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/county.csv")
    git = SubprocessRegistryGitOps()
    assert git.ignored_files(proj, []) == [] and git.listed_files(proj, []) == []
    assert git.ignored_files(proj, ["data/raw"]) == ["data/raw/county.csv"]
    assert git.listed_files(proj, ["data/raw"]) == ["data/raw/.gitkeep", "data/raw/county.csv"]


# Where the walk of a dep and git's list spell or see a file differently,
# the file is matched on one spelling or named as unchecked: never passed.


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("no symlinks here")


def test_a_dep_symlinked_outside_the_project_is_named(tmp_path: Path) -> None:
    """DVC hashes through the link; git sees one entry, the link.

    Mutation: read the dep where it is spelled, not where it resolves -> red."""
    proj = _project(tmp_path)
    (proj / "data" / "raw" / ".gitkeep").unlink()
    (proj / "data" / "raw").rmdir()
    elsewhere = tmp_path / "elsewhere"
    _put(elsewhere, "big.csv")
    _symlink_or_skip(proj / "data" / "raw", elsewhere)
    assert _found(proj) == [("pipeline_input_untracked", "warning",
                             "pipeline input 'data/raw' (dvc.yaml stage 'ingest') is a symlink to a path outside the "
                             "project, so a fresh clone has it only if that path exists on its machine")]
    assert _hint(proj) == [COPY_IN, *RAW_HINT]


def test_a_dep_symlinked_inside_the_project_is_read_where_it_resolves(tmp_path: Path) -> None:
    """Mutation: read the dep where it is spelled -> git sees only the link: red."""
    proj = _project(tmp_path)
    (proj / "data" / "raw" / ".gitkeep").unlink()
    (proj / "data" / "raw").rmdir()
    _put(proj, "store/.gitignore", "/raw/\n")
    _put(proj, "store/raw/county.csv")
    _symlink_or_skip(proj / "data" / "raw", proj / "store" / "raw")
    assert [m for _, _, m in _found(proj)] == [
        f"pipeline input 'data/raw' (dvc.yaml stage 'ingest') is {UNTRACKED}: store/raw/county.csv; "
        "a fresh clone can't rerun the stage"]


def test_a_symlinked_folder_inside_a_dep_is_named_unchecked(tmp_path: Path) -> None:
    """dvc hashes neither it nor what it holds (dvc 3.67: a symlinked dep is
    hashed through, a symlinked folder inside one isn't), so what the stage
    reads there goes unrecorded.

    Mutation: drop the symlinked-folder finding -> red."""
    proj = _project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    _put(elsewhere, "big.csv")
    _symlink_or_skip(proj / "data" / "raw" / "sub", elsewhere)
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert "holds symlinked folders, which DVC doesn't hash: data/raw/sub;" in found[0][2]


class _GitSpellsNFC:
    """git on macOS (`core.precomposeunicode`): names in NFC whatever the disk keeps."""

    def listed_files(self, work_dir: Path, paths: list[str]) -> list[str]:
        return [unicodedata.normalize("NFC", f) for f in SubprocessRegistryGitOps().listed_files(work_dir, paths)]

    def ignored_files(self, work_dir: Path, paths: list[str]) -> list[str]:
        return [unicodedata.normalize("NFC", f) for f in SubprocessRegistryGitOps().ignored_files(work_dir, paths)]


def test_a_decomposed_file_name_is_matched_to_gits(tmp_path: Path) -> None:
    """Mutation: compare without NFC -> the file is unmatched: red."""
    proj = _project(tmp_path)
    _put(proj, "data/raw/" + unicodedata.normalize("NFD", "café.csv"))
    assert [m for _, _, m in _found(proj, git_ops=_GitSpellsNFC())] == [
        f"pipeline input 'data/raw' (dvc.yaml stage 'ingest') is {UNTRACKED}: data/raw/café.csv; "
        "a fresh clone can't rerun the stage"]


@pytest.mark.parametrize("repo", ["data/raw", "data/raw/inner"])
def test_a_git_repository_in_a_dep_is_named_unchecked(tmp_path: Path, repo: str) -> None:
    """git doesn't look inside another repository; DVC hashes its files.

    Mutation: pass files git doesn't list -> red."""
    proj = _project(tmp_path)
    _put(proj, f"{repo}/county.csv")
    _git(proj / repo, "init", "-q")
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert found[0][2].startswith("git doesn't list ")
    assert f"{repo}/county.csv" in found[0][2] and f"; {repo} holds a git repository of its own," in found[0][2]


def _case_insensitive(tmp_path: Path) -> bool:
    (tmp_path / "probe").write_text("", encoding="utf-8")
    return (tmp_path / "PROBE").exists()


def test_a_dep_spelled_in_another_case_is_named_unchecked(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("a case-sensitive file system: the dep is simply missing")
    proj = _project(tmp_path, SCAFFOLD.replace("../data/raw/", "../data/RAW/"))
    _put(proj, "data/raw/county.csv")
    found = _found(proj)
    if sys.platform == "win32":
        # realpath gives the folder's own spelling there, so git's list matches
        assert [m.split(": ")[1] for _, _, m in found] == ["data/raw/county.csv; a fresh clone can't rerun the stage"]
        return
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert "git spells the folder or these names differently, e.g. in letter case" in found[0][2]


def test_a_dep_on_the_whole_project_is_matched_to_gits_names(tmp_path: Path) -> None:
    """`.` joined with `data/raw/county.csv` is `./data/...` until normalized.

    Mutation: drop normpath from the shared spelling -> red."""
    proj = _project(tmp_path, "stages:\n  s:\n    cmd: run\n    deps:\n    - .\n")
    _put(proj, "data/raw/county.csv")
    assert [m for _, _, m in _found(proj)] == [
        f"pipeline input '.' (dvc.yaml stage 's') is {UNTRACKED}: data/raw/county.csv; "
        "a fresh clone can't rerun the stage"]


@pytest.mark.parametrize("text, who", [
    ("- a\n- b\n", "sub/dvc.yaml isn't in the shape dvc writes (its stages are not a mapping)"),
    ("stages:\n  s: [a]\n", "sub/dvc.yaml stage 's' isn't in the shape dvc writes"),
    ("stages:\n  s:\n    foreach: [a]\n    do: 3\n", "sub/dvc.yaml stage 's' isn't in the shape dvc writes"),
])
def test_a_nested_pipeline_in_an_odd_shape_is_named(tmp_path: Path, text: str, who: str) -> None:
    """Mutation: skip it silently -> red."""
    proj = _project(tmp_path)
    _put(proj, "sub/dvc.yaml", text)
    found = _found(proj)
    assert [(k, s) for k, s, _ in found] == [("pipeline_input_unchecked", "warning")]
    assert found[0][2].startswith(who)
