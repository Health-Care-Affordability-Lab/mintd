"""`mintd data push` with a real dvc subprocess against an S3 server.

S3 is `tests/_harness/s3.py`'s server, which refuses ListObjectVersions with
403, as the lab's `mintd` key is refused, and records PUTs. dvc runs for real,
so these tests see what dvc does to the remote and to the pointer files, which
the fakes elsewhere only describe.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from mintd import cli
from mintd.check import check_project
from tests._dvc_fixtures import MINIMAL_METADATA
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.git import _git
from tests._harness.s3 import BUCKET


@pytest.fixture
def producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dvc, s3_server):  # noqa: F811
    """A git repo with a dvc project on a version-aware remote at the server:
    `data/raw/` (two files) tracked by `data/raw.dvc`, and stage `build`
    (wdir `code`) writing `data/final/a.csv`; committed and pushed to a bare
    upstream, no data pushed yet."""
    endpoint = s3_server.endpoint
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
        real_dvc.env[k] = v  # the harness copied the environment before these were set
    # mintd's own dvc subprocesses share the harness's redirected dvc dirs, so
    # a stale site cache left by a raw `dvc push` is the one mintd would read.
    for k in ("HOME", "USERPROFILE", "DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR", "DVC_SITE_CACHE_DIR"):
        monkeypatch.setenv(k, real_dvc.env[k])
    config_dir = tmp_path / "mintd-config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(f"cache_dir: {tmp_path / 'mintd-cache'}\n", encoding="utf-8")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config_dir))

    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["config", "user.email", "t@t"], cwd=proj)
    _git(["config", "user.name", "T"], cwd=proj)
    real_dvc(["init", "-q"], cwd=proj, check=True)
    _remote(proj, real_dvc, endpoint, "lab/a")
    (proj / "data" / "raw").mkdir(parents=True)
    (proj / "data" / "raw" / "x.csv").write_text("x\n")
    (proj / "data" / "raw" / "y.csv").write_text("y\n")
    real_dvc(["add", "-q", "data/raw"], cwd=proj, check=True)
    (proj / "code").mkdir()
    write = "import pathlib; p=pathlib.Path('../data/final'); p.mkdir(parents=True, exist_ok=True); (p/'a.csv').write_text('a\\n')"
    (proj / "dvc.yaml").write_text(yaml.safe_dump({"stages": {"build": {
        "wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": ["../data/final/a.csv"],
    }}}, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=proj, check=True)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "track"], cwd=proj)
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)])
    _git(["remote", "add", "origin", str(origin)], cwd=proj)
    _git(["push", "-q", "-u", "origin", "main"], cwd=proj)
    monkeypatch.chdir(proj)
    return proj


def _remote(proj: Path, real_dvc, endpoint: str, prefix: str) -> None:  # noqa: F811
    real_dvc(["remote", "add", "-d", "-f", "storage", f"s3://{BUCKET}/{prefix}"], cwd=proj, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", endpoint], cwd=proj, check=True)
    real_dvc(["remote", "modify", "storage", "version_aware", "true"], cwd=proj, check=True)


def _ids(proj: Path) -> dict[str, str]:
    """path -> version ID, from data/raw.dvc and dvc.lock."""
    got: dict[str, str] = {}
    raw = yaml.safe_load((proj / "data" / "raw.dvc").read_text())["outs"][0]
    for f in raw.get("files") or []:
        if (f.get("cloud") or {}).get("storage"):
            got[f"data/raw/{f['relpath']}"] = f["cloud"]["storage"]["version_id"]
    out = yaml.safe_load((proj / "dvc.lock").read_text())["stages"]["build"]["outs"][0]
    if (out.get("cloud") or {}).get("storage"):
        got["data/final/a.csv"] = out["cloud"]["storage"]["version_id"]
    return got


def _imports_everything(tmp_path: Path, real_dvc, name: str) -> None:  # noqa: F811
    """From an empty directory, `dvc import` of the pushed commit lands every
    file with its bytes: the check that catches a wrong S3 key, which a key
    bug shared by the repair and the gate would not."""
    consumer = tmp_path / name
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    origin = str(tmp_path / "origin.git")
    for path, body in (("data/raw", None), ("data/final/a.csv", "a\n")):
        r = real_dvc(["import", "-q", origin, path, "-o", Path(path).name], cwd=consumer)
        assert r.returncode == 0, r.stderr
        if body is not None:
            assert (consumer / Path(path).name).read_text() == body
    assert (consumer / "raw" / "x.csv").read_text() == "x\n"
    assert (consumer / "raw" / "y.csv").read_text() == "y\n"


def _out(capsys) -> str:
    return " ".join(capsys.readouterr().err.split())


def test_push_pins_verifies_commits_and_an_import_lands_everything(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    puts = s3_server.puts
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert "✓ 3 file(s) verified on s3://journey-bucket/lab/a" in err
    assert set(_ids(producer)) == {"data/raw/x.csv", "data/raw/y.csv", "data/final/a.csv"}
    assert len(puts) == 3
    _imports_everything(tmp_path, real_dvc, "consumer")


def _drop_ids(proj: Path) -> None:
    """What `dvc commit -f clean` did to cms-pdc's dvc.lock: same md5s, no
    cloud blocks. (dvc 3.67.1's `dvc commit -f` keeps them on these simple
    stages, so the rewrite is applied directly.)"""
    for rel in ("data/raw.dvc", "dvc.lock"):
        doc = yaml.safe_load((proj / rel).read_text())
        bodies = [doc] if rel.endswith(".dvc") else list(doc["stages"].values())
        for body in bodies:
            for out in body["outs"]:
                out.pop("cloud", None)
                for f in out.get("files") or []:
                    f.pop("cloud", None)
        (proj / rel).write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_rewrite_that_drops_the_ids_costs_no_upload(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    puts = s3_server.puts
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()
    first = _ids(producer)
    _drop_ids(producer)
    _git(["commit", "-q", "-am", "dvc commit -f"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert _ids(producer) == first
    assert all(f"re-attached {p} (no ID)" in err for p in first)
    assert "dvc.lock lost these IDs" in err
    _imports_everything(tmp_path, real_dvc, "consumer")


def test_a_folder_stage_dvc_commit_f_rewrote_costs_no_upload(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    """`dvc commit -f` (and `dvc repro` of an unchanged output) writes a folder
    out back as `md5: <tree>.dir`, with no files list and no IDs. mintd
    re-attaches every file from S3 as the files list DVC writes after a push,
    so nothing is uploaded again and DVC still sees the stage as up to date."""
    puts = s3_server.puts
    write = ("import pathlib; p=pathlib.Path('../data/out'); p.mkdir(parents=True, exist_ok=True); "
             "[(p/n).write_text(n) for n in ('a.csv', 'b.csv', 'c.csv')]")
    doc = yaml.safe_load((producer / "dvc.yaml").read_text())
    doc["stages"]["many"] = {"wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": ["../data/out"]}
    (producer / "dvc.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "folder stage"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()

    real_dvc(["commit", "-f", "-q", "dvc.yaml:many"], cwd=producer, check=True)
    (rec,) = yaml.safe_load((producer / "dvc.lock").read_text())["stages"]["many"]["outs"]
    assert "files" not in rec and rec["md5"].endswith(".dir")  # what DVC does to a folder out
    _git(["commit", "-q", "-am", "dvc commit -f many"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert all(f"re-attached data/out/{n} (no ID)" in err for n in ("a.csv", "b.csv", "c.csv"))
    assert "dvc.lock lost these IDs" in err
    assert "up to date" in real_dvc(["status"], cwd=producer).stdout
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    r = real_dvc(["import", "-q", str(tmp_path / "origin.git"), "data/out", "-o", "out"], cwd=consumer)
    assert r.returncode == 0, r.stderr
    assert sorted(f.read_text() for f in (consumer / "out").iterdir()) == ["a.csv", "b.csv", "c.csv"]


def test_a_moved_remote_is_reuploaded_despite_a_stale_dvc_site_cache(
    producer, s3_server, real_dvc, tmp_path, capsys,  # noqa: F811
) -> None:
    """The cms-pdc case. The files are pinned at prefix A, then the remote
    moves to B. A raw `dvc push` keeps the IDs that point at A, can't list
    versions, uploads nothing, and leaves its md5-keyed record of what is on
    B in the site cache (it also writes data/raw.dvc back with an empty file
    list, undone here from git). `mintd data push` strips the stale IDs; with
    that site cache DVC would still upload nothing, since no md5 changed, so
    mintd's push runs with a fresh one, uploads to B and verifies."""
    endpoint, puts = s3_server.endpoint, s3_server.puts
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()
    _remote(producer, real_dvc, endpoint, "lab/b")
    _git(["commit", "-q", "-am", "move the remote"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()
    real_dvc(["push", "-q"], cwd=producer)
    assert puts == []  # the defect: nothing reached the new prefix
    _git(["checkout", "-q", "--", "data/raw.dvc", "dvc.lock"], cwd=producer)

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert "the remote has probably moved" in err
    assert "✓ 3 file(s) verified on s3://journey-bucket/lab/b (3 uploaded and verified)" in err
    assert sorted(p.split("/", 2)[2] for p in puts) == ["lab/b/data/final/a.csv", "lab/b/data/raw/x.csv", "lab/b/data/raw/y.csv"]
    _imports_everything(tmp_path, real_dvc, "consumer")


@pytest.mark.skipif(os.name == "nt", reason="the scope census is platform-independent")
def test_push_false_and_cache_false_outs_are_outside_what_push_checks(producer, s3_server, real_dvc, capsys):  # noqa: F811
    """mintd's scope is DVC's own: what a real `dvc push` uploads."""
    puts = s3_server.puts
    write = "import pathlib; [pathlib.Path(n).write_text(n) for n in ('../data/keep.csv', '../data/tmp.csv')]"
    doc = yaml.safe_load((producer / "dvc.yaml").read_text())
    doc["stages"]["side"] = {"wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": [
        {"../data/keep.csv": {"push": False}}, {"../data/tmp.csv": {"cache": False}},
    ]}
    (producer / "dvc.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "side outputs"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    assert "✓ 3 file(s) verified" in _out(capsys)
    assert not any("keep.csv" in p or "tmp.csv" in p for p in puts)


def test_a_file_sent_in_parts_is_proven_reattached_and_imported(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    """A file of 100 MiB or more goes up in 50 MiB parts, so its ETag isn't its
    md5. mintd proves it by recomputing that ETag from the cache, both after
    the first push and when a rewrite loses the ID, which costs no upload."""
    s3, puts = s3_server.client, s3_server.puts
    big = os.urandom(100 * 2**20 + 7)
    (producer / "data" / "big.bin").write_bytes(big)
    real_dvc(["add", "-q", "data/big.bin"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "big"], cwd=producer)
    _git(["push", "-q"], cwd=producer)

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    assert "✓ 4 file(s) verified" in _out(capsys)
    pointer = producer / "data" / "big.bin.dvc"
    pin = yaml.safe_load(pointer.read_text())["outs"][0]["cloud"]["storage"]
    etag = s3.head_object(Bucket=BUCKET, Key="lab/a/data/big.bin", VersionId=pin["version_id"])["ETag"]
    assert etag.strip('"').endswith("-3")  # 50 MiB + 50 MiB + 7 bytes: really sent in parts

    doc = yaml.safe_load(pointer.read_text())
    doc["outs"][0].pop("cloud")
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False))
    _git(["commit", "-q", "-am", "dvc add again, without the ID"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert "re-attached data/big.bin (no ID)" in err
    assert yaml.safe_load(pointer.read_text())["outs"][0]["cloud"]["storage"]["version_id"] == pin["version_id"]
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    r = real_dvc(["import", "-q", str(tmp_path / "origin.git"), "data/big.bin", "-o", "big.bin"], cwd=consumer)
    assert r.returncode == 0, r.stderr
    assert (consumer / "big.bin").read_bytes() == big


@pytest.mark.parametrize("argv,kind", [
    (["data", "push"], "forced"),
    (["data", "push", "data/"], "forced"),
    (["data", "push"], "untracked"),
    (["data", "push"], "modified-forced"),
    (["--json", "data", "push"], "modified-forced"),
], ids=["plain-forced", "folder-target-forced", "plain-untracked", "plain-modified-forced", "json-modified-forced"])
def test_a_pointer_gitignore_excludes_is_skipped_as_dvc_skips_it(producer, s3_server, real_dvc, capsys, argv, kind):  # noqa: F811
    """A `.dvc` file in a folder `.gitignore` excludes is one DVC never reads:
    force-added, never pushed; untracked, with no data anywhere; or committed
    and then edited. Push names it as skipped, uploads and commits nothing for
    it, and verifies the rest.

    Mutations: filter removed -> plain-forced fails with "no S3 version ID
    recorded ... DVC reported nothing to upload", folder-target-forced hands
    dvc the ignored file ("is git-ignored"), plain-untracked fails with "not
    provably on S3 ... not in the local cache"; filter removed from push's
    commit list -> plain-modified-forced blocks, and from the list after the
    commit -> json-modified-forced lists it; warning deleted -> every
    non-JSON case misses the `skipped` line."""
    puts = s3_server.puts
    vendor = producer / "data" / "ext" / "vendor"
    vendor.mkdir(parents=True)
    if kind in ("forced", "modified-forced"):
        (vendor / "z.csv").write_text("z\n")
        real_dvc(["add", "-q", "data/ext/vendor/z.csv"], cwd=producer, check=True)  # before the ignore: dvc add refuses an ignored target
        (producer / "data" / "ext" / ".gitignore").write_text("vendor\n")
        _git(["add", "-f", "data/ext/.gitignore", "data/ext/vendor/z.csv.dvc"], cwd=producer)
        name = "data/ext/vendor/z.csv.dvc"
    else:
        (producer / "data" / "ext" / ".gitignore").write_text("vendor\n")
        _git(["add", "data/ext/.gitignore"], cwd=producer)
        name = "data/ext/vendor/gone.csv.dvc"
    _git(["commit", "-q", "-m", "a folder .gitignore excludes"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    if kind == "untracked":
        (producer / name).write_text("outs:\n- md5: " + "f" * 32 + "\n  size: 1\n  hash: md5\n  path: gone.csv\n")
    if kind == "modified-forced":
        doc = yaml.safe_load((producer / name).read_text())
        doc["outs"][0]["desc"] = "edited"
        (producer / name).write_text(yaml.safe_dump(doc, sort_keys=False))
    before = (producer / name).read_text()
    puts.clear()

    rc = cli.main(argv)
    captured = capsys.readouterr()
    err = " ".join(captured.err.split())
    assert rc == 0, err
    if argv[0] == "--json":  # the list after the commit leaves the skipped pointer out too
        assert json.loads(captured.out.strip().splitlines()[-1])["uncommitted_pointers"] == []
    else:
        assert f"skipped {name}: DVC never pushes or pulls it, because .gitignore rules exclude it" in err
        assert "✓ 3 file(s) verified" in err
    assert not any("/ext/" in p for p in puts)
    assert (producer / name).read_text() == before
    assert "data/ext" not in _git(["show", "--name-only", "--format=", "HEAD"], cwd=producer)
    status = [s.strip() for s in _git(["status", "--porcelain"], cwd=producer).split("\n") if s.strip()]
    assert status == ([f"M {name}"] if kind == "modified-forced" else [])



def _cut_after_entry(pointer: Path, k: int) -> None:
    """Cut ``pointer`` at the end of its folder's ``k``-th file entry, where
    a write that stopped partway leaves valid YAML."""
    text = pointer.read_text()
    starts = [i for i in range(len(text)) if text.startswith("  - relpath:", i)]
    pointer.write_text(text[:starts[k]])


def test_a_folder_pointer_cut_between_file_entries_commits_nothing_without_the_folder(producer, s3_server, real_dvc, capsys):  # noqa: F811
    """dvc writes no file count beside a folder's list, so a pointer cut at
    the end of a file entry is valid YAML and reads as a smaller folder.
    With the folder gone from disk there is nothing to compare it with
    before the upload; the commit step still refuses it when, as here, the
    shorter list is a tree this project's cache never saved.

    Mutation: drop the tree check in `_judge_out` -> the cut list is
    committed, red.
    """
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    head = _git(["show", "HEAD:data/raw.dvc"], cwd=producer)
    _cut_after_entry(producer / "data" / "raw.dvc", 1)
    shutil.rmtree(producer / "data" / "raw")

    assert cli.main(["data", "push"]) == 1
    assert "data/raw.dvc: data/raw lists 1 file(s), a tree DVC never saved" in _out(capsys)
    assert _git(["show", "HEAD:data/raw.dvc"], cwd=producer) == head


def _sibling_holds_the_shorter_tree(producer: Path, real_dvc) -> None:  # noqa: F811
    (producer / "data" / "sample").mkdir()
    (producer / "data" / "sample" / "x.csv").write_text("x\n")
    real_dvc(["add", "-q", "data/sample"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "sample"], cwd=producer)


def _an_earlier_add_saved_the_shorter_tree(producer: Path, real_dvc) -> None:  # noqa: F811
    y = producer / "data" / "raw" / "y.csv"
    y.rename(producer / "y.csv")
    real_dvc(["add", "-q", "data/raw"], cwd=producer, check=True)
    (producer / "y.csv").rename(y)
    real_dvc(["add", "-q", "data/raw"], cwd=producer, check=True)


def _the_folder_grew(producer: Path, real_dvc) -> None:  # noqa: F811
    (producer / "data" / "raw" / "z.csv").write_text("z\n")
    real_dvc(["add", "-q", "data/raw"], cwd=producer, check=True)
    _git(["commit", "-q", "-am", "z"], cwd=producer)


@pytest.mark.parametrize("case", ["tree-never-saved", "sibling-folder", "uncommitted-add", "grew", "committed",
                                  "committed-targeted"])
def test_a_folder_pointer_shorter_than_its_folder_stops_the_push_before_upload(
    producer, s3_server, real_dvc, capsys, case: str,  # noqa: F811
) -> None:
    """A version-aware folder pointer cut at the end of a file entry reads as
    a smaller folder, and so does one recorded before files were added. The
    commit step's tree check passed it whenever that shorter tree was in the
    cache from any source (a sibling folder holding those files, an earlier
    `dvc add`, an earlier version of the folder), and never looked at one
    already committed. The folder on disk shows it every time: the push
    refuses before anything is uploaded, naming the pointer and the files
    it doesn't list.

    Mutation: drop the folder comparison from `data_push` -> every case
    pushes (the cached ones commit the shorter list), red.
    """
    setup = {"sibling-folder": _sibling_holds_the_shorter_tree,
             "uncommitted-add": _an_earlier_add_saved_the_shorter_tree, "grew": _the_folder_grew}.get(case)
    if setup is not None:
        setup(producer, real_dvc)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    _cut_after_entry(producer / "data" / "raw.dvc", 2 if case == "grew" else 1)
    if case.startswith("committed"):
        _git(["commit", "-q", "-am", "cut"], cwd=producer)
    head = _git(["show", "HEAD:data/raw.dvc"], cwd=producer)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push", *(["data/raw"] if case == "committed-targeted" else [])]) == 1
    err = _out(capsys)
    shown = "z.csv" if case == "grew" else "y.csv"
    assert f"data/raw.dvc lists {2 if case == 'grew' else 1} file(s) of data/raw/, but the folder on disk holds 1 " \
           f"it doesn't list ({shown})" in err, err
    assert "mintd data add data/raw" in err
    assert len(s3_server.puts) == puts
    assert _git(["show", "HEAD:data/raw.dvc"], cwd=producer) == head


def test_a_file_placed_in_a_dir_tracked_folder_is_flagged_by_check_and_push(
    producer, s3_server, real_dvc, capsys,  # noqa: F811
) -> None:
    """Real dvc: `dvc add` writes data/raw.dvc as a `.dir` list (the remote is
    version-aware, but nothing is pushed yet). A file placed in data/raw and
    not recorded is named by check, and the push refuses before any upload,
    without calling it a cut pointer. `mintd data add data/raw` clears it.

    Mutation: skip `.dir` lists -> check is silent and the push runs, red.
    """
    out = yaml.safe_load((producer / "data" / "raw.dvc").read_text())["outs"][0]
    assert out["md5"].endswith(".dir") and "files" not in out  # a .dir list, not a files: list
    # check reads a product: give the producer the minimal metadata.json.
    (producer / "metadata.json").write_text(MINIMAL_METADATA.read_text(encoding="utf-8"), encoding="utf-8")
    _git(["add", "metadata.json"], cwd=producer)
    _git(["commit", "-q", "-m", "metadata"], cwd=producer)
    (producer / "data" / "raw" / "z.csv").write_text("z\n")
    found = [f for f in check_project(producer) if f.kind == "dvc_folder_unlisted"]
    assert len(found) == 1 and "(z.csv): files were added since the folder was recorded" in found[0].message
    puts = len(s3_server.puts)
    assert cli.main(["data", "push"]) == 1
    err = _out(capsys)
    assert "data/raw.dvc lists 2 file(s) of data/raw/, but the folder on disk holds 1 it doesn't list (z.csv)" in err
    assert "mintd data add data/raw" in err and "restore it from git" not in err
    assert len(s3_server.puts) == puts
    assert cli.main(["data", "add", "data/raw"]) == 0
    assert not [f for f in check_project(producer) if f.kind == "dvc_folder_unlisted"]


def test_a_clone_flags_a_file_placed_in_a_folder_pushed_while_empty(
    producer, s3_server, real_dvc, tmp_path, capsys,  # noqa: F811
) -> None:
    """A version-aware push leaves an empty folder's pointer an empty `.dir`
    (not a `files:` list), and a clone has no cache of its list. A file a
    teammate places in it is still named by check, as on the producer.

    Mutation: read the empty `.dir` from the cache -> the clone leaves it
    out, red."""
    (producer / "metadata.json").write_text(MINIMAL_METADATA.read_text(encoding="utf-8"), encoding="utf-8")
    (producer / "data" / "empty").mkdir()
    real_dvc(["add", "-q", "data/empty"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "an empty folder"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    out = yaml.safe_load((producer / "data" / "empty.dvc").read_text())["outs"][0]
    assert out["md5"] == "d751713988987e9331980363e24189ce.dir" and "files" not in out

    clone = tmp_path / "teammate"
    _git(["clone", "-q", str(producer), str(clone)])
    assert not (clone / ".dvc" / "cache").exists()
    (clone / "data" / "empty").mkdir(exist_ok=True)
    (clone / "data" / "empty" / "placed.csv").write_text("p\n")
    found = [f for f in check_project(clone) if f.kind == "dvc_folder_unlisted"]
    assert len(found) == 1 and "data/empty.dvc lists 0 file(s) of data/empty/" in found[0].message
    assert "(placed.csv): files were added since the folder was recorded" in found[0].message


def test_a_files_list_and_a_dir_folder_flagged_together_get_the_cut_clause(
    producer, s3_server, real_dvc, capsys,  # noqa: F811
) -> None:
    """With one flagged folder's list a `files:` list (data/raw, after a
    version-aware push) and the other a `.dir` list, the combined hint keeps
    "if a pointer was cut": one of them could have been.

    Mutation: never add the cut clause -> red."""
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    assert "files" in yaml.safe_load((producer / "data" / "raw.dvc").read_text())["outs"][0]
    (producer / "data" / "more").mkdir()
    (producer / "data" / "more" / "m.csv").write_text("m\n")
    real_dvc(["add", "-q", "data/more"], cwd=producer, check=True)
    (producer / "data" / "raw" / "z.csv").write_text("z\n")
    (producer / "data" / "more" / "n.csv").write_text("n\n")
    assert cli.main(["data", "push"]) == 1
    err = _out(capsys)
    assert "record each folder again" in err and "if a pointer was cut, restore it from git" in err


def test_two_dir_folders_with_placed_files_get_one_hint_without_a_cut(
    producer, s3_server, real_dvc, capsys,  # noqa: F811
) -> None:
    """With two `.dir` folders flagged, the push's combined hint doesn't say
    "if a pointer was cut": neither list can be.

    Mutation: always add the cut clause -> red."""
    (producer / "data" / "more").mkdir()
    (producer / "data" / "more" / "m.csv").write_text("m\n")
    real_dvc(["add", "-q", "data/more"], cwd=producer, check=True)
    (producer / "data" / "raw" / "z.csv").write_text("z\n")
    (producer / "data" / "more" / "n.csv").write_text("n\n")
    assert cli.main(["data", "push"]) == 1
    err = _out(capsys)
    assert "record each folder again" in err and "if a pointer was cut" not in err


def test_a_file_placed_in_a_folder_tracked_while_empty_stops_the_push(
    producer, s3_server, real_dvc, capsys,  # noqa: F811
) -> None:
    """`mintd init data` tracks data/raw while it's empty. A file placed there
    and not yet recorded went straight to `dvc push`, which uploaded the
    folder as last recorded, without it. The push now refuses before any
    upload, naming the file and how to record it.

    Mutation: leave empty folders out of the comparison -> it pushes, red.
    """
    (producer / "data" / "empty").mkdir()
    real_dvc(["add", "-q", "data/empty"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "track an empty folder"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    (producer / "data" / "empty" / "placed.csv").write_text("p\n")
    puts = len(s3_server.puts)

    assert cli.main(["data", "push"]) == 1
    err = _out(capsys)
    assert "data/empty.dvc lists 0 file(s) of data/empty/, but the folder on disk holds 1 it doesn't list " \
           "(placed.csv)" in err, err
    assert "mintd data add data/empty" in err
    assert len(s3_server.puts) == puts


def test_check_remote_finds_what_push_wrote_and_staged_refuses_dvc_commit_f(producer, s3_server, real_dvc, capsys):  # noqa: F811
    """After a real push, `mintd check --remote` finds every pinned version,
    for `.dvc` and dvc.lock outs, and uploads nothing. Then `dvc commit -f`
    writes a stage's folder out back as a bare tree hash with no IDs:
    `check --staged` refuses that commit, with the tree in the local cache
    and without it."""
    from mintd._dvc_state import dvc_cache_dir
    from mintd.check import check_project, check_staged

    write = ("import pathlib; p=pathlib.Path('../data/out'); p.mkdir(parents=True, exist_ok=True); "
             "[(p/n).write_text(n) for n in ('a.csv', 'b.csv')]")
    doc = yaml.safe_load((producer / "dvc.yaml").read_text())
    doc["stages"]["many"] = {"wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": ["../data/out"]}
    (producer / "dvc.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "folder stage"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)  # commits the pinned pointers
    puts = list(s3_server.puts)
    remote = [f for f in check_project(producer, remote=True, all_outs=True) if (f.kind or "").startswith("remote_")]
    assert remote == [] and s3_server.puts == puts

    real_dvc(["commit", "-f", "-q", "dvc.yaml:many"], cwd=producer, check=True)
    rec = yaml.safe_load((producer / "dvc.lock").read_text())["stages"]["many"]["outs"][0]
    assert "files" not in rec and rec["md5"].endswith(".dir")  # what DVC does to a folder out
    _git(["add", "dvc.lock"], cwd=producer)
    want = ["dvc.lock drops S3 version IDs from 2 unchanged file(s) (data/out/a.csv, data/out/b.csv)"]
    assert [f.message for f in check_staged(producer)] == want
    cache = dvc_cache_dir(producer)
    assert cache.is_relative_to(producer)
    # the test's own local cache, gone as from a CI checkout: moved, not
    # deleted, since DVC makes cache files read-only and Windows won't delete those
    cache.rename(cache.with_name("cache-gone"))
    assert [f.message for f in check_staged(producer)] == want


def test_a_nested_pipeline_folder_shorter_than_its_folder_stops_the_push(
    producer, s3_server, real_dvc, capsys,  # noqa: F811
) -> None:
    """A pipeline in `sub/` (its own dvc.yaml and dvc.lock, not a subrepo)
    writes a three-file folder. With its lock cut after the first entry and
    committed, the push refuses before any upload, naming the nested lock
    (`check` and publish: test_folder_unlisted).

    Mutation: read only the root dvc.lock -> exit 0, the cut list stays
    committed, red.
    """
    sub = producer / "sub"
    sub.mkdir()
    write = "import pathlib; p=pathlib.Path('out'); p.mkdir(exist_ok=True); [(p/n).write_text(n) for n in ('a', 'b', 'c')]"
    (sub / "dvc.yaml").write_text(yaml.safe_dump({"stages": {"mk": {"cmd": f'"{sys.executable}" -c "{write}"',
                                                                   "outs": ["out"]}}}))
    real_dvc(["repro", "-q", "sub/dvc.yaml"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "sub"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    _cut_after_entry(sub / "dvc.lock", 1)
    _git(["commit", "-q", "-am", "cut"], cwd=producer)
    head = _git(["show", "HEAD:sub/dvc.lock"], cwd=producer)
    puts = len(s3_server.puts)

    assert cli.main(["data", "push"]) == 1
    err = _out(capsys)
    assert "sub/dvc.lock (stage mk) lists 1 file(s) of sub/out/, but the folder on disk holds 2 it doesn't list (b, c)" in err, err
    assert "dvc commit sub/dvc.yaml:mk" in err
    assert len(s3_server.puts) == puts
    assert _git(["show", "HEAD:sub/dvc.lock"], cwd=producer) == head
