"""The contract the `producer` fakes are licensed by — one body, two arms.

Substrate rule 2: a fake earns the right to stand in for a boundary only if
some test runs *unchanged* over both it and the real thing. `StaticFetcher`
(tests/_fakes/producer.py) stood in for the `Fetcher` protocol unlicensed
since slice 5; D-C made that untenable — the md5 drift rule reads producer
DVC pointers through `fetch_path_at`, and a fake that cannot serve one would
take the `pointer is None` branch and assert `drift_unknown` forever, green
while proving nothing about drift.

The license is narrow, like `_FakeDvcOps`'s: it covers the *transport
semantics* of `fetch_metadata_at` / `fetch_path_at` (bytes served at a rev;
`PATH_MISSING` for a file that is not there), not the content of any real
producer's repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mintd.producer import FetchError, GitArchiveFetcher
from tests._fakes.producer import StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"

_FAKE_REPO = "https://example.org/fake-producer"
_FAKE_PIN = "a" * 40


@pytest.fixture(params=["fake", "real"])
def world(request, tmp_path: Path):
    """`(fetcher, repo, pin)` where the producer has published the directory
    output `data/final` and carries the minimal v2 metadata at `pin`."""
    if request.param == "real":
        from tests._harness.producer import build_local_producer

        producer = build_local_producer(tmp_path / "prod")
        producer.publish({"data/final": {"a.csv": b"v1\n"}})
        return GitArchiveFetcher(), producer.url, producer.head_sha

    pointer = (
        "outs:\n"
        f"- md5: {'e' * 32}.dir\n"
        "  size: 3\n"
        "  nfiles: 1\n"
        "  path: final\n"
    ).encode()
    fetcher = StaticFetcher(
        {(_FAKE_REPO, _FAKE_PIN): V2_MINIMAL.read_bytes()},
        path_store={(_FAKE_REPO, _FAKE_PIN, "data/final.dvc"): pointer},
    )
    return fetcher, _FAKE_REPO, _FAKE_PIN


def test_fetch_path_at_serves_a_parseable_pointer(world) -> None:
    """The pointer for a published directory output parses, names the out by
    its basename, and carries a `.dir` manifest hash — exactly what the D-C
    comparator reads."""
    fetcher, repo, pin = world

    raw = fetcher.fetch_path_at(repo, pin, "data/final.dvc")

    doc = yaml.safe_load(raw)
    outs = doc["outs"]
    assert len(outs) == 1
    assert outs[0]["path"] == "final"
    assert outs[0]["md5"].endswith(".dir")


def test_fetch_path_at_missing_file_raises_path_missing(world) -> None:
    """A file that is not at that rev is `PATH_MISSING` — the reason the
    drift walk reads as "definitively absent", distinct from unreachable."""
    fetcher, repo, pin = world

    with pytest.raises(FetchError) as ei:
        fetcher.fetch_path_at(repo, pin, "nope/nothere.dvc")

    assert ei.value.reason == FetchError.Reason.PATH_MISSING


def test_fetch_metadata_at_serves_the_metadata(world) -> None:
    fetcher, repo, pin = world

    raw = fetcher.fetch_metadata_at(repo, pin)

    assert json.loads(raw)["schema_version"] == "2.0"
