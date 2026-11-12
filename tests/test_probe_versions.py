"""probe_versions: one HEAD per (key, version), classified, in a thread pool.

Every case runs against moto. Failures moto can't produce on its own (429, 5xx,
403, a worker raising) are injected with a botocore ``before-send`` hook, so the
real client still parses the response.
"""

from __future__ import annotations

import pytest
from botocore.awsrequest import AWSResponse

from mintd._fast_sync_ops import S3AccessDenied, probe_versions


class _RawBody:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    def stream(self, **_kw):
        yield self._data

    def read(self, *_a):
        return self._data


def _respond(client, status: int, *, key: str | None = None, times: int | None = None) -> list[int]:
    """Answer HEADs (of ``key``, or every key) with ``status``, ``times`` times."""
    seen: list[int] = []

    def hook(request, **_kw):
        if key is not None and not request.url.split("?")[0].endswith("/" + key):
            return None
        if times is not None and len(seen) >= times:
            return None
        seen.append(status)
        return AWSResponse(request.url, status, {}, _RawBody())

    client.meta.events.register_first("before-send.s3.HeadObject", hook)
    return seen


def test_found_returns_version_etag_and_size(s3_versioned) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"hello")["VersionId"]
    got = probe_versions(s3, bucket, [("p/a.csv", vid), ("p/a.csv", None)])
    assert got[("p/a.csv", vid)].kind == "found"
    assert got[("p/a.csv", vid)].version_id == vid
    assert got[("p/a.csv", vid)].etag == "5d41402abc4b2a76b9719d911017c592"  # md5("hello"), no quotes
    assert got[("p/a.csv", vid)].size == 5
    assert got[("p/a.csv", None)].version_id == vid  # the current object


def test_missing_key_missing_version_and_delete_marker(s3_versioned) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"x")["VersionId"]
    marker = s3.delete_object(Bucket=bucket, Key="p/a.csv")["VersionId"]
    got = probe_versions(s3, bucket, [
        ("p/nope.csv", None), ("p/nope.csv", vid), ("p/a.csv", "not-a-version"), ("p/a.csv", marker),
    ])
    assert {v.kind for v in got.values()} == {"missing"}
    # The pinned version under the marker still resolves.
    assert probe_versions(s3, bucket, [("p/a.csv", vid)])[("p/a.csv", vid)].kind == "found"


def test_suspended_versioning_is_unpinned(s3_versioned) -> None:
    s3, bucket = s3_versioned
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Suspended"})
    s3.put_object(Bucket=bucket, Key="p/s.csv", Body=b"x")
    assert probe_versions(s3, bucket, [("p/s.csv", None)])[("p/s.csv", None)].kind == "unpinned"


def test_429_outlasting_botocores_retries_is_retried_by_mintd(s3_versioned) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"x")["VersionId"]
    # botocore retries a 429 itself, up to its own attempt limit. Throttle for
    # all of those, so only mintd's retry can reach the success that follows.
    botocore_attempts = (s3.meta.config.retries or {}).get("total_max_attempts", 5)
    seen = _respond(s3, 429, times=botocore_attempts)
    assert probe_versions(s3, bucket, [("p/a.csv", vid)])[("p/a.csv", vid)].kind == "found"
    assert len(seen) == botocore_attempts


def test_5xx_after_retries_is_inconclusive(s3_versioned) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"x")["VersionId"]
    seen = _respond(s3, 503)
    verdict = probe_versions(s3, bucket, [("p/a.csv", vid)])[("p/a.csv", vid)]
    assert verdict.kind == "inconclusive"
    # botocore retries a 5xx itself under each of mintd's three attempts.
    assert len(seen) >= 3


def test_403_raises_for_the_whole_probe(s3_versioned) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"x")["VersionId"]
    s3.put_object(Bucket=bucket, Key="p/b.csv", Body=b"y")
    _respond(s3, 403, key="p/a.csv")
    with pytest.raises(S3AccessDenied):
        probe_versions(s3, bucket, [("p/a.csv", vid), ("p/b.csv", None)])


def test_a_worker_that_raises_is_inconclusive_and_keeps_every_other_outcome(s3_versioned) -> None:
    s3, bucket = s3_versioned
    keys = [f"p/{i}.csv" for i in range(6)]
    vids = {k: s3.put_object(Bucket=bucket, Key=k, Body=k.encode())["VersionId"] for k in keys}

    def boom(request, **_kw):
        if request.url.split("?")[0].endswith("/p/3.csv"):
            raise RuntimeError("botocore blew up")

    s3.meta.events.register_first("before-send.s3.HeadObject", boom)
    got = probe_versions(s3, bucket, [(k, vids[k]) for k in keys], jobs=3)
    assert got[("p/3.csv", vids["p/3.csv"])].kind == "inconclusive"
    assert "RuntimeError" in (got[("p/3.csv", vids["p/3.csv"])].reason or "")
    assert {k for (k, _), v in got.items() if v.kind == "found"} == set(keys) - {"p/3.csv"}


@pytest.mark.parametrize("status", [400, 409, 412])
def test_an_unmapped_error_is_inconclusive_never_found(s3_versioned, status: int) -> None:
    s3, bucket = s3_versioned
    vid = s3.put_object(Bucket=bucket, Key="p/a.csv", Body=b"x")["VersionId"]
    _respond(s3, status)
    assert probe_versions(s3, bucket, [("p/a.csv", vid)])[("p/a.csv", vid)].kind == "inconclusive"
