"""`mintd data` family — thin pass-throughs over DvcOps with fast_sync hook.

Distinct module from slice-7's `data.py` (which holds `import_product`
and `bump_import`). The naming asymmetry is deliberate: `data.py` is the
*catalog-aware* family (uses CatalogClient + DvcOps), while `data_ops.py`
is the *DVC-only* family (consumer's own data). They could share a module
in a slice-19 cleanup; for now, the rename would touch slice 7-12 tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ._dvc_ops import DvcOps
from ._fast_sync_ops import FastSyncOps


logger = logging.getLogger(__name__)


def data_pull(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    fast_sync_ops: FastSyncOps | None = None,
    remote: str | None = None,
    jobs: int | None = None,
) -> None:
    if fast_sync_ops is not None and targets:
        remote_name = remote or _default_dvc_remote(project_path) or "origin"
        try:
            result = fast_sync_ops.try_fast_pull(
                project_path=project_path,
                targets=targets,
                remote_name=remote_name,
                jobs=jobs or 8,
            )
        except Exception as exc:
            logger.warning("fast-sync raised; falling back to full dvc pull: %s", exc)
            dvc_ops.pull(targets=targets, remote=remote, jobs=jobs)
            return

        logger.info(
            "fast-sync: synced=%d fallback=%d reason=%r",
            result.synced_count,
            len(result.fallback_targets),
            result.reason,
        )
        fallback_set = set(result.fallback_targets)
        synced_targets = [t for t in targets if t not in fallback_set]

        if synced_targets:
            dvc_ops.checkout(targets=synced_targets)

        if result.fallback_targets:
            dvc_ops.pull(targets=result.fallback_targets, remote=remote, jobs=jobs)
        return

    dvc_ops.pull(targets=targets, remote=remote, jobs=jobs)


def _default_dvc_remote(project_path: Path) -> str | None:
    import configparser
    config_file = project_path / ".dvc" / "config"
    if not config_file.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(config_file)
        if cp.has_section("core") and cp.has_option("core", "remote"):
            return cp.get("core", "remote")
    except configparser.Error:
        pass
    return None


def data_push(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    remote: str | None = None,
    jobs: int | None = None,
) -> None:
    del project_path  # unused; accepted for signature symmetry with pull
    dvc_ops.push(remote=remote, jobs=jobs)  # DVC push doesn't accept targets


def data_add(path: Path, *, dvc_ops: DvcOps) -> Path:
    return dvc_ops.add(path)


def data_verify(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
) -> dict[str, str]:
    del project_path  # currently unused; dvc status doesn't accept a cwd arg
    return dvc_ops.status(targets=targets)


def data_remove(name: str, *, dvc_ops: DvcOps) -> None:
    dvc_ops.remove(name)
