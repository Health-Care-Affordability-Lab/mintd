"""Tests for `mintd._fast_sync_ops` — slice 18 single-file fast-sync.

Coverage: parsing, cache layout, S3 config, spot check, download primitive,
orchestrator branching, and the two reviewer-P0 regressions (original-target
preservation through fallback; boto3-missing degradation).

moto[s3] 5.x exposes the unified `mock_aws` decorator. Use `from moto import
mock_aws` (legacy `mock_s3` is removed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from mintd._fast_sync_ops import (
    SubprocessFastSyncOps,
    _dvc_version_ok,
    cache_path_for,
    check_bucket_versioning,
    classify_targets,
    discover_all_outs,
    ensure_dir_manifest,
    fetch_dir_contents,
    fetch_dir_manifest,
    fetch_files_dir_contents,
    fetch_to_cache,
    get_remote_config,
    is_cached,
    parse_dvc_outs,
    parse_s3_url,
    s3_key_for,
    spot_check_versions,
    DvcFileEntry,
    DvcOut,
)


# ---------- helpers ----------

def _md5_of(b: bytes) -> str:
    return hashlib.md5(b, usedforsecurity=False).hexdigest()


def _manifest_bytes(entries) -> bytes:
    """Mirror ``ensure_dir_manifest``'s serialization without touching disk.

    Keep this in sync with the implementation; the byte format is load-bearing
    because the manifest's md5 becomes its cache filename. Includes
    ``sort_keys=True`` to match the production code's defensive option.
    """
    import json
    sorted_entries = sorted(entries, key=lambda e: e.relpath)
    payload = [{"md5": e.md5, "relpath": e.relpath} for e in sorted_entries]
    return json.dumps(payload, sort_keys=True).encode()


def _put_dir_manifest(s3, bucket, prefix, entries) -> str:
    """Upload a manifest object to S3 at the .dir-suffixed key (DVC's layout).

    Real DVC pushes manifests to ``prefix/files/md5/XX/YYYY.dir`` — the
    ``.dir`` suffix is kept on the filename, not stripped. Returns
    ``{md5}.dir``.
    """
    from mintd._fast_sync_ops import s3_key_for
    body = _manifest_bytes(entries)
    raw_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
    full_md5 = f"{raw_md5}.dir"
    s3.put_object(Bucket=bucket, Key=s3_key_for(prefix, full_md5), Body=body)
    return full_md5


def _write_dvc_file_md5(tmp_path: Path, name: str, md5: str, version_id: str | None = None) -> None:
    body = f"outs:\n  - path: {name}\n    md5: {md5}\n    size: 0\n"
    if version_id:
        body += f"    version_id: {version_id}\n"
    (tmp_path / f"{name}.dvc").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{name}.dvc").write_text(body)


def _write_dvc_config(tmp_path: Path, bucket: str, prefix: str = "") -> None:
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    url = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
    cfg.write_text(
        "[core]\n"
        "    remote = origin\n"
        f"['remote \"origin\"']\n"
        f"    url = {url}\n"
    )


@pytest.fixture
def s3_versioned():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = "test-bucket"
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        yield client, bucket


# ---------- parsing (6) ----------

def test_parse_dvc_outs_standard_md5(tmp_path: Path) -> None:
    f = tmp_path / "a.dvc"
    f.write_text("outs:\n  - path: a\n    md5: cafe\n")
    outs = parse_dvc_outs(f, "origin")
    assert len(outs) == 1
    assert outs[0].md5 == "cafe"
    assert outs[0].is_dir is False
    assert outs[0].is_files_format is False


def test_parse_dvc_outs_dir_detected_from_dir_suffix(tmp_path: Path) -> None:
    f = tmp_path / "d.dvc"
    f.write_text("outs:\n  - path: d\n    md5: deadbeef.dir\n")
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].is_dir is True


def test_parse_dvc_outs_files_format(tmp_path: Path) -> None:
    f = tmp_path / "ff.dvc"
    f.write_text(
        "outs:\n  - path: ff\n    files:\n      - relpath: x\n        md5: 1111\n"
    )
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].is_files_format is True
    assert outs[0].md5 == ""


def test_parse_dvc_outs_hash_missing_returns_empty(tmp_path: Path) -> None:
    f = tmp_path / "h.dvc"
    f.write_text("outs:\n  - path: h\n    hash: md5\n")  # no md5 value, no files
    outs = parse_dvc_outs(f, "origin")
    assert outs == []


def test_parse_dvc_outs_extracts_version_id_top_level(tmp_path: Path) -> None:
    f = tmp_path / "v.dvc"
    f.write_text("outs:\n  - path: v\n    md5: ace\n    version_id: vid-1\n")
    assert parse_dvc_outs(f, "origin")[0].version_id == "vid-1"


def test_parse_dvc_outs_extracts_version_id_from_cloud(tmp_path: Path) -> None:
    f = tmp_path / "c.dvc"
    f.write_text(
        "outs:\n  - path: c\n    md5: bed\n"
        "    cloud:\n      origin:\n        version_id: vid-2\n"
    )
    assert parse_dvc_outs(f, "origin")[0].version_id == "vid-2"


# ---------- cache (2) ----------

def test_cache_path_layout(tmp_path: Path) -> None:
    assert cache_path_for(tmp_path, "abcdef") == tmp_path / "files" / "md5" / "ab" / "cdef"


def test_is_cached_missing_vs_present(tmp_path: Path) -> None:
    assert is_cached(tmp_path, "xyz") is False
    p = cache_path_for(tmp_path, "xyz")
    p.parent.mkdir(parents=True)
    p.write_bytes(b"")
    assert is_cached(tmp_path, "xyz") is True


# ---------- s3 config (5) ----------

def test_parse_s3_url_happy() -> None:
    assert parse_s3_url("s3://b/p/r") == ("b", "p/r")


def test_parse_s3_url_rejects_non_s3() -> None:
    with pytest.raises(ValueError):
        parse_s3_url("https://example.com/foo")


def test_get_remote_config_quoted_section(tmp_path: Path) -> None:
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("['remote \"origin\"']\n    url = s3://b/p\n")
    rc = get_remote_config(tmp_path, "origin")
    assert rc["url"] == "s3://b/p"


def test_get_remote_config_unquoted_section(tmp_path: Path) -> None:
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[remote origin]\n    url = s3://b/p\n")
    rc = get_remote_config(tmp_path, "origin")
    assert rc["url"] == "s3://b/p"


def test_get_remote_config_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        get_remote_config(tmp_path, "origin")


# ---------- versioning + spot check (4) ----------

def test_check_bucket_versioning_enabled(s3_versioned) -> None:
    s3, bucket = s3_versioned
    assert check_bucket_versioning(s3, bucket) is True


def test_check_bucket_versioning_disabled() -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="bare")
        assert check_bucket_versioning(s3, "bare") is False


def test_spot_check_versions_all_match(s3_versioned, tmp_path: Path) -> None:
    from mintd._fast_sync_ops import DvcOut

    s3, bucket = s3_versioned
    body = b"hello"
    md5 = _md5_of(body)
    resp = s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
    out = DvcOut(target="a", path="a", md5=md5, is_dir=False, version_id=resp["VersionId"])
    assert spot_check_versions(s3, bucket, "", [out], tmp_path) is True


def test_spot_check_versions_drift_returns_false(s3_versioned, tmp_path: Path) -> None:
    from mintd._fast_sync_ops import DvcOut

    s3, bucket = s3_versioned
    body = b"v1"
    md5 = _md5_of(body)
    s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
    out = DvcOut(target="a", path="a", md5=md5, is_dir=False, version_id="bogus-version")
    assert spot_check_versions(s3, bucket, "", [out], tmp_path) is False


# ---------- downloads (3) ----------

def test_fetch_to_cache_happy(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    body = b"payload"
    md5 = _md5_of(body)
    key = f"files/md5/{md5[:2]}/{md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    assert fetch_to_cache(s3, bucket, key, cp, md5) is True
    assert cp.read_bytes() == body


def test_fetch_to_cache_md5_mismatch_unlinks(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    body = b"payload"
    bad_md5 = "0" * 32
    key = f"files/md5/00/{bad_md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", bad_md5)
    with pytest.raises(ValueError, match="md5 mismatch"):
        fetch_to_cache(s3, bucket, key, cp, bad_md5)
    assert not cp.exists()
    # Also confirm the tmp sibling got cleaned up.
    assert not cp.with_suffix(".tmp").exists()


def test_fetch_to_cache_non_retryable_propagates(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    cp = cache_path_for(tmp_path / ".dvc" / "cache", "deadbeef")
    # No such key — boto3 raises ClientError with NoSuchKey (404), non-retryable.
    with pytest.raises(ClientError):
        fetch_to_cache(s3, bucket, "files/md5/de/adbeef", cp, "deadbeef")


# ---------- DVC version canary (2) ----------

def test_dvc_version_canary_match() -> None:
    with patch("mintd._fast_sync_ops.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "3.66.1\n"
        assert _dvc_version_ok() is True


def test_dvc_version_canary_mismatch() -> None:
    with patch("mintd._fast_sync_ops.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "3.67.0\n"
        assert _dvc_version_ok() is False


# ---------- classify_targets P0 (2) ----------

def test_classify_targets_stamps_original_target_string(tmp_path: Path) -> None:
    """Reviewer-P0 regression: out.target carries the user's exact string,
    even when they passed `data/foo.csv.dvc` (with .dvc suffix)."""
    _write_dvc_file_md5(tmp_path, "data/foo.csv", "cafe")
    all_outs, fallback, missing = classify_targets(
        tmp_path, ["data/foo.csv.dvc"], "origin"
    )
    assert len(all_outs) == 1
    assert all_outs[0].target == "data/foo.csv.dvc"
    assert all_outs[0].path == "data/foo.csv"


def test_classify_targets_no_longer_routes_dirs_to_fallback(tmp_path: Path) -> None:
    """Slice 20: dir-shaped outs land in all_outs, not fallback. The
    orchestrator's per-out loop dispatches on is_dir / is_files_format."""
    _write_dvc_file_md5(tmp_path, "data/md5dir", "deadbeef.dir")
    (tmp_path / "data" / "ffdir.dvc").write_text(
        "outs:\n  - path: data/ffdir\n    files:\n"
        "      - relpath: a.csv\n        md5: aaaa\n        size: 1\n"
    )
    all_outs, fallback, _ = classify_targets(
        tmp_path, ["data/md5dir", "data/ffdir"], "origin"
    )
    assert len(all_outs) == 2
    assert {o.target for o in all_outs} == {"data/md5dir", "data/ffdir"}
    assert fallback == []


def test_classify_targets_hash_missing_routes_to_hash_missing(tmp_path: Path) -> None:
    """Hash-missing .dvc (declares hash type but no md5 value) — must surface
    to caller as a separate bucket so it's never silently dropped."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.dvc").write_text("outs:\n  - path: x\n    hash: md5\n")
    all_outs, fallback, missing = classify_targets(tmp_path, ["data/x"], "origin")
    assert all_outs == []
    assert missing == ["data/x"]


# ---------- orchestrator (6) ----------

def test_try_fast_pull_falls_back_when_version_mismatch(tmp_path: Path) -> None:
    with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=False):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.success is False
    assert result.fallback_targets == ["a"]
    assert "version" in result.reason.lower()


def test_try_fast_pull_falls_back_when_no_config(tmp_path: Path) -> None:
    with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.fallback_targets == ["a"]
    assert "config" in result.reason.lower()


def test_try_fast_pull_falls_back_when_bucket_not_versioned(tmp_path: Path) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="bare")
        _write_dvc_config(tmp_path, "bare")
        body = b"x"
        md5 = _md5_of(body)
        _write_dvc_file_md5(tmp_path, "a", md5)
        s3.put_object(Bucket="bare", Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
        with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path, targets=["a"], remote_name="origin"
            )
    assert result.fallback_targets == ["a"]
    assert "versioning" in result.reason.lower()


def test_try_fast_pull_single_file_happy_path(tmp_path: Path) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-bucket"
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        body = b"hello world"
        md5 = _md5_of(body)
        s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
        _write_dvc_config(tmp_path, bucket)
        _write_dvc_file_md5(tmp_path, "data/x", md5)
        with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path, targets=["data/x"], remote_name="origin"
            )
    assert result.success is True
    assert result.synced_count == 1
    assert result.fallback_targets == []
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    assert cp.read_bytes() == body


def test_try_fast_pull_partial_failure_preserves_original_target_string(tmp_path: Path) -> None:
    """Reviewer-P0 regression: when a download fails, fallback_targets
    contains the user's original string, never the parsed `out.path`."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "test-bucket"
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        _write_dvc_config(tmp_path, bucket)
        _write_dvc_file_md5(tmp_path, "data/foo.csv", "deadbeef")
        # Do NOT upload the object — fetch will fail with NoSuchKey
        with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path,
                targets=["data/foo.csv.dvc"],  # user passes with .dvc suffix
                remote_name="origin",
            )
    assert result.fallback_targets == ["data/foo.csv.dvc"]


def test_try_fast_pull_falls_back_on_non_s3_remote(tmp_path: Path) -> None:
    """Orchestrator-gate test: a non-S3 URL routes to fallback with reason."""
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("['remote \"origin\"']\n    url = gs://bucket/p\n")
    with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.fallback_targets == ["a"]
    assert "non-s3" in result.reason.lower()


def test_try_fast_pull_falls_back_on_spot_check_drift(tmp_path: Path) -> None:
    """Orchestrator-gate test: when a version_id doesn't match, every target
    routes to fallback. Confirms the gate is actually wired into try_fast_pull
    (not just unit-tested in isolation)."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "drift-bucket"
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        body = b"x"
        md5 = _md5_of(body)
        s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
        _write_dvc_config(tmp_path, bucket)
        _write_dvc_file_md5(tmp_path, "data/a", md5, version_id="stale-version")
        with patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path, targets=["data/a"], remote_name="origin"
            )
    assert result.fallback_targets == ["data/a"]
    assert "spot" in result.reason.lower() or "version" in result.reason.lower()


def test_fetch_to_cache_retries_on_503_then_succeeds(tmp_path: Path) -> None:
    """Confirms the retry loop in fetch_to_cache actually retries on
    transient 503/SlowDown errors (not just non-retryable errors propagate)."""
    body = b"retried"
    md5 = _md5_of(body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    cp.parent.mkdir(parents=True)

    # Inject a 503 ClientError on the first two calls; succeed on the third.
    call_count = {"n": 0}
    slow_down = ClientError(
        {"Error": {"Code": "503", "Message": "SlowDown"}}, "GetObject"
    )

    def fake_download_file(Filename, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise slow_down
        Path(Filename).write_bytes(body)

    fake_s3 = type("FakeS3", (), {"download_file": staticmethod(fake_download_file)})()
    assert fetch_to_cache(fake_s3, "b", "k", cp, md5) is True
    assert call_count["n"] == 3
    assert cp.read_bytes() == body


def test_try_fast_pull_with_missing_boto3_falls_back(tmp_path: Path) -> None:
    """Step-6 reviewer-P1 regression: when boto3 is None at module scope,
    the orchestrator routes everything to fallback rather than crashing."""
    _write_dvc_config(tmp_path, "irrelevant")
    _write_dvc_file_md5(tmp_path, "a", "deadbeef")
    with patch("mintd._fast_sync_ops.boto3", None), \
         patch("mintd._fast_sync_ops._dvc_version_ok", return_value=True):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.fallback_targets == ["a"]
    assert "boto3" in result.reason.lower()



# ---------- slice 20 dir handling (11) ----------

def test_parse_dvc_outs_md5_dir_detected(tmp_path: Path) -> None:
    f = tmp_path / "d.dvc"
    f.write_text("outs:\n  - path: d\n    md5: abc.dir\n")
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].is_dir is True
    assert outs[0].files is None


def test_fetch_dir_manifest_returns_entries(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    h_a = "9a0364b9e99bb480dd25e1f0284c8555"
    h_b = "8c736521e9a8e2eee7e8a13a6e88f1ce"
    entries = [DvcFileEntry(h_a, "a.csv"), DvcFileEntry(h_b, "b.csv")]
    dir_md5 = _put_dir_manifest(s3, bucket, "", entries)
    res = fetch_dir_manifest(s3, bucket, "", dir_md5, tmp_path / "cache")
    # ensure_dir_manifest's payload format is {md5, relpath} only — no size —
    # so reads back as size=0 regardless of the original entry's size.
    assert res is not None
    assert {(e.md5, e.relpath) for e in res} == {(h_a, "a.csv"), (h_b, "b.csv")}


def test_fetch_dir_manifest_unverifiable_returns_none(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    dir_md5 = "bad.dir"
    # Put garbage at the .dir-suffixed key (DVC's layout).
    s3.put_object(Bucket=bucket, Key="files/md5/ba/d.dir", Body=b"garbage")
    cache_dir = tmp_path / "cache"
    res = fetch_dir_manifest(s3, bucket, "", dir_md5, cache_dir)
    assert res is None
    # And the corrupt download is unlinked from cache.
    assert not (cache_dir / "files" / "md5" / "ba" / "d.dir").exists()


def test_try_fast_pull_md5_dir_happy(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    h = "9a0364b9e99bb480dd25e1f0284c8555"
    entries = [DvcFileEntry(h, "a.csv", 7), DvcFileEntry(h, "b.csv", 7)]
    dir_md5 = _put_dir_manifest(s3, bucket, "", entries)
    for e in entries:
        s3.put_object(Bucket=bucket, Key=s3_key_for("", e.md5), Body=b"content")
    (tmp_path / "data").mkdir()
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')
    (tmp_path / "data" / "dir.dvc").write_text(f"outs:\n  - path: data/dir\n    md5: {dir_md5}\n")

    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=tmp_path, targets=["data/dir"], remote_name="origin"
    )
    assert result.success is True
    assert result.synced_count == 1
    assert result.fallback_targets == []
    for e in entries:
        assert (tmp_path / ".dvc" / "cache" / "files" / "md5" / e.md5[:2] / e.md5[2:]).exists()


def test_parse_dvc_outs_files_format_populates_files(tmp_path: Path) -> None:
    f = tmp_path / "f.dvc"
    f.write_text(
        "outs:\n  - path: f\n    files:\n      - relpath: a.csv\n        md5: aaaa\n"
        "        size: 1\n        cloud:\n          origin:\n            version_id: v1\n"
    )
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].files[0].version_id == "v1"


def test_fetch_files_dir_uses_real_paths(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    out = DvcOut(
        target="data/f", path="data/f", md5="", is_dir=False,
        is_files_format=True,
        files=[DvcFileEntry("a", "a.csv", 1, "v1")]
    )
    s3.put_object(Bucket=bucket, Key="data/f/a.csv", Body=b"content")
    with patch.object(s3, 'download_file', wraps=s3.download_file) as mock:
        fetch_files_dir_contents(s3, bucket, "", out, tmp_path / "cache", 1, "origin")
        assert mock.call_args[1]['Key'] == 'data/f/a.csv'


def test_ensure_dir_manifest_writes_synthetic_dir_file(tmp_path: Path) -> None:
    """Byte-match anchor: synthetic manifest bytes must equal what real DVC
    produces — ``{md5, relpath}`` only, sorted by relpath,
    ``json.dumps(sort_keys=True)`` with otherwise-default kwargs (no trailing
    newline). Filename lives at ``XX/YYYY.dir`` (matches DVC's
    ``LocalHashFileDB.oid_to_path``); any divergence breaks ``dvc checkout``.
    """
    entries = [DvcFileEntry("b", "b.csv"), DvcFileEntry("a", "a.csv")]
    manifest_name = ensure_dir_manifest(tmp_path, entries)
    expected_bytes = _manifest_bytes(entries)
    expected_md5 = hashlib.md5(expected_bytes, usedforsecurity=False).hexdigest()
    assert manifest_name == f"{expected_md5}.dir"
    # Filename keeps the .dir suffix (DVC's cache layout).
    manifest_path = tmp_path / "files" / "md5" / expected_md5[:2] / f"{expected_md5[2:]}.dir"
    assert manifest_path.exists()
    assert manifest_path.read_bytes() == expected_bytes
    # Anchor against DVC's exact bytes: keys alphabetized (md5 < relpath),
    # comma+space separators, no trailing newline.
    assert expected_bytes == b'[{"md5": "a", "relpath": "a.csv"}, {"md5": "b", "relpath": "b.csv"}]'


def test_try_fast_pull_files_format_happy(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    (tmp_path / "data").mkdir()
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')
    (tmp_path / "data" / "f.dvc").write_text(
        "outs:\n  - path: data/f\n    files:\n"
        "      - relpath: a.csv\n        md5: 9a0364b9e99bb480dd25e1f0284c8555\n        size: 7\n"
    )
    s3.put_object(Bucket=bucket, Key="data/f/a.csv", Body=b"content")
    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=tmp_path, targets=["data/f"], remote_name="origin"
    )
    assert result.success is True
    assert result.synced_count == 1
    # Synthetic manifest lands at the .dir-suffixed canonical cache location.
    expected_bytes = _manifest_bytes(
        [DvcFileEntry("9a0364b9e99bb480dd25e1f0284c8555", "a.csv")]
    )
    expected_md5 = hashlib.md5(expected_bytes, usedforsecurity=False).hexdigest()
    assert (
        tmp_path / ".dvc" / "cache" / "files" / "md5"
        / expected_md5[:2] / f"{expected_md5[2:]}.dir"
    ).exists()


def test_try_fast_pull_mixed_target_types_partial_failure_preserves_originals(s3_versioned, tmp_path: Path) -> None:
    """Original-target preservation pin for the dir lane: the user passes
    the failing dir target with the explicit ``.dvc`` suffix; the fallback
    list must carry that exact string, not the parsed ``out.path``. This
    mirrors the slice-18 single-file P0 pin."""
    s3, bucket = s3_versioned
    (tmp_path / "data").mkdir()
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')

    _write_dvc_file_md5(tmp_path, "data/s", "9a0364b9e99bb480dd25e1f0284c8555")
    s3.put_object(Bucket=bucket, Key="files/md5/9a/0364b9e99bb480dd25e1f0284c8555", Body=b"content")

    # md5-dir whose manifest object isn't in S3 → fallback.
    _write_dvc_file_md5(tmp_path, "data/mdir", "bad.dir")

    (tmp_path / "data" / "ffdir.dvc").write_text(
        "outs:\n  - path: data/ffdir\n    files:\n"
        "      - relpath: a.csv\n        md5: 9a0364b9e99bb480dd25e1f0284c8555\n        size: 7\n"
    )
    s3.put_object(Bucket=bucket, Key="data/ffdir/a.csv", Body=b"content")

    # User passes the failing dir target WITH the ``.dvc`` suffix —
    # ``out.target`` must carry that exact string, not ``out.path``
    # (which is ``data/mdir``, no suffix).
    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=tmp_path,
        targets=["data/s", "data/mdir.dvc", "data/ffdir"],
        remote_name="origin",
    )
    assert "data/mdir.dvc" in result.fallback_targets
    assert "data/mdir" not in result.fallback_targets  # confirm it's the user string, not out.path
    assert result.synced_count == 2


def test_try_fast_pull_files_format_empty_files_still_writes_manifest(s3_versioned, tmp_path: Path) -> None:
    """Plan step 11 invariant: even when out.files is empty, the synthetic
    .dir manifest must still be written so ``dvc checkout`` finds the dir
    hash in cache. An empty-array manifest hashes deterministically."""
    s3, bucket = s3_versioned
    (tmp_path / "data").mkdir()
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')
    (tmp_path / "data" / "empty.dvc").write_text(
        "outs:\n  - path: data/empty\n    files: []\n"
    )

    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=tmp_path, targets=["data/empty"], remote_name="origin"
    )
    assert result.success is True
    assert result.synced_count == 1
    # Empty-array manifest md5 lands at the canonical .dir cache path.
    import hashlib as _h
    expected_md5 = _h.md5(b"[]", usedforsecurity=False).hexdigest()
    assert (
        tmp_path / ".dvc" / "cache" / "files" / "md5"
        / expected_md5[:2] / f"{expected_md5[2:]}.dir"
    ).exists()


def test_try_fast_pull_md5_dir_local_manifest_no_refetch(s3_versioned, tmp_path: Path) -> None:
    """Round-2 P1 regression: a locally-cached good manifest must not be
    re-fetched (the prior draft would re-download AND unlink the existing
    local manifest when only the constituents were partially cached)."""
    s3, bucket = s3_versioned
    (tmp_path / "data").mkdir()
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')

    constituent_md5 = "9a0364b9e99bb480dd25e1f0284c8555"  # md5 of b"content"
    entries = [DvcFileEntry(constituent_md5, "a.csv"), DvcFileEntry(constituent_md5, "b.csv")]

    # Compute the manifest's raw_md5 and pre-cache it locally so the dispatch
    # sees a valid local manifest (don't put it in S3 — if try_fast_pull
    # incorrectly refetches, the S3 GET would 404 and the dir falls back).
    # The manifest lives at ...XX/YYYY.dir locally (DVC layout).
    manifest_bytes = _manifest_bytes(entries)
    manifest_raw_md5 = hashlib.md5(manifest_bytes, usedforsecurity=False).hexdigest()
    cache_dir = tmp_path / ".dvc" / "cache"
    manifest_path = (
        cache_dir / "files" / "md5"
        / manifest_raw_md5[:2] / f"{manifest_raw_md5[2:]}.dir"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)

    # Pre-cache the constituent so no downloads are needed.
    constituent_path = cache_dir / "files" / "md5" / constituent_md5[:2] / constituent_md5[2:]
    constituent_path.parent.mkdir(parents=True, exist_ok=True)
    constituent_path.write_bytes(b"content")

    (tmp_path / "data" / "dir.dvc").write_text(
        f"outs:\n  - path: data/dir\n    md5: {manifest_raw_md5}.dir\n"
    )

    # Patch `_create_s3_client` so try_fast_pull uses the fixture's recorded
    # client — otherwise it builds its own and the mock never fires.
    with patch("mintd._fast_sync_ops._create_s3_client", return_value=s3), \
         patch.object(s3, "download_file", wraps=s3.download_file) as mock:
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["data/dir"], remote_name="origin"
        )
    assert result.success is True
    assert result.synced_count == 1
    assert result.fallback_targets == []
    # No S3 GETs — both manifest and constituent are pre-cached.
    assert mock.call_count == 0


def test_fetch_dir_contents_dedups_by_md5(s3_versioned, tmp_path: Path) -> None:
    """Round-1/Round-2 P1 regression: entries sharing an md5 must be
    deduped before submission to the thread pool. ``fetch_to_cache`` uses
    a static ``.tmp`` sibling per cache_path, so two threads downloading
    the same md5 would race on the same temp file → md5 verify fails → dir
    falls back unnecessarily. Five entries, two distinct md5s → exactly
    two ``download_file`` calls."""
    s3, bucket = s3_versioned
    a_md5 = "9a0364b9e99bb480dd25e1f0284c8555"  # md5 of b"content"
    b_md5 = "ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73"[:32]
    # Use a real second md5 we can compute.
    other = b"different"
    import hashlib as _h
    b_md5 = _h.md5(other, usedforsecurity=False).hexdigest()
    s3.put_object(Bucket=bucket, Key=s3_key_for("", a_md5), Body=b"content")
    s3.put_object(Bucket=bucket, Key=s3_key_for("", b_md5), Body=other)
    entries = [
        DvcFileEntry(a_md5, "a1"),
        DvcFileEntry(a_md5, "a2"),
        DvcFileEntry(b_md5, "b1"),
        DvcFileEntry(a_md5, "a3"),
        DvcFileEntry(b_md5, "b2"),
    ]
    cache_dir = tmp_path / ".dvc" / "cache"
    with patch.object(s3, "download_file", wraps=s3.download_file) as mock:
        failures = fetch_dir_contents(s3, bucket, "", entries, cache_dir, jobs=8)
    assert failures == []
    assert mock.call_count == 2
    # Both md5s landed in cache despite five entries.
    assert (cache_dir / "files" / "md5" / a_md5[:2] / a_md5[2:]).exists()
    assert (cache_dir / "files" / "md5" / b_md5[:2] / b_md5[2:]).exists()


def test_fetch_files_dir_contents_dedups_by_md5(s3_versioned, tmp_path: Path) -> None:
    """Sibling of ``test_fetch_dir_contents_dedups_by_md5`` for the
    files-format path (real-path keys, per-file version_ids)."""
    s3, bucket = s3_versioned
    import hashlib as _h
    a_md5 = _h.md5(b"content", usedforsecurity=False).hexdigest()
    b_md5 = _h.md5(b"different", usedforsecurity=False).hexdigest()
    # Dedup picks first-occurrence per md5; only those real-path keys are GET'd.
    s3.put_object(Bucket=bucket, Key="data/d/a1", Body=b"content")
    s3.put_object(Bucket=bucket, Key="data/d/b1", Body=b"different")
    out = DvcOut(
        target="data/d", path="data/d", md5="", is_dir=False,
        is_files_format=True,
        files=[
            DvcFileEntry(a_md5, "a1"),
            DvcFileEntry(a_md5, "a2"),
            DvcFileEntry(b_md5, "b1"),
            DvcFileEntry(a_md5, "a3"),
            DvcFileEntry(b_md5, "b2"),
        ],
    )
    cache_dir = tmp_path / ".dvc" / "cache"
    with patch.object(s3, "download_file", wraps=s3.download_file) as mock:
        failures = fetch_files_dir_contents(s3, bucket, "", out, cache_dir, 8, "origin")
    assert failures == []
    assert mock.call_count == 2


# ---------- slice 26: discover_all_outs ---------------------------------


def test_discover_all_outs_walks_repo(tmp_path: Path) -> None:
    """Recursive walk emits all .dvc files relative to project_path,
    sorted lexicographically."""
    (tmp_path / "a.dvc").write_text("outs:\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.dvc").write_text("outs:\n", encoding="utf-8")
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "c.dvc").write_text("outs:\n", encoding="utf-8")

    assert discover_all_outs(tmp_path) == ["a.dvc", "sub/b.dvc", "sub/deep/c.dvc"]


def test_discover_all_outs_excludes_dvc_internals_and_lock(tmp_path: Path) -> None:
    """``.dvc/`` directory (DVC internals) and the top-level ``dvc.lock``
    pipeline file are not data pointers; both must be excluded."""
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "lock").write_text("internals", encoding="utf-8")
    (tmp_path / ".dvc" / "foo.dvc").write_text("internals", encoding="utf-8")
    (tmp_path / "dvc.lock").write_text("pipeline lock", encoding="utf-8")
    (tmp_path / "something.dvc").write_text("outs:\n", encoding="utf-8")

    assert discover_all_outs(tmp_path) == ["something.dvc"]


def test_discover_all_outs_handles_empty_repo(tmp_path: Path) -> None:
    assert discover_all_outs(tmp_path) == []
