"""Fake `FastSyncOps` for tests."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from mintd._fast_sync_ops import DvcOut
from mintd.model import FastPullResult


class FastPullCall(NamedTuple):
    project_path: Path
    targets: list[str]
    remote_name: str
    jobs: int
    pipeline_outs: list[DvcOut] | None


class _FakeFastSyncOps:
    """Implements `mintd._fast_sync_ops.FastSyncOps` structurally.

    Default returns success=False so caller falls through; toggle `result` to True
    or set `raises` to test branching.
    """

    def __init__(self) -> None:
        self.calls: list[FastPullCall] = []
        self.result: FastPullResult = FastPullResult(success=False, fallback_targets=[])
        self.raises: Exception | None = None

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
        reporter: object = None,  # slice 36 Pattern D — accepted, ignored
    ) -> FastPullResult:
        self.calls.append(
            FastPullCall(
                project_path=project_path,
                targets=targets,
                remote_name=remote_name,
                jobs=jobs,
                pipeline_outs=pipeline_outs,
            )
        )
        if self.raises:
            raise self.raises
        return self.result
