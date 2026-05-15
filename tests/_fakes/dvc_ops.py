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


class _FakeDvcOps:
    """Implements `mintd._dvc_ops.DvcOps` structurally."""

    def __init__(self) -> None:
        self.calls: list[DvcImportCall] = []
        self.push_calls: list[DvcPushCall] = []
        self.push_raises: Exception | None = None

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
