"""Stratum T (transport) tests for ``mintd._share_ops``.

Verb-agnostic: these exercise the three primitives directly, including the
cache-contract test (non-``share/`` key, ``Tagging`` / ``Metadata`` extra_args,
``ChecksumAlgorithm`` override rejection) and the signature test that pins the
"no Config / Reporter" boundary — proving the later extraction into
``_transfer_ops.py`` is rename-only.
"""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError, NoCredentialsError

from mintd._share_ops import (
    RemoteObjectInfo,
    RemoteObjectNotFound,
    TransferError,
    download_object,
    file_sha256,
    head_remote_object,
    upload_object,
)


def _client_error(code: str, status: int, op: str = "HeadObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, op
    )


class _UploadWrapsClientError:
    """Mimics real boto3: ``upload_file`` re-raises any transfer-time
    ``ClientError`` wrapped in ``S3UploadFailedError`` (with the ClientError as
    ``__context__``), optionally succeeding after ``fail_times`` attempts."""

    def __init__(self, client_error: ClientError, *, fail_times: int = 1_000) -> None:
        self.client_error = client_error
        self.fail_times = fail_times
        self.calls = 0

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Callback=None):  # noqa: N803
        self.calls += 1
        if self.calls > self.fail_times:
            if Callback is not None:
                Callback(len(Path(filename).read_bytes()))
            return
        try:
            raise self.client_error
        except ClientError:
            # Bare raise inside except => __context__ is the ClientError, as
            # boto3/s3/transfer.py:456-459 does.
            raise S3UploadFailedError(
                f"Failed to upload {filename} to {bucket}/{key}"
            )


class _SpyUploadClient:
    """Records the ``ExtraArgs`` of each ``upload_file`` and fires the Callback."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Callback=None):  # noqa: N803
        self.calls.append(
            {"filename": filename, "bucket": bucket, "key": key, "extra_args": ExtraArgs}
        )
        if Callback is not None:
            Callback(len(Path(filename).read_bytes()))


class _AssertNotCalledClient:
    """Any transport call is a test failure — proves preflight/guard-before-wire."""

    def upload_file(self, *a, **k):
        raise AssertionError("upload_file must not be called")

    def download_file(self, *a, **k):
        raise AssertionError("download_file must not be called")

    def head_object(self, *a, **k):
        raise AssertionError("head_object must not be called")


# ---------- signature / boundary ----------


def test_transport_functions_carry_no_config_or_reporter() -> None:
    for fn in (head_remote_object, upload_object, download_object):
        params = set(inspect.signature(fn).parameters)
        assert "config" not in params
        assert "reporter" not in params
        assert "SHARE_PREFIX" not in params
    # And they take the transport tuple, positionally.
    assert list(inspect.signature(upload_object).parameters)[:4] == [
        "s3",
        "bucket",
        "key",
        "local_path",
    ]


# ---------- reserved-key guard + cache contract ----------


def test_upload_reserved_key_override_raises_and_never_touches_wire() -> None:
    client = _AssertNotCalledClient()
    with pytest.raises(ValueError, match="ChecksumAlgorithm"):
        upload_object(
            client,
            "b",
            "k",
            Path("/does/not/matter"),
            progress=lambda _n: None,
            extra_args={"ChecksumAlgorithm": "CRC32"},
        )


def test_cache_contract_upload_non_share_key_merges_extra_args(tmp_path: Path) -> None:
    src = tmp_path / "blob.bin"
    src.write_bytes(b"cache-bytes")
    client = _SpyUploadClient()
    advances: list[int] = []

    n = upload_object(
        client,
        "test-bucket",
        "lab/proj/cache/blob.bin",  # non-share key: transport is verb-agnostic
        src,
        progress=advances.append,
        extra_args={"Tagging": "mintd-lane=cache", "Metadata": {"mintd-sha256": "abc"}},
    )

    assert n == len(b"cache-bytes")
    assert client.calls[0]["key"] == "lab/proj/cache/blob.bin"
    assert client.calls[0]["extra_args"] == {
        "ChecksumAlgorithm": "SHA256",
        "Tagging": "mintd-lane=cache",
        "Metadata": {"mintd-sha256": "abc"},
    }
    assert sum(advances) == len(b"cache-bytes")


# ---------- upload failure mapping (S3UploadFailedError unwrap) ----------


def test_upload_wrapped_access_denied_maps_to_hinted_transfer_error(
    tmp_path: Path,
) -> None:
    # Real boto3 wraps a transfer-time AccessDenied in S3UploadFailedError, which
    # is NOT a ClientError; upload_object must unwrap it back to a hinted
    # TransferError instead of letting the raw wrapper escape as a traceback.
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    client = _UploadWrapsClientError(_client_error("AccessDenied", 403, "PutObject"))
    with pytest.raises(TransferError) as ei:
        upload_object(client, "b", "share/alice/f", src, progress=lambda _n: None)
    assert not isinstance(ei.value, RemoteObjectNotFound)
    assert ei.value.hint == "check AWS credentials: mintd config validate"


def test_upload_wrapped_no_such_bucket_maps_to_bucket_hint(tmp_path: Path) -> None:
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    client = _UploadWrapsClientError(_client_error("NoSuchBucket", 404, "PutObject"))
    with pytest.raises(TransferError) as ei:
        upload_object(client, "b", "share/alice/f", src, progress=lambda _n: None)
    assert ei.value.hint == "check storage_bucket_prefix (mintd config setup)"


def test_upload_wrapped_transient_error_is_retried_then_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    # A transient SlowDown wrapped in S3UploadFailedError must still be retried
    # at the mintd level (unwrapped to a ClientError the shared policy sees).
    monkeypatch.setattr("mintd._fast_sync_ops.time.sleep", lambda _s: None)
    src = tmp_path / "f.bin"
    src.write_bytes(b"payload")
    client = _UploadWrapsClientError(
        _client_error("SlowDown", 503, "PutObject"), fail_times=1
    )
    n = upload_object(client, "b", "share/alice/f", src, progress=lambda _n: None)
    assert n == src.stat().st_size
    assert client.calls == 2  # one retry


# ---------- head ----------


def test_head_missing_key_raises_typed_not_found() -> None:
    class _C:
        def head_object(self, **k):
            raise _client_error("NoSuchKey", 404)

    with pytest.raises(RemoteObjectNotFound) as ei:
        head_remote_object(_C(), "b", "share/alice/x.parquet")
    assert str(ei.value) == "no object at share/alice/x.parquet"
    assert ei.value.hint is None  # hint-free; policy layer re-words it


def test_head_returns_size_and_checksum() -> None:
    class _C:
        def head_object(self, **k):
            return {"ContentLength": 7, "ChecksumSHA256": "deadbeef"}

    info = head_remote_object(_C(), "b", "k")
    assert info == RemoteObjectInfo(size=7, checksum_sha256="deadbeef")


def test_head_without_stored_checksum_reports_none() -> None:
    class _C:
        def head_object(self, **k):
            return {"ContentLength": 7}

    info = head_remote_object(_C(), "b", "k")
    assert info.checksum_sha256 is None


def test_head_sends_checksum_mode_enabled() -> None:
    seen: dict = {}

    class _C:
        def head_object(self, **k):
            seen.update(k)
            return {"ContentLength": 1}

    head_remote_object(_C(), "b", "k")
    assert seen["ChecksumMode"] == "ENABLED"


# ---------- credentials (R4) ----------


def test_missing_credentials_maps_to_transfer_error_zero_retries(tmp_path: Path) -> None:
    counts = {"head": 0, "upload": 0, "download": 0}

    class _NoCreds:
        def head_object(self, **k):
            counts["head"] += 1
            raise NoCredentialsError()

        def upload_file(self, *a, **k):
            counts["upload"] += 1
            raise NoCredentialsError()

        def download_file(self, **k):
            counts["download"] += 1
            raise NoCredentialsError()

    client = _NoCreds()
    for call in (
        lambda: head_remote_object(client, "b", "k"),
        lambda: upload_object(client, "b", "k", Path(__file__), progress=lambda _n: None),
        lambda: download_object(
            client, "b", "k", tmp_path / "x", progress=lambda _n: None
        ),
    ):
        with pytest.raises(TransferError) as ei:
            call()
        assert "AWS credentials unavailable (not retried)" in str(ei.value)
        assert ei.value.hint == "check AWS credentials: mintd config validate"
    assert counts == {"head": 1, "upload": 1, "download": 1}  # no retries


# ---------- retry wiring ----------


def test_transient_error_is_retried_then_succeeds(monkeypatch) -> None:
    monkeypatch.setattr("mintd._fast_sync_ops.time.sleep", lambda _s: None)
    calls = {"n": 0}

    class _C:
        def head_object(self, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _client_error("SlowDown", 503)
            return {"ContentLength": 3}

    info = head_remote_object(_C(), "b", "k")
    assert info.size == 3
    assert calls["n"] == 2  # one retry


# ---------- download atomicity ----------


class _WriteThenRaiseClient:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
        Path(Filename).write_bytes(b"partial-bytes")
        raise self.exc


def test_download_client_error_leaves_no_dest_no_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "out.parquet"
    tmp = tmp_path / "out.parquet.tmp"
    client = _WriteThenRaiseClient(_client_error("AccessDenied", 403, "GetObject"))
    with pytest.raises(TransferError):
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert not dest.exists()
    assert not tmp.exists()


def test_download_no_such_bucket_404_maps_to_bucket_hint_not_not_found(
    tmp_path: Path,
) -> None:
    # A real NoSuchBucket response carries HTTPStatusCode 404; the mapping must
    # surface the storage_bucket_prefix hint, not mis-diagnose it as a missing
    # object (RemoteObjectNotFound) which the policy layer re-words as a bad ref.
    dest = tmp_path / "out.parquet"
    client = _WriteThenRaiseClient(_client_error("NoSuchBucket", 404, "GetObject"))
    with pytest.raises(TransferError) as ei:
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert not isinstance(ei.value, RemoteObjectNotFound)
    assert ei.value.hint == "check storage_bucket_prefix (mintd config setup)"


def test_download_keyboardinterrupt_leaves_no_dest_no_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "out.parquet"
    tmp = tmp_path / "out.parquet.tmp"
    client = _WriteThenRaiseClient(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert not dest.exists()
    assert not tmp.exists()


def test_download_verify_tmp_raise_leaves_no_dest_no_tmp(tmp_path: Path) -> None:
    dest = tmp_path / "out.parquet"
    tmp = tmp_path / "out.parquet.tmp"

    class _GoodClient:
        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            Path(Filename).write_bytes(b"good-bytes")

    def _verify(_p: Path) -> None:
        raise ValueError("checksum mismatch")

    with pytest.raises(ValueError, match="checksum mismatch"):
        download_object(
            _GoodClient(), "b", "k", dest, progress=lambda _n: None, verify_tmp=_verify
        )
    assert not dest.exists()
    assert not tmp.exists()


def test_download_tmp_name_appends_not_with_suffix(tmp_path: Path) -> None:
    # report.parquet -> report.parquet.tmp (NOT report.tmp), so sibling
    # dests with different extensions never collide.
    dest = tmp_path / "report.parquet"
    captured: dict = {}

    class _C:
        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            captured["filename"] = Filename
            Path(Filename).write_bytes(b"x")

    download_object(_C(), "b", "k", dest, progress=lambda _n: None)
    assert captured["filename"].endswith("report.parquet.tmp")


def test_download_sends_checksum_mode_enabled(tmp_path: Path) -> None:
    captured: dict = {}

    class _C:
        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            captured["extra"] = ExtraArgs
            Path(Filename).write_bytes(b"x")

    download_object(_C(), "b", "k", tmp_path / "o", progress=lambda _n: None)
    assert captured["extra"] == {"ChecksumMode": "ENABLED"}


# ---------- moto round-trip ----------


def test_upload_download_roundtrip_is_byte_identical(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    src = tmp_path / "src.parquet"
    src.write_bytes(b"hello-transport-world" * 1000)
    key = "share/alice/grant/src.parquet"

    up_advances: list[int] = []
    n_up = upload_object(s3, bucket, key, src, progress=up_advances.append)
    assert n_up == src.stat().st_size

    info = head_remote_object(s3, bucket, key)
    assert info.size == src.stat().st_size

    dest = tmp_path / "dest.parquet"
    down_advances: list[int] = []
    n_down = download_object(s3, bucket, key, dest, progress=down_advances.append)

    assert n_down == src.stat().st_size
    assert file_sha256(src) == file_sha256(dest)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == file_sha256(dest)


# ---------- file_sha256 ----------


# ---------------------------------------------------------------------------
# Adversarial-QA regressions (red-team 2026-07-07)
# ---------------------------------------------------------------------------


def test_download_network_error_after_retry_maps_to_hinted_error(tmp_path: Path) -> None:
    """P0: a botocore network error surviving retry_transient must become a
    hinted TransferError, not a raw traceback."""
    from botocore.exceptions import EndpointConnectionError

    dest = tmp_path / "out.parquet"
    client = _WriteThenRaiseClient(EndpointConnectionError(endpoint_url="https://x"))
    with pytest.raises(TransferError) as ei:
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert ei.value.hint is not None
    assert not dest.exists() and not (tmp_path / "out.parquet.tmp").exists()


def test_download_retries_exceeded_maps_to_hinted_error(tmp_path: Path) -> None:
    """s3transfer's own stream-retry loop exhausts to boto3
    RetriesExceededError (not a ClientError/BotoCoreError). It must map to a
    hinted TransferError — unwrapping to the underlying cause when present —
    not escape as a raw traceback on a real large-file download."""
    from boto3.exceptions import RetriesExceededError
    from botocore.exceptions import ReadTimeoutError

    dest = tmp_path / "out.parquet"
    # cause is a known network error -> precise network hint via delegation
    client = _WriteThenRaiseClient(
        RetriesExceededError(ReadTimeoutError(endpoint_url="https://x"))
    )
    with pytest.raises(TransferError) as ei:
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert ei.value.hint is not None
    assert not dest.exists() and not (tmp_path / "out.parquet.tmp").exists()

    # opaque cause -> generic hinted transfer error, still no traceback
    client2 = _WriteThenRaiseClient(RetriesExceededError(RuntimeError("opaque")))
    with pytest.raises(TransferError) as ei2:
        download_object(client2, "b", "k", tmp_path / "o2.parquet", progress=lambda _n: None)
    assert ei2.value.hint is not None


def test_download_checksum_mismatch_maps_to_hinted_error(tmp_path: Path) -> None:
    """P0: FlexibleChecksumError (corrupt object) must become a hinted
    TransferError with the tmp cleaned up, never a traceback."""
    from botocore.exceptions import FlexibleChecksumError

    dest = tmp_path / "out.parquet"
    client = _WriteThenRaiseClient(FlexibleChecksumError(error_msg="sha256 mismatch"))
    with pytest.raises(TransferError) as ei:
        download_object(client, "b", "k", dest, progress=lambda _n: None)
    assert "checksum mismatch" in str(ei.value)
    assert not dest.exists() and not (tmp_path / "out.parquet.tmp").exists()


def test_upload_network_error_after_retry_maps_to_hinted_error(tmp_path: Path) -> None:
    from botocore.exceptions import ReadTimeoutError

    src = tmp_path / "f.bin"
    src.write_bytes(b"x" * 10)

    class _C:
        def upload_file(self, *a, **k):
            raise ReadTimeoutError(endpoint_url="https://x")

    with pytest.raises(TransferError) as ei:
        upload_object(_C(), "b", "k", src, progress=lambda _n: None)
    assert ei.value.hint is not None


def test_reserved_key_guard_is_case_insensitive(tmp_path: Path) -> None:
    """A miscased / padded ChecksumAlgorithm override must hit the typed guard
    before any wire call — not slip through to an unmapped boto3 ValueError."""
    src = tmp_path / "f.bin"
    src.write_bytes(b"x")
    for bad in ("checksumalgorithm", "CHECKSUMALGORITHM", "ChecksumAlgorithm "):
        with pytest.raises(ValueError, match="reserved upload arg"):
            upload_object(
                _AssertNotCalledClient(), "b", "k", src,
                progress=lambda _n: None, extra_args={bad: "CRC32"},
            )


def test_download_size_mismatch_for_uncensummed_object_fails(tmp_path: Path) -> None:
    """The 'verified by size only' guarantee is real: a download whose byte
    count != the HEAD size raises and leaves nothing behind."""
    dest = tmp_path / "out.parquet"

    class _WrongSizeClient:
        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            Path(Filename).write_bytes(b"only-three-hundred")  # != expected_size

    with pytest.raises(TransferError, match="size mismatch"):
        download_object(
            _WrongSizeClient(), "b", "k", dest,
            progress=lambda _n: None, expected_size=99999,
        )
    assert not dest.exists() and not (tmp_path / "out.parquet.tmp").exists()


def test_download_does_not_follow_symlink_at_tmp_path(tmp_path: Path) -> None:
    """A planted symlink at the predictable <dest>.tmp must not be followed —
    the pre-download unlink forces a fresh regular file, protecting the target."""
    dest = tmp_path / "out.parquet"
    tmp = tmp_path / "out.parquet.tmp"
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite me")
    tmp.symlink_to(victim)  # attacker plants a symlink at the tmp path

    class _C:
        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            Path(Filename).write_bytes(b"downloaded")

    download_object(_C(), "b", "k", dest, progress=lambda _n: None)
    assert victim.read_text() == "do not overwrite me"  # untouched
    assert dest.read_bytes() == b"downloaded"


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    payload = b"a" * (1024 * 1024 + 7)  # spans multiple chunks
    p.write_bytes(payload)
    assert file_sha256(p) == hashlib.sha256(payload).hexdigest()
