"""`mintd data` family — thin pass-throughs over DvcOps with fast_sync hook.

Distinct module from slice-7's `data.py` (which holds `import_product`
and `bump_import`). The naming asymmetry is deliberate: `data.py` is the
*catalog-aware* family (uses CatalogClient + DvcOps), while `data_ops.py`
is the *DVC-only* family (consumer's own data). They could share a module
in a slice-19 cleanup; for now, the rename would touch slice 7-12 tests.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from ._dvc_ops import DvcOps
from ._fast_sync_ops import FastSyncOps, discover_all_outs, parse_dvc_outs

if TYPE_CHECKING:
    from ._console import Reporter


logger = logging.getLogger(__name__)


def data_pull(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    fast_sync_ops: FastSyncOps | None = None,
    remote: str | None = None,
    jobs: int | None = None,
    reporter: "Reporter | None" = None,
) -> None:
    """Pull dvc-tracked data via fast-sync (boto3 → cache) when available;
    fall back to ``dvc pull`` for anything fast-sync can't handle.

    Slice 26: when ``targets is None`` and fast_sync_ops is available,
    discovers all ``.dvc`` files in the project and routes through
    fast-sync. Without discovery, the call would fall through to
    ``dvc pull`` directly and hit DVC 3.66.1's cache-write bug on
    version_aware buckets.
    """
    if fast_sync_ops is not None:
        # Track the original request shape: "pull-all" (None) carries different
        # post-fast-sync semantics than "pull these specific .dvc files."
        pull_all_requested = targets is None
        if pull_all_requested:
            targets = discover_all_outs(project_path)
            if targets:
                logger.info("fast-sync: discovered %d .dvc target(s)", len(targets))
            else:
                logger.info("no .dvc targets discovered; only dvc.yaml stages remain (if any)")

        remote_name = remote or _default_dvc_remote(project_path) or "origin"

        # Compute total bytes upfront for the progress bar (filesystem-only,
        # sub-second on realistic repos).
        total_bytes = 0
        for t in targets or []:
            dvc_path = project_path / t if t.endswith(".dvc") else project_path / f"{t}.dvc"
            try:
                for out in parse_dvc_outs(dvc_path, remote_name):
                    total_bytes += out.size
            except Exception:
                # Malformed or missing .dvc — fast-sync will route to fallback.
                pass

        progress_cm = (
            reporter.progress(total_bytes, desc=f"Pulling {project_path.name}")
            if reporter is not None
            else nullcontext(lambda _n: None)
        )

        fast_sync_failed = False
        result = None
        if targets:
            with progress_cm as advance:
                had_setter = hasattr(fast_sync_ops, "set_progress")
                if had_setter:
                    fast_sync_ops.set_progress(advance)  # type: ignore[attr-defined]
                try:
                    try:
                        result = fast_sync_ops.try_fast_pull(
                            project_path=project_path,
                            targets=targets,
                            remote_name=remote_name,
                            jobs=jobs or 8,
                        )
                    except Exception as exc:
                        logger.warning("fast-sync raised; falling back to full dvc pull: %s", exc)
                        fast_sync_failed = True
                finally:
                    if had_setter:
                        fast_sync_ops.set_progress(None)  # type: ignore[attr-defined]

        # Fast-sync raised — fall back to dvc pull on the full target set.
        # MUST happen OUTSIDE the progress widget's with-block; otherwise
        # dvc's subprocess output corrupts the active rich.Progress render.
        # If the user asked for pull-all (targets was None at entry), pass
        # None back so dvc also pulls dvc.yaml pipeline stages — the
        # discovered .dvc list alone would silently drop them.
        if fast_sync_failed:
            dvc_ops.pull(
                targets=None if pull_all_requested else targets,
                remote=remote, jobs=jobs,
            )
            return

        if result is not None:
            logger.info(
                "fast-sync: synced=%d fallback=%d reason=%r",
                result.synced_count,
                len(result.fallback_targets),
                result.reason,
            )
            fallback_set = set(result.fallback_targets)
            synced_targets = [t for t in targets if t not in fallback_set] if targets else []

            if synced_targets:
                dvc_ops.checkout(targets=synced_targets)

            if result.fallback_targets:
                dvc_ops.pull(targets=result.fallback_targets, remote=remote, jobs=jobs)

        # When the user requested pull-all and the project has a dvc.yaml,
        # do a final `dvc pull` with no targets to catch pipeline-stage
        # outputs (which discover_all_outs deliberately doesn't enumerate —
        # they're listed in dvc.yaml, not in .dvc files, and fast-sync's
        # classify_targets can't consume them). Without this, dvc.yaml-only
        # outputs would be silently dropped on pull-all. Same code path as
        # `dvc pull` with no args, just gated on user intent.
        if pull_all_requested and (project_path / "dvc.yaml").is_file():
            logger.info("dvc.yaml present; running dvc pull to catch pipeline-stage outputs")
            dvc_ops.pull(targets=None, remote=remote, jobs=jobs)
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
