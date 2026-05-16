from __future__ import annotations

import pytest
from pathlib import Path
from mintd._dvc_ops import DvcPullError
from mintd.data_ops import data_add, data_pull, data_push, data_remove, data_verify
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


def test_data_pull_fast_sync_true_skips_dvc_ops_pull(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = True
    data_pull(tmp_path, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert fake.pull_calls == []
    assert len(fast_fake.calls) == 1


def test_data_pull_fast_sync_false_falls_back(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.result = False
    data_pull(tmp_path, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1
    assert len(fast_fake.calls) == 1


def test_data_pull_fast_sync_raises_falls_back(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    fast_fake = _FakeFastSyncOps()
    fast_fake.raises = RuntimeError("boom")
    data_pull(tmp_path, dvc_ops=fake, fast_sync_ops=fast_fake)
    assert len(fake.pull_calls) == 1


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
