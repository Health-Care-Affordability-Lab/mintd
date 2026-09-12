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

    Default returns success=False with an EMPTY `fallback_targets`, i.e.
    "served nothing, and nothing for `dvc pull` to serve either"; toggle
    `result` to True or set `raises` to test branching.

    `fallback_all` is the third shape, and the one a caller wants when it
    cares about what reaches `dvc pull` rather than about fast-sync: every
    target handed in comes back as a `fallback_targets` entry, so the whole
    request lands as a scoped `dvc pull <targets>`. It is what the CLI's
    pull/clone tests observe now that `fast_sync_ops` is required (issue18) —
    before, they got the same `dvc pull` from `data_pull`'s deleted
    `fast_sync_ops is None` branch, which reached it by skipping fast-sync
    altogether.
    """

    def __init__(self) -> None:
        self.calls: list[FastPullCall] = []
        self.result: FastPullResult = FastPullResult(success=False, fallback_targets=[])
        self.raises: Exception | None = None
        self.fallback_all: bool = False

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
        if self.fallback_all:
            return FastPullResult(success=False, fallback_targets=list(targets))
        return self.result
