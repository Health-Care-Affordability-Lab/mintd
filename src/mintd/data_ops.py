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
    """Pull DVC-tracked data to the local cache.

    If `fast_sync_ops` is provided, try the fast path first. On True
    return, skip dvc pull. On False return OR any exception from
    fast_sync_ops, fall through to dvc_ops.pull. This makes the fast
    path strictly additive — failures don't block the normal pull.
    """
    if fast_sync_ops is not None:
        try:
            if fast_sync_ops.try_fast_pull(project_path=project_path, targets=targets):
                logger.info("fast-sync hit; skipping dvc pull")
                return
        except Exception as exc:
            logger.warning("fast-sync failed; falling back to dvc pull: %s", exc)
    dvc_ops.pull(targets=targets, remote=remote, jobs=jobs)


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
