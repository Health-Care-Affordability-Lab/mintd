"""Tests for `ProducerView` and `_ProducerCache`."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from mintd.producer import (
    FetchError,
    MissingPrimaryDataProduct,
    ProducerError,
    ProducerView,
    _safe_repo_dirname,
)

from tests._fakes.producer import ErroringFetcher, StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"

REPO = "https://github.com/example-org/provider_xw"
PIN = "a" * 40


def _valid_bytes(
    *,
    primary: str | None = "outputs/main.parquet",
    outputs: list[dict[str, Any]] | None = None,
    schema_version: str = "2.0",
) -> bytes:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["schema_version"] = schema_version
    data["data_products"]["primary"] = primary
    if outputs is not None:
        data["data_products"]["outputs"] = outputs
    return json.dumps(data).encode()


def test_producer_view_at_returns_validated_view(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.repo == REPO
    assert view.pin == PIN
    assert view.metadata.schema_version == "2.0"


def test_producer_view_at_caches_first_fetch(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)
    ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert fetcher.calls == [(REPO, PIN)]


def test_producer_view_at_reads_from_cache_when_present(tmp_path: Path) -> None:
    cache_path = tmp_path / _safe_repo_dirname(REPO) / f"{PIN}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(_valid_bytes())
    fetcher = ErroringFetcher(FetchError.unreachable(REPO, PIN))

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.metadata.schema_version == "2.0"
    assert fetcher.calls == []


def test_producer_view_at_raises_unreachable(tmp_path: Path) -> None:
    fetcher = ErroringFetcher(FetchError.unreachable(REPO, PIN))

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.UNREACHABLE
    assert isinstance(ei.value.__cause__, FetchError)


def test_producer_view_at_raises_pin_missing(tmp_path: Path) -> None:
    fetcher = ErroringFetcher(FetchError.pin_missing(REPO, PIN))

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.PIN_MISSING


def test_producer_view_at_raises_metadata_missing(tmp_path: Path) -> None:
    fetcher = ErroringFetcher(FetchError.metadata_missing(REPO, PIN))

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.METADATA_MISSING


def test_producer_view_at_raises_metadata_invalid_bad_json(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): b"{not-json"})

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.METADATA_INVALID


def test_producer_view_at_raises_metadata_invalid_missing_required_field(
    tmp_path: Path,
) -> None:
    data = json.loads(_valid_bytes())
    del data["project"]
    fetcher = StaticFetcher({(REPO, PIN): json.dumps(data).encode()})

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.METADATA_INVALID


def test_producer_view_at_raises_schema_too_old(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes(schema_version="1.1")})

    with pytest.raises(ProducerError) as ei:
        ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert ei.value.reason == ProducerError.Reason.SCHEMA_TOO_OLD
    assert "1.1" in ei.value.detail


def test_producer_view_try_at_returns_error_object_on_failure(tmp_path: Path) -> None:
    fetcher = ErroringFetcher(FetchError.pin_missing(REPO, PIN))

    result = ProducerView.try_at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert isinstance(result, ProducerError)
    assert result.reason == ProducerError.Reason.PIN_MISSING


def test_producer_view_try_at_returns_view_on_success(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    result = ProducerView.try_at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert isinstance(result, ProducerView)


def test_producer_view_outputs_and_paths(tmp_path: Path) -> None:
    outputs = [
        {"path": "outputs/a.parquet", "description": "", "primary": True, "last_published": ""},
        {"path": "outputs/b.parquet", "description": "", "primary": False, "last_published": ""},
    ]
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes(outputs=outputs)})

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert len(view.outputs()) == 2
    assert view.output_paths() == ["outputs/a.parquet", "outputs/b.parquet"]


def test_producer_view_primary_or_raise_returns_primary(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes(primary="outputs/x.parquet")})

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.primary_or_raise() == "outputs/x.parquet"


def test_producer_view_primary_or_raise_raises_when_missing(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes(primary=None)})

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    with pytest.raises(MissingPrimaryDataProduct):
        view.primary_or_raise()


def test_cache_atomic_write_no_half_files(tmp_path: Path) -> None:
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert list(tmp_path.rglob("*.tmp")) == []


def test_cache_unique_tmp_filename_avoids_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded_srcs: list[Path] = []
    original_replace = os.replace

    def recording_replace(src: Any, dst: Any) -> None:
        recorded_srcs.append(Path(src))
        original_replace(src, dst)

    monkeypatch.setattr("mintd.producer.os.replace", recording_replace)

    data = _valid_bytes()

    def _fetch_once(_: int) -> ProducerView:
        fetcher = StaticFetcher({(REPO, PIN): data})
        # Use a fresh cache_dir per worker so each one writes (skips cache hit).
        # Concurrent processes targeting the *same* (repo,pin) dir is the race
        # the unique-tmp filename prevents.
        return ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(_fetch_once, range(10)))

    assert len(recorded_srcs) >= 1
    assert len(set(recorded_srcs)) == len(recorded_srcs)
    cache_file = tmp_path / _safe_repo_dirname(REPO) / f"{PIN}.json"
    assert cache_file.read_bytes() == data


def test_cache_recovers_from_orphan_tmp_file(tmp_path: Path) -> None:
    repo_dir = tmp_path / _safe_repo_dirname(REPO)
    repo_dir.mkdir(parents=True)
    (repo_dir / f"{PIN}.json.99999.deadbeef.tmp").write_bytes(b"leftover")
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.metadata.schema_version == "2.0"
    assert (repo_dir / f"{PIN}.json").exists()


def test_cache_key_filesystem_safe_short_url(tmp_path: Path) -> None:
    repo_https = "https://github.com/org/name"
    repo_ssh = "git@github.com:org/name.git"
    fetcher_https = StaticFetcher({(repo_https, PIN): _valid_bytes()})
    fetcher_ssh = StaticFetcher({(repo_ssh, PIN): _valid_bytes()})

    ProducerView.at(repo_https, PIN, fetcher=fetcher_https, cache_dir=tmp_path)
    ProducerView.at(repo_ssh, PIN, fetcher=fetcher_ssh, cache_dir=tmp_path)

    dirs = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert len(dirs) == 2
    decoded = {urllib.parse.unquote(d) for d in dirs}
    assert decoded == {repo_https, repo_ssh}


def test_cache_write_oserror_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    original_write = Path.write_bytes

    def failing_write_bytes(self: Path, data: bytes) -> int:
        if self.suffix == ".tmp":
            raise PermissionError("read-only")
        return original_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)
    fetcher = StaticFetcher({(REPO, PIN): _valid_bytes()})

    caplog.set_level(logging.WARNING, logger="mintd.producer")
    view = ProducerView.at(REPO, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.metadata.schema_version == "2.0"
    assert any("producer cache write failed" in r.message for r in caplog.records)


def test_safe_repo_dirname_truncates_long_url() -> None:
    long_repo = "https://" + "x" * 1000 + ".example.com/org/name"

    result = _safe_repo_dirname(long_repo)

    assert len(result.encode("utf-8")) <= 200
    digest = hashlib.sha256(long_repo.encode("utf-8")).hexdigest()[:16]
    assert result.endswith(f"_{digest}")

    other = "https://" + "x" * 1000 + ".example.com/org/Name"
    assert _safe_repo_dirname(other) != result


def test_safe_repo_dirname_short_url_is_pure_quote() -> None:
    repo = "https://github.com/org/name"

    result = _safe_repo_dirname(repo)

    assert result == urllib.parse.quote(repo, safe="")


def test_cache_long_repo_url_writes_successfully(tmp_path: Path) -> None:
    long_repo = "https://" + "x" * 500 + ".example.com/org/name"
    fetcher = StaticFetcher({(long_repo, PIN): _valid_bytes()})

    view = ProducerView.at(long_repo, PIN, fetcher=fetcher, cache_dir=tmp_path)

    assert view.metadata.schema_version == "2.0"
    sub = tmp_path / _safe_repo_dirname(long_repo)
    assert sub.is_dir()
    assert len(sub.name.encode("utf-8")) <= 200
    assert (sub / f"{PIN}.json").exists()


def test_try_at_resolves_the_empty_pin_to_head(local_producer, tmp_path) -> None:
    """``pin=""`` means HEAD, against a real producer and not just a double.

    `_consumer_findings` (`src/mintd/check.py:328`) asks "where is the producer
    now?" as ``try_at(repo, "")``. Until 1b that sentinel was honoured by test
    doubles only: the real implementation handed the empty string to git as a
    revision, which fails with ``path 'metadata.json' does not exist``, and
    `check.py:329-331` reads *any* HEAD failure as "cannot reach HEAD, degrade
    to up-to-date". The two failures compounded into a silent, total one —
    against a real producer `mintd check` called every stale import current and
    `mintd data import <name> --bump` could never fire.

    It stayed invisible because all 48 `producer_view_factory` injection sites
    in this suite supply a fake that special-cases ``""``, so no test had ever
    driven the real path. That is the gap `tests/_harness/` closes, and this is
    the unit-level pin for it.

    Mutation that must redden this: restore the unconditional
    ``cls.at(repo, pin, ...)`` in `ProducerView.try_at`.
    """
    local_producer.commit_more("advance past the seed commit")

    result = ProducerView.try_at(
        local_producer.url, "", cache_dir=tmp_path / "pcache"
    )

    assert not isinstance(result, ProducerError), getattr(result, "detail", result)
    assert result.pin == local_producer.head_sha


def _catalog_for(url: str):
    """A catalog whose `provider-xw` entry points at `url`."""
    import json

    from mintd.catalog import InMemoryCatalogClient
    from mintd.model import Metadata

    fixtures = Path(__file__).parent / "fixtures"
    data = json.loads((fixtures / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = url
    client = InMemoryCatalogClient()
    client.register(Metadata.model_validate(data))
    return client


def test_an_empty_manifest_pin_is_refused_not_resolved_to_head(
    local_producer, tmp_path
) -> None:
    """A hand-edited `pin: ''` must fail loudly, not silently mean HEAD.

    `try_at(repo, "")` resolves HEAD — that is the sentinel `_consumer_findings`
    uses to ask "where is the producer now?". `ApprovedProduct.pin` is a bare
    `str` with no validator, and `mintd enclave add <repo> --pin=""` (say,
    `--pin="$SHA"` with `SHA` unset) writes an empty one. Without the guard at
    `check.py`, the two meanings collide and an enclave pinned at nothing
    reports a clean "up to date" while `enclave_bump` silently no-ops.

    Caught in review of the `""`-means-HEAD fix itself — the fix that made
    drift detection work at all also widened `""` past its intended caller.
    """
    from mintd.check import _consumer_findings_from_enclave_manifest

    manifest = tmp_path / "enclave_manifest.yaml"
    manifest.write_text(
        "schema_version: '2.0'\n"
        "enclave_name: probe\n"
        "approved_products:\n"
        "  - repo: provider-xw\n"
        "    registry_entry: catalog/data/provider-xw.yaml\n"
        "    pin: ''\n"
        "    source_path: data/final/\n"
        "downloaded: []\n"
        "transferred: []\n",
        encoding="utf-8",
    )

    findings = _consumer_findings_from_enclave_manifest(
        tmp_path,
        upgrades=True,
        producer_view_factory=None,
        client=_catalog_for(local_producer.url),
    )

    kinds = {f.kind for f in findings if f.section == "consumer"}
    assert "up_to_date" not in kinds, (
        "an empty pin resolved to HEAD and reported clean — the HEAD sentinel "
        "swallowed user data"
    )
    assert "pin_missing" in kinds


def test_an_empty_dvc_rev_lock_is_refused_not_resolved_to_head(
    local_producer, tmp_path
) -> None:
    """The same guard, on the `.dvc` import lane.

    Sibling of `test_an_empty_manifest_pin_is_refused_not_resolved_to_head`.
    The first cut of that guard was written only on the enclave-manifest
    branch, so `_consumer_findings_from_dvc` still let an empty `rev_lock`
    through — and the `""`-means-HEAD fix then made that WORSE than before it:

        before the fix   `[warning] producer unreachable: ...`
        after the fix    `✓ up to date`

    because both `factory(...)` calls resolved HEAD, the two views matched, and
    `_drift_finding` saw no drift. A warning became a green tick on an import
    pinned at nothing, and `data_bump` (data.py:568, gates on kind) went from
    blocked to a silent no-op.

    Found in review round 3. Filed here rather than beside the enclave test
    because the lesson is the pairing: a guard on one of two sibling branches
    is not a guard.
    """
    from mintd.check import _consumer_findings_from_dvc

    project = tmp_path / "consumer"
    (project / "data" / "imports").mkdir(parents=True)
    (project / "data" / "imports" / "thing.dvc").write_text(
        "outs:\n"
        f"  - md5: {'c' * 32}\n"
        "    size: 12345\n"
        "    path: final\n"
        "deps:\n"
        "  - path: outputs/final/\n"
        "    repo:\n"
        f"      url: {local_producer.url}\n"
        "      rev: main\n"
        "      rev_lock: ''\n",
        encoding="utf-8",
    )

    findings = _consumer_findings_from_dvc(
        project, upgrades=True, producer_view_factory=None
    )

    kinds = {f.kind for f in findings if f.section == "consumer"}
    assert "up_to_date" not in kinds, (
        "an empty rev_lock resolved to HEAD and reported clean — the HEAD "
        "sentinel swallowed user data on the .dvc lane"
    )
    assert "pin_missing" in kinds
