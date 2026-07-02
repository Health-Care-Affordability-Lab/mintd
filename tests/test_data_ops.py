from __future__ import annotations

import pytest
from pathlib import Path
from mintd._dvc_ops import DvcPullError
from mintd._fast_sync_ops import DvcFileEntry, DvcOut
from mintd.model import FastPullResult
from mintd.data_ops import _out_aggregate_bytes, data_add, data_pull, data_push, data_remove, data_verify
from tests._fakes.dvc_ops import DvcPullCall, _FakeDvcOps
from tests._fakes.fast_sync_ops import _FakeFastSyncOps


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


def test_data_pull_fast_sync_raises_pull_all_falls_back_to_dvc_pull_no_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pull-all was requested and fast-sync raises, the fallback
    dvc pull MUST use targets=None (not the discovered .dvc list) so
    dvc.yaml pipeline stages are also pulled."""
    monkeypatch.setattr(
        "mintd.data_ops.discover_all_outs",
        lambda _p: ["a.dvc", "b.dvc"],
    )
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom")
    data_pull(tmp_path, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert fake.pull_calls[0].targets is None  # not ["a.dvc", "b.dvc"]


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

    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=6, fallback_targets=[])
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
    Both groups reach fast-sync; both appear in dvc_ops.checkout's targets."""
    import shutil
    fixture = Path("tests/fixtures/pipeline_project")
    shutil.copytree(fixture, tmp_path / "project")
    project = tmp_path / "project"

    monkeypatch.setattr("mintd.data_ops.discover_all_outs", lambda _p: ["a.dvc"])

    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = FastPullResult(success=True, synced_count=7, fallback_targets=[])
    data_pull(project, targets=None, dvc_ops=fake, fast_sync_ops=fast_fake)

    assert len(fast_fake.calls) == 1
    assert fast_fake.calls[0].targets == ["a.dvc"]
    assert fast_fake.calls[0].pipeline_outs is not None
    assert len(fast_fake.calls[0].pipeline_outs) == 6

    checkout_targets = fake.checkout_calls[0].targets or []
    assert "a.dvc" in checkout_targets
    assert any(t.startswith("data/final/") for t in checkout_targets)


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


def test_data_pull_catch_all_only_uncovered_no_fast_sync_counts_correctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No .dvc files and no fast-syncable stage outs → fast-sync never runs
    (result is None). The catch-all pulls the uncovered out, and file_count
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
    assert summary.file_count == 1
