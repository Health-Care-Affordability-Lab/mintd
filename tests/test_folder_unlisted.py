"""A folder pointer's file list read against the folder on disk.

dvc writes no file count beside a version-aware folder's `files:` list, so a
pointer cut at the end of an entry reads as a smaller folder, and so does one
recorded before files were added. The folder on disk is the only reference:
`unlisted_on_disk` names the files it holds that the list doesn't, as dvc
would hash them (`.dvcignore`, its built-in ignores, subrepos), leaving OS
junk out of the comparison. Push refuses before upload and `check` reports
an error (so publish refuses).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mintd._dvc_state import dvc_cache_dir, DvcFileEntry
from mintd._pin_proof import unlisted_on_disk
from mintd._fast_sync_ops import ensure_dir_manifest
from mintd.check import check_project
from tests._dvc_fixtures import REMOTE, stage_dvc_project
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.s3 import BUCKET
from tests.test_pointer_cut_short import DVC_YAML, FOLDER_DVC, LOCK

PARTS = ("part-00.csv", "part-01.csv", "part-02.csv")  # what FOLDER_DVC lists


def _folder(root: Path, rel: str, names) -> None:
    for name in names:
        (root / rel / name).parent.mkdir(parents=True, exist_ok=True)
        (root / rel / name).write_text(name)


def _pushed_folder(root: Path, extra=()) -> None:
    """data/final.dvc as dvc 3.67.1 writes it after a version-aware push,
    and data/final/ holding its three files plus ``extra``."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "final.dvc").write_text(FOLDER_DVC)
    _folder(root, "data/final", [*PARTS, *extra])


def test_the_folder_walk_matches_what_dvc_hashes(tmp_path: Path, real_dvc, s3_server) -> None:  # noqa: F811
    """dvc itself writes the list: a folder holding dot-files, a `.gitignore`
    and subfolders, under a root `.dvcignore` and one in its parent folder
    (dvc refuses one inside the folder), pushed to a version-aware remote.
    Nothing on disk is unlisted; one file added afterwards is the only one
    named.

    Mutations: skip `.dvcignore` -> the ignored files are named, red; read
    only the root's -> the parent's file is named, red.
    """
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        real_dvc.env[k] = v
    p = tmp_path / "proj"
    p.mkdir()
    real_dvc(["init", "-q", "--no-scm"], cwd=p, check=True)
    real_dvc(["remote", "add", "-d", "storage", f"s3://{BUCKET}/lab"], cwd=p, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", s3_server.endpoint], cwd=p, check=True)
    real_dvc(["remote", "modify", "storage", "version_aware", "true"], cwd=p, check=True)
    folder = p / "data" / "final"
    for rel, text in {"a.csv": "a", ".hidden.csv": "h", ".gitignore": "*.log\n", "sub/b.csv": "b",
                      "sub/skip.csv": "s", "x.tmp": "t", "deep/er/c.csv": "c"}.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    (p / ".dvcignore").write_text("*.tmp\n")
    (p / "data" / ".dvcignore").write_text("skip.csv\n")
    real_dvc(["add", "-q", "data/final"], cwd=p, check=True)
    r = real_dvc(["push"], cwd=p)
    assert r.returncode == 0, r.stderr
    listed = [f["relpath"] for f in yaml.safe_load((p / "data" / "final.dvc").read_text())["outs"][0]["files"]]
    assert listed == [".gitignore", ".hidden.csv", "a.csv", "deep/er/c.csv", "sub/b.csv"]

    assert unlisted_on_disk(p, ["data/final.dvc"], remote="storage") == []
    (folder / "sub" / "new.csv").write_text("n")
    (u,) = unlisted_on_disk(p, ["data/final.dvc"], remote="storage")
    assert u.unlisted == ("sub/new.csv",)


def test_a_folder_holding_files_its_pointer_doesnt_list_is_named(tmp_path: Path) -> None:
    """Mutation: compare the other way (listed minus on disk) -> nothing
    found, red."""
    _pushed_folder(tmp_path, extra=["part-03.csv", "part-04.csv"])
    (u,) = unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE)
    assert (u.pointer, u.stage, u.folder, u.listed, u.unlisted) == (
        "data/final.dvc", None, "data/final", 3, ("part-03.csv", "part-04.csv"))
    assert u.message() == ("data/final.dvc lists 3 file(s) of data/final/, but the folder on disk holds 2 it doesn't "
                           "list (part-03.csv, part-04.csv): the pointer was cut short, or the folder changed since "
                           "it was recorded")
    assert u.hint().startswith("record the folder again (mintd data add data/final), or list files DVC should skip "
                               "in .dvcignore")


def test_the_message_names_the_first_five_unlisted_files(tmp_path: Path) -> None:
    _pushed_folder(tmp_path, extra=[f"new-{i}.csv" for i in range(7)])
    (u,) = unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE)
    assert "(new-0.csv, new-1.csv, new-2.csv, new-3.csv, new-4.csv and 2 more)" in u.message()


def test_os_junk_is_left_out_of_the_comparison(tmp_path: Path) -> None:
    """Finder writes `.DS_Store` whenever a folder is opened, and its `._*`
    files beside others; Windows writes `Thumbs.db` and `desktop.ini`. None
    of them stops a push, at the top or in a subfolder; a real file beside
    them still does, and is the only one named.

    Mutations: drop the junk exclusion -> red; match it by full path
    instead of name -> the subfolder's junk is named, red.
    """
    junk = [".DS_Store", "._part-00.csv", "Thumbs.db", "desktop.ini"]
    _pushed_folder(tmp_path, extra=[*junk, *(f"sub/{j}" for j in junk)])
    assert unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE) == []
    _folder(tmp_path, "data/final", ["sub/new.csv"])
    (u,) = unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE)
    assert u.unlisted == ("sub/new.csv",)


@pytest.mark.parametrize("case", ["whole", "fewer-on-disk", "folder-absent", "subrepo", "dvc-dir"])
def test_no_folder_is_named_when_disk_holds_nothing_the_list_lacks(tmp_path: Path, case: str) -> None:
    """Files the list holds but the folder lacks (deleted locally, never
    pulled), a folder not on disk, a subrepo inside it, and dvc's own
    `.dvc`/`.git` entries are not unlisted files.

    Mutations: walk into a subrepo, or into `.dvc` -> their files are
    named, red.
    """
    _pushed_folder(tmp_path)
    if case == "fewer-on-disk":
        (tmp_path / "data" / "final" / "part-02.csv").unlink()
    elif case == "folder-absent":
        for name in PARTS:
            (tmp_path / "data" / "final" / name).unlink()
        (tmp_path / "data" / "final").rmdir()
    elif case == "subrepo":
        _folder(tmp_path, "data/final", ["inner/.dvc/config", "inner/data.csv"])
    elif case == "dvc-dir":
        _folder(tmp_path, "data/final", [".dvc/tmp/x", ".git", ".hg/store"])
    assert unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE) == []


@pytest.mark.parametrize("pointer", [
    "deps:\n- path: data/final\n  repo:\n    url: https://example.org/producer\n    rev_lock: abc\n" + FOLDER_DVC,
    FOLDER_DVC.replace("  path: final\n", "  path: final\n  cache: false\n"),
    FOLDER_DVC.replace("  path: final\n", "  path: final\n  push: false\n"),
], ids=["dvc-import", "cache-false", "push-false"])
def test_only_folders_dvc_pushes_are_compared(tmp_path: Path, pointer: str) -> None:
    """A dvc import's list is the producer's, and DVC pushes no `cache:
    false` or `push: false` out: an extra file on disk beside any of them is
    not named.

    Mutation: drop publishable_scope's predicate -> red.
    """
    cache = dvc_cache_dir(tmp_path)
    tree = ensure_dir_manifest(cache, [DvcFileEntry(md5="0" * 32, relpath=n, size=1) for n in PARTS])
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "final.dvc").write_text(pointer.replace("{tree}", tree))
    _folder(tmp_path, "data/final", [*PARTS, "extra.csv"])
    assert unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE) == []


def _dir_pointer(root: Path, names) -> None:
    """data/final.dvc in md5 layout (a `.dir` out, as `dvc add` writes it on a
    remote that isn't version-aware), its list cached, listing ``names``."""
    tree = ensure_dir_manifest(dvc_cache_dir(root), [DvcFileEntry(md5="0" * 32, relpath=n, size=1) for n in names])
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "final.dvc").write_text(
        f"outs:\n- md5: {tree}\n  size: {len(names)}\n  nfiles: {len(names)}\n  hash: md5\n  path: final\n")


@pytest.mark.parametrize("listed", [PARTS, ()], ids=["dir-list", "empty-folder"])
def test_a_file_placed_in_a_dir_tracked_folder_is_named(tmp_path: Path, listed) -> None:
    """A `.dir` list in the cache, and an empty folder (`nfiles: 0`, as
    `mintd init` tracks data/raw), are compared too: a file placed in the
    folder and not yet recorded is named, with the way to record it. A cached
    `.dir` list can't be cut short, so then neither message nor hint says so;
    an empty one may be a pointer emptied since, so it keeps that cause.

    Mutations: skip `.dir` entries (legacy_dir), or skip scope.empty -> red.
    """
    _dir_pointer(tmp_path, listed)
    _folder(tmp_path, "data/final", [*listed, "placed.csv"])
    (u,) = unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE)
    assert (u.folder, u.listed, u.unlisted, u.cut_possible) == ("data/final", len(listed), ("placed.csv",), not listed)
    assert u.hint().startswith("record the folder again (mintd data add data/final), or list files DVC should skip "
                               "in .dvcignore")
    if listed:
        assert u.message().endswith(": files were added since the folder was recorded")
        assert "restore it from git" not in u.hint()
    else:
        assert u.message().endswith(": the pointer was cut short, or the folder changed since it was recorded")


def test_a_dir_folder_whose_list_isnt_cached_is_left_out(tmp_path: Path) -> None:
    """Without the `.dir` list in the cache there's nothing to compare."""
    _dir_pointer(tmp_path, PARTS)
    for obj in dvc_cache_dir(tmp_path).rglob("*.dir"):
        obj.unlink()
    _folder(tmp_path, "data/final", [*PARTS, "placed.csv"])
    assert unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE) == []


def test_check_reports_a_file_placed_in_an_empty_tracked_folder(tmp_path: Path) -> None:
    """The case `mintd init data` creates: data/raw tracked while empty, then
    a raw file placed in it. check names it, as for any tracked folder."""
    stage_dvc_project(tmp_path)
    _dir_pointer(tmp_path, ())
    _folder(tmp_path, "data/final", ["placed.csv"])
    found = [f for f in check_project(tmp_path) if f.kind == "dvc_folder_unlisted"]
    assert len(found) == 1 and "placed.csv" in found[0].message
    assert "mintd data add data/final" in (found[0].hint or "")


def test_a_lock_folder_out_is_compared_with_its_folder(tmp_path: Path) -> None:
    """The same comparison for a stage's folder out, named by its stage,
    with the stage's way to record it again."""
    (tmp_path / "dvc.yaml").write_text(DVC_YAML)
    (tmp_path / "dvc.lock").write_text(LOCK)
    _folder(tmp_path, "data", ["a.csv", "out/a.csv", "out/b.csv", "out/c.csv"])
    (u,) = unlisted_on_disk(tmp_path, ["dvc.lock"], remote=REMOTE)
    assert (u.pointer, u.stage, u.folder, u.unlisted) == ("dvc.lock", "build", "data/out", ("c.csv",))
    assert "dvc.lock (stage build) lists 2 file(s) of data/out/" in u.message()
    assert "dvc commit dvc.yaml:build" in u.hint()


def test_check_reports_a_folder_holding_files_its_pointer_doesnt_list(tmp_path: Path) -> None:
    """`check` (and so publish, which runs it) reports it as an error, even
    when the pointer is committed as it is.

    Mutation: drop the finding -> red.
    """
    stage_dvc_project(tmp_path)
    _pushed_folder(tmp_path, extra=["part-03.csv"])
    found = [f for f in check_project(tmp_path) if f.kind == "dvc_folder_unlisted"]
    assert [(f.severity, f.source) for f in found] == [("error", tmp_path / "data" / "final.dvc")]
    assert "holds 1 it doesn't list (part-03.csv)" in found[0].message


def test_publish_refuses_a_committed_folder_pointer_shorter_than_its_folder(tmp_path: Path) -> None:
    """A cut (or stale) pointer committed as it is passes the push's commit
    step, which only judges pointers that differ from HEAD. Publish runs
    `check`, so it refuses before anything is tagged or advertised.

    Mutation: drop the `check` finding -> publish goes ahead, red.
    """
    import subprocess

    from mintd.publish import PublishBlocked, publish_project
    from tests._fakes.dvc_ops import _FakeDvcOps
    from tests._fakes.registry_git_ops import _FakeRegistryGitOps
    from tests.test_publish import _FakeCatalogClient, _seed_project

    proj = _seed_project(tmp_path)
    stage_dvc_project(proj)
    _pushed_folder(proj, extra=["part-03.csv"])
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "cut"], cwd=proj, check=True)

    with pytest.raises(PublishBlocked) as excinfo:
        publish_project(project_path=proj, version="0.2.0", client=_FakeCatalogClient(),
                        dvc_ops=_FakeDvcOps(), git_ops=_FakeRegistryGitOps())
    assert [f.kind for f in excinfo.value.findings] == ["dvc_folder_unlisted"]


def _nested_pipeline(root: Path, extra=()) -> None:
    """A pipeline in sub/ (its own dvc.yaml and dvc.lock, as dvc 3.67.1
    writes them after a version-aware push), its folder out holding
    ``extra`` beside the two files the lock lists."""
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "sub" / "dvc.yaml").write_text(DVC_YAML)
    (root / "sub" / "dvc.lock").write_text(LOCK)
    _folder(root, "sub/data", ["a.csv", "out/a.csv", "out/b.csv", *(f"out/{e}" for e in extra)])


def test_a_nested_pipeline_folder_is_compared_and_named_by_its_own_lock(tmp_path: Path) -> None:
    """Mutation: read only the root dvc.lock -> nothing found, red."""
    _nested_pipeline(tmp_path, extra=["c.csv"])
    (u,) = unlisted_on_disk(tmp_path, ["sub/dvc.lock"], remote=REMOTE)
    assert (u.pointer, u.stage, u.folder, u.unlisted) == ("sub/dvc.lock", "build", "sub/data/out", ("c.csv",))
    assert "dvc commit sub/dvc.yaml:build" in u.hint()


def test_a_folder_holding_a_dvc_file_is_named_with_why(tmp_path: Path) -> None:
    """A `.dvc` file or folder at the top of a tracked folder makes dvc
    record it as an empty folder, so the message says that rather than
    blaming a cut, and the hint says to move it out first.

    Mutation: drop the `.dvc` check -> the cut-or-changed wording, red.
    """
    _pushed_folder(tmp_path, extra=[".dvc"])
    (u,) = unlisted_on_disk(tmp_path, ["data/final.dvc"], remote=REMOTE)
    assert u.unlisted == (".dvc",)
    assert "it holds a .dvc file or folder, so DVC would record it as an empty folder" in u.message()
    assert u.hint().startswith("move the .dvc file or folder out of data/final/, then record the folder again")


def test_check_and_publish_compare_a_nested_pipeline_folder(tmp_path: Path) -> None:
    """`check` reads every lockfile a push reads, nested pipelines included,
    so publish refuses a committed cut there too.

    Mutation: give `check` only the root dvc.lock -> no finding, publish
    goes ahead, red.
    """
    import subprocess

    from mintd.publish import PublishBlocked, publish_project
    from tests._fakes.dvc_ops import _FakeDvcOps
    from tests._fakes.registry_git_ops import _FakeRegistryGitOps
    from tests.test_publish import _FakeCatalogClient, _seed_project

    proj = _seed_project(tmp_path)
    stage_dvc_project(proj)
    _nested_pipeline(proj, extra=["c.csv"])
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "cut"], cwd=proj, check=True)

    found = [f for f in check_project(proj) if f.kind == "dvc_folder_unlisted"]
    assert [f.source for f in found] == [proj / "sub" / "dvc.lock"]
    with pytest.raises(PublishBlocked) as excinfo:
        publish_project(project_path=proj, version="0.2.0", client=_FakeCatalogClient(),
                        dvc_ops=_FakeDvcOps(), git_ops=_FakeRegistryGitOps())
    assert "dvc_folder_unlisted" in [f.kind for f in excinfo.value.findings]
