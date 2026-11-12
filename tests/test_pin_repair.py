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

    def bad_dump(doc):
        calls.append(1)
        if len(calls) == 2:  # the second file's dump also corrupts its md5
            doc["outs"][0]["md5"] = "0" * 32
        return yaml.safe_dump(doc, sort_keys=False)

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
    _head_reply(s3, 400, versioned=False)
    plan = _plan(tmp_path, s3, bucket, ["a.csv.dvc"])
    assert [e.path for e in plan.inconclusive] == ["a.csv"]
    assert not plan.changes
