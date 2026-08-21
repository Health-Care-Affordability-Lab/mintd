from __future__ import annotations

import pytest
from pathlib import Path
from mintd._dvc_ops import DvcPullError
from mintd._fast_sync_ops import EMPTY_DIR_MD5, DvcFileEntry, DvcOut, cache_path_for
from mintd.model import FastPullResult
from mintd.data_ops import _out_aggregate_bytes, data_add, data_pull, data_push, data_remove, data_verify
from tests._fakes.dvc_ops import DvcPullCall, _FakeDvcOps
from tests._fakes.fast_sync_ops import _FakeFastSyncOps
from tests._fakes.reporter import RecordingReporter


def test_data_pull_default_calls_dvc_ops_pull(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_pull(tmp_path, dvc_ops=fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0] == DvcPullCall(targets=None, remote=None, jobs=None)


def test_data_pull_with_targets_passes_them(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_pull(tmp_path, targets=["data/a", "data/b"], dvc_ops=fake)
    assert fake.pull_calls[0].targets == ["data/a", "data/b"]


def test_data_pull_fast_sync_success_runs_dvc_checkout(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, fallback_targets=[])
    data_pull(tmp_path, targets=["a"], dvc_ops=fake, fast_sync_ops=fast_fake)
    assert fake.pull_calls == []
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["a"]


def test_data_pull_fast_sync_partial_runs_dvc_pull_on_fallback(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    # 2 files, 1 success (fast), 1 fallback
    fast_fake.result = FastPullResult(
        success=False,
        synced_count=1,
        fallback_targets=["data/raw.csv"],
    )
    targets = ["data/fast.csv", "data/raw.csv"]
    data_pull(tmp_path, targets=targets, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/raw.csv"]
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["data/fast.csv"]


def test_data_pull_fast_sync_raises_falls_back(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom")
    data_pull(tmp_path, targets=["a"], dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["a"]


def test_data_pull_no_targets_discovers_and_routes_through_fast_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 26: when targets=None, data_pull discovers .dvc files and
    routes through fast-sync (was: skipped fast-sync, hit DVC cache-write
    bug for version_aware buckets)."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["a.dvc", "b.dvc"],
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    # Fast-sync invoked with the discovered targets.
    assert len(fast_fake.calls) == 1
    assert fast_fake.calls[0].targets == ["a.dvc", "b.dvc"]
    # Default fast-fake returns success=False, fallback_targets=[] —
    # meaning no fallback dvc pull and no checkout to do.
    assert fake.pull_calls == []


def test_data_pull_no_targets_empty_repo_returns_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty discovery → no fast-sync, no dvc pull, just log + return."""
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert fast_fake.calls == []
    assert fake.pull_calls == []


def test_data_pull_no_targets_no_fast_sync_falls_through_to_dvc_pull(
    tmp_path: Path,
) -> None:
    """When fast_sync_ops is None, route directly to dvc pull (unchanged
    behavior for the no-fast-sync case)."""
    fake = _FakeDvcOps()
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=None)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets is None


def test_data_push_calls_dvc_ops_push(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_push(tmp_path, dvc_ops=fake)
    assert len(fake.push_calls) == 1


def test_data_push_with_remote_passes_it(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_push(tmp_path, dvc_ops=fake, remote="origin")
    assert fake.push_calls[0].remote == "origin"


def test_data_push_with_targets_passes_them(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_push(tmp_path, targets=["data/a.dvc"], dvc_ops=fake)
    assert fake.push_calls[0].targets == ["data/a.dvc"]


def test_data_push_default_targets_none(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_push(tmp_path, dvc_ops=fake)
    assert fake.push_calls[0].targets is None


def test_data_add_returns_dvc_path(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    path = tmp_path / "raw.csv"
    path.write_text("data")
    produced = data_add(path, dvc_ops=fake)
    assert produced == tmp_path / "raw.csv.dvc"
    assert (tmp_path / "raw.csv.dvc").exists()


def test_data_verify_returns_status_map(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fake.status_result = {"a.csv": "clean", "b.csv": "dirty"}
    status_map = data_verify(tmp_path, dvc_ops=fake)
    assert status_map == {"a.csv": "clean", "b.csv": "dirty"}


def test_data_verify_with_targets_filters(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_verify(tmp_path, targets=["a.csv"], dvc_ops=fake)
    assert fake.status_calls[0].targets == ["a.csv"]


def test_data_remove_calls_dvc_ops_remove(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    data_remove("raw.csv.dvc", dvc_ops=fake)
    assert fake.remove_calls[0].name == "raw.csv.dvc"


def test_data_pull_propagates_dvc_pull_error(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fake.pull_raises = DvcPullError("nope")
    with pytest.raises(DvcPullError, match="nope"):
        data_pull(tmp_path, dvc_ops=fake)


def test_data_pull_all_fell_back_skips_checkout(tmp_path: Path) -> None:
    """When fast-sync handled nothing, data_pull must NOT call dvc_ops.checkout
    (otherwise it would try to materialize uncached targets and crash). Only
    dvc_ops.pull runs, for every target."""
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(
        success=False, synced_count=0, fallback_targets=["A", "B"]
    )
    data_pull(tmp_path, targets=["A", "B"], dvc_ops=fake, fast_sync_ops=fast_fake)
    assert fake.checkout_calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["A", "B"]


def test_data_pull_partial_pull_failure_still_keeps_synced_checkout(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(
        success=False, synced_count=1, fallback_targets=["B"]
    )
    fake.pull_raises = DvcPullError("network")
    with pytest.raises(DvcPullError, match="network"):
        data_pull(tmp_path, targets=["A", "B"], dvc_ops=fake, fast_sync_ops=fast_fake)
    # checkout MUST have been called for the synced target before the pull blew up.
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["A"]


def test_data_pull_pull_all_skips_catch_all_when_dvc_yaml_has_no_lock_outs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SLICE-42: the catch-all triggers on uncovered ``dvc.lock`` stage outs,
    NOT on ``dvc.yaml`` existence. A ``dvc.yaml`` with no ``dvc.lock`` (so no
    resolved stage outs) must NOT trigger a ``dvc pull(targets=None)`` that
    would re-pull everything fast-sync already handled."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["a.dvc"],
    )
    (tmp_path / "dvc.yaml").write_text("stages:\n  foo:\n    cmd: x\n", encoding="utf-8")
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=1, fallback_targets=[])
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    # Fast-sync runs on the discovered .dvc list...
    assert fast_fake.calls[0].targets == ["a.dvc"]
    # ...and NO catch-all dvc pull runs (no uncovered stage outs).
    assert fake.pull_calls == []


def test_data_pull_pull_all_skips_dvc_yaml_catch_when_no_dvc_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dvc.yaml present → no catch-all dvc pull. Avoids re-pulling
    everything that fast-sync already handled."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["a.dvc"],
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    # No dvc.yaml → no extra catch-all dvc pull.
    pull_calls_without_targets = [c for c in fake.pull_calls if c.targets is None]
    assert pull_calls_without_targets == []


def test_data_pull_pull_all_with_only_empty_dvc_yaml_skips_catch_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo with an empty/stages-less dvc.yaml and no dvc.lock: fast-sync has
    nothing to do AND there are no uncovered stage outs, so the catch-all dvc
    pull is skipped (no targets=None re-pull)."""
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    (tmp_path / "dvc.yaml").write_text("stages: {}\n", encoding="utf-8")
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    # No fast-sync call (nothing to sync) and no uncovered outs → no dvc pull.
    assert fast_fake.calls == []
    assert fake.pull_calls == []


def test_data_pull_fast_sync_raises_pull_all_falls_back_to_scoped_dvc_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pull-all was requested and fast-sync raises, the fallback
    dvc pull is SCOPED to the discovered .dvc targets plus dvc.lock stage
    outs — never targets=None, which would re-validate version-aware outs
    through plain dvc pull's rehash-on-pull pathology (pull-all audit)."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["a.dvc", "b.dvc"],
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom")
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["a.dvc", "b.dvc"]


def test_data_pull_fast_sync_raises_pull_all_includes_stage_outs_in_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull-all + fast-sync raises: dvc.lock stage outs (all_pipeline —
    including non-fast-syncable ones) join the scoped fallback pull, so
    dropping targets=None doesn't silently drop pipeline stages."""
    stage_out = DvcOut(
        target="data/final/b.parquet", path="data/final/b.parquet",
        md5="b" * 32, is_dir=False, version_id=None,
    )
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: ["a.dvc"])
    monkeypatch.setattr(
        "mintd.data_ops.partition_pipeline_outs",
        lambda _p, _r: ([], [stage_out]),
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom")
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["a.dvc", "data/final/b.parquet"]


def test_data_pull_fast_sync_handles_pipeline_only_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline-only project (no .dvc files): fast-sync receives the 6
    version-aware pipeline outs discovered from dvc.lock and dvc_ops.checkout
    materializes them. SLICE-42: because every stage out is fast-syncable
    there are NO uncovered outs, so the catch-all dvc pull is skipped — it must
    NOT re-pull (targets=None) what fast-sync already cached. This is the
    regression behind the data_mergerbuild hang."""
    import shutil
    fixture = Path("tests/fixtures/pipeline_project")
    shutil.copytree(fixture, tmp_path / "project")
    project = tmp_path / "project"

    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])

    fake, fast_fake = _synced_fakes(project, synced_count=6)
    data_pull(project, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    assert len(fast_fake.calls) == 1
    assert fast_fake.calls[0].pipeline_outs is not None
    assert len(fast_fake.calls[0].pipeline_outs) == 6

    assert len(fake.checkout_calls) == 1
    checkout_targets = fake.checkout_calls[0].targets or []
    assert all(t.startswith("data/final/") for t in checkout_targets)
    assert len(checkout_targets) == 6

    # All 6 stage outs were fast-synced → no uncovered outs → no catch-all pull.
    assert fake.pull_calls == []


def test_data_pull_fast_sync_handles_mixed_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed project: one .dvc file AND the pipeline fixture's dvc.lock.
    Both groups reach fast-sync and both land in the checkout set. (The
    per-call argv grouping — all-.dvc vs all-bare, never mixed — is owned
    by test_data_pull_checkout_never_mixes_dvc_and_stage_out_targets.)"""
    import shutil
    fixture = Path("tests/fixtures/pipeline_project")
    shutil.copytree(fixture, tmp_path / "project")
    project = tmp_path / "project"

    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: ["a.dvc"])

    fake, fast_fake = _synced_fakes(project, synced_count=7)
    data_pull(project, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    assert len(fast_fake.calls) == 1
    assert fast_fake.calls[0].targets == ["a.dvc"]
    assert fast_fake.calls[0].pipeline_outs is not None
    assert len(fast_fake.calls[0].pipeline_outs) == 6

    checked_out = {t for c in fake.checkout_calls for t in (c.targets or [])}
    assert "a.dvc" in checked_out
    assert sum(1 for t in checked_out if t.startswith("data/final/")) == 6


# ---------- regression: total_bytes from files-format .dvc targets ----------


def test_out_aggregate_bytes_sums_files_for_files_format() -> None:
    """Files-format dir-outs: top-level out.size is the manifest size,
    not the aggregate. Aggregate must sum per-file sizes — otherwise the
    progress bar undershoots actual bytes-on-the-wire (mergerbuild
    bug, ~60 GB pulled but bar showed a small fraction)."""
    out = DvcOut(
        target="data/big",
        path="data/big",
        md5="",
        is_dir=True,
        is_files_format=True,
        size=128,  # manifest size, not aggregate
        files=[
            DvcFileEntry("aaa", "a.parquet", size=20_000_000),
            DvcFileEntry("bbb", "b.parquet", size=30_000_000),
            DvcFileEntry("ccc", "c.parquet", size=10_000_000),
        ],
    )
    assert _out_aggregate_bytes(out) == 60_000_000


def test_out_aggregate_bytes_uses_size_for_single_file_out() -> None:
    out = DvcOut(target="data/x", path="data/x", md5="m", is_dir=False, size=4096)
    assert _out_aggregate_bytes(out) == 4096


def test_out_aggregate_bytes_uses_size_for_md5_dir_out() -> None:
    """md5-keyed dirs (non-files-format) carry the aggregate in out.size
    directly — DvcOut.size is the sum from the manifest, not just the
    manifest's own bytes."""
    out = DvcOut(
        target="data/md5dir",
        path="data/md5dir",
        md5="aaa.dir",
        is_dir=True,
        is_files_format=False,
        size=12_345,
    )
    assert _out_aggregate_bytes(out) == 12_345


def test_data_pull_total_bytes_sums_files_format_for_dvc_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``data_pull`` summed ``out.size`` for .dvc targets
    even when the out was files-format — DVC's top-level ``size:`` for
    those is just the manifest bytes (slice 27). PullSummary.total_bytes
    must reflect the aggregate (sum of files[].size), matching how
    pipeline_outs are already summed."""
    project = tmp_path
    # Files-format .dvc with three per-file entries:
    (project / "data").mkdir()
    dvc_file = project / "data" / "big.dvc"
    dvc_file.write_text(
        "outs:\n"
        "  - path: big\n"
        "    size: 128\n"  # manifest size — should NOT be the total
        "    files:\n"
        "      - relpath: a.parquet\n        md5: a\n        size: 20000000\n"
        "        cloud:\n          origin:\n            version_id: v1\n"
        "      - relpath: b.parquet\n        md5: b\n        size: 30000000\n"
        "        cloud:\n          origin:\n            version_id: v2\n"
        "      - relpath: c.parquet\n        md5: c\n        size: 10000000\n"
        "        cloud:\n          origin:\n            version_id: v3\n"
    )

    monkeypatch.setattr("mintd.data_ops.partition_pipeline_outs", lambda _p, _r: ([], []))

    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=1, fallback_targets=[])

    summary = data_pull(
        project,
        targets=["data/big.dvc"],
        dvc_ops=fake,
        fast_sync_ops=fast_fake,
    )

    assert summary.total_bytes == 60_000_000, (
        f"expected sum of per-file sizes (60M), got {summary.total_bytes} "
        "— files-format dir-outs from .dvc targets must aggregate via files[].size"
    )


@pytest.mark.parametrize("raw_target", ["data/foo.csv/", ".\\data\\foo.csv"])
def test_data_pull_total_bytes_counts_denormalized_targets(
    raw_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the total-bytes pre-pass used the raw target string
    while classify_targets normalizes, so a trailing-slash or backslash
    target fast-synced fine but the progress total (and
    PullSummary.total_bytes) reported 0."""
    project = tmp_path
    (project / "data").mkdir()
    (project / "data" / "foo.csv.dvc").write_text(
        "outs:\n  - path: foo.csv\n    md5: cafe\n    size: 1234\n"
    )

    monkeypatch.setattr("mintd.data_ops.partition_pipeline_outs", lambda _p, _r: ([], []))

    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=1, fallback_targets=[])

    summary = data_pull(
        project,
        targets=[raw_target],
        dvc_ops=fake,
        fast_sync_ops=fast_fake,
    )

    assert summary.total_bytes == 1234


# ---------- SLICE-42: catch-all dvc pull scoped to uncovered stage outs ----------


def test_data_pull_skips_catch_all_when_no_dvc_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """data_mergerbuild regression: a stages-less dvc.yaml (vars/docs only),
    NO dvc.lock, and version-aware .dvc files handled by fast-sync. The
    catch-all must be skipped entirely — never dvc pull(targets=None) — so the
    248 already-fast-synced outs aren't re-pulled (the multi-GB hang)."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["temp/a.csv.dvc", "temp/b.dta.dvc"],
    )
    (tmp_path / "dvc.yaml").write_text("vars:\n  - dvc_vars.yaml\n", encoding="utf-8")
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=2, fallback_targets=[])
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    # Fast-sync handled the .dvc files; checkout materialized them.
    assert fast_fake.calls[0].targets == ["temp/a.csv.dvc", "temp/b.dta.dvc"]
    assert fake.checkout_calls[0].targets == ["temp/a.csv.dvc", "temp/b.dta.dvc"]
    # No dvc.lock → no uncovered stage outs → NO catch-all pull at all.
    assert fake.pull_calls == []


def test_data_pull_catch_all_pulls_only_uncovered_pipeline_outs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline product with one version-aware stage out (fast-synced) and
    one without a cloud version_id: the catch-all dvc pull fires for ONLY the
    uncovered target, never targets=None and never the covered one."""
    covered = DvcOut(
        target="data/final/a.parquet", path="data/final/a.parquet",
        md5="a", is_dir=False, version_id="v1",
    )
    uncovered = DvcOut(
        target="data/final/b.parquet", path="data/final/b.parquet",
        md5="b", is_dir=False, version_id=None,
    )
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    monkeypatch.setattr(
        "mintd.data_ops.partition_pipeline_outs",
        lambda _p, _r: ([covered], [covered, uncovered]),
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=1, fallback_targets=[])
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/final/b.parquet"]


def test_data_pull_catch_all_excludes_fallback_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-aware stage out routed to result.fallback_targets is pulled
    once (the fallback pull at L185), and the catch-all must NOT pull it
    again — it's already in `covered`, so `uncovered` is empty."""
    out = DvcOut(
        target="data/final/a.parquet", path="data/final/a.parquet",
        md5="a", is_dir=False, version_id="v1",
    )
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    monkeypatch.setattr(
        "mintd.data_ops.partition_pipeline_outs",
        lambda _p, _r: ([out], [out]),
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(
        success=False, synced_count=0, fallback_targets=["data/final/a.parquet"],
    )
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    # Pulled exactly once — the fallback pull — never a second catch-all pull.
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/final/a.parquet"]


# ---------- degraded pulls: checkout what fast-sync already cached ----------


_MD5_CACHED = "aa" + "0" * 30
_MD5_MISSING = "bb" + "0" * 30
_FF_BLOB_A = "1" * 32
_FF_BLOB_B = "2" * 32


def _write_single_file_dvc(project: Path, rel: str, md5: str) -> str:
    """Write a minimal single-file .dvc for ``rel``; return its target string."""
    dvc = project / f"{rel}.dvc"
    dvc.parent.mkdir(parents=True, exist_ok=True)
    dvc.write_text(
        f"outs:\n  - path: {Path(rel).name}\n    md5: {md5}\n    size: 4\n"
    )
    return f"{rel}.dvc"


def _write_files_format_dvc(
    project: Path, rel: str, entries: list[tuple[str, str]]
) -> str:
    """Write a files-format dir-out .dvc (per-file cloud version_ids, no
    top-level md5 — the slice 27 shape); return its target string."""
    lines = [
        "outs:",
        f"  - path: {Path(rel).name}",
        "    size: 128",
        "    files:",
    ]
    for i, (relpath, md5) in enumerate(entries, start=1):
        lines += [
            f"      - relpath: {relpath}",
            f"        md5: {md5}",
            "        size: 4",
            "        cloud:",
            "          origin:",
            f"            version_id: v{i}",
        ]
    dvc = project / f"{rel}.dvc"
    dvc.parent.mkdir(parents=True, exist_ok=True)
    dvc.write_text("\n".join(lines) + "\n")
    return f"{rel}.dvc"


def _seed_cache(project: Path, md5: str, content: bytes = b"data") -> None:
    """Drop a blob into the project's DVC cache at DVC's files/md5 layout."""
    blob = cache_path_for(project / ".dvc" / "cache", md5)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(content)


def _two_target_project(project: Path) -> tuple[str, str]:
    """One .dvc whose blob is in cache, one whose blob is not."""
    cached = _write_single_file_dvc(project, "data/cached.csv", _MD5_CACHED)
    missing = _write_single_file_dvc(project, "data/missing.csv", _MD5_MISSING)
    _seed_cache(project, _MD5_CACHED)
    return cached, missing


def _degraded_fakes(**result_kwargs) -> tuple[_FakeDvcOps, _FakeFastSyncOps]:
    """The standard degraded-pull fixture pair: a recording DvcOps fake plus
    a fast-sync fake returning a failed FastPullResult built from
    ``result_kwargs``."""
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=False, **result_kwargs)
    return fake, fast_fake


def _synced_fakes(
    workspace: Path, synced_count: int
) -> tuple[_FakeDvcOps, _FakeFastSyncOps]:
    """The healthy fast-sync fixture pair: a DvcOps fake whose checkout
    materializes into ``workspace``, plus a fast-sync fake reporting
    ``synced_count`` synced targets and no fallback. Tests flip exactly one
    knob on top (e.g. ``fake.checkout_materializes = False``) so the axis
    under test stays visible."""
    fake = _FakeDvcOps()
    fake.workspace = workspace
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(
        success=True, synced_count=synced_count, fallback_targets=[],
    )
    return fake, fast_fake


def _crashing_fakes(workspace: Path) -> tuple[_FakeDvcOps, _FakeFastSyncOps]:
    """The crash-branch fixture pair: fast-sync raises mid-run; the DvcOps
    fake still materializes recovery checkouts into ``workspace``."""
    fake = _FakeDvcOps()
    fake.workspace = workspace
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom mid-sync")
    return fake, fast_fake


def _degrade(fast_fake: _FakeFastSyncOps, mode: str, fallback: list[str]) -> None:
    """Configure HOW fast-sync degrades — the axis the parametrized tests
    below share. ``raises*`` crashes try_fast_pull mid-run; ``all_fallback``
    returns the guard-fired shape (every target in fallback_targets). The
    two branches must keep identical checkout/pull behavior."""
    if mode.startswith("raises"):
        fast_fake.raises = RuntimeError("boom mid-sync")
    else:
        fast_fake.result = FastPullResult(
            success=False, synced_count=0, fallback_targets=fallback,
        )


@pytest.mark.parametrize("degrade", ["raises", "all_fallback"])
def test_data_pull_degraded_checks_out_cached_before_fallback_pull(
    degrade: str, tmp_path: Path,
) -> None:
    """Both degraded branches: a target whose blobs fast-sync (or a prior
    run) already cached is checked out BEFORE the fallback pull runs —
    proven by making the pull raise and asserting the checkout already
    happened. A hanging/crashing dvc pull must not leave a fresh clone
    with zero workspace data."""
    cached, missing = _two_target_project(tmp_path)
    fake = _FakeDvcOps()
    fake.pull_raises = DvcPullError("network")
    fast_fake = _FakeFastSyncOps()
    _degrade(fast_fake, degrade, [cached, missing])
    with pytest.raises(DvcPullError, match="network"):
        data_pull(
            tmp_path, targets=[cached, missing], dvc_ops=fake, fast_sync_ops=fast_fake,
        )
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == [cached]


@pytest.mark.parametrize("degrade", ["raises", "raises_pull_all", "all_fallback"])
def test_data_pull_degraded_pull_excludes_cached_targets(
    degrade: str, tmp_path: Path,
) -> None:
    """Both degraded branches, targeted and pull-all: the fully-cached
    target is materialized via checkout and the fallback pull covers ONLY
    the uncached rest — never an already-cached out, and never targets=None
    (a blanket pull would re-validate the just-checked-out version-aware
    outs through plain dvc pull)."""
    cached, missing = _two_target_project(tmp_path)
    fake = _FakeDvcOps()
    fake.workspace = tmp_path
    fast_fake = _FakeFastSyncOps()
    _degrade(fast_fake, degrade, [cached, missing])
    targets = None if degrade == "raises_pull_all" else [cached, missing]
    data_pull(tmp_path, targets=targets, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == [cached]
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == [missing]


@pytest.mark.parametrize("degrade", ["raises", "all_fallback"])
def test_data_pull_degraded_nothing_cached_behaves_as_before(
    degrade: str, tmp_path: Path,
) -> None:
    """Empty cache: no checkout at all, one fallback pull with the full
    target list — identical to the pre-audit behavior on both branches."""
    a = _write_single_file_dvc(tmp_path, "data/a.csv", _MD5_CACHED)
    b = _write_single_file_dvc(tmp_path, "data/b.csv", _MD5_MISSING)
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    _degrade(fast_fake, degrade, [a, b])
    data_pull(tmp_path, targets=[a, b], dvc_ops=fake, fast_sync_ops=fast_fake)
    assert fake.checkout_calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == [a, b]


@pytest.mark.parametrize(
    ("seeded_blobs", "fully_cached"),
    [
        pytest.param([_FF_BLOB_A, _FF_BLOB_B], True, id="fully-cached"),
        pytest.param([_FF_BLOB_A], False, id="partially-cached"),
    ],
)
def test_data_pull_all_fallback_files_format_cache_probe(
    seeded_blobs: list[str], fully_cached: bool, tmp_path: Path,
) -> None:
    """Files-format dir-out on the fallback route. With EVERY per-file blob
    in cache: checked out, excluded from the fallback pull, and the
    synthetic .dir manifest is written so dvc checkout can materialize the
    directory (slice 27 layout). With only SOME blobs cached: not
    verifiably cached, so no checkout and it stays on the fallback pull."""
    target = _write_files_format_dvc(
        tmp_path, "data/big", [("a.parquet", _FF_BLOB_A), ("b.parquet", _FF_BLOB_B)],
    )
    for md5 in seeded_blobs:
        _seed_cache(tmp_path, md5)
    fake, fast_fake = _degraded_fakes(synced_count=0, fallback_targets=[target])
    fake.workspace = tmp_path
    summary = data_pull(tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake)
    if fully_cached:
        # Exactly ONE checkout: the verify probe must accept the
        # materialized files-format DIRECTORY (is_dir=False in the parsed
        # out — no top-level md5) without a single-target retry or error.
        assert [c.targets for c in fake.checkout_calls] == [[target]]
        assert fake.pull_calls == []
        assert summary.error_count == 0
        manifests = list((tmp_path / ".dvc" / "cache" / "files" / "md5").rglob("*.dir"))
        assert len(manifests) == 1
    else:
        assert fake.checkout_calls == []
        assert fake.pull_calls[0].targets == [target]


def test_data_pull_all_fallback_md5_dir_out_fully_cached_checked_out(
    tmp_path: Path,
) -> None:
    """md5-keyed dir-out: cached .dir manifest whose every entry blob is in
    cache counts as verifiably cached — checked out, not pulled."""
    entry_md5 = "3" * 32
    manifest = f'[{{"md5": "{entry_md5}", "relpath": "f.csv"}}]'
    dir_md5 = "4" * 30 + "ab.dir"
    _seed_cache(tmp_path, dir_md5, manifest.encode())
    _seed_cache(tmp_path, entry_md5)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "dir.dvc").write_text(
        f"outs:\n  - path: dir\n    md5: {dir_md5}\n    size: 4\n"
    )
    fake, fast_fake = _degraded_fakes(synced_count=0, fallback_targets=["data/dir.dvc"])
    fake.workspace = tmp_path
    data_pull(tmp_path, targets=["data/dir.dvc"], dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["data/dir.dvc"]
    assert fake.pull_calls == []


def test_data_pull_all_fallback_import_stays_on_dvc_pull_even_when_cached(
    tmp_path: Path,
) -> None:
    """Slice 29 contract: a dvc-import target routes to plain dvc pull even
    if its pinned blob happens to be in the local cache — imports are never
    short-circuited by the cache probe."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "imp.csv.dvc").write_text(
        "outs:\n"
        f"  - path: imp.csv\n    md5: {_MD5_CACHED}\n    size: 4\n"
        "deps:\n"
        "  - path: imp.csv\n"
        "    repo:\n"
        "      url: git@example.com:lab/source.git\n"
        "      rev_lock: " + "c" * 40 + "\n"
    )
    _seed_cache(tmp_path, _MD5_CACHED)
    fake, fast_fake = _degraded_fakes(synced_count=0, fallback_targets=["data/imp.csv.dvc"])
    data_pull(
        tmp_path, targets=["data/imp.csv.dvc"], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert fake.checkout_calls == []
    assert fake.pull_calls[0].targets == ["data/imp.csv.dvc"]


def test_data_pull_fast_sync_raises_pull_all_checks_out_cached_pipeline_outs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) pull-all with pipeline outs (dvc.lock stage outs): a fully-cached
    pipeline out is checked out by target id, and — everything being cached —
    the fallback pull is skipped entirely (pull-all audit: no blanket
    targets=None re-pull of what checkout just materialized)."""
    pipe_md5 = "5" * 32
    pipe_out = DvcOut(
        target="data/final/a.parquet", path="data/final/a.parquet",
        md5=pipe_md5, is_dir=False, version_id="v1",
    )
    _seed_cache(tmp_path, pipe_md5)
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    monkeypatch.setattr(
        "mintd.data_ops.partition_pipeline_outs",
        lambda _p, _r: ([pipe_out], [pipe_out]),
    )
    fake = _FakeDvcOps()
    fake.workspace = tmp_path
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom mid-sync")
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["data/final/a.parquet"]
    assert fake.pull_calls == []


def test_data_pull_catch_all_only_uncovered_no_fast_sync_counts_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .dvc files and no fast-syncable stage outs → fast-sync never runs
    (result is None). The catch-all pulls the uncovered out, and targets_pulled
    must equal exactly that count — not double-count. (`targets` from
    discover_all_outs are .dvc files, disjoint from pipeline out paths, and
    here empty.)"""
    uncovered = DvcOut(
        target="data/final/b.parquet", path="data/final/b.parquet",
        md5="b", is_dir=False, version_id=None,
    )
    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: [])
    monkeypatch.setattr(
        "mintd.data_ops.partition_pipeline_outs",
        lambda _p, _r: ([], [uncovered]),
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    summary = data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    # Fast-sync skipped (nothing fast-syncable); catch-all pulls the one out.
    assert fast_fake.calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/final/b.parquet"]
    assert summary.targets_pulled == 1


# ---------- fail-loudly contract for version-aware outs ----------

def test_data_pull_blocked_targets_reported_loudly_not_pulled_not_checked_out(
    tmp_path: Path,
) -> None:
    """A version-aware target fast-sync could not serve is reported via
    reporter.error naming the target + reason, with the targeted-retry
    hint; it is NEITHER checked out (nothing verified in cache) NOR fed to
    plain dvc pull; PullSummary.error_count drives the CLI's non-zero exit."""
    fake, fast_fake = _degraded_fakes(
        synced_count=1,
        fallback_targets=["data/imported"],
        blocked_targets=["data/versioned"],
        blocked_reasons={"data/versioned": "bucket versioning probe failed: 503"},
        reason="partial fallback",
    )
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path,
        targets=["data/ok", "data/imported", "data/versioned"],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "data/versioned" in msg
    assert "bucket versioning probe failed: 503" in msg
    assert hint == "retry just this target: mintd data pull data/versioned"
    # Errored target excluded from checkout AND from the fallback pull.
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == ["data/ok"]
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/imported"]


def test_data_pull_guard_mixed_imports_fall_back_version_aware_errors(
    tmp_path: Path,
) -> None:
    """Guard-fired all-fallback result with mixed shapes — the
    import keeps the plain dvc pull route, the version-aware target errors
    loudly, and (nothing cached) no checkout runs."""
    fake, fast_fake = _degraded_fakes(
        synced_count=0,
        fallback_targets=["data/imports/a"],
        blocked_targets=["data/versioned"],
        blocked_reasons={"data/versioned": "dvc version mismatch"},
        reason="dvc version mismatch",
    )
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path,
        targets=["data/imports/a", "data/versioned"],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 1
    assert fake.checkout_calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/imports/a"]
    errors = rep.events_of("error")
    assert len(errors) == 1
    assert "data/versioned" in errors[0][1]
    assert "dvc version mismatch" in errors[0][1]


def test_data_pull_imports_only_guard_full_fallback_no_errors(tmp_path: Path) -> None:
    """An imports-only repo under a guard is UNCHANGED — full
    fallback to plain dvc pull (slice 29 contract), zero error_count (the
    CLI exits 0), no reporter.error calls."""
    fake, fast_fake = _degraded_fakes(
        synced_count=0,
        fallback_targets=["data/imports/a", "data/imports/b"],
        reason="dvc version mismatch",
    )
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path,
        targets=["data/imports/a", "data/imports/b"],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 0
    assert rep.events_of("error") == []
    assert fake.checkout_calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/imports/a", "data/imports/b"]


def test_data_pull_error_count_set_even_without_reporter(tmp_path: Path) -> None:
    """reporter=None must not silence the failure signal —
    error_count still reflects the errored targets."""
    fake, fast_fake = _degraded_fakes(
        blocked_targets=["data/versioned"],
        blocked_reasons={"data/versioned": "version_id spot-check drift"},
        fallback_targets=[],
    )
    summary = data_pull(
        tmp_path, targets=["data/versioned"], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert summary.error_count == 1
    assert fake.pull_calls == []
    assert fake.checkout_calls == []


# ---------- cache probe rescues fully-cached blocked targets ----------


def _write_version_aware_dvc(project: Path, rel: str, md5: str) -> str:
    """Minimal version-aware single-file .dvc (top-level version_id + md5)."""
    dvc = project / f"{rel}.dvc"
    dvc.parent.mkdir(parents=True, exist_ok=True)
    dvc.write_text(
        f"outs:\n  - path: {Path(rel).name}\n    md5: {md5}\n    size: 4\n"
        "    version_id: v1\n"
    )
    return f"{rel}.dvc"


def test_data_pull_guard_error_target_fully_cached_checked_out_no_error(
    tmp_path: Path,
) -> None:
    """A guard-shaped result (e.g. expired credentials) whose
    version-aware blocked target is FULLY cached locally is satisfied by
    `dvc checkout` — no reporter.error, error_count 0, no fallback pull.
    The pin is met from cache; the guard's reason is moot for it."""
    target = _write_version_aware_dvc(tmp_path, "data/final.csv", _MD5_CACHED)
    _seed_cache(tmp_path, _MD5_CACHED)
    fake, fast_fake = _degraded_fakes(
        fallback_targets=[],
        blocked_targets=[target],
        blocked_reasons={target: "AWS credentials unavailable (not retried)"},
        reason="AWS credentials unavailable (not retried)",
    )
    fake.workspace = tmp_path
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 0
    assert rep.events_of("error") == []
    assert len(fake.checkout_calls) == 1
    assert fake.checkout_calls[0].targets == [target]
    assert fake.pull_calls == []
    assert summary.targets_pulled == 1


def test_data_pull_blocked_targets_mixed_cached_and_uncached(
    tmp_path: Path,
) -> None:
    """Only the fully-cached blocked target is rescued via checkout;
    the uncached one still errors loudly and drives error_count."""
    cached = _write_version_aware_dvc(tmp_path, "data/cached.csv", _MD5_CACHED)
    missing = _write_version_aware_dvc(tmp_path, "data/missing.csv", _MD5_MISSING)
    _seed_cache(tmp_path, _MD5_CACHED)
    fake, fast_fake = _degraded_fakes(
        fallback_targets=[],
        blocked_targets=[cached, missing],
        blocked_reasons={
            cached: "bucket versioning probe failed: timeout",
            missing: "bucket versioning probe failed: timeout",
        },
        reason="bucket versioning probe failed: timeout",
    )
    fake.workspace = tmp_path
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[cached, missing],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    assert missing in errors[0][1]
    assert cached not in errors[0][1]
    assert fake.checkout_calls[0].targets == [cached]
    assert fake.pull_calls == []


def test_data_pull_error_target_cached_no_reporter_still_checked_out(
    tmp_path: Path,
) -> None:
    """reporter=None: the cache rescue is not tied to reporting —
    checkout still runs and error_count is still 0."""
    target = _write_version_aware_dvc(tmp_path, "data/final.csv", _MD5_CACHED)
    _seed_cache(tmp_path, _MD5_CACHED)
    fake, fast_fake = _degraded_fakes(
        fallback_targets=[],
        blocked_targets=[target],
        blocked_reasons={target: "dvc version mismatch"},
        reason="dvc version mismatch",
    )
    fake.workspace = tmp_path
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert summary.error_count == 0
    assert fake.checkout_calls[0].targets == [target]
    assert fake.pull_calls == []


# ---------- incomplete targets (per-file failures) fail the run ----------


def test_data_pull_incomplete_targets_counted_in_error_count_and_reported(
    tmp_path: Path,
) -> None:
    """Per-file failures that survived the retries leave the out absent from
    the workspace — they must drive the same loud failure as blocked_targets:
    reporter.error naming the target (with the file count) + targeted-retry
    hint, and a non-zero PullSummary.error_count (exit 1, no ✓ line)."""
    fake, fast_fake = _degraded_fakes(
        synced_count=1,
        fallback_targets=[],
        incomplete_targets=["data/final"],
        files_dir_failures=["data/final: b.csv: 404"],
        reason="per-file download failures (not demoted to dvc pull): data/final",
    )
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=["data/ok", "data/final"],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "data/final" in msg
    assert "1 file(s)" in msg
    assert hint == "retry just this target: mintd data pull data/final"
    # Still excluded from checkout (blobs incomplete) AND from dvc pull.
    assert fake.pull_calls == []
    assert fake.checkout_calls[0].targets == ["data/ok"]


def test_data_pull_incomplete_targets_error_count_without_reporter(
    tmp_path: Path,
) -> None:
    """reporter=None must not silence the failure signal for incomplete_targets."""
    fake, fast_fake = _degraded_fakes(
        fallback_targets=[],
        incomplete_targets=["data/final"],
        files_dir_failures=["data/final: b.csv: 404"],
    )
    summary = data_pull(
        tmp_path, targets=["data/final"], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert summary.error_count == 1
    assert fake.pull_calls == []
    assert fake.checkout_calls == []


# ---------- malformed .dvc must not crash the cache probe ----------


def test_data_pull_malformed_dvc_does_not_crash_cache_probe(
    tmp_path: Path,
) -> None:
    """A YAML-valid but wrong-typed .dvc (e.g. mapping `size:`) makes
    parse_dvc_outs raise. The degraded branch's cache probe must swallow it
    (target simply isn't cached) so the run still degrades to the fallback
    dvc pull instead of dying with a raw TypeError traceback."""
    dvc = tmp_path / "data" / "bad.csv.dvc"
    dvc.parent.mkdir(parents=True)
    dvc.write_text(
        "outs:\n  - path: bad.csv\n    md5: " + "a" * 32 + "\n    size: {oops: 1}\n"
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom mid-sync")
    data_pull(
        tmp_path, targets=["data/bad.csv.dvc"], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert fake.checkout_calls == []
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets == ["data/bad.csv.dvc"]


def test_data_pull_malformed_dvc_in_blocked_targets_probe_does_not_crash(
    tmp_path: Path,
) -> None:
    """Same guard on the result-branch probe over blocked_targets: a malformed
    .dvc named as an error target is 'not cached', so it errors loudly
    instead of crashing the pull."""
    dvc = tmp_path / "data" / "bad.csv.dvc"
    dvc.parent.mkdir(parents=True)
    dvc.write_text(
        "outs:\n  - path: bad.csv\n    md5: " + "a" * 32 + "\n    size: {oops: 1}\n"
    )
    fake, fast_fake = _degraded_fakes(
        fallback_targets=[],
        blocked_targets=["data/bad.csv.dvc"],
        blocked_reasons={"data/bad.csv.dvc": "unparseable"},
        reason="unparseable",
    )
    summary = data_pull(
        tmp_path, targets=["data/bad.csv.dvc"], dvc_ops=fake, fast_sync_ops=fast_fake,
    )
    assert summary.error_count == 1
    assert fake.checkout_calls == []
    assert fake.pull_calls == []


# ---------- dvc 3.67.1 mixed-argv checkout bug: never mix target shapes ----------


def test_data_pull_checkout_never_mixes_dvc_and_stage_out_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root-cause regression for the 37 GB-cached / one-out-materialized
    clone: dvc 3.67.1's ``index_from_targets`` fast path leaks its loop
    variable when a ``dvc checkout`` argv mixes ``.dvc`` file paths with
    bare dvc.lock stage-out path strings — the checkout exits 0 having
    materialized only the last ``.dvc`` target's out. Minimized offline
    against dvc 3.67.1 (repro recipe:
    notes/issue-dvc-checkout-mixed-argv.md): homogeneous argvs are safe,
    mixed argvs are not. Contract: every ``dvc checkout`` invocation
    data_pull issues is all-``.dvc`` or all-bare, never mixed, and no
    target is dropped."""
    import shutil
    fixture = Path("tests/fixtures/pipeline_project")
    shutil.copytree(fixture, tmp_path / "project")
    project = tmp_path / "project"

    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs", lambda _p: ["a.dvc", "b.dvc"],
    )

    fake, fast_fake = _synced_fakes(project, synced_count=8)
    data_pull(project, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    assert fake.checkout_calls, "expected checkout invocations"
    checked_out: list[str] = []
    for call in fake.checkout_calls:
        call_targets = call.targets or []
        is_dvc = [t.endswith(".dvc") for t in call_targets]
        assert all(is_dvc) or not any(is_dvc), (
            f"mixed dvc checkout argv (dvc 3.67.1 silently no-ops on it): "
            f"{call_targets}"
        )
        checked_out.extend(call_targets)
    assert set(checked_out) >= {"a.dvc", "b.dvc"}
    assert sum(1 for t in checked_out if t.startswith("data/final/")) == 6


# ---------- verify-after-checkout: dvc checkout exit 0 is not trusted ----------


def _two_synced_targets(project: Path) -> tuple[str, str]:
    """Two parseable single-file .dvc targets for the fast-synced path."""
    a = _write_single_file_dvc(project, "data/a.csv", _MD5_CACHED)
    b = _write_single_file_dvc(project, "data/b.csv", _MD5_MISSING)
    return a, b


def test_data_pull_checkout_silent_noop_retries_each_target_then_errors(
    tmp_path: Path,
) -> None:
    """A ``dvc checkout`` that exits 0 but materializes NOTHING (the dvc
    3.67.1 cluster failure) must not produce a quiet ✓: every checkout
    target is stat-verified, each missing one gets a single-target retry,
    and the still-missing drive reporter.error (with the retry hint) plus a
    non-zero error_count — same loud path as blocked/incomplete targets."""
    a, b = _two_synced_targets(tmp_path)
    fake, fast_fake = _synced_fakes(tmp_path, synced_count=2)
    fake.checkout_materializes = False
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[a, b], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    # Bulk checkout, then one single-target retry per missing target.
    assert [c.targets for c in fake.checkout_calls] == [[a, b], [a], [b]]
    assert fake.pull_calls == []
    errors = rep.events_of("error")
    assert len(errors) == 2
    for (_, msg, hint), target in zip(errors, [a, b]):
        assert target in msg
        assert "not materialized by dvc checkout" in msg
        assert hint == f"retry just this target: mintd data pull {target}"
    assert summary.error_count == 2
    assert summary.targets_pulled == 0


def test_data_pull_checkout_silent_noop_single_target_retry_rescues(
    tmp_path: Path,
) -> None:
    """Cluster shape: the multi-target checkout silently no-ops but a
    single-target ``dvc checkout <t>`` works. The per-target retries land
    everything, so the summary stays honest — zero errors, all targets
    counted, ✓ line allowed."""
    a, b = _two_synced_targets(tmp_path)
    fake, fast_fake = _synced_fakes(tmp_path, synced_count=2)
    fake.checkout_single_target_only = True
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[a, b], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert [c.targets for c in fake.checkout_calls] == [[a, b], [a], [b]]
    assert rep.events_of("error") == []
    assert summary.error_count == 0
    assert summary.targets_pulled == 2


def test_data_pull_healthy_checkout_verifies_with_zero_retries(
    tmp_path: Path,
) -> None:
    """Healthy path: checkout materializes everything on the first call, so
    verification is stat-only — exactly one checkout invocation, no
    retries, no errors."""
    a, b = _two_synced_targets(tmp_path)
    fake, fast_fake = _synced_fakes(tmp_path, synced_count=2)
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[a, b], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert [c.targets for c in fake.checkout_calls] == [[a, b]]
    assert rep.events_of("error") == []
    assert summary.error_count == 0
    assert summary.targets_pulled == 2


def test_data_pull_crash_recovery_checkout_noop_retried_and_counted(
    tmp_path: Path,
) -> None:
    """The fast-sync-crash recovery path verifies its checkout too: a
    silently no-op'd checkout of the cached target gets the single-target
    retry, and a still-missing target is reported and drives error_count
    (previously the crash branch always claimed success)."""
    cached, missing = _two_target_project(tmp_path)
    fake, fast_fake = _crashing_fakes(tmp_path)
    fake.checkout_materializes = False
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[cached, missing],
        dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    # Bulk checkout of the cached target, fallback pull, then the retry.
    assert [c.targets for c in fake.checkout_calls] == [[cached], [cached]]
    assert fake.pull_calls[0].targets == [missing]
    errors = rep.events_of("error")
    assert len(errors) == 1
    assert cached in errors[0][1]
    assert "not materialized by dvc checkout" in errors[0][1]
    assert summary.error_count == 1
    assert summary.targets_pulled == 1


def test_data_pull_files_format_dir_out_verifies_as_directory(
    tmp_path: Path,
) -> None:
    """Regression: the verify probe must treat a files-format .dvc dir-out
    (no top-level md5, so parse_dvc_outs leaves is_dir=False) as a
    DIRECTORY — the version_aware default shape. Previously the probe took
    the file branch, required is_file() on the materialized directory, and
    every healthy pull got a wasted single-target retry, a false
    'not materialized' error, and a non-zero exit."""
    target = _write_files_format_dvc(
        tmp_path, "data/big", [("a.parquet", _FF_BLOB_A), ("b.parquet", _FF_BLOB_B)],
    )
    fake, fast_fake = _synced_fakes(tmp_path, synced_count=1)
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    # The fake now materializes like real dvc: a directory with the pinned
    # files — the shape the probe must accept with zero retries.
    assert (tmp_path / "data" / "big").is_dir()
    assert (tmp_path / "data" / "big" / "a.parquet").is_file()
    assert [c.targets for c in fake.checkout_calls] == [[target]]
    assert fake.pull_calls == []
    assert rep.events_of("error") == []
    assert summary.error_count == 0
    assert summary.targets_pulled == 1


def test_data_pull_crash_recovery_files_format_dir_out_no_false_error(
    tmp_path: Path,
) -> None:
    """Same probe, crash-recovery route: a fully-cached files-format dir
    target checked out after a fast-sync crash verifies clean — one
    checkout, no retry, no error."""
    target = _write_files_format_dvc(
        tmp_path, "data/big", [("a.parquet", _FF_BLOB_A), ("b.parquet", _FF_BLOB_B)],
    )
    for md5 in (_FF_BLOB_A, _FF_BLOB_B):
        _seed_cache(tmp_path, md5)
    fake, fast_fake = _crashing_fakes(tmp_path)
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    assert [c.targets for c in fake.checkout_calls] == [[target]]
    assert fake.pull_calls == []
    assert rep.events_of("error") == []
    assert summary.error_count == 0
    assert summary.targets_pulled == 1


def test_data_pull_empty_files_format_dir_out_accepted_as_materialized(
    tmp_path: Path,
) -> None:
    """A files-format out pinning ZERO files (files: []) is legal DVC — the
    correct workspace state is an empty directory. It passes the cache
    probe vacuously, joins the checkout set, and the verify probe must
    accept the empty dir instead of retrying and erroring."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "empty.dvc").write_text(
        "outs:\n  - path: empty\n    size: 0\n    files: []\n"
    )
    target = "data/empty.dvc"
    fake, fast_fake = _degraded_fakes(synced_count=0, fallback_targets=[target])
    fake.workspace = tmp_path
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    ws = tmp_path / "data" / "empty"
    assert ws.is_dir() and list(ws.iterdir()) == []
    # Cache-rescued into checkout (vacuously fully cached), never pulled,
    # and verified clean with zero retries.
    assert [c.targets for c in fake.checkout_calls] == [[target]]
    assert fake.pull_calls == []
    assert rep.events_of("error") == []
    assert summary.error_count == 0


def test_data_pull_empty_md5_dir_out_accepted_as_materialized(
    tmp_path: Path,
) -> None:
    """An md5-keyed dir-out pinning the canonical empty-dir manifest
    (d751713988987e9331980363e24189ce.dir, md5 of b'[]') materializes as an
    empty directory; the verify probe must not flag it."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "empty.dvc").write_text(
        f"outs:\n  - path: empty\n    md5: {EMPTY_DIR_MD5}\n    size: 0\n"
    )
    target = "data/empty.dvc"
    fake, fast_fake = _synced_fakes(tmp_path, synced_count=1)
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=[target], dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    ws = tmp_path / "data" / "empty"
    assert ws.is_dir() and list(ws.iterdir()) == []
    assert [c.targets for c in fake.checkout_calls] == [[target]]
    assert rep.events_of("error") == []
    assert summary.error_count == 0
    assert summary.targets_pulled == 1


def test_data_pull_crash_recovery_count_excludes_stage_out_failures(
    tmp_path: Path,
) -> None:
    """Crash-branch accounting: crash recovery's checkout candidates include
    dvc.lock stage outs, but targets_pulled counts only discovered .dvc
    targets — a stage out that fails verification must drive error_count
    without being subtracted from the .dvc-target tally (previously a
    failed stage out understated targets_pulled)."""
    stage_md5 = "5" * 32
    cached = _write_single_file_dvc(tmp_path, "data/cached.csv", _MD5_CACHED)
    _seed_cache(tmp_path, _MD5_CACHED)
    _seed_cache(tmp_path, stage_md5)
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: python build.py\n"
        "    outs:\n"
        "      - path: data/out.csv\n"
        f"        md5: {stage_md5}\n"
        "        size: 4\n"
    )
    fake, fast_fake = _crashing_fakes(tmp_path)
    fake.checkout_never_materializes = {"data/out.csv"}
    rep = RecordingReporter()
    summary = data_pull(
        tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake, reporter=rep,
    )
    # Both candidates were fully cached: grouped checkout (.dvc argv, bare
    # argv), no fallback pull, then the stage out's single-target retry.
    assert [c.targets for c in fake.checkout_calls] == [
        [cached], ["data/out.csv"], ["data/out.csv"],
    ]
    assert fake.pull_calls == []
    errors = rep.events_of("error")
    assert len(errors) == 1
    assert "data/out.csv" in errors[0][1]
    # The failure is a stage out, not a discovered .dvc target: it drives
    # error_count but must NOT be subtracted from the .dvc-target count.
    assert summary.error_count == 1
    assert summary.targets_pulled == 1
