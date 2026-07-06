"""Fake `DvcOps` for tests.

Records every `import_` call and writes a parseable stub `.dvc` file to disk
so downstream `scan_imports()` can pick it up. The stub mirrors the real
`dvc import` shape closely enough that `DataDependency.from_dvc_file` parses
it cleanly (see tests/test_dvc_ops.py for the round-trip).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from mintd._dvc_ops import DvcOpError, DvcPushResult


class DvcInitCall(NamedTuple):
    cwd: Path | None


class DvcImportCall(NamedTuple):
    repo_url: str
    path: str
    dest: Path
    rev: str | None
    force: bool
    extra_args: list[str] | None = None


class DvcPushCall(NamedTuple):
    remote: str | None
    jobs: int | None


class DvcPullCall(NamedTuple):
    targets: list[str] | None
    remote: str | None
    jobs: int | None
    extra_args: list[str] | None = None


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
        self.init_calls: list[DvcInitCall] = []
        self.calls: list[DvcImportCall] = []
        self.push_calls: list[DvcPushCall] = []
        self.push_raises: Exception | None = None
        self.push_result: DvcPushResult = DvcPushResult(pushed=1, up_to_date=False)
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
        # Post-checkout verification (data_pull) stats workspace paths, so a
        # fake checkout must be able to MATERIALIZE its targets. Set
        # ``workspace`` to the project root to enable it. Knobs model the
        # dvc 3.67.1 index_from_targets bug:
        # - checkout_materializes=False: checkout exits 0 having written
        #   nothing (the silent multi-target no-op);
        # - checkout_single_target_only=True: only single-target invocations
        #   materialize (the cluster shape — bulk no-ops, retries work).
        self.workspace: Path | None = None
        self.checkout_materializes: bool = True
        self.checkout_single_target_only: bool = False
        # Targets checkout NEVER materializes (even single-target retries)
        # — models a target whose cache blobs are unusable/corrupt.
        self.checkout_never_materializes: set[str] = set()

    def init(self, *, cwd: Path | None = None) -> None:
        self.init_calls.append(DvcInitCall(cwd=cwd))

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        rev: str | None = None,
        force: bool = False,
        extra_args: list[str] | None = None,
    ) -> Path:
        self.calls.append(
            DvcImportCall(
                repo_url=repo_url, path=path, dest=dest, rev=rev, force=force,
                extra_args=extra_args,
            )
        )
        # Mirror real `dvc import`: the destination's parent (the stage working
        # dir) must already exist. The caller is responsible for creating it;
        # do NOT mkdir here, or we mask the "stage working dir does not exist"
        # failure that bit enclave_pull (slice 47).
        if not dest.parent.exists():
            raise DvcOpError(
                f"dvc import failed (exit 1): stage working dir "
                f"'{dest.parent}' does not exist"
            )
        dvc_file = dest.parent / (dest.name + ".dvc")
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

    def push(self, *, remote: str | None = None, jobs: int | None = None) -> DvcPushResult:
        if self.push_raises:
            raise self.push_raises
        self.push_calls.append(DvcPushCall(remote=remote, jobs=jobs))
        return self.push_result

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        if self.pull_raises:
            raise self.pull_raises
        self.pull_calls.append(
            DvcPullCall(
                targets=targets, remote=remote, jobs=jobs, extra_args=extra_args,
            )
        )

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
        if (
            self.workspace is not None
            and self.checkout_materializes
            and (not self.checkout_single_target_only or len(targets or []) == 1)
        ):
            for t in targets or []:
                if t not in self.checkout_never_materializes:
                    self._materialize_target(t)

    def _materialize_target(self, target: str) -> None:
        """Write what a real `dvc checkout` would: the target's workspace
        path(s). Out shapes (file vs dir vs files-format dir) come from the
        on-disk .dvc / dvc.lock, same as production's verification pass; a
        target with neither is materialized as a plain file.

        Path resolution and shape dispatch are imported from production
        (`workspace_path_for`, `DvcOut.materializes_as_dir`,
        `EMPTY_DIR_MD5`) — the fake WRITES the paths production STATS, so
        writer and reader must agree on the address by construction. Only
        the stand-in file CONTENT below is fake-specific."""
        from mintd._fast_sync_ops import (
            EMPTY_DIR_MD5,
            outs_for_target,
            parse_dvc_lock_outs,
            workspace_path_for,
        )

        root = self.workspace
        assert root is not None
        outs = outs_for_target(root, target, "origin")
        if not outs:
            outs = [o for o in parse_dvc_lock_outs(root, "origin") if o.target == target]
        if not outs:
            dest = root / (target[:-4] if target.endswith(".dvc") else target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("materialized")
            return
        for out in outs:
            dest = workspace_path_for(root, out)
            if out.materializes_as_dir:
                dest.mkdir(parents=True, exist_ok=True)
                if out.files is not None:
                    # files-format: exactly the pinned entries — an empty
                    # files: [] list yields an EMPTY directory, like real dvc.
                    rels = [fe.relpath for fe in out.files]
                elif out.md5 == EMPTY_DIR_MD5:
                    rels = []  # empty-manifest md5 dir: real dvc makes it empty
                else:
                    # md5-keyed dir: the fake can't read the cached .dir
                    # manifest, so stand in one file for "non-empty content".
                    rels = [".materialized"]
                for rel in rels:
                    p = dest / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("materialized")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("materialized")
