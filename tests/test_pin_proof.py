"""_pin_proof: which entries a push answers for, their keys, and the content proof."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from mintd._fast_sync_ops import HeadVerdict, cache_path_for, probe_versions
from mintd._pin_proof import CacheHashes, Entry, hash_cache_object, prove, publishable_scope

R = "storage"


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _write(root: Path, rel: str, doc: object) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _cache(root: Path, data: bytes) -> str:
    md5 = _md5(data)
    path = cache_path_for(root / ".dvc" / "cache", md5)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return md5


def _scope(root: Path, dvc_files: list[str]):
    return publishable_scope(root, remote=R, prefix="lab/p", cache_dir=root / ".dvc" / "cache",
                             dvc_files=dvc_files)


def test_dvc_file_and_files_format_entries_with_keys(tmp_path: Path) -> None:
    _write(tmp_path, "data/raw.dvc", {"outs": [{"path": "x.csv", "md5": "a" * 32, "size": 3,
                                                 "cloud": {R: {"version_id": "v1", "etag": "a" * 32}}}]})
    _write(tmp_path, "data/final.dvc", {"outs": [{"path": "final", "files": [
        {"relpath": "a.csv", "md5": "b" * 32, "size": 1, "cloud": {R: {"version_id": "v2", "etag": "b" * 32}}},
        {"relpath": "sub/b.csv", "md5": "c" * 32, "size": 2},
    ]}]})
    entries = {e.path: e for e in _scope(tmp_path, ["data/raw.dvc", "data/final.dvc"]).entries}
    assert set(entries) == {"data/x.csv", "data/final/a.csv", "data/final/sub/b.csv"}
    assert entries["data/x.csv"].key == "lab/p/data/x.csv"
    assert entries["data/x.csv"].version_id == "v1"
    assert entries["data/final/sub/b.csv"].key == "lab/p/data/final/sub/b.csv"
    assert entries["data/final/sub/b.csv"].version_id is None


def test_an_id_under_another_remote_is_no_id_for_this_one(tmp_path: Path) -> None:
    _write(tmp_path, "x.dvc", {"outs": [{"path": "x.csv", "md5": "a" * 32,
                                          "cloud": {"backup": {"version_id": "vb", "etag": "e"}}}]})
    (entry,) = _scope(tmp_path, ["x.dvc"]).entries
    assert entry.version_id is None and entry.etag is None


def test_lock_outs_resolve_against_the_stage_wdir_and_skip_cache_and_push_false(tmp_path: Path) -> None:
    _write(tmp_path, "dvc.yaml", {"stages": {"clean": {"wdir": "code", "cmd": "x", "outs": [
        "../data/final/x.csv", {"../data/tmp.csv": {"cache": False}}, {"../data/local.csv": {"push": False}},
    ]}}})
    _write(tmp_path, "dvc.lock", {"schema": "2.0", "stages": {"clean": {"cmd": "x", "outs": [
        {"path": "../data/final/x.csv", "hash": "md5", "md5": "a" * 32, "size": 1},
        {"path": "../data/tmp.csv", "hash": "md5", "md5": "b" * 32, "size": 1},
        {"path": "../data/local.csv", "hash": "md5", "md5": "c" * 32, "size": 1},
        {"path": "../../outside.csv", "hash": "md5", "md5": "d" * 32, "size": 1},
    ]}}})
    entries = _scope(tmp_path, []).entries
    assert [(e.path, e.key, e.stage) for e in entries] == [
        ("data/final/x.csv", "lab/p/data/final/x.csv", "clean"),
    ]


def test_names_are_read_as_dvc_reads_them_yaml_1_2(tmp_path: Path) -> None:
    """DVC leaves `on`, `no` and `12:30` unquoted; under YAML 1.1 they would
    be a boolean stage name (dropped) and keys like `.../True`."""
    (tmp_path / "dvc.yaml").write_text("stages:\n  on:\n    cmd: x\n    outs: [no]\n")
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  on:\n    cmd: x\n    outs:\n    - path: no\n"
        f"      hash: md5\n      md5: {'a' * 32}\n      size: 1\n"
    )
    (tmp_path / "labels.dvc").write_text(
        f"outs:\n- path: labels\n  files:\n  - relpath: on\n    md5: {'b' * 32}\n    size: 1\n"
        f"  - relpath: 12:30\n    md5: {'c' * 32}\n    size: 1\n"
    )
    keys = sorted(e.key for e in _scope(tmp_path, ["labels.dvc"]).entries)
    assert keys == ["lab/p/labels/12:30", "lab/p/labels/on", "lab/p/no"]


def test_a_dvc_file_out_with_push_false_and_an_undeclared_lock_stage_are_out_of_scope(tmp_path: Path) -> None:
    _write(tmp_path, "local.dvc", {"outs": [{"path": "local.csv", "md5": "a" * 32, "push": False}]})
    _write(tmp_path, "dvc.yaml", {"stages": {"kept": {"cmd": "x", "outs": ["k.csv"]}}})
    _write(tmp_path, "dvc.lock", {"schema": "2.0", "stages": {
        "kept": {"cmd": "x", "outs": [{"path": "k.csv", "md5": "b" * 32, "size": 1}]},
        "renamed": {"cmd": "x", "outs": [{"path": "r.csv", "md5": "c" * 32, "size": 1}]},
    }})
    assert [e.path for e in _scope(tmp_path, ["local.dvc"]).entries] == ["k.csv"]


def test_repo_imports_are_out_of_scope(tmp_path: Path) -> None:
    _write(tmp_path, "imp.dvc", {"deps": [{"path": "d", "repo": {"url": "u", "rev_lock": "r"}}],
                                 "outs": [{"path": "d", "md5": "a" * 32}]})
    assert _scope(tmp_path, ["imp.dvc"]).entries == ()


def test_a_legacy_dir_out_is_listed_from_its_cached_tree_or_unrecoverable(tmp_path: Path) -> None:
    tree = [{"md5": "a" * 32, "relpath": "a.csv", "size": 1}]
    tree_md5 = _md5(json.dumps(tree).encode()) + ".dir"
    path = cache_path_for(tmp_path / ".dvc" / "cache", tree_md5)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(tree))
    _write(tmp_path, "d.dvc", {"outs": [{"path": "d", "md5": tree_md5}]})
    _write(tmp_path, "gone.dvc", {"outs": [{"path": "gone", "md5": "f" * 32 + ".dir"}]})
    scope = _scope(tmp_path, ["d.dvc", "gone.dvc"])
    assert [(e.path, e.legacy_dir, e.version_id) for e in scope.entries] == [("d/a.csv", True, None)]
    assert [o.path for o in scope.unrecoverable] == ["gone"]


def _entry(md5: str, size: int | None) -> Entry:
    return Entry(pointer="x.dvc", stage=None, out="x", relpath=None, md5=md5, size=size, key="k")


def test_single_part_proof(tmp_path: Path) -> None:
    cache = CacheHashes(tmp_path)
    md5 = _md5(b"abc")
    assert prove(_entry(md5, 3), HeadVerdict("found", "v", md5, 3), cache) == "ok"
    assert prove(_entry(md5, 3), HeadVerdict("found", "v", _md5(b"abd"), 3), cache) == "wrong_bytes"
    for kind in ("missing", "unpinned", "inconclusive"):
        assert prove(_entry(md5, 3), HeadVerdict(kind), cache) == kind


def test_multipart_without_a_cache_file_is_an_allowance_only_when_the_size_matches(tmp_path: Path) -> None:
    cache = CacheHashes(tmp_path)
    md5 = "a" * 32
    assert prove(_entry(md5, 200), HeadVerdict("found", "v", "e" * 32 + "-3", 200), cache) == "size_only"
    assert prove(_entry(md5, 200), HeadVerdict("found", "v", "e" * 32 + "-3", 201), cache) == "unproven"
    assert prove(_entry(md5, None), HeadVerdict("found", "v", "e" * 32 + "-3", 200), cache) == "unproven"


def test_multipart_is_proven_by_recomputing_the_etag_from_the_cache_file(s3_versioned, tmp_path: Path) -> None:
    """A 101 MiB file, uploaded the way s3fs uploads it (50 MiB parts), to moto."""
    s3, bucket = s3_versioned
    data = bytes(range(256)) * (101 * 4096)  # 101 MiB
    md5 = _cache(tmp_path, data)
    part = 50 * 2**20
    mpu = s3.create_multipart_upload(Bucket=bucket, Key="p/big.bin")
    parts = [
        {"PartNumber": i + 1, "ETag": s3.upload_part(
            Bucket=bucket, Key="p/big.bin", UploadId=mpu["UploadId"], PartNumber=i + 1,
            Body=data[off:off + part])["ETag"]}
        for i, off in enumerate(range(0, len(data), part))
    ]
    s3.complete_multipart_upload(Bucket=bucket, Key="p/big.bin", UploadId=mpu["UploadId"],
                                 MultipartUpload={"Parts": parts})
    verdict = probe_versions(s3, bucket, [("p/big.bin", None)])[("p/big.bin", None)]
    cache = CacheHashes(tmp_path / ".dvc" / "cache")
    assert "-" in (verdict.etag or "")
    assert prove(_entry(md5, len(data)), verdict, cache) == "ok"
    # Same bytes, other chunking: unproven, never wrong_bytes.
    other = HeadVerdict("found", "v", _md5(b"other") + "-5", len(data))
    assert prove(_entry(md5, len(data)), other, cache) == "unproven"


def test_hash_cache_object_small_file_has_no_multipart_etag(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"hello")
    assert hash_cache_object(f) == (_md5(b"hello"), None, 5)
