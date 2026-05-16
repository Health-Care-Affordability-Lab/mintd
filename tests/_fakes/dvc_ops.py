"""Fake `DvcOps` for tests.

Records every `import_` call and writes a parseable stub `.dvc` file to disk
so downstream `scan_imports()` can pick it up. The stub mirrors the real
`dvc import` shape closely enough that `DataDependency.from_dvc_file` parses
it cleanly (see tests/test_dvc_ops.py for the round-trip).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple


class DvcImportCall(NamedTuple):
    repo_url: str
    path: str
    dest: Path
    rev: str | None
    force: bool


class DvcPushCall(NamedTuple):
    remote: str | None
    jobs: int | None


class DvcPullCall(NamedTuple):
    targets: list[str] | None
    remote: str | None
    jobs: int | None


class DvcAddCall(NamedTuple):
    path: Path


class DvcStatusCall(NamedTuple):
    targets: list[str] | None


class DvcRemoveCall(NamedTuple):
    name: str


class DvcCheckoutCall(NamedTuple):
    targets: list[str] | None


class _FakeDvcOps:
    """Implements `mintd._dvc_ops.DvcOps` structurally."""

    def __init__(self) -> None:
        self.calls: list[DvcImportCall] = []
        self.push_calls: list[DvcPushCall] = []
        self.push_raises: Exception | None = None
        self.pull_calls: list[DvcPullCall] = []
        self.pull_raises: Exception | None = None
        self.add_calls: list[DvcAddCall] = []
        self.add_raises: Exception | None = None
        self.status_calls: list[DvcStatusCall] = []
        self.status_raises: Exception | None = None
        self.status_result: dict[str, str] = {}
        self.remove_calls: list[DvcRemoveCall] = []
        self.remove_raises: Exception | None = None
        self.checkout_calls: list[DvcCheckoutCall] = []
        self.checkout_raises: Exception | None = None

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        rev: str | None = None,
        force: bool = False,
    ) -> Path:
        self.calls.append(
            DvcImportCall(repo_url=repo_url, path=path, dest=dest, rev=rev, force=force)
        )
        dvc_file = dest.parent / (dest.name + ".dvc")
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        # Stub shape: enough for DataDependency.from_dvc_file to parse.
        rev_lock = rev if (rev and len(rev) == 40) else "fake0pin" + "0" * 32
        dvc_file.write_text(
            "outs:\n"
            f"  - md5: {'f' * 32}\n"
            "    size: 0\n"
            f"    path: {dest.name}\n"
            "deps:\n"
            f"  - path: {path}\n"
            "    repo:\n"
            f"      url: {repo_url}\n"
            f"      rev: {rev or 'main'}\n"
            f"      rev_lock: {rev_lock}\n"
        )
        return dvc_file

    def push(self, *, remote: str | None = None, jobs: int | None = None) -> None:
        if self.push_raises:
            raise self.push_raises
        self.push_calls.append(DvcPushCall(remote=remote, jobs=jobs))

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> None:
        if self.pull_raises:
            raise self.pull_raises
        self.pull_calls.append(DvcPullCall(targets=targets, remote=remote, jobs=jobs))

    def add(self, path: Path) -> Path:
        if self.add_raises:
            raise self.add_raises
        self.add_calls.append(DvcAddCall(path=path))
        dvc_file = path.parent / (path.name + ".dvc")
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        dvc_file.write_text("")
        return dvc_file

    def status(self, targets: list[str] | None = None) -> dict[str, str]:
        if self.status_raises:
            raise self.status_raises
        self.status_calls.append(DvcStatusCall(targets=targets))
        return self.status_result.copy()

    def remove(self, name: str) -> None:
        if self.remove_raises:
            raise self.remove_raises
        self.remove_calls.append(DvcRemoveCall(name=name))

    def checkout(self, *, targets: list[str] | None = None) -> None:
        if self.checkout_raises:
            raise self.checkout_raises
        self.checkout_calls.append(DvcCheckoutCall(targets=targets))
