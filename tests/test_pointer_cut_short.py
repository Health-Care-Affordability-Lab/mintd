"""A pointer file cut short, read by every reader with one rule.

A write to a `.dvc` file or `dvc.lock` that stops partway (dvc killed while
it dumps one) can leave a null where DVC writes a value, or a file list cut
between two entries. Read on, DVC pushes nothing for it at exit 0 and a pull
lands an empty folder. `check_dvc_file` / `check_lock` say when a pointer
isn't whole; both parsers raise `PointerCutShort` for it, and push, pull,
clone, check, import and the delivery verdict all stop on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._dvc_state import delivery, does_not_parse, stages_behind
from mintd._fast_sync_ops import (
    PointerCutShort,
    SubprocessFastSyncOps,
    parse_dvc_lock_outs,
    parse_dvc_outs,
)
from mintd.check import check_project
from mintd.data_ops import data_pull
from tests._dvc_fixtures import REMOTE, stage_dvc_project
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.fast_sync_ops import _FakeFastSyncOps

A = "60b725f10c9c85c70d97880dfe8191b3"
TREE = "f39414088c4df9b3b5fa08e518820649.dir"

# What dvc 3.67.1 writes after `dvc push` to a version-aware remote (the
# lock's `cmd` shortened). A folder out holds `hash`, `path` and `files`
# only: no `md5`, `size` or `nfiles` beside the list (`Output.dumpd` with
# `with_files`), so nothing in the file says how many entries it had.
FOLDER_DVC = """\
outs:
- hash: md5
  path: final
  files:
  - relpath: part-00.csv
    md5: 60b725f10c9c85c70d97880dfe8191b3
    size: 2
    cloud:
      storage:
        etag: 60b725f10c9c85c70d97880dfe8191b3
        version_id: f2cbcca4-beb0-4664-9786-d5feb70d76e0
  - relpath: part-01.csv
    md5: 3b5d5c3712955042212316173ccf37be
    size: 2
    cloud:
      storage:
        etag: 3b5d5c3712955042212316173ccf37be
        version_id: f87e7cd2-eead-4441-8bdc-c7a5fb8eb7db
  - relpath: part-02.csv
    md5: 2cd6ee2c70b0bde53fbe6cac3c8b8bb1
    size: 2
    cloud:
      storage:
        etag: 2cd6ee2c70b0bde53fbe6cac3c8b8bb1
        version_id: e1dce057-0771-4e9a-abd4-69e0f2ebcc00
"""
FILE_DVC = """\
outs:
- md5: 401b30e3b8b5d629635a5c613cdb7919
  size: 2
  hash: md5
  path: x.csv
  cloud:
    storage:
      etag: 401b30e3b8b5d629635a5c613cdb7919
      version_id: 44986e95-4cf5-4704-974e-3f8efddac79f
"""
LOCK = """\
schema: '2.0'
stages:
  build:
    cmd: python build.py
    deps:
    - path: build.py
      hash: md5
      md5: bd4ed4d14c653a0d49ea45244ce91de5
      size: 192
    outs:
    - path: data/a.csv
      hash: md5
      md5: 60b725f10c9c85c70d97880dfe8191b3
      size: 2
      cloud:
        storage:
          etag: 60b725f10c9c85c70d97880dfe8191b3
          version_id: b1c1faa6-fbeb-4aca-b808-edd1cefa2fe3
    - path: data/out
      hash: md5
      files:
      - relpath: a.csv
        md5: b026324c6904b2a9cb4b88d6d61c81d1
        size: 2
        cloud:
          storage:
            etag: b026324c6904b2a9cb4b88d6d61c81d1
            version_id: 1829089e-b489-4f35-a969-f2bfa057f0bf
      - relpath: b.csv
        md5: 26ab0db90d72e28ad0ba1e22ee510510
        size: 2
        cloud:
          storage:
            etag: 26ab0db90d72e28ad0ba1e22ee510510
            version_id: 8530b6ca-8c65-49c9-8110-b3035fa10364
"""
# What a push to a version-aware remote writes when dvc.yaml declares a
# stage that never ran: `b` with `path` and `hash` only, no md5.
UNRUN_LOCK = """\
schema: '2.0'
stages:
  a:
    cmd: run
    outs:
    - path: data/a
      hash: md5
      files:
      - relpath: f.csv
        md5: 1e84025731a9331f59f3a61078fe4420
        size: 6
        cloud:
          storage:
            etag: 1e84025731a9331f59f3a61078fe4420
            version_id: c18f6d06-d13d-4b1b-a4b4-da6c4d89ed19
  b:
    cmd: run
    outs:
    - path: data/b
      hash: md5
"""
UNRUN_DVC_YAML = "stages:\n  a:\n    cmd: run\n    outs:\n    - data/a\n  b:\n    cmd: run\n    outs:\n    - data/b\n"
DVC_YAML = "stages:\n  build:\n    cmd: python build.py\n    deps:\n    - build.py\n    outs:\n    - data/a.csv\n    - data/out\n"

FILES_NULL = "outs:\n- path: final\n  hash: md5\n  files:\n"


@pytest.mark.parametrize("text, detail", [
    ("", "it is empty"),
    ("outs:\n", "it lists no outputs"),
    ("outs: []\n", "it lists no outputs"),
    ("outs:\n-\n", "an output entry is empty"),
    (f"outs:\n- md5: {A}\n  size: 2\n", "an output has no path"),
    ("outs:\n- path: x\n  hash: md5\n", "x records no hash"),
    ("outs:\n- md5:\n  path: x\n", "x records no hash"),
    (FILES_NULL, "the file list of final is missing"),
    ("outs:\n- path: final\n  hash: md5\n  files:\n  -\n", "a file entry of final is incomplete"),
    ("outs:\n- path: final\n  hash: md5\n  files:\n  - relpath: a.csv\n", "a file entry of final is incomplete"),
    (f"outs:\n- path: final\n  hash: md5\n  files:\n  - relpath:\n    md5: {A}\n", "a file entry of final is incomplete"),
    (f"outs:\n- path: final\n  hash: md5\n  files:\n  - relpath: a.csv\n    md5: {A}\n    size: [2]\n",
     "a file entry of final is incomplete"),
    (f"outs:\n- md5: {A}\n  size: twelve\n  path: x\n", "a value of x is not in the shape dvc writes"),
    ("outs: [\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  extra: !!bool x\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  size: !!int ''\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  size: !!int x\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  size: !!float x\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  recorded: !!timestamp 2020-02-30\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  recorded: 2020-02-30\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  size: {'9' * 4400}\n", "it isn't valid YAML"),
    (f"outs:\n- md5: {A}\n  path: x\n  cloud:\n    s\n", "the version block of x is incomplete"),
    (f"outs:\n- md5: {A}\n  path: x\n  cloud:\n    storage:\n      etag\n", "the version block of x is incomplete"),
    (f"outs:\n- md5: {A}\n  path: x\n  cloud:\n    storage:\n      version_id: [v]\n",
     "the version block of x is incomplete"),
], ids=["empty", "outs-null", "outs-empty", "out-null", "no-path", "no-hash", "md5-null", "files-null",
        "entry-null", "entry-no-md5", "entry-no-relpath", "entry-size-not-a-number", "size-not-a-number",
        "not-yaml", "a-bool-tag-on-no-bool", "an-int-tag-on-nothing", "an-int-tag-on-a-word",
        "a-float-tag-on-a-word", "a-tagged-date-no-calendar-has", "a-date-no-calendar-has",
        "a-number-too-long-to-read", "cloud-key-cut", "remote-entry-cut", "version-id-not-text"])
def test_a_dvc_file_cut_short_raises_naming_it(tmp_path: Path, text: str, detail: str) -> None:
    """Mutations, each through `_check_out` / `check_dvc_file`: let a null or
    empty `outs` through, a null out, a missing path, a missing hash, a null
    file list, an entry without its md5 or relpath, a value of the wrong
    type, or a remote's version block that isn't a mapping -> the matching
    case reads on (or raises ValueError), red. Drop LookupError from
    `load12`'s except -> the `!!bool x` and `!!int ''` cases raise KeyError
    and IndexError; drop ValueError -> the word, impossible-date and
    too-long cases raise ValueError; red."""
    (tmp_path / "x.dvc").write_text(text)
    with pytest.raises(PointerCutShort) as excinfo:
        parse_dvc_outs(tmp_path / "x.dvc", REMOTE)
    assert str(excinfo.value) == f"{tmp_path / 'x.dvc'} is cut short or damaged: {detail}"


@pytest.mark.parametrize("text, detail", [
    ("", "it lists no stages"),
    ("schema: '2.0'\nstages:\n", "it lists no stages"),
    ("schema: '2.0'\nstages:\n  build:\n", "stage build is empty"),
    ("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n", "stage build lists no outputs"),
    ("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: data/out\n", "data/out records no hash"),
    ("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: data/out\n      hash:\n", "data/out records no hash"),
    ("schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: data/out\n      hash: md5\n      files:\n",
     "the file list of data/out is missing"),
    (f"schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: data/a.csv\n      md5: {A}\n      size: two\n",
     "a value of data/a.csv is not in the shape dvc writes"),
    (f"schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - path: data/a.csv\n      md5: {A}\n      size: !!float ''\n",
     "it isn't valid YAML"),
], ids=["empty", "stages-null", "stage-null", "outs-null", "no-hash", "hash-null", "files-null", "size-not-a-number",
        "a-float-tag-on-nothing"])
def test_a_dvc_lock_cut_short_raises_naming_it(tmp_path: Path, text: str, detail: str) -> None:
    """Mutations: `check_lock` lets a null stage or stage outs through, or
    an out with no `hash` key, or a null one -> red."""
    (tmp_path / "dvc.yaml").write_text(DVC_YAML)
    (tmp_path / "dvc.lock").write_text(text)
    with pytest.raises(PointerCutShort, match=f"is cut short or damaged: {detail}"):
        parse_dvc_lock_outs(tmp_path, REMOTE)


def test_a_lock_dvc_wrote_for_a_stage_that_never_ran_reads_on_and_is_behind(tmp_path: Path) -> None:
    """A push to a version-aware remote writes a declared stage that never
    ran with no hash. It isn't damage: the pull reads the lock on, skipping
    that out as main did, and only the push's check counts the stage as
    behind (it refuses, naming the stage, never calling the lock damaged).

    Mutations: let `check_lock` refuse an out with `hash` but no md5 -> it
    raises, red; count every out in `stages_behind` -> b isn't behind, red.
    """
    (tmp_path / "dvc.yaml").write_text(UNRUN_DVC_YAML)
    (tmp_path / "dvc.lock").write_text(UNRUN_LOCK)
    (out,) = parse_dvc_lock_outs(tmp_path, REMOTE)
    assert out.path == "data/a" and [f.relpath for f in out.files or []] == ["f.csv"]
    assert not does_not_parse(tmp_path, "dvc.lock")
    assert stages_behind(tmp_path, "dvc.lock", ["a", "b"]) == ["b"]


def test_a_cut_dvc_lock_with_no_dvc_yaml_beside_it_reads_as_nothing(tmp_path: Path) -> None:
    """DVC never reads a dvc.lock with no dvc.yaml beside it, so neither does
    the pull: no error for a stray one.

    Mutation: raise whether or not dvc.yaml exists -> red.
    """
    (tmp_path / "dvc.lock").write_text("schema: '2.0'\nstages:\n  build:\n")
    assert parse_dvc_lock_outs(tmp_path, REMOTE) == []


@pytest.mark.parametrize("name", ["on", "2024"])
def test_a_stage_name_yaml_reads_as_a_boolean_or_number_reads_as_text(tmp_path: Path, name: str) -> None:
    """dvc writes a stage named `on` bare, which YAML 1.1 reads as True, so a
    lock stage key isn't always a string. It reads as text, with no crash
    and no refusal: the lock is whole.

    Mutation: use the key as read -> AttributeError on `.split`, red.
    """
    (tmp_path / "dvc.yaml").write_text(f"stages:\n  {name}:\n    cmd: run\n    outs:\n    - a.csv\n")
    (tmp_path / "dvc.lock").write_text(
        f"schema: '2.0'\nstages:\n  {name}:\n    cmd: run\n    outs:\n    - path: a.csv\n      hash: md5\n"
        f"      md5: {A}\n      size: 2\n")
    (out,) = parse_dvc_lock_outs(tmp_path, REMOTE)
    assert out.path == "a.csv"


@pytest.mark.parametrize("text", [
    f"outs:\n- md5: {A}\n  path: a\n  version_id:\n",
    f"outs:\n- md5: {A}\n  path: a\n  cloud:\n    {REMOTE}:\n      version_id:\n",
    f"outs:\n- md5: {A}\n  path: a\n  cloud:\n    {REMOTE}:\n",
    f"outs:\n- md5: {TREE}\n  path: d\n  files:\n  - relpath: a\n    md5: {A}\n    cloud:\n      {REMOTE}:\n"
    "        version_id:\n",
], ids=["top-level", "cloud", "remote-null", "file-entry"])
def test_a_null_version_id_reads_as_unpinned(tmp_path: Path, text: str) -> None:
    """A version ID cut to null is no ID: unpinned, never the string "None"
    sent to S3 as `VersionId`.

    Mutation: test `"version_id" in ...` again -> "None", red.
    """
    (tmp_path / "x.dvc").write_text(text)
    (out,) = parse_dvc_outs(tmp_path / "x.dvc", REMOTE)
    assert out.version_id is None and all(f.version_id is None for f in out.files or [])


def _strings(outs) -> list[str]:
    found = []
    for o in outs:
        found += [o.md5, o.path, o.version_id]
        found += [v for f in o.files or [] for v in (f.md5, f.relpath, f.version_id)]
    return [s for s in found if s is not None]


@pytest.mark.parametrize("name, text", [("final.dvc", FOLDER_DVC), ("x.csv.dvc", FILE_DVC), ("dvc.lock", LOCK)])
def test_every_cut_of_a_pointer_reads_whole_or_is_refused(tmp_path: Path, name: str, text: str) -> None:
    """Cut the pointer at every byte. Each cut raises `PointerCutShort` or
    reads on with no other exception and never "None" for a hash, path or
    ID (a value cut partway reads as a shorter string, a file pointer's path
    too). A folder reads with its whole file list or, cut at the end of an
    entry, with the first entries only: dvc writes no count beside the list,
    so nothing in the file shows that cut (the push must catch it against
    the folder on disk; not yet). A dvc.lock cut that drops an out, or cuts
    one right after `hash: md5` (the shape dvc writes for a stage that never
    ran), is refused by the push's check as behind, and on a `.dvc` file the
    push's check and the parsers agree on every cut.

    Mutations: drop any arm of `_check_out` or `check_lock`, or the push
    check's out count -> some cut reads on, red.
    """
    lock = name == "dvc.lock"
    if lock:
        (tmp_path / "dvc.yaml").write_text(DVC_YAML)
    path = tmp_path / name

    def parse():
        return parse_dvc_lock_outs(tmp_path, REMOTE) if lock else parse_dvc_outs(path, REMOTE)

    path.write_text(text)
    whole = {o.path: [f.relpath for f in o.files or []] for o in parse()}
    refused = shorter = behind_after_hash = 0
    for n in range(len(text)):
        path.write_text(text[:n])
        try:
            outs = parse()
        except PointerCutShort:
            refused += 1
            assert does_not_parse(tmp_path, name), n
            continue
        assert "None" not in _strings(outs), (n, text[:n])
        for o in outs:
            if o.path not in whole:  # a file pointer's path, written after its hash, cut partway: shorter
                assert any(w.startswith(o.path) for w in whole) and not o.files, (n, text[:n])
                continue
            listed = [f.relpath for f in o.files or []]
            assert listed == whole[o.path][:len(listed)], (n, text[:n])
            shorter += len(listed) < len(whole[o.path])
        if not lock:
            assert not does_not_parse(tmp_path, name), (n, text[:n])
        elif len(outs) < len(whole):
            assert not does_not_parse(tmp_path, name) and stages_behind(tmp_path, name, ["build"]), (n, text[:n])
            behind_after_hash += text[:n].endswith("hash: md5\n")
    assert refused
    assert bool(behind_after_hash) == lock  # a lock out cut after `hash: md5` reads as a stage that hasn't run
    assert bool(shorter) == ("files:" in text)  # the gap is real wherever a folder is listed


def test_delivery_stops_on_a_cut_pointer_rather_than_call_it_pinned(tmp_path: Path) -> None:
    """A folder pointer cut right after `files:` used to read as a pinned
    empty folder, which `check` and `data import` both accepted.

    Mutation: read a null file list as `[]` -> "pinned", red.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_text(FILES_NULL)
    with pytest.raises(PointerCutShort, match="final.dvc is cut short"):
        delivery(tmp_path, "data/final/", REMOTE, version_aware=True)


def test_check_names_a_cut_pointer(tmp_path: Path) -> None:
    """`check` screens every pointer with the parsers' rule before its
    delivery gate: one error finding naming the file, never "pinned".

    Mutation: screen pointers with the old shape check -> the cut pointer
    passes the screen and the gate reports dvc.lock instead, red.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_text(FILES_NULL)

    (finding,) = check_project(tmp_path)
    assert finding.kind == "dvc_state_unreadable" and finding.severity == "error"
    assert finding.source == tmp_path / "data" / "final.dvc"
    assert "the file list of final is missing" in finding.message


def test_check_passes_over_a_dvc_lock_dvc_never_reads(tmp_path: Path) -> None:
    """A dvc.lock with no dvc.yaml beside it is never read by DVC, so the
    cut-short rule doesn't apply to it in `check` either.

    Mutation: apply `check_lock` to every dvc.lock -> a finding, red.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "dvc.lock").write_text("schema: '2.0'\nstages:\n")
    assert [f for f in check_project(tmp_path) if f.kind == "dvc_state_unreadable"] == []


def _cut_project(tmp_path: Path) -> Path:
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_text(FILES_NULL)
    return tmp_path


def test_pull_stops_on_a_cut_pointer_with_no_empty_folder_and_no_dvc_pull(tmp_path: Path) -> None:
    """The real fast-sync lane, which wrote an empty `.dir` manifest for a
    null file list and reported the folder pulled. Now the pull stops before
    any download, writes nothing to the cache, and doesn't hand the pointer
    to `dvc pull`, which reads it as empty too.

    Mutation: let `outs_for_target` swallow `PointerCutShort` and the
    fast-sync fallback catch it -> dvc pull runs, red.
    """
    project = _cut_project(tmp_path)
    dvc = _FakeDvcOps()
    with pytest.raises(PointerCutShort, match="final.dvc is cut short"):
        data_pull(project_path=project, dvc_ops=dvc, fast_sync_ops=SubprocessFastSyncOps())
    assert dvc.pull_calls == []
    assert not (project / ".dvc" / "cache").exists() or not any((project / ".dvc" / "cache").rglob("*.dir"))


def test_pull_does_not_fall_back_to_dvc_when_fast_sync_meets_a_cut_pointer(tmp_path: Path) -> None:
    """Fast-sync's own failures fall back to `dvc pull`; a cut pointer is not
    one of them.

    Mutation: drop the `except MintdError: raise` before the fallback -> red.
    """
    project = tmp_path
    stage_dvc_project(project)
    fast = _FakeFastSyncOps()
    fast.raises = PointerCutShort(project / "data" / "final.dvc", "the file list of final is missing")
    dvc = _FakeDvcOps()
    with pytest.raises(PointerCutShort):
        data_pull(project_path=project, targets=["data/final"], dvc_ops=dvc, fast_sync_ops=fast)
    assert dvc.pull_calls == []
