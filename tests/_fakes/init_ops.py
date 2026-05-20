"""Fake `InitOps` for tests — records calls without subprocess."""

from __future__ import annotations

from pathlib import Path

from mintd._init_ops import InitOpError


class _FakeInitOps:
    """Implements `mintd._init_ops.InitOps` structurally.

    Records every call. ``fail_on`` lets tests inject a failure on a
    specific method (e.g. ``{"dvc_remote_add"}``) to exercise the
    rollback path in ``init_project``.
    """

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.git_calls: list[Path] = []
        self.dvc_calls: list[Path] = []
        self.remote_add_calls: list[dict] = []
        self.fail_on: set[str] = fail_on or set()

    def git_init(self, target_dir: Path) -> None:
        if "git_init" in self.fail_on:
            raise InitOpError("fake git_init failure")
        self.git_calls.append(target_dir)

    def dvc_init(self, target_dir: Path) -> None:
        if "dvc_init" in self.fail_on:
            raise InitOpError("fake dvc_init failure")
        self.dvc_calls.append(target_dir)

    def dvc_remote_add(
        self,
        target_dir: Path,
        *,
        name: str,
        url: str,
        default: bool,
        endpoint: str | None,
        profile: str | None,
    ) -> None:
        if "dvc_remote_add" in self.fail_on:
            raise InitOpError("fake dvc_remote_add failure")
        self.remote_add_calls.append(
            {
                "target_dir": target_dir,
                "name": name,
                "url": url,
                "default": default,
                "endpoint": endpoint,
                "profile": profile,
            }
        )
