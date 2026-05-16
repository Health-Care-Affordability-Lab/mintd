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
    fetch_to_cache,
    get_remote_config,
    is_cached,
    parse_dvc_outs,
    parse_s3_url,
    spot_check_versions,
)


# ---------- helpers ----------

def _md5_of(b: bytes) -> str:
    return hashlib.md5(b, usedforsecurity=False).hexdigest()


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


def test_classify_targets_routes_dir_entries_to_fallback(tmp_path: Path) -> None:
    _write_dvc_file_md5(tmp_path, "data/dir", "deadbeef.dir")
    all_outs, fallback, _ = classify_targets(tmp_path, ["data/dir"], "origin")
    assert all_outs == []
    assert fallback == ["data/dir"]


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


