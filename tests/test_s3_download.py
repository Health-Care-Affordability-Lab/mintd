"""Every lane that downloads from S3 retries a 5xx that botocore raises as
``AttributeError``, because every lane downloads through ``mintd._s3.download``.

The 5xx is the real one: ``serve_5xx`` answers GetObject on a moto client with a
checksummed error body, and botocore's own code turns it into the
``'StreamingChecksumBody' object has no attribute 'strip'`` error a Wasabi 5xx
gave on a real clone.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from mintd._cache_ops import cache_pull, cache_push
from mintd._config import Config
from mintd._console import Reporter
from mintd._fast_sync_ops import fetch_to_cache
from tests._harness.s3_faults import serve_5xx

BODY = b"payload"


def _in_the_remote(s3, bucket: str, tmp_path: Path) -> tuple[str, str, Path]:
    """One DVC object in the remote: its key, its md5 and where fast sync caches it."""
    md5 = hashlib.md5(BODY).hexdigest()
    key = f"lab/p/files/md5/{md5[:2]}/{md5[2:]}"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    return key, md5, tmp_path / ".dvc" / "cache" / "files" / "md5" / md5[:2] / md5[2:]


def test_fast_sync_retries_a_5xx_botocore_raises_as_attribute_error(s3_versioned, tmp_path: Path) -> None:
    """Clone and pull fetch each file of an out on its own GET. One 5xx whose body botocore
    can't parse is retried like any 500, and the file lands in the cache, md5-checked.
    Mutations that must redden this: drop `_s3.download`'s translation; translate to a 403."""
    s3, bucket = s3_versioned
    key, md5, cached = _in_the_remote(s3, bucket, tmp_path)
    sent = serve_5xx(s3, times=1)

    assert fetch_to_cache(s3, bucket, key, cached, md5) is True

    assert cached.read_bytes() == BODY
    assert len(sent) == 2


def test_fast_sync_stops_a_persistent_5xx_after_three_gets(s3_versioned, tmp_path: Path) -> None:
    """A 5xx that outlasts the retries costs the same three GETs as any 500, then fails the
    file with the 5xx it was, not botocore's `AttributeError`, and leaves no tmp behind.
    Mutation that must redden this: retry fast sync's attempt around the retrying download
    again (nine GETs)."""
    s3, bucket = s3_versioned
    key, md5, cached = _in_the_remote(s3, bucket, tmp_path)
    sent = serve_5xx(s3, times=99)

    with pytest.raises(ClientError) as raised:
        fetch_to_cache(s3, bucket, key, cached, md5)

    assert raised.value.response["ResponseMetadata"]["HTTPStatusCode"] == 500
    assert isinstance(raised.value.__cause__, AttributeError)  # botocore still mislabels it
    assert len(sent) == 3
    assert list(cached.parent.iterdir()) == []


def _project(root: Path, bucket: str) -> Path:
    (root / ".dvc").mkdir(parents=True)
    (root / ".dvc" / "config").write_text(
        f'[core]\n    remote = origin\n[\'remote "origin"\']\n    url = s3://{bucket}/lab/proj\n'
    )
    return root


def _pull(s3, project: Path):
    return cache_pull(project_path=project, config=Config(), reporter=Reporter(json_mode=True),
                      s3_client_factory=lambda *_a, **_k: s3)


def _a_file_in_the_cache(s3, bucket: str, tmp_path: Path) -> Path:
    """`x.bin` pushed to the repo file cache from one project; returns a fresh one to pull into."""
    src = _project(tmp_path / "src", bucket)
    (src / "x.bin").write_bytes(BODY)
    cache_push(project_path=src, paths=["x.bin"], config=Config(), reporter=Reporter(json_mode=True),
               s3_client_factory=lambda *_a, **_k: s3)
    return _project(tmp_path / "dest", bucket)


def test_cache_pull_retries_a_5xx_botocore_raises_as_attribute_error(s3_versioned, tmp_path: Path) -> None:
    """The cache lane downloads through the same function, so the same 5xx is retried there
    and the file lands. Mutations that must redden this: drop `_s3.download`'s translation;
    translate to a 403."""
    s3, bucket = s3_versioned
    dest = _a_file_in_the_cache(s3, bucket, tmp_path)
    sent = serve_5xx(s3, times=1)

    summary = _pull(s3, dest)

    assert summary.pulled == 1 and not summary.failed
    assert (dest / "x.bin").read_bytes() == BODY
    assert len(sent) == 2


def test_cache_pull_reports_a_persistent_5xx_after_three_gets(s3_versioned, tmp_path: Path) -> None:
    """A 5xx that outlasts the retries costs three GETs and fails that file, with no tmp
    left in the working tree. Mutation that must redden this: retry `download_object`'s
    attempt around the retrying download again (nine GETs)."""
    s3, bucket = s3_versioned
    dest = _a_file_in_the_cache(s3, bucket, tmp_path)
    sent = serve_5xx(s3, times=99)

    summary = _pull(s3, dest)

    assert [o.rel for o in summary.failed] == ["x.bin"]
    assert len(sent) == 3
    assert sorted(p.name for p in dest.iterdir()) == [".dvc"]
