"""Temp-name parity across the two fetch lanes.

The contract: mintd never destroys a file it did not create, and both
documented-equivalent fetch lanes (``share get`` and ``cache pull``) must
prove it the same way. Before this test the two lanes disagreed — cache
passed a collision-proof ``tmp_suffix`` at its call site while share took
``download_object``'s predictable ``<dest>.tmp`` default and deleted a
user's own scratch file of that name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd._cache_ops import cache_pull
from mintd._config import Config
from mintd._console import Reporter
from mintd._share_ops import share_get, share_put

PAYLOAD = b"payload-bytes" * 3
SCRATCH = b"user-scratch-do-not-touch"


def _share_lane(s3, bucket: str, tmp_path: Path):
    """Seed share/alice/report.csv; return (fetch, dest)."""
    cfg = Config(storage_bucket_prefix=bucket, storage_endpoint="https://s3")

    def factory(_c, _p):
        return s3

    src = tmp_path / "report.csv"
    src.write_bytes(PAYLOAD)
    share_put(
        local_path=src, user="alice", config=cfg,
        reporter=Reporter(json_mode=True), s3_client_factory=factory,
    )
    dest = tmp_path / "inbox" / "report.csv"
    dest.parent.mkdir()

    def fetch() -> None:
        share_get(
            ref="alice/report.csv", config=cfg,
            reporter=Reporter(json_mode=True), out=dest,
            s3_client_factory=factory,
        )

    return fetch, dest


def _cache_lane(s3, bucket: str, tmp_path: Path):
    """Seed <prefix>/cache/report.csv in a scaffolded project; return (fetch, dest)."""
    proj = tmp_path / "proj"
    (proj / ".dvc").mkdir(parents=True)
    (proj / ".dvc" / "config").write_text(
        f'[remote "origin"]\n    url = s3://{bucket}/lab/proj\n'
    )
    s3.put_object(Bucket=bucket, Key="lab/proj/cache/report.csv", Body=PAYLOAD)

    def fetch() -> None:
        cache_pull(
            project_path=proj, config=Config(),
            reporter=Reporter(json_mode=True),
            s3_client_factory=lambda _c, _p: s3,
        )

    return fetch, proj / "report.csv"


@pytest.mark.parametrize("lane", ["share_get", "cache_pull"])
def test_pre_existing_dest_tmp_survives(lane: str, s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    build = {"share_get": _share_lane, "cache_pull": _cache_lane}[lane]
    fetch, dest = build(s3, bucket, tmp_path)

    # The user's OWN file, sitting at the path the old default used as its temp.
    scratch = dest.with_name(dest.name + ".tmp")
    scratch.write_bytes(SCRATCH)

    fetch()

    assert dest.read_bytes() == PAYLOAD  # the payload still lands
    assert scratch.read_bytes() == SCRATCH  # ...and the neighbour survives
