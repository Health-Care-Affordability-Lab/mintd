"""Tests for `mintd._fast_sync_ops` — slice 18 single-file fast-sync.

Coverage: parsing, cache layout, S3 config, spot check, download primitive,
orchestrator branching, and the two reviewer-P0 regressions (original-target
preservation through fallback; boto3-missing degradation).

moto[s3] 5.x exposes the unified `mock_aws` decorator. Use `from moto import
mock_aws` (legacy `mock_s3` is removed).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from mintd._fast_sync_ops import (
    SubprocessFastSyncOps,
    _check_dvc,
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
    normalize_target,
    parse_dvc_lock_outs,
    parse_dvc_outs,
    partition_pipeline_outs,
    parse_s3_url,
    s3_key_for,
    s3_key_for_out,
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


def _write_dvc_file_md5(
    tmp_path: Path, name: str, md5: str, version_id: str | None = None,
    *, size: int = 0,
) -> None:
    body = f"outs:\n  - path: {name}\n    md5: {md5}\n    size: {size}\n"
    if version_id:
        body += f"    version_id: {version_id}\n"
    (tmp_path / f"{name}.dvc").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{name}.dvc").write_text(body)


def _write_dvc_file_import(
    tmp_path: Path, name: str, md5_dir: str,
    *, source_url: str = "https://example.com/source.git",
    rev_lock: str = "deadbeefcafebabe",
) -> Path:
    """Write a .dvc file matching the shape `dvc import` produces:
    ``frozen: true`` + ``deps[].repo.{url, rev_lock}`` + dir-suffixed md5.
    Mirrors the lab's `data/imports/<product>/final.dvc` shape (cms-ipps).
    """
    body = (
        "frozen: true\n"
        "deps:\n"
        f"  - path: {Path(name).name}\n"
        "    repo:\n"
        f"      url: {source_url}\n"
        f"      rev_lock: {rev_lock}\n"
        "outs:\n"
        f"  - path: {Path(name).name}\n"
        f"    md5: {md5_dir}\n"
        "    hash: md5\n"
    )
    p = tmp_path / f"{name}.dvc"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


def _write_dvc_file_path_based(
    tmp_path: Path, name: str, md5: str, version_id: str, *, remote: str = "origin",
) -> Path:
    """Write a path-based (version_aware) .dvc file: version_id lives
    ONLY under cloud[<remote>], NOT at the top level. Mirrors the lab's
    actual catalog shape.
    """
    body = (
        f"outs:\n"
        f"  - path: {Path(name).name}\n"
        f"    md5: {md5}\n"
        f"    size: 0\n"
        f"    cloud:\n"
        f"      {remote}:\n"
        f"        version_id: {version_id}\n"
    )
    p = tmp_path / f"{name}.dvc"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


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


# ---------- slice 29: dvc-import detection ---------------------------

def test_parse_dvc_outs_detects_dvc_import_via_deps_repo(tmp_path: Path) -> None:
    """Real lab shape (cms-ipps `data/imports/cms-impactfiles/final.dvc`):
    frozen + deps[].repo + dir-suffixed md5. Detection is at the file
    level — every out parsed from the file inherits is_import=True."""
    p = _write_dvc_file_import(tmp_path, "imported", "deadbeef.dir")
    outs = parse_dvc_outs(p, "origin")
    assert len(outs) == 1
    assert outs[0].is_import is True
    # `.dir` suffix still classifies as a dir at parse time; classify_targets
    # short-circuits BEFORE the orchestrator's dir branch would run.
    assert outs[0].is_dir is True


def test_parse_dvc_outs_frozen_without_repo_is_not_import(tmp_path: Path) -> None:
    """Pins the discriminator: `frozen: true` alone is NOT an import.
    Frozen pipeline stages have their data in this repo's bucket and
    must still go through fast-sync."""
    f = tmp_path / "frozen_stage.dvc"
    f.write_text(
        "frozen: true\n"
        "deps:\n"
        "  - path: input.csv\n"
        "outs:\n"
        "  - path: output.csv\n"
        "    md5: cafe\n"
    )
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].is_import is False


def test_parse_dvc_outs_deps_without_repo_is_not_import(tmp_path: Path) -> None:
    """Pipeline stages with regular deps (no repo:) are not imports."""
    f = tmp_path / "stage.dvc"
    f.write_text(
        "deps:\n"
        "  - path: input.csv\n"
        "  - path: script.py\n"
        "outs:\n"
        "  - path: out.csv\n"
        "    md5: face\n"
    )
    outs = parse_dvc_outs(f, "origin")
    assert outs[0].is_import is False


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


def test_parse_s3_url_strips_trailing_slash() -> None:
    """Lab's .dvc/config writes URLs with a trailing slash
    (``s3://bucket/lab/data_x/``). Without stripping, every downstream
    key gets a double slash and S3 returns 404 — surfaced during
    slice-27 path-based smoke against cms-ipps-reimbursement."""
    assert parse_s3_url("s3://b/lab/data_x/") == ("b", "lab/data_x")
    assert parse_s3_url("s3://b/") == ("b", "")


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


# ---------- parse_remote_config_text (text-level parser) ----------

def test_parse_remote_config_text_quoted_section() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = "['remote \"storage\"']\n    url = s3://b/p\n"
    assert parse_remote_config_text(text, "storage")["url"] == "s3://b/p"


def test_parse_remote_config_text_double_quoted_section() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = '[remote "storage"]\n    url = s3://b/p\n'
    assert parse_remote_config_text(text, "storage")["url"] == "s3://b/p"


def test_parse_remote_config_text_unquoted_section() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = "[remote storage]\n    url = s3://b/p\n"
    assert parse_remote_config_text(text, "storage")["url"] == "s3://b/p"


def test_parse_remote_config_text_default_from_core() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = (
        "[core]\n    remote = storage\n"
        "['remote \"storage\"']\n    url = s3://b/p\n"
        "['remote \"other\"']\n    url = s3://b/q\n"
    )
    assert parse_remote_config_text(text, None)["url"] == "s3://b/p"


def test_parse_remote_config_text_default_single_remote() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = "['remote \"only\"']\n    url = s3://b/p\n"
    assert parse_remote_config_text(text, None)["url"] == "s3://b/p"


def test_parse_remote_config_text_default_ambiguous_raises() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = (
        "['remote \"a\"']\n    url = s3://b/p\n"
        "['remote \"b\"']\n    url = s3://b/q\n"
    )
    with pytest.raises(KeyError):
        parse_remote_config_text(text, None)


def test_parse_remote_config_text_named_absent_raises() -> None:
    from mintd._fast_sync_ops import parse_remote_config_text
    text = "['remote \"storage\"']\n    url = s3://b/p\n"
    with pytest.raises(KeyError):
        parse_remote_config_text(text, "nope")


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
    # Slice B: returns the list of VERIFIED-drift (target, reason) pairs —
    # empty means no drift (was: bool True).
    assert spot_check_versions(s3, bucket, "", [out], tmp_path) == []


def test_spot_check_versions_drift_names_affected_out(s3_versioned, tmp_path: Path) -> None:
    """Slice B: a verified 404 on the pinned version reports (target, reason)
    for THAT out only (was: bool False, which the orchestrator turned into
    demote-everything)."""
    from mintd._fast_sync_ops import DvcOut

    s3, bucket = s3_versioned
    body = b"v1"
    md5 = _md5_of(body)
    s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
    out = DvcOut(target="a", path="a", md5=md5, is_dir=False, version_id="bogus-version")
    drift = spot_check_versions(s3, bucket, "", [out], tmp_path)
    assert [t for t, _ in drift] == ["a"]
    assert "404" in drift[0][1]


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


# ---------- slice 28: byte-granular progress ----------

def test_fetch_to_cache_threads_progress_callback_to_boto3(
    s3_versioned, tmp_path: Path
) -> None:
    """``progress`` MUST be passed through as boto3's ``Callback=``."""
    s3, bucket = s3_versioned
    body = b"payload"
    md5 = _md5_of(body)
    key = f"files/md5/{md5[:2]}/{md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)

    def advance(_n: int) -> None:
        pass

    with patch.object(s3, "download_file", wraps=s3.download_file) as mock_dl:
        assert fetch_to_cache(s3, bucket, key, cp, md5, progress=advance) is True
    assert mock_dl.call_args.kwargs.get("Callback") is advance


def test_fetch_to_cache_omits_callback_when_progress_none(
    s3_versioned, tmp_path: Path
) -> None:
    """``Callback=None`` would crash boto3 (it calls ``Callback(bytes)``).
    The kwarg must be ABSENT, not None, when progress is None or unset."""
    s3, bucket = s3_versioned
    body = b"payload"
    md5 = _md5_of(body)
    key = f"files/md5/{md5[:2]}/{md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    with patch.object(s3, "download_file", wraps=s3.download_file) as mock_dl:
        assert fetch_to_cache(s3, bucket, key, cp, md5) is True
    assert "Callback" not in mock_dl.call_args.kwargs
    # And the explicit-None form also must not pass Callback.
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    cp2 = cache_path_for(tmp_path / ".dvc" / "cache2", md5)
    with patch.object(s3, "download_file", wraps=s3.download_file) as mock_dl2:
        assert fetch_to_cache(s3, bucket, key, cp2, md5, progress=None) is True
    assert "Callback" not in mock_dl2.call_args.kwargs


def test_fetch_dir_contents_progress_per_entry(
    s3_versioned, tmp_path: Path
) -> None:
    """Per-entry advance: each entry's bytes flow through ``progress``;
    callback fires from worker threads → use a Lock."""
    import threading
    from mintd._fast_sync_ops import fetch_dir_contents as _fetch_dir

    s3, bucket = s3_versioned
    bodies = [b"a" * 5, b"b" * 7, b"c" * 11]
    entries = []
    for body in bodies:
        md5 = _md5_of(body)
        s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
        entries.append(DvcFileEntry(md5=md5, relpath=f"f_{md5[:4]}", size=len(body)))

    lock = threading.Lock()
    calls: list[int] = []
    def advance(n: int) -> None:
        with lock:
            calls.append(n)

    cache_dir = tmp_path / ".dvc" / "cache"
    failures = _fetch_dir(s3, bucket, "", entries, cache_dir, 4, progress=advance)
    assert failures == []
    assert sum(calls) == 5 + 7 + 11
    # Each tiny body produces at least one Callback fire → ≥ 3 calls.
    assert len(calls) >= 3


def test_fetch_dir_contents_progress_counts_duplicate_entries(
    s3_versioned, tmp_path: Path
) -> None:
    """Reviewer P1 regression: dedup-by-md5 drops duplicate entries
    from the download set, but the progress bar's total is sum(out.size)
    which counts every relpath (including duplicates). Without firing
    advance for the dedup-dropped entries, dirs with duplicate-content
    files would undershoot the bar."""
    import threading
    from mintd._fast_sync_ops import fetch_dir_contents as _fetch_dir

    s3, bucket = s3_versioned
    body = b"shared content"
    md5 = _md5_of(body)
    s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
    # Three entries with the SAME md5 (3 relpaths sharing one content).
    entries = [
        DvcFileEntry(md5=md5, relpath=f"copy_{i}", size=len(body))
        for i in range(3)
    ]
    lock = threading.Lock()
    calls: list[int] = []
    def advance(n: int) -> None:
        with lock:
            calls.append(n)

    cache_dir = tmp_path / ".dvc" / "cache"
    failures = _fetch_dir(s3, bucket, "", entries, cache_dir, 4, progress=advance)
    assert failures == []
    # Bar should advance by the full aggregate (3 × len(body)), not just
    # the one actually-downloaded copy.
    assert sum(calls) == 3 * len(body)


def test_try_fast_pull_updates_per_out_description(
    s3_versioned, tmp_path: Path,
) -> None:
    """Pattern D: reporter.update_progress_desc fires once per output in the
    per-out loop, with `(i/n)` suffixes in order. Lets the user see which
    file is downloading during the multi-minute fetch instead of a static
    spinner."""
    s3, bucket = s3_versioned
    bodies = [b"a" * 1024, b"b" * 2048, b"c" * 4096]
    md5s = [_md5_of(b) for b in bodies]
    for body, md5 in zip(bodies, md5s):
        key = f"files/md5/{md5[:2]}/{md5[2:]}"
        s3.put_object(Bucket=bucket, Key=key, Body=body)

    _write_dvc_config(tmp_path, bucket)
    for name, body, md5 in zip(["a", "b", "c"], bodies, md5s):
        _write_dvc_file_md5(tmp_path, name, md5, size=len(body))

    class _RecordingReporter:
        def __init__(self) -> None:
            self.labels: list[str] = []

        def update_progress_desc(self, msg: str) -> None:
            self.labels.append(msg)

    rep = _RecordingReporter()
    ops = SubprocessFastSyncOps()
    with patch("mintd._fast_sync_ops._create_s3_client", return_value=s3):
        result = ops.try_fast_pull(
            project_path=tmp_path, targets=["a", "b", "c"],
            remote_name="origin", reporter=rep,  # type: ignore[arg-type]
        )

    assert result.success is True
    assert len(rep.labels) == 3
    assert "(1/3)" in rep.labels[0]
    assert "(2/3)" in rep.labels[1]
    assert "(3/3)" in rep.labels[2]


def test_try_fast_pull_advance_fires_per_chunk_during_download(
    s3_versioned, tmp_path: Path
) -> None:
    """Single-file network branch: advance fires multiple times (boto3's
    transfer manager invokes Callback per chunk) and the sum equals the
    out's size — proving the bar is byte-granular, not per-out lump."""
    s3, bucket = s3_versioned
    # 16 MB body → at least 2 chunks at boto3's 8 MB default.
    body = b"x" * (16 * 1024 * 1024)
    md5 = _md5_of(body)
    key = f"files/md5/{md5[:2]}/{md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=body)

    _write_dvc_config(tmp_path, bucket)
    _write_dvc_file_md5(tmp_path, "big", md5, size=len(body))

    calls: list[int] = []
    ops = SubprocessFastSyncOps()
    ops.set_progress(calls.append)

    with patch("mintd._fast_sync_ops._create_s3_client", return_value=s3):
        result = ops.try_fast_pull(
            project_path=tmp_path, targets=["big"], remote_name="origin"
        )
    assert result.success is True
    assert sum(calls) == len(body)
    # Per-chunk: more than one Callback fire on a 16 MB body.
    assert len(calls) > 1


def test_try_fast_pull_advance_fires_once_on_single_file_cache_hit(
    s3_versioned, tmp_path: Path
) -> None:
    """Cache-hit fast-path must explicitly fire ``advance(out.size)`` —
    boto3 isn't called, so without the explicit fire the bar would stall
    on warm-cache re-runs."""
    s3, bucket = s3_versioned
    body = b"cached body"
    md5 = _md5_of(body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_bytes(body)  # pre-populate cache → is_cached returns True

    _write_dvc_config(tmp_path, bucket)
    _write_dvc_file_md5(tmp_path, "warm", md5, size=len(body))

    calls: list[int] = []
    ops = SubprocessFastSyncOps()
    ops.set_progress(calls.append)

    with patch("mintd._fast_sync_ops._create_s3_client", return_value=s3), \
         patch.object(s3, "download_file", wraps=s3.download_file) as mock_dl:
        result = ops.try_fast_pull(
            project_path=tmp_path, targets=["warm"], remote_name="origin"
        )
    assert result.success is True
    assert mock_dl.call_count == 0  # no network
    # Exactly one advance — the explicit cache-hit fire — with body size.
    assert calls == [len(body)]


# ---------- DVC version canary (2) ----------

@pytest.mark.parametrize(
    "returncode, stdout, stderr, exception, expected_ok, expected_reason",
    [
        (0, "3.66.1\n", "", None, True, None),
        (0, "3.99.9\n", "", None, True, None),
        (0, "4.0.0\n", "", None, False, "dvc 4.0 above ceiling 4.0"),
        (0, "3.65.9\n", "", None, False, "dvc 3.65 below floor 3.66"),
        (0, "invalid\n", "", None, False, "dvc version unparseable: 'invalid'"),
        (1, "", "", None, False, "dvc version probe failed (exit 1)"),
        # `sys.executable -m dvc` exits 1 + ModuleNotFoundError when dvc is
        # absent from mintd's env — we re-emit the honest reason instead of
        # the opaque "probe failed (exit 1)".
        (1, "", "ModuleNotFoundError: No module named 'dvc'\n", None, False, "dvc not installed"),
        (1, "", "/usr/bin/python: No module named dvc\n", None, False, "dvc not installed"),
        (0, "", "", FileNotFoundError(), False, "dvc not installed"),
        (0, "", "", subprocess.TimeoutExpired(cmd=["dvc", "--version"], timeout=5), False, "dvc version probe timed out"),
    ],
)
def test_check_dvc(returncode: int, stdout: str, stderr: str, exception: Exception | None, expected_ok: bool, expected_reason: str | None) -> None:
    with patch("mintd._fast_sync_ops.subprocess.run") as run:
        if exception:
            run.side_effect = exception
        else:
            run.return_value.returncode = returncode
            run.return_value.stdout = stdout
            run.return_value.stderr = stderr

        ok, reason = _check_dvc()
        assert ok is expected_ok
        assert reason == expected_reason


# ---------- normalize_target ----------

@pytest.mark.parametrize("raw,expected", [
    ("./outputs/main.parquet", "outputs/main.parquet"),
    ("outputs/main.parquet/", "outputs/main.parquet"),
    (".\\outputs\\main.parquet", "outputs/main.parquet"),  # backslashes
    (".\\outputs\\main.parquet\\", "outputs/main.parquet"),  # combined
    ("outputs/main.parquet", "outputs/main.parquet"),  # idempotent
])
def test_normalize_target(raw: str, expected: str) -> None:
    assert normalize_target(raw) == expected


def test_classify_targets_normalizes_backslash_target(tmp_path: Path) -> None:
    """A backslash/'./'-prefixed target still finds the on-disk .dvc file,
    while out.target preserves the caller's original string."""
    _write_dvc_file_md5(tmp_path, "data/foo.csv", "cafe")
    all_outs, fallback, missing = classify_targets(
        tmp_path, ["./data\\foo.csv"], "origin"
    )
    assert len(all_outs) == 1
    assert all_outs[0].path == "data/foo.csv"
    assert all_outs[0].target == "./data\\foo.csv"  # original preserved
    assert fallback == []


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


def test_classify_targets_targeted_bare_stage_out_routes_to_scoped_fallback(
    tmp_path: Path,
) -> None:
    """A targeted pull of a bare dvc.lock stage-out path (no per-output
    .dvc file) has no `<path>.dvc` for classify_targets to parse, so it
    routes to `fallback` (a scoped `dvc pull <path>`), NOT fast-sync and
    NOT `all_outs`. Pipeline stage outs only fast-sync on pull-all, where
    partition_pipeline_outs enumerates them from dvc.lock; by name they
    fall back. This pins the sane-but-suboptimal behavior so a refactor
    can't turn 'targeted stage out -> loud scoped fallback' into a silent
    drop. See the data_ops.py pull_all_requested comment."""
    # A dvc.lock stage out exists, but there is no data/staged.dvc pointer.
    (tmp_path / "data").mkdir()
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  build:\n    cmd: make\n    outs:\n"
        "      - path: data/staged\n        hash: md5\n        md5: cafe\n"
        "        size: 100\n"
    )
    all_outs, fallback, missing = classify_targets(tmp_path, ["data/staged"], "origin")
    assert all_outs == []          # not fast-synced
    assert fallback == ["data/staged"]  # scoped dvc pull, never targets=None
    assert missing == []


def test_classify_targets_routes_imports_to_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 29 contract: dvc-import .dvc files (deps[].repo) route to
    fallback BEFORE any S3 client is constructed. Surfaces as one INFO
    log per import. The user-supplied target string is preserved
    verbatim (slice-18 P0 invariant)."""
    import logging
    _write_dvc_file_md5(tmp_path, "data/regular", "cafe")
    _write_dvc_file_import(tmp_path, "data/imported", "deadbeef.dir")
    with caplog.at_level(logging.INFO, logger="mintd._fast_sync_ops"):
        all_outs, fallback, missing = classify_targets(
            tmp_path, ["data/regular", "data/imported"], "origin",
        )
    assert [o.target for o in all_outs] == ["data/regular"]
    assert fallback == ["data/imported"]
    assert missing == []
    skip_records = [
        r for r in caplog.records
        if "skipping" in r.message and "dvc-import" in r.message
    ]
    assert len(skip_records) == 1
    assert "data/imported" in skip_records[0].message


# ---------- orchestrator (6) ----------

def test_try_fast_pull_falls_back_when_version_mismatch(tmp_path: Path) -> None:
    with patch("mintd._fast_sync_ops._check_dvc", return_value=(False, "dvc version mismatch")):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.success is False
    assert result.fallback_targets == ["a"]
    assert "version" in result.reason.lower()


def test_try_fast_pull_falls_back_when_no_config(tmp_path: Path) -> None:
    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
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
        with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
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
        with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
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
        with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
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
    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.fallback_targets == ["a"]
    assert "non-s3" in result.reason.lower()


def test_try_fast_pull_spot_check_drift_errors_loudly_not_fallback(tmp_path: Path) -> None:
    """Orchestrator-gate test: verified version_id drift on an out. A drifted
    out is by definition version-aware (only outs with a version_id are
    probed), so under the fix-4 contract it lands in blocked_targets — plain
    `dvc pull` is documented broken on version-aware outs and is never
    attempted for them. (Pre-slice-C this test expected fallback_targets.)"""
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
        with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path, targets=["data/a"], remote_name="origin"
            )
    assert result.success is False
    assert result.fallback_targets == []
    assert result.blocked_targets == ["data/a"]
    assert "drift" in result.blocked_reasons["data/a"].lower()
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
         patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
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
        fetch_files_dir_contents(s3, bucket, "", out, tmp_path / "cache", 1, "origin", project_path=tmp_path)
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


def test_ensure_dir_manifest_survives_fsync_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces the Windows failure mode: os.fsync on the fresh tmp file
    raises OSError [Errno 9] Bad file descriptor. The manifest write is
    durable-best-effort, so it must complete and produce the correct file
    rather than crash the cache probe (was: OSError propagated out of
    _split_cached during data_pull on Windows)."""
    import mintd._atomic as _atomic

    monkeypatch.setattr(
        _atomic.os, "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(9, "Bad file descriptor")),
    )
    entries = [DvcFileEntry("b", "b.csv"), DvcFileEntry("a", "a.csv")]
    manifest_name = ensure_dir_manifest(tmp_path, entries)  # must not raise
    expected_bytes = _manifest_bytes(entries)
    expected_md5 = hashlib.md5(expected_bytes, usedforsecurity=False).hexdigest()
    assert manifest_name == f"{expected_md5}.dir"
    manifest_path = tmp_path / "files" / "md5" / expected_md5[:2] / f"{expected_md5[2:]}.dir"
    assert manifest_path.read_bytes() == expected_bytes


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
        failures = fetch_files_dir_contents(s3, bucket, "", out, cache_dir, 8, "origin", project_path=tmp_path)
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


# ---------- slice 27: path-based (version_aware) mode ------------------


def test_parse_dvc_outs_detects_path_based_from_cloud_nested_version_id(tmp_path: Path) -> None:
    """Lab's actual .dvc shape: version_id ONLY under cloud[remote]; no top-level."""
    dvc_path = _write_dvc_file_path_based(tmp_path, "data/raw/foo", "abc", "vid-1")
    outs = parse_dvc_outs(dvc_path, "origin")
    assert len(outs) == 1
    assert outs[0].is_path_based is True
    assert outs[0].dvc_file == dvc_path
    assert outs[0].version_id == "vid-1"


def test_parse_dvc_outs_md5_keyed_when_top_level_version_id(tmp_path: Path) -> None:
    """Top-level version_id → content-addressable (md5-keyed) mode."""
    _write_dvc_file_md5(tmp_path, "a", "abc", version_id="vid-top")
    outs = parse_dvc_outs(tmp_path / "a.dvc", "origin")
    assert outs[0].is_path_based is False


def test_parse_dvc_outs_mixed_top_and_cloud_prefers_top(tmp_path: Path) -> None:
    """Top-level wins (v1 contract); entry treated as md5-keyed."""
    body = (
        "outs:\n"
        "  - path: x\n"
        "    md5: abc\n"
        "    size: 0\n"
        "    version_id: vid-top\n"
        "    cloud:\n"
        "      origin:\n"
        "        version_id: vid-cloud\n"
    )
    (tmp_path / "x.dvc").write_text(body)
    outs = parse_dvc_outs(tmp_path / "x.dvc", "origin")
    assert outs[0].is_path_based is False
    assert outs[0].version_id == "vid-top"


def test_parse_dvc_outs_files_format_per_entry_path_based_flag(tmp_path: Path) -> None:
    """Each file entry's path_based flag computed independently."""
    body = (
        "outs:\n"
        "  - path: dir\n"
        "    files:\n"
        "      - md5: aa\n"
        "        relpath: a\n"
        "        cloud:\n"
        "          origin:\n"
        "            version_id: vid-a\n"
        "      - md5: bb\n"
        "        relpath: b\n"
        "        version_id: vid-b-top\n"  # top-level → md5-keyed at entry level
    )
    (tmp_path / "dir.dvc").write_text(body)
    outs = parse_dvc_outs(tmp_path / "dir.dvc", "origin")
    assert outs[0].is_files_format is True
    assert outs[0].files is not None
    files = outs[0].files
    assert files[0].is_path_based is True
    assert files[1].is_path_based is False


def test_s3_key_for_out_path_based_root_level(tmp_path: Path) -> None:
    """Project-root .dvc file → key is <prefix>/<out.path> (no nested dir)."""
    out = DvcOut(
        target="x", path="foo.parquet", md5="abc", is_dir=False,
        version_id="vid", is_path_based=True, dvc_file=tmp_path / "foo.parquet.dvc",
    )
    assert s3_key_for_out("lab/data_x", out, tmp_path) == "lab/data_x/foo.parquet"


def test_s3_key_for_out_path_based_nested_dvc_file(tmp_path: Path) -> None:
    """Nested .dvc → key includes the parent dir relative to project."""
    out = DvcOut(
        target="x", path="foo.parquet", md5="abc", is_dir=False,
        version_id="vid", is_path_based=True,
        dvc_file=tmp_path / "data/raw/foo.parquet.dvc",
    )
    assert s3_key_for_out("lab/data_x", out, tmp_path) == "lab/data_x/data/raw/foo.parquet"


def test_s3_key_for_out_path_based_empty_prefix(tmp_path: Path) -> None:
    out = DvcOut(
        target="x", path="foo.parquet", md5="abc", is_dir=False,
        version_id="vid", is_path_based=True, dvc_file=tmp_path / "foo.parquet.dvc",
    )
    assert s3_key_for_out("", out, tmp_path) == "foo.parquet"


def test_s3_key_for_out_md5_keyed_falls_back_to_files_md5(tmp_path: Path) -> None:
    """Non-path-based out preserves the pre-slice-27 content-addressable key."""
    out = DvcOut(target="x", path="x", md5="abcdef", is_dir=False)
    assert s3_key_for_out("lab/data_x", out, tmp_path) == "lab/data_x/files/md5/ab/cdef"


def test_s3_key_for_out_raises_when_path_based_missing_dvc_file(tmp_path: Path) -> None:
    out = DvcOut(
        target="x", path="foo", md5="abc", is_dir=False,
        version_id="vid", is_path_based=True, dvc_file=None,
    )
    with pytest.raises(ValueError, match="missing dvc_file"):
        s3_key_for_out("lab", out, tmp_path)


def test_spot_check_versions_uses_path_for_path_based(s3_versioned, tmp_path: Path) -> None:
    """spot_check HEADs the path-based key, not files/md5/..."""
    s3, bucket = s3_versioned
    body = b"hello"
    md5 = _md5_of(body)
    resp = s3.put_object(Bucket=bucket, Key="lab/data/foo.parquet", Body=body)
    version_id = resp["VersionId"]
    out = DvcOut(
        target="x", path="foo.parquet", md5=md5, is_dir=False,
        version_id=version_id, is_path_based=True,
        dvc_file=tmp_path / "data/foo.parquet.dvc",
    )
    # Slice B: no-drift is now the empty drift list, not bool True.
    assert spot_check_versions(s3, bucket, "lab", [out], tmp_path) == []


def test_fetch_files_dir_contents_branches_on_entry_is_path_based(
    s3_versioned, tmp_path: Path
) -> None:
    """A single files-format dir with mixed-mode entries routes each to
    its own key shape: path-based → <prefix>/<dvc_dir>/<out.path>/<rel>;
    non-path-based → <prefix>/<out.path>/<rel>."""
    import hashlib as _h
    from mintd._fast_sync_ops import fetch_files_dir_contents

    s3, bucket = s3_versioned
    a_body = b"path-based entry"
    b_body = b"md5-keyed entry"
    a_md5 = _h.md5(a_body, usedforsecurity=False).hexdigest()
    b_md5 = _h.md5(b_body, usedforsecurity=False).hexdigest()
    # Path-based entry lives at the .dvc's nested dir
    s3.put_object(Bucket=bucket, Key="data/raw/d/a", Body=a_body)
    # Non-path-based entry lives at the prefix/out.path layout (preserved)
    s3.put_object(Bucket=bucket, Key="d/b", Body=b_body)

    out = DvcOut(
        target="data/raw/d", path="d", md5="", is_dir=True,
        is_files_format=True,
        dvc_file=tmp_path / "data/raw/d.dvc",
        files=[
            DvcFileEntry(md5=a_md5, relpath="a", is_path_based=True),
            DvcFileEntry(md5=b_md5, relpath="b", is_path_based=False),
        ],
    )
    cache_dir = tmp_path / ".dvc" / "cache"
    failures = fetch_files_dir_contents(
        s3, bucket, "", out, cache_dir, 4, "origin", project_path=tmp_path,
    )
    assert failures == []
    # Both files cached at md5 paths
    assert (cache_dir / "files" / "md5" / a_md5[:2] / a_md5[2:]).exists()
    assert (cache_dir / "files" / "md5" / b_md5[:2] / b_md5[2:]).exists()


def test_try_fast_pull_path_based_files_format_dir_nested(
    s3_versioned, tmp_path: Path
) -> None:
    """End-to-end integration smoke for slice 27.

    Layout mirrors the lab's real catalog: a files-format dir whose
    .dvc file lives in a nested directory, with each file entry carrying
    a path-based ``cloud.<remote>.version_id`` (no top-level md5 keying
    in S3). The S3 objects live at the literal repo path, not at
    ``files/md5/...``. try_fast_pull must:
      - detect path-based mode per-entry
      - fetch each entry from ``<prefix>/<dvc_dir>/<out.path>/<rel>``
      - still synthesize the .dir manifest at the canonical md5 cache path
      - report success
    """
    s3, bucket = s3_versioned
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\nurl = s3://{bucket}\n')

    a_body = b"alpha"
    b_body = b"beta-beta"
    a_md5 = _md5_of(a_body)
    b_md5 = _md5_of(b_body)

    # Upload to path-based keys (no md5 prefix, no files/md5 indirection).
    resp_a = s3.put_object(Bucket=bucket, Key="data/raw/dir/a.parquet", Body=a_body)
    resp_b = s3.put_object(Bucket=bucket, Key="data/raw/dir/b.parquet", Body=b_body)

    (tmp_path / "data" / "raw" / "dir.dvc").write_text(
        f"outs:\n"
        f"  - path: dir\n"
        f"    files:\n"
        f"      - relpath: a.parquet\n"
        f"        md5: {a_md5}\n"
        f"        size: {len(a_body)}\n"
        f"        cloud:\n"
        f"          origin:\n"
        f"            version_id: {resp_a['VersionId']}\n"
        f"      - relpath: b.parquet\n"
        f"        md5: {b_md5}\n"
        f"        size: {len(b_body)}\n"
        f"        cloud:\n"
        f"          origin:\n"
        f"            version_id: {resp_b['VersionId']}\n"
    )

    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=tmp_path, targets=["data/raw/dir"], remote_name="origin"
    )

    assert result.success is True, result.fallback_targets
    assert result.synced_count == 1

    # Constituents land in the standard md5-keyed cache layout regardless of
    # how they were fetched — DVC checkout reads from there.
    assert (tmp_path / ".dvc" / "cache" / "files" / "md5" / a_md5[:2] / a_md5[2:]).read_bytes() == a_body
    assert (tmp_path / ".dvc" / "cache" / "files" / "md5" / b_md5[:2] / b_md5[2:]).read_bytes() == b_body

    # Synthetic .dir manifest written at canonical cache path.
    expected_bytes = _manifest_bytes(
        [DvcFileEntry(a_md5, "a.parquet"), DvcFileEntry(b_md5, "b.parquet")]
    )
    expected_md5 = hashlib.md5(expected_bytes, usedforsecurity=False).hexdigest()
    assert (
        tmp_path / ".dvc" / "cache" / "files" / "md5"
        / expected_md5[:2] / f"{expected_md5[2:]}.dir"
    ).exists()

def _write_lock(tmp_path: Path, body: str, yaml_body: str | None = None) -> Path:
    """Write a minimal pipeline project (dvc.lock + optional dvc.yaml) to tmp_path."""
    (tmp_path / "dvc.lock").write_text(body, encoding="utf-8")
    if yaml_body is not None:
        (tmp_path / "dvc.yaml").write_text(yaml_body, encoding="utf-8")
    return tmp_path


def test_parse_dvc_lock_outs_basic(tmp_path: Path) -> None:
    """Single-stage lock, no wdir → one DvcOut with the cloud version_id."""
    _write_lock(
        tmp_path,
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: data/out.parquet\n"
            "        md5: abcdef1234567890abcdef1234567890\n"
            "        size: 1024\n"
            "        cloud:\n"
            "          test-bucket:\n"
            "            etag: e1\n"
            "            version_id: v1\n"
        ),
    )
    outs = parse_dvc_lock_outs(tmp_path, "test-bucket")
    assert len(outs) == 1
    o = outs[0]
    assert o.target == "data/out.parquet"
    assert o.path == "data/out.parquet"
    assert o.md5 == "abcdef1234567890abcdef1234567890"
    assert o.version_id == "v1"
    assert o.is_path_based is True
    assert o.dvc_file == tmp_path / "dvc.lock"


def test_parse_dvc_lock_outs_resolves_wdir_relative_paths(tmp_path: Path) -> None:
    """dvc.lock paths are relative to the stage's wdir from dvc.yaml."""
    _write_lock(
        tmp_path,
        yaml_body=(
            "stages:\n"
            "  build:\n"
            "    wdir: code\n"
            "    outs:\n"
            "      - path: ../data/final/foo.parquet\n"
        ),
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: ../data/final/foo.parquet\n"
            "        md5: abcdef1234567890abcdef1234567890\n"
            "        cloud:\n"
            "          x:\n"
            "            version_id: v1\n"
        ),
    )
    outs = parse_dvc_lock_outs(tmp_path, "x")
    assert len(outs) == 1
    assert outs[0].path == "data/final/foo.parquet"


def test_parse_dvc_lock_outs_skips_outs_without_cloud_section(tmp_path: Path) -> None:
    """parse_dvc_lock_outs returns BOTH outs (no filter); the fast-syncable
    partition only keeps ones with a top-level cloud.<remote>.version_id."""
    _write_lock(
        tmp_path,
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: with_cloud.parquet\n"
            "        md5: 11111111111111111111111111111111\n"
            "        cloud:\n"
            "          x:\n"
            "            version_id: v1\n"
            "      - path: no_cloud.parquet\n"
            "        md5: 22222222222222222222222222222222\n"
        ),
    )
    parsed = parse_dvc_lock_outs(tmp_path, "x")
    assert len(parsed) == 2
    discovered = partition_pipeline_outs(tmp_path, "x")[0]
    assert len(discovered) == 1
    assert discovered[0].path == "with_cloud.parquet"


def test_parse_dvc_lock_outs_missing_dvc_lock_returns_empty(tmp_path: Path) -> None:
    """No dvc.lock at all → []."""
    assert parse_dvc_lock_outs(tmp_path, "x") == []


def test_parse_dvc_lock_outs_missing_dvc_yaml_degrades_to_default_wdir(
    tmp_path: Path,
) -> None:
    """dvc.lock present, dvc.yaml absent: parser falls back to wdir='.' and
    still returns the outs (does NOT return []). Locks the spec contract that
    dvc.yaml is optional."""
    _write_lock(
        tmp_path,
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: data/final/foo.parquet\n"
            "        md5: 33333333333333333333333333333333\n"
            "        cloud:\n"
            "          x:\n"
            "            version_id: v1\n"
        ),
    )
    outs = parse_dvc_lock_outs(tmp_path, "x")
    assert len(outs) == 1
    assert outs[0].path == "data/final/foo.parquet"


def test_try_fast_pull_early_abort_routes_pipeline_outs_to_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P0-catching regression test. When try_fast_pull aborts early
    (here: _check_dvc returns False), every target and pipeline out MUST
    land in fallback_targets or blocked_targets — never be computed as
    'synced' (dvc_ops.checkout would crash on cache files that were never
    fetched). Slice C sharpens the split: the version-aware pipeline out
    errors loudly (plain `dvc pull` is documented broken on it) while the
    md5-keyed pipeline out and the unparseable .dvc target keep the
    fallback route."""
    monkeypatch.setattr("mintd._fast_sync_ops._check_dvc", lambda: (False, "dvc version mismatch"))

    pipe_out = DvcOut(
        target="data/final/foo.parquet",
        path="data/final/foo.parquet",
        md5="aabbccddeeff00112233445566778899",
        is_dir=False,
        version_id="v-pipeline",
        is_path_based=True,
        dvc_file=tmp_path / "dvc.lock",
    )
    md5_pipe_out = DvcOut(
        target="data/interim/bar.parquet",
        path="data/interim/bar.parquet",
        md5="00112233445566778899aabbccddeeff",
        is_dir=False,
        dvc_file=tmp_path / "dvc.lock",
    )
    ops = SubprocessFastSyncOps()
    result = ops.try_fast_pull(
        project_path=tmp_path,
        targets=["a.dvc"],
        remote_name="x",
        pipeline_outs=[pipe_out, md5_pipe_out],
    )

    assert result.success is False
    assert "a.dvc" in result.fallback_targets
    assert "data/interim/bar.parquet" in result.fallback_targets
    # Slice C (fix 4): version-aware → loud error, never plain dvc pull.
    assert "data/final/foo.parquet" in result.blocked_targets
    assert "data/final/foo.parquet" not in result.fallback_targets
    assert "version mismatch" in result.blocked_reasons["data/final/foo.parquet"]


def test_parse_dvc_lock_outs_against_real_pipeline_fixture(tmp_path: Path):
    import shutil
    fixture = Path("tests/fixtures/pipeline_project")
    shutil.copytree(fixture, tmp_path / "project")
    project = tmp_path / "project"

    # Verify parser against the 6-out fixture (5 files + 1 dir out)
    outs = parse_dvc_lock_outs(project, "test-bucket")
    assert len(outs) == 6
    assert len(partition_pipeline_outs(project, "test-bucket")[0]) == 6

    # All paths under data/final/
    for o in outs:
        assert o.path.startswith("data/final/")
        assert o.md5

    # 5 single-file outs are path-based + have version_ids
    single_file_outs = [o for o in outs if not o.is_files_format]
    assert len(single_file_outs) == 5
    for o in single_file_outs:
        assert o.is_path_based is True
        assert o.version_id

    # Aggregate size of single-file outs (5 files, ~200 MB total)
    single_file_size = sum(o.size for o in single_file_outs)
    assert 1.9e8 < single_file_size < 2.1e8

    # Spot check subdir/ — real-world DVC lockfile shape: dir-outs have
    # only per-file cloud blocks, NO top-level cloud block. The dir-out's
    # top-level version_id is therefore None, but per-file entries carry
    # their own version_ids — that's what fetch_files_dir_contents uses.
    subdir = next(o for o in outs if "subdir" in o.path)
    assert subdir.is_files_format is True
    assert subdir.is_dir is True
    assert subdir.files is not None
    assert len(subdir.files) == 2
    assert subdir.version_id is None  # no top-level cloud block (real DVC shape)
    assert subdir.path == "data/final/subdir"

    # Per-file cloud.<remote>.version_id must round-trip into DvcFileEntry
    # so fetch_files_dir_contents can fetch by version (version-aware
    # safety net for directory outputs).
    for fe in subdir.files:
        assert fe.version_id, f"file entry {fe.relpath!r} lost its version_id"
        assert fe.is_path_based is True


def test_fast_syncable_pipeline_outs_include_files_format_dir_with_only_per_file_cloud(
    tmp_path: Path,
) -> None:
    """Real-world `dvc push` output: directory outs land in ``dvc.lock`` with
    only per-file ``cloud`` blocks under ``files:`` — no top-level ``cloud``
    on the dir-out itself. The fast-syncable partition must include such dir-outs
    so fast-sync handles them via fetch_files_dir_contents (which keys off
    per-file version_id, not the absent top-level one)."""
    _write_lock(
        tmp_path,
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: data/dir/\n"
            "        md5: ffffffffffffffffffffffffffffffff.dir\n"
            "        size: 500\n"
            "        files:\n"
            "          - relpath: a.parquet\n"
            "            md5: 11111111111111111111111111111111\n"
            "            cloud:\n"
            "              x:\n"
            "                version_id: v-a\n"
            "          - relpath: b.parquet\n"
            "            md5: 22222222222222222222222222222222\n"
            "            cloud:\n"
            "              x:\n"
            "                version_id: v-b\n"
        ),
    )
    outs = partition_pipeline_outs(tmp_path, "x")[0]
    assert len(outs) == 1
    out = outs[0]
    assert out.is_files_format is True
    assert out.version_id is None  # no top-level cloud
    assert out.files is not None
    assert all(fe.version_id for fe in out.files)


def test_fast_syncable_pipeline_outs_exclude_files_format_dir_with_missing_per_file_version_id(
    tmp_path: Path,
) -> None:
    """If even one per-file entry lacks a version_id, the dir-out routes to
    fallback — fast-sync would silently skip that file's fetch."""
    _write_lock(
        tmp_path,
        body=(
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            "      - path: data/dir/\n"
            "        md5: ffffffffffffffffffffffffffffffff.dir\n"
            "        files:\n"
            "          - relpath: a.parquet\n"
            "            md5: 11111111111111111111111111111111\n"
            "            cloud:\n"
            "              x:\n"
            "                version_id: v-a\n"
            "          - relpath: b.parquet\n"
            "            md5: 22222222222222222222222222222222\n"
        ),
    )
    assert partition_pipeline_outs(tmp_path, "x")[0] == []


@pytest.mark.integration
def test_dvc_cmd_smoke() -> None:
    from mintd._fast_sync_ops import _check_dvc
    # the integration tag ensures we actually run the shell command in the dev env
    ok, reason = _check_dvc()
    assert ok is True, f"bundled dvc probe failed: {reason}"


# ---------- slice B (pull-all audit fixes 2+3): retry, don't demote ----------
#
# Style note: fakes below patch ``mintd._fast_sync_ops.time.sleep`` so the
# capped exponential backoff is instant in tests.

def _client_error(code: str, status: int = 400, op: str = "HeadObject") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        op,
    )


def test_is_transient_s3_error_classification() -> None:
    from botocore.exceptions import (
        ConnectionClosedError,
        EndpointConnectionError,
        NoCredentialsError,
        ReadTimeoutError,
    )

    from mintd._fast_sync_ops import is_transient_s3_error

    # Widened ClientError code set (503/500/RequestTimeout/SlowDown + Throttling).
    assert is_transient_s3_error(_client_error("503", 503)) is True
    assert is_transient_s3_error(_client_error("SlowDown", 503)) is True
    assert is_transient_s3_error(_client_error("Throttling", 400)) is True
    # botocore network-layer errors are transient too (were previously not).
    assert is_transient_s3_error(EndpointConnectionError(endpoint_url="https://s3.example")) is True
    assert is_transient_s3_error(ReadTimeoutError(endpoint_url="https://s3.example")) is True
    assert is_transient_s3_error(ConnectionClosedError(endpoint_url="https://s3.example")) is True
    # Verified 404 and credential failures are NOT transient.
    assert is_transient_s3_error(_client_error("404", 404)) is False
    assert is_transient_s3_error(NoCredentialsError()) is False


def test_check_bucket_versioning_transient_error_retried_not_disabled() -> None:
    """Slice B: a transient 503 on the versioning probe is retried, not read
    as 'versioning disabled' (which used to demote every target)."""
    calls = {"n": 0}

    class _FlakyS3:
        def get_bucket_versioning(self, Bucket):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _client_error("503", 503, "GetBucketVersioning")
            return {"Status": "Enabled"}

    with patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        assert check_bucket_versioning(_FlakyS3(), "bkt") is True
    assert calls["n"] == 3
    assert mock_sleep.call_count == 2


def test_spot_check_versions_transient_head_error_retried_no_drift(tmp_path: Path) -> None:
    """Slice B: a transient error on a spot-check HEAD is retried and, once
    the HEAD succeeds with the pinned version, causes NO demotion."""
    from botocore.exceptions import EndpointConnectionError

    calls = {"n": 0}

    class _FlakyS3:
        def head_object(self, Bucket, Key, VersionId):
            calls["n"] += 1
            if calls["n"] == 1:
                raise EndpointConnectionError(endpoint_url="https://s3.example")
            return {"VersionId": VersionId}

    out = DvcOut(target="a", path="a", md5="c" * 32, is_dir=False, version_id="v1")
    with patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        drift = spot_check_versions(_FlakyS3(), "bkt", "", [out], tmp_path)
    assert drift == []
    assert calls["n"] == 2
    assert mock_sleep.call_count == 1


def test_spot_check_versions_persistent_transient_is_inconclusive_not_drift(tmp_path: Path) -> None:
    """Slice B: a probe that still fails after all retries is inconclusive —
    logged and skipped — never counted as drift."""
    from botocore.exceptions import ReadTimeoutError

    calls = {"n": 0}

    class _DeadS3:
        def head_object(self, Bucket, Key, VersionId):
            calls["n"] += 1
            raise ReadTimeoutError(endpoint_url="https://s3.example")

    out = DvcOut(target="a", path="a", md5="c" * 32, is_dir=False, version_id="v1")
    with patch("mintd._fast_sync_ops.time.sleep"):
        drift = spot_check_versions(_DeadS3(), "bkt", "", [out], tmp_path)
    assert drift == []
    assert calls["n"] == 3  # all attempts consumed


def test_try_fast_pull_spot_check_drift_demotes_only_affected_target(
    s3_versioned, tmp_path: Path
) -> None:
    """Slice B: verified drift on one out demotes ONLY that out (named in
    the reason); the healthy out still fast-syncs. Slice C: the drifted out
    is version-aware, so it errors loudly (blocked_targets) instead of being
    fed to plain `dvc pull` (fallback_targets stays empty)."""
    s3, bucket = s3_versioned
    body = b"good content"
    md5 = _md5_of(body)
    resp = s3.put_object(Bucket=bucket, Key=f"files/md5/{md5[:2]}/{md5[2:]}", Body=body)
    _write_dvc_config(tmp_path, bucket)
    _write_dvc_file_md5(tmp_path, "data/good", md5, version_id=resp["VersionId"])
    # Pinned version doesn't exist on the remote → verified 404 → drift.
    _write_dvc_file_md5(tmp_path, "data/stale", "d" * 32, version_id="stale-version")

    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path,
            targets=["data/good", "data/stale"],
            remote_name="origin",
        )
    assert result.fallback_targets == []
    assert result.blocked_targets == ["data/stale"]
    assert "drift" in result.blocked_reasons["data/stale"].lower()
    assert result.synced_count == 1
    assert "data/stale" in result.reason
    assert "data/good" not in result.reason
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    assert cp.read_bytes() == body


def test_try_fast_pull_no_credentials_named_reason_not_retried(tmp_path: Path) -> None:
    """Slice B: NoCredentialsError is a NAMED degradation reason and is not
    retried (retrying cannot mint credentials)."""
    from botocore.exceptions import NoCredentialsError

    calls = {"n": 0}

    class _NoCredsS3:
        def get_bucket_versioning(self, Bucket):
            calls["n"] += 1
            raise NoCredentialsError()

    _write_dvc_config(tmp_path, "some-bucket")
    _write_dvc_file_md5(tmp_path, "a", "deadbeef")
    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)), \
         patch("mintd._fast_sync_ops._create_s3_client", return_value=_NoCredsS3()), \
         patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["a"], remote_name="origin"
        )
    assert result.fallback_targets == ["a"]
    assert "credentials" in result.reason.lower()
    assert calls["n"] == 1  # non-retried
    assert mock_sleep.call_count == 0


def test_fetch_to_cache_retries_connection_reset_then_succeeds(tmp_path: Path) -> None:
    """Slice B: a mid-file connection reset (botocore ConnectionClosedError —
    previously non-retryable) is retried and the fetch succeeds."""
    from botocore.exceptions import ConnectionClosedError

    body = b"reset then fine"
    md5 = _md5_of(body)
    cp = cache_path_for(tmp_path / ".dvc" / "cache", md5)
    cp.parent.mkdir(parents=True)

    calls = {"n": 0}

    def fake_download_file(Filename, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionClosedError(endpoint_url="https://s3.example")
        Path(Filename).write_bytes(body)

    fake_s3 = type("FakeS3", (), {"download_file": staticmethod(fake_download_file)})()
    with patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        assert fetch_to_cache(fake_s3, "b", "k", cp, md5) is True
    assert calls["n"] == 2
    assert mock_sleep.call_count == 1
    assert cp.read_bytes() == body


@pytest.mark.parametrize("version_aware", [True, False], ids=["version-aware", "md5-keyed"])
def test_try_fast_pull_files_format_per_file_failure_routing(
    version_aware: bool, s3_versioned, tmp_path: Path
) -> None:
    """A persistent per-file failure inside a files-format dir fails THAT
    FILE by name (files_dir_failures + reason) and routes the out per
    dvc_pull_can_serve: a VERSION-AWARE dir (per-file cloud version pins)
    lands in incomplete_targets — NEVER demoted to the dvc-pull fallback
    (plain dvc pull is documented broken on these) — while a dir with no
    version pins anywhere keeps the fallback route (plain dvc pull
    genuinely serves md5-keyed outs)."""
    s3, bucket = s3_versioned
    body = b"content"
    md5_ok = _md5_of(body)
    md5_missing = "e" * 32
    (tmp_path / "data").mkdir()
    _write_dvc_config(tmp_path, bucket)
    resp = s3.put_object(Bucket=bucket, Key="data/f/a.csv", Body=body)
    # data/f/b.csv is NOT uploaded → persistent (non-transient) per-file failure.
    if version_aware:
        (tmp_path / "data" / "f.dvc").write_text(
            "outs:\n  - path: f\n    files:\n"
            f"      - relpath: a.csv\n        md5: {md5_ok}\n        size: 7\n"
            "        cloud:\n          origin:\n"
            f"            version_id: {resp['VersionId']}\n"
            f"      - relpath: b.csv\n        md5: {md5_missing}\n        size: 7\n"
            "        cloud:\n          origin:\n            version_id: bogus\n"
        )
    else:
        (tmp_path / "data" / "f.dvc").write_text(
            "outs:\n  - path: data/f\n    files:\n"
            f"      - relpath: a.csv\n        md5: {md5_ok}\n        size: 7\n"
            f"      - relpath: b.csv\n        md5: {md5_missing}\n        size: 7\n"
        )

    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["data/f"], remote_name="origin"
        )
    assert result.fallback_targets == ([] if version_aware else ["data/f"])
    assert result.incomplete_targets == (["data/f"] if version_aware else [])
    assert result.success is False
    assert result.synced_count == 0
    assert any("data/f" in f and "b.csv" in f for f in result.files_dir_failures)
    if version_aware:
        assert "data/f" in result.reason


@pytest.mark.parametrize("version_aware", [True, False], ids=["version-aware", "md5-keyed"])
def test_try_fast_pull_md5_dir_per_file_failure_routing(
    version_aware: bool, s3_versioned, tmp_path: Path
) -> None:
    """The same routing split in the md5-dir lane: an out-level version pin
    makes constituent fetch failures land in incomplete_targets (never the
    dvc-pull fallback), while an unpinned md5-keyed dir-out keeps the
    fallback route (plain dvc pull can restore it) — the failed file is
    named either way."""
    s3, bucket = s3_versioned
    entries = [DvcFileEntry("f" * 32, "a.csv")]  # blob never uploaded
    full_md5 = _put_dir_manifest(s3, bucket, "", entries)
    (tmp_path / "data").mkdir()
    _write_dvc_config(tmp_path, bucket)
    dvc_body = f"outs:\n  - path: data/mdir\n    md5: {full_md5}\n"
    if version_aware:
        manifest_resp = s3.list_object_versions(
            Bucket=bucket, Prefix=f"files/md5/{full_md5[:2]}/"
        )
        version_id = manifest_resp["Versions"][0]["VersionId"]
        dvc_body += f"    version_id: {version_id}\n"
    (tmp_path / "data" / "mdir.dvc").write_text(dvc_body)

    with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path, targets=["data/mdir"], remote_name="origin"
        )
    assert result.fallback_targets == ([] if version_aware else ["data/mdir"])
    assert result.incomplete_targets == (["data/mdir"] if version_aware else [])
    assert result.success is False
    assert any("data/mdir" in f and "a.csv" in f for f in result.files_dir_failures)


# ---------- guards split per dvc_pull_can_serve ----------

def test_guard_mixed_targets_imports_md5_fall_back_version_aware_error(
    tmp_path: Path,
) -> None:
    """Slice C: an all-or-nothing guard (here: dvc version mismatch) no
    longer dumps every target into one plain `dvc pull`. Classification is
    pure .dvc parsing (no S3, no boto3), then: imports + md5-keyed outs keep
    the fallback route; version-aware outs (top-level version_id AND
    cloud-nested/path-based) land in blocked_targets with the guard's reason."""
    _write_dvc_file_md5(tmp_path, "data/md5only", "ca" * 16)
    _write_dvc_file_import(tmp_path, "data/imported", "deadbeef.dir")
    _write_dvc_file_md5(tmp_path, "data/versioned", "be" * 16, version_id="v1")
    (tmp_path / "data" / "pathbased.dvc").write_text(
        "outs:\n"
        f"  - path: pathbased\n"
        f"    md5: {'ab' * 16}\n"
        "    size: 1\n"
        "    cloud:\n"
        "      origin:\n"
        "        version_id: v2\n"
    )
    with patch(
        "mintd._fast_sync_ops._check_dvc",
        return_value=(False, "dvc version mismatch"),
    ):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path,
            targets=["data/md5only", "data/imported", "data/versioned", "data/pathbased"],
            remote_name="origin",
        )
    assert result.success is False
    assert sorted(result.fallback_targets) == ["data/imported", "data/md5only"]
    assert sorted(result.blocked_targets) == ["data/pathbased", "data/versioned"]
    for t in ("data/versioned", "data/pathbased"):
        assert "version mismatch" in result.blocked_reasons[t]
    assert "version mismatch" in result.reason


def test_guard_imports_only_repo_full_fallback_no_errors(tmp_path: Path) -> None:
    """Slice C: an imports-only repo under a guard behaves exactly as before
    the fix — every target routes to plain `dvc pull` (the slice 29 contract)
    and nothing errors, so the CLI still exits 0."""
    _write_dvc_file_import(tmp_path, "data/imports/a", "aa.dir")
    _write_dvc_file_import(tmp_path, "data/imports/b", "bb.dir")
    with patch(
        "mintd._fast_sync_ops._check_dvc",
        return_value=(False, "dvc version mismatch"),
    ):
        result = SubprocessFastSyncOps().try_fast_pull(
            project_path=tmp_path,
            targets=["data/imports/a", "data/imports/b"],
            remote_name="origin",
        )
    assert result.success is False
    assert result.blocked_targets == []
    assert result.blocked_reasons == {}
    assert sorted(result.fallback_targets) == ["data/imports/a", "data/imports/b"]


def test_guard_versioning_disabled_also_splits_by_version_awareness(
    tmp_path: Path,
) -> None:
    """Slice C: the split applies to every guard, not just _check_dvc —
    here the 'bucket versioning disabled' guard (the last all-or-nothing
    return before per-out processing)."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="bare")  # versioning never enabled
        _write_dvc_config(tmp_path, "bare")
        _write_dvc_file_md5(tmp_path, "data/plain", "ca" * 16)
        _write_dvc_file_md5(tmp_path, "data/versioned", "be" * 16, version_id="v1")
        with patch("mintd._fast_sync_ops._check_dvc", return_value=(True, None)):
            result = SubprocessFastSyncOps().try_fast_pull(
                project_path=tmp_path,
                targets=["data/plain", "data/versioned"],
                remote_name="origin",
            )
    assert result.fallback_targets == ["data/plain"]
    assert result.blocked_targets == ["data/versioned"]
    assert "versioning disabled" in result.blocked_reasons["data/versioned"]


# ---------- slice B: retry backoff policy is pinned, not just attempt count ----------


def test_retry_transient_backoff_durations_pinned() -> None:
    """The locked Slice B policy is 3 attempts with capped exponential
    backoff from a 0.5s base: sleeps between the attempts must be exactly
    0.5s then 1.0s. Attempt counts alone don't pin this — a time.sleep(0)
    or an unbounded backoff would otherwise pass the suite."""
    from unittest.mock import call

    from mintd._fast_sync_ops import retry_transient

    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _client_error("503", 503)
        return "ok"

    with patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        assert retry_transient(_flaky) == "ok"
    assert calls["n"] == 3
    assert mock_sleep.call_args_list == [call(0.5), call(1.0)]


def test_retry_transient_backoff_caps_at_8s() -> None:
    """The exponential backoff is capped at 8s: a long retry run must plateau
    (0.5, 1, 2, 4, 8, 8, ...) — never keep doubling."""
    def _dead() -> None:
        raise _client_error("503", 503)

    from mintd._fast_sync_ops import retry_transient

    with patch("mintd._fast_sync_ops.time.sleep") as mock_sleep:
        with pytest.raises(ClientError):
            retry_transient(_dead, attempts=10)
    assert [c.args[0] for c in mock_sleep.call_args_list] == [
        0.5, 1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0, 8.0,
    ]
