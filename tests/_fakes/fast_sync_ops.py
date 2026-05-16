"""Fake `FastSyncOps` for tests."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class FastPullCall(NamedTuple):
    project_path: Path
    targets: list[str] | None


class _FakeFastSyncOps:
    """Implements `mintd._fast_sync_ops.FastSyncOps` structurally.

    Default returns False so caller falls through; toggle `result` to True
    or set `raises` to test branching.
    """

    def __init__(self) -> None:
        self.calls: list[FastPullCall] = []
        self.result: bool = False
        self.raises: Exception | None = None

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str] | None = None,
    ) -> bool:
        self.calls.append(FastPullCall(project_path=project_path, targets=targets))
        if self.raises:
            raise self.raises
        return self.result
