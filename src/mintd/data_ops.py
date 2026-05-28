"""`mintd data` family — thin pass-throughs over DvcOps with fast_sync hook.

Distinct module from slice-7's `data.py` (which holds `import_product`
and `bump_import`). The naming asymmetry is deliberate: `data.py` is the
*catalog-aware* family (uses CatalogClient + DvcOps), while `data_ops.py`
is the *DVC-only* family (consumer's own data). They could share a module
in a slice-19 cleanup; for now, the rename would touch slice 7-12 tests.
"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._dvc_ops import DvcOps
from ._fast_sync_ops import (
    DvcOut,
    FastSyncOps,
    discover_all_outs,
    parse_dvc_outs,
    partition_pipeline_outs,
)

if TYPE_CHECKING:
    from ._console import Reporter


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullSummary:
    """What a ``data_pull`` did — for the CLI's completion line (slice 38b)."""
    file_count: int
    total_bytes: int
    elapsed_s: float


def _out_aggregate_bytes(out: DvcOut) -> int:
    """Bytes the progress bar should expect for this out.

    Files-format dir-outs (slice 27, version_aware mode) write the
    top-level ``size:`` as the manifest size only, not the aggregate.
    Sum per-file sizes instead, otherwise the progress total
    massively undershoots actual bytes-on-the-wire.
    """
    if out.is_files_format and out.files:
        return sum(fe.size for fe in out.files)
    return out.size


def data_pull(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    fast_sync_ops: FastSyncOps | None = None,
    remote: str | None = None,
    jobs: int | None = None,
    extra_dvc_args: list[str] | None = None,
    reporter: "Reporter | None" = None,
) -> PullSummary:
    """Pull dvc-tracked data via fast-sync (boto3 → cache) when available;
    fall back to ``dvc pull`` for anything fast-sync can't handle.

    Slice 26: when ``targets is None`` and fast_sync_ops is available,
    discovers all ``.dvc`` files in the project and routes through
    fast-sync. Without discovery, the call would fall through to
    ``dvc pull`` directly and hit DVC 3.66.1's cache-write bug on
    version_aware buckets.

    Returns a ``PullSummary`` (file count, total bytes, elapsed) so the CLI
    can render an informative completion line (slice 38b).
    """
    start_t = time.monotonic()
    if fast_sync_ops is not None:
        # Track the original request shape: "pull-all" (None) carries different
        # post-fast-sync semantics than "pull these specific .dvc files."
        pull_all_requested = targets is None
        remote_name = remote or _default_dvc_remote(project_path) or "origin"

        pipeline_outs: list[DvcOut] = []
        all_pipeline: list[DvcOut] = []
        if pull_all_requested:
            targets = discover_all_outs(project_path)
            pipeline_outs, all_pipeline = partition_pipeline_outs(project_path, remote_name)

            n_dvc = len(targets)
            n_pipe = len(pipeline_outs)
            if n_dvc + n_pipe:
                logger.info(
                    "fast-sync: discovered %d .dvc target(s) + %d pipeline output(s)",
                    n_dvc, n_pipe,
                )
            else:
                logger.info("no .dvc targets and no pipeline outs discovered")

        # Compute total bytes upfront for the progress bar (filesystem-only,
        # sub-second on realistic repos). See _out_aggregate_bytes for the
        # files-format quirk that drove this helper.
        total_bytes = 0
        for t in targets or []:
            dvc_path = project_path / t if t.endswith(".dvc") else project_path / f"{t}.dvc"
            try:
                for out in parse_dvc_outs(dvc_path, remote_name):
                    total_bytes += _out_aggregate_bytes(out)
            except Exception:
                # Malformed or missing .dvc — fast-sync will route to fallback.
                pass

        for out in pipeline_outs:
            total_bytes += _out_aggregate_bytes(out)

        progress_cm = (
            reporter.progress(total_bytes, desc=f"Pulling {project_path.name}")
            if reporter is not None
            else nullcontext(lambda _n: None)
        )

        fast_sync_failed = False
        result = None
        if targets or pipeline_outs:
            with progress_cm as advance:
                had_setter = hasattr(fast_sync_ops, "set_progress")
                if had_setter:
                    fast_sync_ops.set_progress(advance)  # type: ignore[attr-defined]
                try:
                    try:
                        result = fast_sync_ops.try_fast_pull(
                            project_path=project_path,
                            targets=targets or [],
                            remote_name=remote_name,
                            jobs=jobs or 8,
                            pipeline_outs=pipeline_outs,
                            reporter=reporter,
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
                remote=remote, jobs=jobs, extra_args=extra_dvc_args,
            )
            return PullSummary(
                file_count=len(targets or []),
                total_bytes=total_bytes,
                elapsed_s=time.monotonic() - start_t,
            )

        if result is not None:
            logger.info(
                "fast-sync: synced=%d fallback=%d reason=%r",
                result.synced_count,
                len(result.fallback_targets),
                result.reason,
            )
            if extra_dvc_args and result.synced_count:
                logger.debug(
                    "fast-sync handled %d target(s); --dvc-arg ignored for those",
                    result.synced_count,
                )
            fallback_set = set(result.fallback_targets)

            pipeline_target_ids = [out.target for out in pipeline_outs]
            candidate_synced = (targets or []) + pipeline_target_ids
            synced_targets = [t for t in candidate_synced if t not in fallback_set]

            if synced_targets:
                dvc_ops.checkout(targets=synced_targets)

            if result.fallback_targets:
                dvc_ops.pull(
                    targets=result.fallback_targets,
                    remote=remote, jobs=jobs, extra_args=extra_dvc_args,
                )

        # Pipeline-stage outputs fast-sync couldn't serve (no usable
        # cloud.<remote> version_id) still need a `dvc pull`. Pull ONLY those —
        # never targets=None. A blanket pull would re-validate the version-aware
        # outs fast-sync just cached and re-trigger DVC 3.66.1's rehash-on-pull
        # (the multi-GB re-download SLICE-37 added fast-sync to avoid).
        #
        # The trigger is "are there uncovered stage outs?", NOT "does a dvc.yaml
        # exist". A stages-less dvc.yaml (e.g. one carrying only `vars:`/docs)
        # with no dvc.lock yields no stage outs, so the catch-all is correctly
        # skipped and the project's version-aware .dvc outs stay fast-synced.
        uncovered: list[str] = []
        if pull_all_requested:
            covered = {out.target for out in pipeline_outs}
            uncovered = sorted({out.target for out in all_pipeline} - covered)
            if uncovered:
                logger.info("dvc pull for %d stage out(s) fast-sync can't serve", len(uncovered))
                dvc_ops.pull(
                    targets=uncovered, remote=remote, jobs=jobs, extra_args=extra_dvc_args,
                )
            else:
                logger.info("no uncovered stage outs; skipping catch-all dvc pull")
        # Count fast-synced outputs, the dvc-pull fallback, and the uncovered
        # stage outs the catch-all pulled — all land on disk, so all belong in
        # the file count.
        synced_count = (
            result.synced_count + len(result.fallback_targets) + len(uncovered)
            if result is not None
            else len(targets or []) + len(uncovered)
        )
        return PullSummary(
            file_count=synced_count,
            total_bytes=total_bytes,
            elapsed_s=time.monotonic() - start_t,
        )

    dvc_ops.pull(
        targets=targets, remote=remote, jobs=jobs, extra_args=extra_dvc_args,
    )
    return PullSummary(
        file_count=len(targets or []),
        total_bytes=0,
        elapsed_s=time.monotonic() - start_t,
    )


def _default_dvc_remote(project_path: Path) -> str | None:
    """Pick the dvc remote name for the project.

    Order:
      1. ``[core] remote = <name>`` if present (DVC's standard default).
      2. The single ``[remote "..."]`` section if there's exactly one
         (covers freshly-cloned data products: their .dvc/config typically
         declares one remote per product and no [core] default).
      3. None — caller defaults to "origin".
    """
    import configparser
    import re
    config_file = project_path / ".dvc" / "config"
    if not config_file.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(config_file)
    except configparser.Error:
        return None
    if cp.has_section("core") and cp.has_option("core", "remote"):
        return cp.get("core", "remote")
    # Fallback: extract remote names from section headers. DVC has shipped
    # three formats: 'remote "name"' (single-quoted, the modern default),
    # 'remote "name"' (double-quoted only), 'remote name' (unquoted).
    # Same probes as get_remote_config in _fast_sync_ops.py.
    remote_names: list[str] = []
    for section in cp.sections():
        m = re.fullmatch(r"""'?remote\s+"?(?P<name>[^"']+)"?'?""", section)
        if m:
            remote_names.append(m.group("name"))
    if len(remote_names) == 1:
        return remote_names[0]
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
