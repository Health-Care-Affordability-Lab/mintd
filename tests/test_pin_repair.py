"""Repair before the push: plan per entry, write only cloud[R], check every write."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from mintd._fast_sync_ops import cache_path_for
from mintd._pin_proof import (
    NO_ID,
    STALE_ID,
    CacheHashes,
    PointerWriteError,
    cause_lines,
    plan_repair,
    publishable_scope,
    write_repair,
)

R = "storage"
PREFIX = "lab/p"


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _cache(root: Path, data: bytes) -> str:
    path = cache_path_for(root / ".dvc" / "cache", _md5(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _md5(data)


def _pointer(root: Path, rel: str, out: str, data: bytes, cloud: dict | None = None) -> None:
    rec: dict = {"md5": _md5(data), "size": len(data), "hash": "md5", "path": out}
    if cloud is not None:
        rec["cloud"] = cloud
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(yaml.safe_dump({"outs": [rec]}, sort_keys=False), encoding="utf-8")


def _plan(root: Path, s3, bucket: str, files: list[str]):
    scope = publishable_scope(root, remote=R, prefix=PREFIX, cache_dir=root / ".dvc" / "cache",
                              dvc_files=files)
    return plan_repair(s3, bucket, scope.entries, CacheHashes(root / ".dvc" / "cache"))


def test_a_folder_dvc_rewrote_as_a_dir_entry_is_reattached_as_a_files_list(s3_versioned, tmp_path: Path) -> None:
    """`dvc commit -f` and `dvc repro` write a folder out back as `md5: <tree>.dir`,
    dropping every ID. Files whose bytes are at their key are re-attached in the
    files list DVC writes after a push; the rest are listed for DVC to upload."""
    import json

    s3, bucket = s3_versioned
    a = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/data/out/a.csv", Body=b"A")
    tree = [{"md5": _cache(tmp_path, b"A"), "relpath": "a.csv"}, {"md5": _cache(tmp_path, b"B"), "relpath": "b.csv"}]
    tree_md5 = _md5(json.dumps(tree).encode()) + ".dir"
    obj = cache_path_for(tmp_path / ".dvc" / "cache", tree_md5)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_text(json.dumps(tree))
    (tmp_path / "dvc.yaml").write_text("stages:\n  many:\n    cmd: make\n    outs: [data/out]\n")
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  many:\n    cmd: make\n    outs:\n    - path: data/out\n"
        f"      hash: md5\n      md5: {tree_md5}\n      size: 2\n      nfiles: 2\n"
    )
    plan = _plan(tmp_path, s3, bucket, [])
    assert [(e.path, r) for e, _, r in plan.reattach] == [("data/out/a.csv", NO_ID)]
    assert [e.path for e, _ in plan.strip] == ["data/out/b.csv"]
    assert "dvc.lock lost these IDs" in " ".join(cause_lines(plan, "s3://b/lab/p"))

    assert write_repair(tmp_path, plan, R) == ["dvc.lock"]
    (out,) = yaml.safe_load((tmp_path / "dvc.lock").read_text())["stages"]["many"]["outs"]
    assert out == {"path": "data/out", "hash": "md5", "files": [
        {"relpath": "a.csv", "md5": _md5(b"A"),
         "cloud": {R: {"etag": a["ETag"].strip('"'), "version_id": a["VersionId"]}}},
        {"relpath": "b.csv", "md5": _md5(b"B")},
    ]}


def test_new_data_with_no_id_writes_nothing(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    _cache(tmp_path, b"A")
    before = (tmp_path / "a.csv.dvc").read_bytes()
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert [e.path for e, _ in plan.strip] == ["a.csv"]
    assert write_repair(tmp_path, plan, R) == [] and (tmp_path / "a.csv.dvc").read_bytes() == before


def test_a_pin_that_proves_its_file_is_kept(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": _md5(b"A"), "version_id": v["VersionId"]}})
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert [e.path for e in plan.keep] == ["a.csv"] and not plan.changes


def test_no_id_with_the_bytes_already_at_the_key_is_reattached_not_uploaded(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")["VersionId"]
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")  # dvc commit -f dropped the cloud block
    _cache(tmp_path, b"A")
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    ((old, new, reason),) = plan.reattach
    assert (old.version_id, new.version_id, new.etag, reason) == (None, v, _md5(b"A"), NO_ID)
    assert not plan.strip


def test_a_stale_id_is_reattached_to_the_current_object_when_it_proves_the_file(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    old = s3.put_object(Bucket=bucket, Key="old-prefix/a.csv", Body=b"A")["VersionId"]
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")["VersionId"]
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": _md5(b"A"), "version_id": old}})
    ((_, new, reason),) = _plan(tmp_path, s3, bucket, ["a.csv.dvc"]).reattach
    assert (new.version_id, reason) == (v, STALE_ID)


def test_other_bytes_at_the_key_are_never_reattached(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    other = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"B")["VersionId"]
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    _cache(tmp_path, b"A")
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert not plan.reattach
    assert [(e.path, r) for e, r in plan.strip] == [("a.csv", NO_ID)]
    assert plan.observed == {f"{PREFIX}/a.csv": other}


def test_nothing_at_the_key_and_no_cache_is_unrecoverable(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": _md5(b"A"), "version_id": "gone"}})
    ((e, why),) = _plan(tmp_path, s3, bucket, ["a.csv.dvc"]).unrecoverable
    assert e.path == "a.csv" and "not in the local cache" in why


def test_a_cache_file_with_other_bytes_is_unrecoverable_and_named(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    bad = cache_path_for(tmp_path / ".dvc" / "cache", _md5(b"A"))
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"corrupt")
    ((_, why),) = _plan(tmp_path, s3, bucket, ["a.csv.dvc"]).unrecoverable
    assert str(bad) in why


def test_strip_removes_only_this_remotes_block(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _cache(tmp_path, b"A")
    other = {"etag": "e", "version_id": "vb"}
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": "x", "version_id": "gone"}, "backup": other})
    _pointer(tmp_path, "b.csv.dvc", "b.csv", b"A", {R: {"etag": "x", "version_id": "gone"}})
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc", "b.csv.dvc"])
    assert write_repair(tmp_path, plan, R) == ["a.csv.dvc", "b.csv.dvc"]
    a = yaml.safe_load((tmp_path / "a.csv.dvc").read_text())["outs"][0]
    b = yaml.safe_load((tmp_path / "b.csv.dvc").read_text())["outs"][0]
    assert a["cloud"] == {"backup": other}
    assert "cloud" not in b
    assert a["md5"] == _md5(b"A") and a["path"] == "a.csv"


def test_reattach_writes_the_found_version_and_etag(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")["VersionId"]
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    write_repair(tmp_path, _plan(tmp_path, s3, bucket, ["a.csv.dvc"]), R)
    rec = yaml.safe_load((tmp_path / "a.csv.dvc").read_text())["outs"][0]
    assert rec["cloud"] == {R: {"etag": _md5(b"A"), "version_id": v}}


def test_a_write_that_changes_anything_else_restores_every_pointer(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _cache(tmp_path, b"A")
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": "x", "version_id": "gone"}})
    _pointer(tmp_path, "b.csv.dvc", "b.csv", b"A", {R: {"etag": "x", "version_id": "gone"}})
    before = {p: (tmp_path / p).read_bytes() for p in ("a.csv.dvc", "b.csv.dvc")}
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc", "b.csv.dvc"])
    calls = []

    from mintd._pin_proof import _dump

    def bad_dump(doc):
        calls.append(1)
        if len(calls) == 2:  # the second file's dump also corrupts its md5
            doc["outs"][0]["md5"] = "0" * 32
        return _dump(doc)

    with pytest.raises(PointerWriteError, match="b.csv.dvc"):
        write_repair(tmp_path, plan, R, dump=bad_dump)
    assert {p: (tmp_path / p).read_bytes() for p in before} == before


def test_cause_lines_name_the_likely_cause(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    lines = cause_lines(_plan(tmp_path, s3, bucket, ["a.csv.dvc"]), f"s3://{bucket}/{PREFIX}")
    assert lines == ["`dvc add` or `dvc commit` rewrote these .dvc files without their IDs"]
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": "x", "version_id": "gone"}})
    lines = cause_lines(_plan(tmp_path, s3, bucket, ["a.csv.dvc"]), f"s3://{bucket}/{PREFIX}")
    assert lines == [f"the recorded versions aren't at s3://{bucket}/{PREFIX}; the remote has probably moved"]


class _Body:
    def stream(self, **_kw):
        yield b""

    def read(self, *_a):
        return b""


def _head_reply(s3, status: int, headers: dict[str, str] | None = None, *, versioned: bool | None = None) -> None:
    """Answer HEADs with ``status``; ``versioned`` limits it to HEADs with
    (True) or without (False) a ``versionId``."""
    from botocore.awsrequest import AWSResponse

    def hook(request, **_kw):
        if versioned is not None and ("versionId=" in request.url) != versioned:
            return None
        return AWSResponse(request.url, status, headers or {}, _Body())

    s3.meta.events.register_first("before-send.s3.HeadObject", hook)


def test_a_multipart_pin_with_no_cache_file_is_kept_on_size_alone(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A" * 7, {R: {"etag": "e" * 32 + "-3", "version_id": "v1"}})
    _head_reply(s3, 200, {"ETag": '"' + "e" * 32 + '-3"', "Content-Length": "7", "x-amz-version-id": "v1"})
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert [e.path for e in plan.keep] == ["a.csv"] and not plan.changes


def test_no_answer_for_the_current_object_leaves_the_entry_unchanged(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _cache(tmp_path, b"A")
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A")
    _head_reply(s3, 409, versioned=False)  # unmapped: inconclusive (a HEAD's 400 is re-sent)
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert [e.path for e in plan.inconclusive] == ["a.csv"]
    assert not plan.changes


# --- the proof memo -----------------------------------------------------------

from mintd._fast_sync_ops import S3AccessDenied  # noqa: E402
from mintd._pin_proof import MEMO_SAMPLE, ProofMemo  # noqa: E402


def _pinned_files(root: Path, s3, bucket: str, n: int) -> list[str]:
    files = []
    for i in range(n):
        data = f"file {i}\n".encode()
        v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/f{i}.csv", Body=data)["VersionId"]
        _pointer(root, f"f{i}.csv.dvc", f"f{i}.csv", data, {R: {"etag": _md5(data), "version_id": v}})
        files.append(f"f{i}.csv.dvc")
    return files


def _count_heads(s3) -> list[str]:
    seen: list[str] = []
    s3.meta.events.register("before-send.s3.HeadObject", lambda request, **_: seen.append(request.url))
    return seen


def _plan_with(root: Path, s3, bucket: str, files: list[str], memo: ProofMemo):
    scope = publishable_scope(root, remote=R, prefix=PREFIX, cache_dir=root / ".dvc" / "cache", dvc_files=files)
    return plan_repair(s3, bucket, scope.entries, CacheHashes(root / ".dvc" / "cache"), memo)


def test_a_second_push_of_unchanged_data_heads_only_the_sample(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    files = _pinned_files(tmp_path, s3, bucket, MEMO_SAMPLE + 8)
    db = tmp_path / "memo.sqlite"
    heads = _count_heads(s3)
    assert len(_plan_with(tmp_path, s3, bucket, files, ProofMemo(db, "e")).keep) == len(files)
    assert len(heads) == len(files)
    heads.clear()
    memo = ProofMemo(db, "e")
    assert len(_plan_with(tmp_path, s3, bucket, files, memo).keep) == len(files)
    assert len(heads) == MEMO_SAMPLE and memo.hits == 8


def test_a_remembered_version_whose_md5_changed_is_probed_again(s3_versioned, tmp_path: Path) -> None:
    """Whether or not the sample happens to include it, a file whose md5
    changed since its version was proven is HEADed, fails, and the rest of
    the memo still answers."""
    s3, bucket = s3_versioned
    n = MEMO_SAMPLE * 2 + 8
    files = _pinned_files(tmp_path, s3, bucket, n)
    db = tmp_path / "memo.sqlite"
    _plan_with(tmp_path, s3, bucket, files, ProofMemo(db, "e"))
    rec = yaml.safe_load((tmp_path / files[0]).read_text())
    rec["outs"][0]["md5"] = _md5(b"new data")
    (tmp_path / files[0]).write_text(yaml.safe_dump(rec, sort_keys=False))
    memo = ProofMemo(db, "e")
    plan = _plan_with(tmp_path, s3, bucket, files, memo)
    assert "f0.csv" not in [e.path for e in plan.keep]
    assert memo.hits == n - 1 - MEMO_SAMPLE  # every other file: remembered, beyond the sample


def test_lost_read_access_is_caught_by_the_sample(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    files = _pinned_files(tmp_path, s3, bucket, 3)
    db = tmp_path / "memo.sqlite"
    _plan_with(tmp_path, s3, bucket, files, ProofMemo(db, "e"))
    _head_reply(s3, 403, versioned=True)  # versioned reads now denied
    with pytest.raises(S3AccessDenied):
        _plan_with(tmp_path, s3, bucket, files, ProofMemo(db, "e"))


def test_a_size_only_pass_is_never_remembered(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A" * 7, {R: {"etag": "e" * 32 + "-3", "version_id": "v1"}})
    _head_reply(s3, 200, {"ETag": '"' + "e" * 32 + '-3"', "Content-Length": "7", "x-amz-version-id": "v1"})
    db = tmp_path / "memo.sqlite"
    plan = _plan_with(tmp_path, s3, bucket, ["a.csv.dvc"], ProofMemo(db, "e"))
    assert [e.path for e in plan.keep] == ["a.csv"]
    import sqlite3

    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM proven").fetchone() == (0,)


def test_a_memo_that_stops_answering_is_turned_off_not_raised(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")
    _pointer(tmp_path, "a.csv.dvc", "a.csv", b"A", {R: {"etag": v["ETag"].strip('"'), "version_id": v["VersionId"]}})
    memo = ProofMemo(tmp_path / "memo.sqlite", "e")
    memo._db.close()  # any sqlite error from here on
    plan = _plan_with(tmp_path, s3, bucket, ["a.csv.dvc"], memo)
    assert [e.path for e in plan.keep] == ["a.csv"] and memo.disabled


def test_a_rewrite_keeps_dvcs_yaml_1_2_values(s3_versioned, tmp_path: Path) -> None:
    """DVC writes dvc.lock as YAML 1.2, where an unquoted `no` is a string.
    Rewritten through a YAML 1.1 library it would become `false`."""
    s3, bucket = s3_versioned
    v = s3.put_object(Bucket=bucket, Key=f"{PREFIX}/a.csv", Body=b"A")["VersionId"]
    lock = (
        "schema: '2.0'\nstages:\n  build:\n    cmd: make\n    params:\n      params.yaml:\n"
        "        flag: no\n        t: 1:30\n    outs:\n    - path: a.csv\n      hash: md5\n"
        f"      md5: {_md5(b'A')}\n      size: 1\n"
    )
    (tmp_path / "dvc.lock").write_text(lock)
    scope = publishable_scope(tmp_path, remote=R, prefix=PREFIX, cache_dir=tmp_path / ".dvc" / "cache", dvc_files=[])
    plan = plan_repair(s3, bucket, scope.entries, CacheHashes(tmp_path / ".dvc" / "cache"))
    assert write_repair(tmp_path, plan, R) == ["dvc.lock"]
    text = (tmp_path / "dvc.lock").read_text()
    assert "flag: no" in text and "t: 1:30" in text and f"version_id: {v}" in text
