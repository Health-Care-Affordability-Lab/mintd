"""`mintd data locate NAME [PATH] [--rev REV]`: a catalog name in, the files
of one published DVC version out, each with its s3:// url, md5 and S3 version
ID as the producer's pointers record them at that rev. Its `--json` payload is
what a reader in another language parses, so these tests pin the shape as well
as the lookup.

The producer is a real git repo on disk (`tests/_harness/producer.py`), read
by mintd's real fetcher. Its pointers are written by hand in the shape `dvc
push` leaves them on a version-aware remote: a folder out in `dvc.lock` with
a per-file list, a single file in a `.dvc` file. Every test runs from a folder
that is not a project, and none has AWS credentials or a bucket: locate reads
git, never S3.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shlex
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from mintd import cli
from mintd._s3_listing_ops import list_product_objects
from mintd.catalog import InMemoryCatalogClient
from mintd.model import Metadata
from mintd.producer import FetchError, GitArchiveFetcher
from tests._harness.access import clear_access_caches
from tests._harness.git import _git
from tests._harness.producer import LocalProducer, build_local_producer

MINIMAL = Path(__file__).parent / "fixtures" / "metadata_v2_minimal.json"
REMOTE = "data_demo-map"
DVC_CONFIG = (
    f"[core]\n    remote = {REMOTE}\n"
    f"['remote \"{REMOTE}\"']\n"
    "    url = s3://lab-bucket/pub/data_demo-map/\n"
    "    endpointurl = https://s3.example.com\n"
    "    profile = mintd\n"
    "    version_aware = true\n"
)
DVC_YAML = "stages:\n  build:\n    wdir: code\n    cmd: python build.py\n    outs:\n    - ../data/final/\n"


def _md5(relpath: str, version_id: str | None) -> str:
    """A distinct md5 per file and version, so a test can tell revs apart."""
    return hashlib.md5(f"{relpath}@{version_id}".encode()).hexdigest()


def _lock(files: dict[str, str | None]) -> str:
    """A `dvc.lock` whose one stage (`wdir: code`) writes `../data/final/`,
    listing ``files`` (relpath -> S3 version ID, or None for none) as `dvc
    push` records them."""
    entries = []
    for rel, vid in files.items():
        entry: dict[str, Any] = {"relpath": rel, "md5": _md5(rel, vid), "size": len(rel)}
        if vid is not None:
            entry["cloud"] = {REMOTE: {"etag": entry["md5"], "version_id": vid}}
        entries.append(entry)
    out = {"path": "../data/final/", "hash": "md5", "files": entries}
    return yaml.safe_dump({"schema": "2.0", "stages": {"build": {"cmd": "python build.py", "outs": [out]}}})


def _dvc_file(name: str, vid: str) -> str:
    """A `.dvc` file tracking the single file ``name`` beside it."""
    md5 = _md5(name, vid)
    out = {"md5": md5, "size": len(name), "hash": "md5", "path": name, "cloud": {REMOTE: {"etag": md5, "version_id": vid}}}
    return yaml.safe_dump({"outs": [out]})


def _commit(producer: LocalProducer, files: dict[str, str | bytes], *, tag: str | None = None) -> str:
    """Write ``files`` (path -> text, or bytes) into the producer beside its
    DVC remote and pipeline, commit, push, tag if asked, and return the
    commit."""
    for rel, text in {".dvc/config": DVC_CONFIG, "dvc.yaml": DVC_YAML, **files}.items():
        (producer.work / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            (producer.work / rel).write_bytes(text)
        else:
            (producer.work / rel).write_text(text, encoding="utf-8")
    _git(["add", "-A"], cwd=producer.work)
    _git(["commit", "-m", "pointers"], cwd=producer.work)
    _git(["push", "origin", "main"], cwd=producer.work)
    if tag:
        producer.tag(tag)
    return producer.head_sha


def _register(
    client: InMemoryCatalogClient,
    url: str,
    name: str = "demo-map",
    *,
    primary: str | None = "data/final/",
    release: str = "",
    project_type: str = "data",
) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"].update(name=name, type=project_type, full_name=f"{project_type}_{name}")
    data["repository"]["github_url"] = url
    data["data_products"]["primary"] = primary
    data["status"]["last_published_version"] = release
    # Another bucket than the producer's `.dvc/config` names: locate takes the
    # remote from `.dvc/config` at the rev it reads, where that rev's version
    # IDs live. `data ls` lists this one.
    data["storage"] = {
        "provider": "s3", "bucket": "test-bucket", "prefix": f"products/{name}/",
        "endpoint": "https://s3.catalog.example", "versioning": True, "dvc": {"remote_name": REMOTE},
    }
    client.register(Metadata.model_validate(data))


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> InMemoryCatalogClient:
    """An empty catalog wired into the CLI; the working directory is a bare
    folder, not a project, and config is all defaults but ``cache_dir``,
    which is ``tmp_path / "cache"``."""
    client = InMemoryCatalogClient()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(f"cache_dir: {tmp_path / 'cache'}\n", encoding="utf-8")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("mintd.cli._resolve_catalog_client", lambda cfg: client)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return client


@pytest.fixture
def producer(tmp_path: Path) -> LocalProducer:
    return build_local_producer(tmp_path / "prod")


def _locate(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    rc = cli.main(["--json", "data", "locate", *argv])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured.err


def _versions(payload: dict[str, Any]) -> dict[str, str | None]:
    return {f["key"]: f["version_id"] for f in payload["files"]}


# --- which version ------------------------------------------------------------


@pytest.fixture
def three_versions(catalog, producer) -> dict[str, str]:
    """v0.1.0 and v0.2.0 released, v0.2.0 in the catalog, then a commit of
    files pushed but never published, tagged by hand with a release's shape."""
    shas = {
        "v0.1.0": _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})}, tag="v0.1.0"),
        "v0.2.0": _commit(producer, {"dvc.lock": _lock({"a.parquet": "va2", "sub/b.dta": "vb2"})}, tag="v0.2.0"),
        "head": _commit(producer, {"dvc.lock": _lock({"a.parquet": "va3"})}, tag="v2026.4.7"),
    }
    _register(catalog, producer.url, release="0.2.0")
    return shas


def test_by_default_it_reads_the_newest_release_the_catalog_lists(three_versions, capsys) -> None:
    """Not the branch head, whose files were pushed but not published, and
    not the tag with the highest version: `v2026.4.7` was made by hand."""
    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert (payload["rev"], payload["commit"]) == ("v0.2.0", three_versions["v0.2.0"])
    assert _versions(payload) == {"data/final/a.parquet": "va2", "data/final/sub/b.dta": "vb2"}


def test_rev_reads_that_tag(three_versions, capsys) -> None:
    rc, payload, _ = _locate(capsys, "demo-map", "--rev", "v0.1.0")

    assert rc == 0
    assert (payload["rev"], payload["commit"]) == ("v0.1.0", three_versions["v0.1.0"])
    assert _versions(payload) == {"data/final/a.parquet": "va1"}


def test_rev_reads_that_commit(three_versions, capsys) -> None:
    sha = three_versions["head"]

    rc, payload, _ = _locate(capsys, "demo-map", "--rev", sha)

    assert rc == 0
    assert (payload["rev"], payload["commit"]) == (sha, sha)
    assert _versions(payload) == {"data/final/a.parquet": "va3"}


def test_a_product_with_no_release_reads_its_default_branch_and_says_so(catalog, producer, capsys) -> None:
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    head = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va2"})})
    _register(catalog, producer.url, release="")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert (payload["rev"], payload["commit"]) == (None, head)
    assert _versions(payload) == {"data/final/a.parquet": "va2"}


def test_a_short_sha_finds_the_commit_a_tag_is_at(three_versions, capsys) -> None:
    """As `mintd use` and locate print a commit: its first seven characters."""
    sha = three_versions["v0.1.0"]

    rc, payload, _ = _locate(capsys, "demo-map", "--rev", sha[:7])

    assert rc == 0
    assert (payload["rev"], payload["commit"]) == (sha[:7], sha)
    assert _versions(payload) == {"data/final/a.parquet": "va1"}


def test_a_short_sha_of_a_commit_no_tag_or_branch_is_at_asks_for_the_full_one(catalog, producer, capsys) -> None:
    """Only a full SHA can be fetched, so the error says that, not that the
    commit doesn't exist."""
    old = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va2"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map", "--rev", old[:7])

    assert rc == 1
    assert f"no tag or branch in {producer.url} is at a commit starting {old[:7]}" in payload["error"]
    assert payload["hint"] == "give the commit's full 40-character SHA\na short SHA finds only a commit a tag or branch is at"


def test_a_commit_read_once_is_read_again_without_the_producer(catalog, producer, capsys, tmp_path) -> None:
    """A commit never changes, so its DVC files are kept under `cache_dir`
    (`producers/`) and a full SHA read again needs no fetch: here the
    producer's repo is gone. Only the DVC files are kept, not the repo's
    other files."""
    sha = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"}), "code/build.py": "print(1)\n"})
    _register(catalog, producer.url)
    assert _locate(capsys, "demo-map", "--rev", sha)[0] == 0
    producer.bare.rename(tmp_path / "gone.git")

    rc, payload, _ = _locate(capsys, "demo-map", "--rev", sha)

    assert rc == 0
    assert (payload["commit"], _versions(payload)) == (sha, {"data/final/a.parquet": "va1"})
    [kept] = (tmp_path / "cache" / "producers").glob(f"*/{sha}-dvc-files")
    assert (kept / "dvc.lock").is_file() and (kept / ".dvc" / "config").is_file()
    assert not (kept / "code" / "build.py").exists() and not (kept / "metadata.json").exists()


def test_a_tag_is_looked_up_on_every_read(three_versions, producer, capsys) -> None:
    """A tag can be moved, so only the commit it names is kept, never the
    tag's answer."""
    assert _locate(capsys, "demo-map", "--rev", "v0.2.0")[1]["commit"] == three_versions["v0.2.0"]
    producer.move_tag("v0.2.0", rev=three_versions["head"])

    rc, payload, _ = _locate(capsys, "demo-map", "--rev", "v0.2.0")

    assert rc == 0
    assert (payload["commit"], _versions(payload)) == (three_versions["head"], {"data/final/a.parquet": "va3"})


# --- which files --------------------------------------------------------------


def test_a_folder_output_is_expanded_to_each_file_its_pointer_lists(catalog, producer, capsys) -> None:
    """One `dvc.lock` out with a per-file list becomes one entry per file, at
    any depth; a sibling folder whose name starts the same is not under it."""
    _commit(producer, {
        "dvc.lock": _lock({"a.parquet": "va1", "sub/b.dta": "vb1"}),
        "data/final_old/c.dta.dvc": _dvc_file("c.dta", "vc1"),
        "data/raw/d.csv.dvc": _dvc_file("d.csv", "vd1"),
    })
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert payload["target"] == "data/final/"
    assert [(f["key"], f["md5"], f["size"], f["version_id"]) for f in payload["files"]] == [
        ("data/final/a.parquet", _md5("a.parquet", "va1"), len("a.parquet"), "va1"),
        ("data/final/sub/b.dta", _md5("sub/b.dta", "vb1"), len("sub/b.dta"), "vb1"),
    ]


def test_a_single_file_output_is_that_file_alone(catalog, producer, capsys) -> None:
    _commit(producer, {
        "data/final/eo.parquet.dvc": _dvc_file("eo.parquet", "veo1"),
        "data/final/eo.parquet.bak.dvc": _dvc_file("eo.parquet.bak", "vbak1"),
    })
    _register(catalog, producer.url, primary="data/final/eo.parquet")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert payload["target"] == "data/final/eo.parquet"
    assert [(f["key"], f["md5"], f["version_id"]) for f in payload["files"]] == [
        ("data/final/eo.parquet", _md5("eo.parquet", "veo1"), "veo1"),
    ]


@pytest.mark.parametrize(
    ("path", "target", "keys"),
    [
        ("data/final/sub/b.dta", "data/final/sub/b.dta", ["data/final/sub/b.dta"]),
        ("data/final/sub/", "data/final/sub/", ["data/final/sub/b.dta", "data/final/sub/c.dta"]),
        ("data/final/sub", "data/final/sub/", ["data/final/sub/b.dta", "data/final/sub/c.dta"]),
    ],
    ids=["file-in-a-folder-out", "folder", "folder-without-slash"],
)
def test_an_explicit_path_overrides_the_primary(catalog, producer, capsys, path: str, target: str, keys: list[str]) -> None:
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1", "sub/b.dta": "vb1", "sub/c.dta": "vc1"})})
    _register(catalog, producer.url, primary="data/final/a.parquet")

    rc, payload, _ = _locate(capsys, "demo-map", path)

    assert rc == 0
    assert payload["primary"] == "data/final/a.parquet"
    assert payload["target"] == target
    assert [f["key"] for f in payload["files"]] == keys


@pytest.mark.parametrize("path", ["data/final/missing.dta", "data/final/a", "data/final/a.parquet/"])
def test_a_path_no_pointer_records_fails_and_names_the_outputs(catalog, producer, capsys, path: str) -> None:
    """`a` is a prefix of a real key but names no file or folder, and a file
    asked for as a folder (`a.parquet/`) is not that file."""
    commit = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _register(catalog, producer.url)

    rc, payload, err = _locate(capsys, "demo-map", path)

    assert rc == 1
    assert payload["exit_code"] == 1
    assert path.rstrip("/") in payload["error"] and commit[:7] in payload["error"]
    assert payload["hint"] == "its outputs:\ndata/final/"
    assert err.startswith("error: ")


def _out(name: str, vid: str, **flags: Any) -> dict[str, Any]:
    md5 = _md5(name, vid)
    return {"md5": md5, "size": len(name), "hash": "md5", "path": name, "cloud": {REMOTE: {"etag": md5, "version_id": vid}}, **flags}


def test_outs_a_push_never_uploads_are_left_out(catalog, producer, capsys) -> None:
    """DVC never uploads a `push: false` or `cache: false` out, from a `.dvc`
    file or from `dvc.yaml`, nor a dvc import's (its bytes are in the source
    repo's bucket), so there is no S3 object to name, and a path naming only
    such a file is one no published file matches."""
    stage = {"cmd": "python side.py", "outs": [
        {"path": f"../data/out/{n}", "md5": _md5(n, "v"), "size": len(n), "hash": "md5",
         "cloud": {REMOTE: {"etag": _md5(n, "v"), "version_id": f"v-{n}"}}}
        for n in ("lock_ok.parquet", "lock_nopush.parquet", "lock_nocache.parquet")
    ]}
    imported = {"md5": "0" * 32, "frozen": True, "outs": [_out("imported.csv", "vi")],
                "deps": [{"path": "data/final/x.csv", "repo": {"url": "https://github.com/example-lab/other", "rev_lock": "a" * 40}}]}
    commit = _commit(producer, {
        "dvc.yaml": yaml.safe_dump({"stages": {"side": {"wdir": "code", "cmd": "python side.py", "outs": [
            "../data/out/lock_ok.parquet",
            {"../data/out/lock_nopush.parquet": {"push": False}},
            {"../data/out/lock_nocache.parquet": {"cache": False}},
        ]}}}),
        "dvc.lock": yaml.safe_dump({"schema": "2.0", "stages": {"side": stage}}),
        "data/out/ok.csv.dvc": yaml.safe_dump({"outs": [_out("ok.csv", "vok")]}),
        "data/out/nopush.csv.dvc": yaml.safe_dump({"outs": [_out("nopush.csv", "vnp", push=False)]}),
        "data/out/nocache.csv.dvc": yaml.safe_dump({"outs": [_out("nocache.csv", "vnc", cache=False)]}),
        "data/out/imported.csv.dvc": yaml.safe_dump(imported),
    })
    _register(catalog, producer.url, primary="data/out/")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert _versions(payload) == {"data/out/lock_ok.parquet": "v-lock_ok.parquet", "data/out/ok.csv": "vok"}
    for path in ("data/out/nopush.csv", "data/out/lock_nopush.parquet"):
        rc, payload, _ = _locate(capsys, "demo-map", path)
        assert rc == 1
        assert payload["error"] == f"demo-map publishes no file at {path} at its default branch (commit {commit[:7]})"


@pytest.mark.parametrize(
    ("cloud", "expected"),
    [({"backup": {"version_id": "V-BACKUP"}}, None),
     ({"backup": {"version_id": "V-BACKUP"}, REMOTE: {"version_id": "V-MAIN"}}, "V-MAIN")],
    ids=["only-another-remote", "another-remote-listed-first"],
)
def test_a_version_id_is_the_default_remotes_alone(catalog, producer, capsys, cloud: dict, expected: str | None) -> None:
    """The url is in the default remote's bucket, so another remote's version
    ID would name no object there."""
    out = {"md5": _md5("eo.parquet", None), "size": 10, "hash": "md5", "path": "eo.parquet", "cloud": cloud}
    _commit(producer, {"data/final/eo.parquet.dvc": yaml.safe_dump({"outs": [out]})})
    _register(catalog, producer.url, primary="data/final/eo.parquet")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert _versions(payload) == {"data/final/eo.parquet": expected}


def test_a_pointer_dvc_skips_for_gitignore_is_left_out(catalog, producer, capsys) -> None:
    """DVC doesn't read a `.dvc` file under a folder `.gitignore` excludes,
    tracked or not, so locate doesn't either."""
    _commit(producer, {"data/final/old/x.csv.dvc": _dvc_file("x.csv", "vx"), "data/final/eo.csv.dvc": _dvc_file("eo.csv", "veo")})
    _commit(producer, {".gitignore": "/data/final/old/\n"})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert _versions(payload) == {"data/final/eo.csv": "veo"}


def test_a_folder_recorded_as_one_hash_is_refused(catalog, producer, capsys) -> None:
    """Its file list is an object in the bucket, which locate never reads."""
    out = {"md5": "a" * 32 + ".dir", "size": 20, "nfiles": 2, "hash": "md5", "path": "final"}
    _commit(producer, {"data/final.dvc": yaml.safe_dump({"outs": [out]})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"].startswith("data/final is recorded as one folder hash with no list of its files")
    assert payload["hint"] == "ask the producer to run 'mintd data push'\nit records the folder file by file"


@pytest.mark.parametrize(
    "relpath",
    ["../../../../etc/evil", "/abs/evil", "sub//b.dta", "sub/./b.dta", "sub\\b.dta", "sub/b\x1b[2J.dta"],
    ids=["dot-dot", "absolute", "empty-segment", "dot-segment", "backslash", "control-char"],
)
def test_a_file_whose_key_leaves_the_product_is_refused(catalog, producer, capsys, relpath: str) -> None:
    """A reader joins a key onto a folder of its own: one that climbs out, or
    reads differently on another OS, is refused here, once, for every
    reader."""
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1", relpath: "vx"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"].startswith(f"dvc.lock records a file at {f'data/final/{relpath}'!r}")
    assert payload["hint"] == "ask the producer to fix that pointer"
    assert "files" not in payload


def test_a_file_whose_key_starts_with_a_windows_drive_is_refused(catalog, producer, capsys) -> None:
    """Joined onto a folder on Windows, `C:evil.csv` is a path on drive C."""
    _commit(producer, {"C:evil.csv.dvc": yaml.safe_dump({"outs": [_out("C:evil.csv", "v")]})})
    _register(catalog, producer.url, primary="C:evil.csv")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"].startswith("C:evil.csv.dvc records a file at 'C:evil.csv'")


def _damage(lock: str, old: str, new: str) -> str:
    assert lock.count(old) == 1, old
    return lock.replace(old, new)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda lock: _damage(lock, f"md5: {_md5('a.parquet', 'va1')}", "md5: not-hex"), "dvc.lock is cut short or damaged"),
        (lambda lock: _damage(lock, "size: 9", "size: nine"), "dvc.lock is cut short or damaged"),
        (lambda lock: lock[: lock.index("files:")], "dvc.lock is cut short or damaged"),
        (lambda lock: _damage(lock, "version_id: va1", 'version_id: "va1\\n  - forged"'),
         "dvc.lock records a version ID for 'data/final/a.parquet' holding a control character"),
    ],
    ids=["md5-not-hex", "size-not-a-number", "cut-short", "version-id-with-newline"],
)
def test_a_damaged_pointer_is_refused(catalog, producer, capsys, damage, message: str) -> None:
    """Every value locate hands a reader comes from the producer's pointers,
    and a reader writes some of them into files of its own."""
    _commit(producer, {"dvc.lock": damage(_lock({"a.parquet": "va1"}))})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"].startswith(message)
    assert payload["hint"] == "ask the producer to fix that pointer"


def test_a_file_with_no_recorded_size_has_size_null(catalog, producer, capsys) -> None:
    """A missing value is null in the payload, never 0, and `-` in the text."""
    _commit(producer, {"dvc.lock": _damage(_lock({"a.parquet": "va1"}), "        size: 9\n", "")})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert payload["files"][0]["size"] is None
    assert cli.main(["data", "locate", "demo-map"]) == 0
    assert capsys.readouterr().out.splitlines()[3].endswith("  -")


# --- failures -----------------------------------------------------------------


def test_an_unknown_rev_lists_the_release_tags(catalog, producer, capsys) -> None:
    for tag in ("v0.1.0", "v0.10.0", "nightly"):
        _commit(producer, {"dvc.lock": _lock({"a.parquet": tag})}, tag=tag)
    _register(catalog, producer.url, release="0.10.0")

    rc, payload, err = _locate(capsys, "demo-map", "--rev", "v9.9.9")

    assert rc == 1
    assert payload["exit_code"] == 1
    assert "v9.9.9" in payload["error"]
    assert payload["error"].endswith("its release tags: v0.10.0, v0.1.0")
    assert payload["hint"] == "give --rev one of its release tags or a commit's SHA"
    assert "candidates" not in payload
    assert "v0.10.0, v0.1.0" in err


@pytest.mark.skipif(os.name == "nt", reason="the stub gh is a POSIX script")
def test_an_unreachable_producer_says_to_log_in_with_gh(catalog, capsys, monkeypatch, tmp_path) -> None:
    """mintd reaches GitHub through gh only; with gh logged out it stops
    before git runs and says how to log in. A stub gh first on PATH stands in
    for a logged-out one (the real one may find a token in the keychain), and
    a proxy on a closed port fails any request that would leave the machine."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "gh").write_text('#!/bin/sh\necho "no oauth token found for github.com" >&2\nexit 1\n')
    (stubs / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    for var in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{closed}")
    clear_access_caches()
    _register(catalog, "https://github.com/example-lab/data_demo-map")

    rc, payload, err = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["exit_code"] == 1
    assert "demo-map" in payload["error"]
    assert "gh auth login" in payload["hint"]
    assert "gh auth login" in err


def test_an_unknown_name_carries_the_close_names_best_first(catalog, capsys) -> None:
    for name in ("county-income", "county-incomes", "hospital-beds"):
        _register(catalog, f"https://github.com/example-lab/{name}", name)

    rc, payload, err = _locate(capsys, "county-incme")

    assert rc == 1
    assert payload["exit_code"] == 1
    assert payload["candidates"] == ["county-income", "county-incomes"]
    assert "county-incme" in payload["error"]
    assert payload["hint"] == "did you mean:\ncounty-income\ncounty-incomes"
    assert "county-income" in err


def test_an_unknown_name_with_no_close_match_carries_every_data_product(catalog, capsys) -> None:
    _register(catalog, "https://github.com/example-lab/a", "hospital-beds")
    _register(catalog, "https://github.com/example-lab/b", "county-income")
    _register(catalog, "https://github.com/example-lab/c", "survey-tools", project_type="code")

    rc, payload, _ = _locate(capsys, "zzz")

    assert rc == 1
    assert payload["candidates"] == ["county-income", "hospital-beds"]


def test_a_product_stored_by_content_hash_is_refused(catalog, producer, capsys) -> None:
    """Without `version_aware` a file's S3 key is its md5, not its path, so
    there is no path to hand a reader."""
    _commit(producer, {
        "dvc.lock": _lock({"a.parquet": None}),
        ".dvc/config": DVC_CONFIG.replace("    version_aware = true\n", ""),
    })
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["exit_code"] == 1
    assert "versioning: false" in payload["error"]
    assert payload["hint"] == "clone the repo and read its .dvc files:\nmintd data clone demo-map"
    assert "files" not in payload


@pytest.mark.parametrize("rev", ["-v", "--tags", "--upload-pack=touch {marker}; git-upload-pack"])
def test_a_rev_that_reads_as_an_option_is_refused_before_git_runs(catalog, producer, capsys, tmp_path, rev: str) -> None:
    """git would read it as a fetch option: `-v` once read the default
    branch and called it that rev, and `--upload-pack` runs a command."""
    marker = tmp_path / "pwned"
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map", f"--rev={rev.format(marker=marker)}")

    assert rc == 1
    assert payload["error"].startswith("--rev '-") and payload["error"].endswith("is not a tag or a commit")
    assert payload["hint"] == "give --rev one of its release tags or a commit's SHA"
    assert not marker.exists()


def test_the_fetcher_reads_an_option_like_pin_as_a_ref(producer, tmp_path) -> None:
    """Below locate's own check: `checkout_at` ends git's options before the
    pin, so git reads it as a refspec, and refuses it, rather than run it."""
    marker = tmp_path / "pwned"

    with pytest.raises(FetchError):
        GitArchiveFetcher().checkout_at(producer.url, f"--upload-pack=touch {marker}; git-upload-pack", tmp_path / "tree")

    assert not marker.exists()


@pytest.mark.parametrize(
    "config",
    [DVC_CONFIG + "    endpointurl = https://other.example.com\n",
     DVC_CONFIG + f"['remote \"{REMOTE}\"']\n    url = s3://other/\n",
     DVC_CONFIG.encode() + b"    region = caf\xe9\n",
     DVC_CONFIG.replace("version_aware = true", "version_aware = yes")],
    ids=["option-twice", "section-twice", "not-utf-8", "version-aware-neither-true-nor-false"],
)
def test_a_dvc_config_that_cant_be_read_is_a_json_error(catalog, producer, capsys, config: str | bytes) -> None:
    commit = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"}), ".dvc/config": config})
    _register(catalog, producer.url)

    rc, payload, err = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["exit_code"] == 1
    assert payload["error"].startswith(f"demo-map's .dvc/config at its default branch (commit {commit[:7]}) can't be read: ")
    assert payload["hint"] == "ask the producer to fix .dvc/config"
    assert err.startswith("error: ")


@pytest.mark.parametrize(
    "config",
    [DVC_CONFIG.replace("s3://lab-bucket/pub/data_demo-map/", "/mnt/elsewhere"), DVC_CONFIG.replace(f"    remote = {REMOTE}\n", "")],
    ids=["not-s3", "no-default-remote"],
)
def test_a_dvc_config_with_no_s3_default_remote_is_refused(catalog, producer, capsys, config: str) -> None:
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"}), ".dvc/config": config})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert "names no S3 remote" in payload["error"]
    assert payload["hint"] == "ask the producer to run 'mintd check'"


def test_a_product_with_no_primary_and_no_path_is_refused(catalog, producer, capsys) -> None:
    _register(catalog, producer.url, primary=None, project_type="project")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"] == "demo-map declares no data_products.primary"
    assert payload["hint"] == "name a file or folder; this lists them:\nmintd data ls demo-map"


def test_a_cache_dir_it_cant_write_is_named(catalog, producer, capsys, tmp_path) -> None:
    (tmp_path / "cache").write_text("a file where the folder should be", encoding="utf-8")
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 1
    assert payload["error"].startswith("could not write mintd's copy of the producer's DVC files in ")
    assert payload["hint"] == "set cache_dir in mintd's config to a folder you can write"


# --- the payload --------------------------------------------------------------


def test_the_json_payload_has_the_shape_every_reader_parses(catalog, producer, capsys, monkeypatch, tmp_path) -> None:
    """The contract every reader shares: exactly these keys, a url per file
    that is `s3://<bucket>/<prefix><key>`, the md5 and version ID the pointer
    records (null where it records none), and the profile mintd's own S3
    clients use (here `[mintd]`, found in the credentials file)."""
    creds = tmp_path / "credentials"
    creds.write_text("[mintd]\naws_access_key_id = x\naws_secret_access_key = y\n", encoding="utf-8")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    commit = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1", "b.dta": None})}, tag="v0.1.1")
    _register(catalog, producer.url, release="0.1.1")

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert payload == {
        "name": "demo-map",
        "bucket": "lab-bucket",
        "prefix": "pub/data_demo-map/",
        "endpoint": "https://s3.example.com",
        "profile": "mintd",
        "primary": "data/final/",
        "target": "data/final/",
        "rev": "v0.1.1",
        "commit": commit,
        "classification": "public",
        "files": [
            {
                "key": "data/final/a.parquet",
                "url": "s3://lab-bucket/pub/data_demo-map/data/final/a.parquet",
                "size": len("a.parquet"),
                "md5": _md5("a.parquet", "va1"),
                "version_id": "va1",
            },
            {
                "key": "data/final/b.dta",
                "url": "s3://lab-bucket/pub/data_demo-map/data/final/b.dta",
                "size": len("b.dta"),
                "md5": _md5("b.dta", None),
                "version_id": None,
            },
        ],
    }


def test_it_needs_no_aws_credentials(catalog, producer, capsys, monkeypatch, tmp_path) -> None:
    """Reading pointers asks S3 nothing. Listing the bucket, as locate once
    did, stopped a machine with no AWS credentials with a botocore traceback
    and no JSON on stdout."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert _versions(payload) == {"data/final/a.parquet": "va1"}


@pytest.mark.parametrize(
    ("aws_profile", "expected"),
    [(None, None), ("lab-reader", "lab-reader")],
    ids=["none-named", "from-AWS_PROFILE"],
)
def test_profile_is_null_when_mintd_names_none_and_follows_aws_profile(
    catalog, producer, capsys, monkeypatch, aws_profile: str | None, expected: str | None,
) -> None:
    """No `[mintd]` section and no AWS_PROFILE: mintd uses boto3's default
    credentials and names no profile, so neither does the payload. An
    exported AWS_PROFILE is the profile mintd uses, so it is the one
    reported. The producer's `.dvc/config` names `mintd`, a profile on the
    producer's machine, which is not what a reader uses."""
    if aws_profile:
        monkeypatch.setenv("AWS_PROFILE", aws_profile)
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"})})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert payload["profile"] == expected


def test_it_reads_no_metadata_json(catalog, producer, capsys) -> None:
    """Everything it prints from the producer comes from its DVC files, so a
    `metadata.json` it couldn't parse is no matter."""
    _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1"}), "metadata.json": "[]\n"})
    _register(catalog, producer.url)

    rc, payload, _ = _locate(capsys, "demo-map")

    assert rc == 0
    assert _versions(payload) == {"data/final/a.parquet": "va1"}


def test_without_json_it_prints_no_control_character_the_producer_wrote(catalog, producer, capsys) -> None:
    """The endpoint comes from the producer's `.dvc/config`, as keys come
    from its pointers: an escape sequence in either would reach the
    terminal."""
    _commit(producer, {
        "dvc.lock": _lock({"a.parquet": "va1"}),
        ".dvc/config": DVC_CONFIG.replace("https://s3.example.com", "https://s3.example.com\x1b[2J\x1b[31mFAKE"),
    })
    _register(catalog, producer.url)

    rc = cli.main(["data", "locate", "demo-map"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "\x1b" not in out
    assert "https://s3.example.com" in out


def test_without_json_it_prints_the_version_and_each_url(catalog, producer, capsys) -> None:
    commit = _commit(producer, {"dvc.lock": _lock({"a.parquet": "va1", "sub/b.dta": "vb1"})}, tag="v0.1.1")
    _register(catalog, producer.url, release="0.1.1")

    rc = cli.main(["data", "locate", "demo-map"])
    out = capsys.readouterr().out

    assert rc == 0
    assert f"demo-map at v0.1.1 (commit {commit[:7]}, public): data/final/" in out
    assert "s3://lab-bucket/pub/data_demo-map/data/final/a.parquet" in out
    assert "s3://lab-bucket/pub/data_demo-map/data/final/sub/b.dta" in out
    assert "https://s3.example.com" in out
    assert "2 file(s)" in out


def test_data_ls_hints_a_command_that_works_outside_a_project(
    catalog, producer, capsys, monkeypatch, s3_versioned: tuple[Any, str],
) -> None:
    """`data ls` runs from anywhere, so the next step it suggests must too. It
    once suggested `mintd data pull <name> <key>`, which fails outside a
    project (no .dvc/) and, inside one, reads the name as a tracked target."""
    s3, bucket = s3_versioned
    monkeypatch.setattr(
        "mintd.cli._resolve_s3_listing_ops",
        lambda cfg: functools.partial(list_product_objects, s3_client_factory=lambda remote, profile: s3),
    )
    s3.put_object(Bucket=bucket, Key="products/demo-map/data/final/sub/b.dta", Body=b"x")
    _commit(producer, {"dvc.lock": _lock({"sub/b.dta": "vb1"})})
    _register(catalog, producer.url)

    assert cli.main(["--json", "data", "ls", "demo-map", "data/final/"]) == 0
    hint = json.loads(capsys.readouterr().out)["hint"]
    argv = shlex.split(hint)
    assert argv[0] == "mintd"

    rc = cli.main(["--json", *argv[1:]])

    assert rc == 0, capsys.readouterr().err
    assert [f["key"] for f in json.loads(capsys.readouterr().out)["files"]] == ["data/final/sub/b.dta"]
