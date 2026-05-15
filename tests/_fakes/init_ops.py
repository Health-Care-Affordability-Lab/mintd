"""Fake `InitOps` for tests — records calls without subprocess."""

from __future__ import annotations

from pathlib import Path


class _FakeInitOps:
    """Implements `mintd._init_ops.InitOps` structurally."""

    def __init__(self) -> None:
        self.git_calls: list[Path] = []
        self.dvc_calls: list[Path] = []

    def git_init(self, target_dir: Path) -> None:
        self.git_calls.append(target_dir)

    def dvc_init(self, target_dir: Path) -> None:
        self.dvc_calls.append(target_dir)
