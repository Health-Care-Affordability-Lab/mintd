"""`mintd data` family — thin pass-throughs over DvcOps with fast_sync hook.

Distinct module from slice-7's `data.py` (which holds `import_product`
and `bump_import`). The naming asymmetry is deliberate: `data.py` is the
*catalog-aware* family (uses CatalogClient + DvcOps), while `data_ops.py`
is the *DVC-only* family (consumer's own data). They could share a module
in a slice-19 cleanup; for now, the rename would touch slice 7-12 tests.

``data_pull``'s degraded-path behavior (checkout-before-pull ordering, the
fallback/blocked/incomplete routing, the scoped catch-all) implements the
pull-all audit; the full catalogue of those fixes lives in
notes/issue-data-pull-all-fallback-skips-checkout.md.
"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ._dvc_ops import (
    DvcOps,
    DvcPullError,
    DvcPushResult,
    DvcStorageKeyError,
    pull_retry_hint,
)
from ._fast_sync_ops import (
    DvcOut,
    FastSyncOps,
    cached_targets,
    discover_all_outs,
    outs_for_target,
    outs_materialized,
    partition_pipeline_outs,
    resolve_target_outs,
)
from ._import_rescue_ops import RescueResult, rescue_import_pull
from .model import FastPullResult

if TYPE_CHECKING:
    from ._console import Reporter


logger = logging.getLogger(__name__)

# Injectable seam for the consumer-side import-rescue lane (see
# ``_import_rescue_ops.rescue_import_pull``). Kept as a loose alias so tests
# can pass a sentinel/spy without matching the full keyword-only signature.
ImportRescueFn = Callable[..., RescueResult]


@dataclass(frozen=True)
class PullSummary:
    """What a ``data_pull`` did — for the CLI's completion line (slice 38b).

    ``targets_pulled`` counts targets/outs landed on disk, NOT files (a
    dir-out counts once however many files it contains), and its basis
    differs per branch:

    - fast-sync ran to a result: fast-synced outs + fallback targets +
      uncovered stage outs the catch-all pulled + blocked targets checkout
      satisfied from cache — everything that landed on disk (checkout
      targets that never materialized are subtracted);
    - fast-sync raised: the REQUESTED/discovered targets minus those the
      recovery checkout could not materialize, best-effort (the fallback
      ``dvc pull`` reports no per-target count);
    - no fast-sync available: the number of REQUESTED targets (a pull-all
      reports 0).

    ``error_count`` drives the CLI's non-zero exit — every target left
    absent from the workspace:

    - ``FastPullResult.blocked_targets`` still unserved after the
      cache-rescue probe (version-aware, so no plain ``dvc pull`` fallback);
    - ``incomplete_targets`` (per-file download failures);
    - checkout targets ``dvc checkout`` claimed (exit 0) but never
      materialized, even after a single-target retry — any out shape, not
      only version-aware.

    On the crash branch, error_count counts every unmaterialized recovery
    target (dvc.lock stage outs included) while targets_pulled subtracts
    only requested ones — so error_count can legitimately exceed the number
    of requested targets. Each errored target was already reported via
    ``reporter.error`` with a targeted-retry hint.
    """
    targets_pulled: int
    total_bytes: int
    elapsed_s: float
    error_count: int = 0


@dataclass(frozen=True)
class PushSummary:
    """What a ``data_push`` did — for the CLI's completion line (slice 48).

    Mirrors ``PullSummary``. ``pushed``/``bytes`` are best-effort (dvc push has
    no ``--json``; the count is scraped, bytes are never reported), so both are
    optional. ``up_to_date`` distinguishes a no-op from a real upload.
    """
    remote: str
    pushed: int | None
    bytes: int | None
    elapsed_s: float
    up_to_date: bool


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


def _compute_total_bytes(
    project_path: Path,
    targets: list[str],
    pipeline_outs: list[DvcOut],
    remote_name: str,
) -> int:
    """Expected bytes-on-the-wire, for the progress bar's total.

    Filesystem-only (parse .dvc files; sub-second on realistic repos).
    ``outs_for_target`` applies the same normalization classify_targets
    does — a denormalized target that fast-syncs fine must not be counted
    as 0 bytes here — and returns [] for malformed or missing .dvc files
    (fast-sync routes those to the fallback pull).
    """
    total_bytes = 0
    for t in targets:
        for out in outs_for_target(project_path, t, remote_name):
            total_bytes += _out_aggregate_bytes(out)
    for out in pipeline_outs:
        total_bytes += _out_aggregate_bytes(out)
    return total_bytes


def _split_cached(
    project_path: Path,
    candidates: list[str],
    remote_name: str,
    pipeline_outs: list[DvcOut],
) -> tuple[list[str], list[str]]:
    """Probe the local cache and split ``candidates`` into
    ``(cached, uncached)`` — a complete, order-preserving partition.

    A cached target is one whose every pinned blob is verifiably in the
    local DVC cache (see ``cached_targets`` in _fast_sync_ops): `dvc
    checkout` can materialize it with zero network, regardless of what
    went wrong on the remote.
    """
    cached = cached_targets(project_path, candidates, remote_name, pipeline_outs)
    cached_set = set(cached)
    return cached, [t for t in candidates if t not in cached_set]


def _checkout_grouped(dvc_ops: DvcOps, targets: list[str]) -> None:
    """Run ``dvc checkout`` without ever mixing ``.dvc`` file paths and bare
    out-path strings (dvc.lock stage outs, suffix-less user targets) in one
    argv.

    dvc 3.67.1's ``index_from_targets`` (dvc/repo/index.py) has a per-target
    fast path that builds one Index per argv entry: ``.dvc`` paths load via
    ``Index.from_file``, anything else goes to ``repo.stage.collect`` — which
    treats a bare out PATH as a dvc.yaml STAGE NAME and raises StageNotFound.
    The swallowing ``except`` aborts the fast path but leaks the loop
    variable: ``index`` stays bound to the LAST ``.dvc`` target's
    single-target Index instead of resetting, so the ``if index is None``
    fallback to ``repo.index`` never runs and the checkout exits 0 having
    materialized essentially nothing (the 37 GB-cached, one-out-materialized
    clone bug). Homogeneous argvs are safe: an all-``.dvc`` argv completes
    the fast path, and an all-bare argv aborts it on the FIRST target while
    ``index`` is still None, taking the correct repo.index + granular-collect
    fallback. Repro recipe and removal criterion:
    notes/issue-dvc-checkout-mixed-argv.md.
    """
    dvc_file_targets = [t for t in targets if t.endswith(".dvc")]
    bare_targets = [t for t in targets if not t.endswith(".dvc")]
    if dvc_file_targets:
        dvc_ops.checkout(targets=dvc_file_targets)
    if bare_targets:
        dvc_ops.checkout(targets=bare_targets)


def _verify_and_retry_checkout(
    project_path: Path,
    checkout_targets: list[str],
    remote_name: str,
    pipeline_outs: list[DvcOut],
    *,
    dvc_ops: DvcOps,
) -> list[str]:
    """Post-checkout guard: ``dvc checkout`` can exit 0 without materializing
    its targets (dvc 3.67.1 ``index_from_targets`` leak — see
    ``_checkout_grouped``), so trust nothing: stat every checkout target's
    workspace path(s). Each missing target gets ONE single-target
    ``dvc checkout <target>`` retry (proven to work where the multi-target
    call silently no-opped); the still-missing are returned for loud
    reporting and the non-zero exit.

    Healthy-path cost is trivial: .dvc parse + stat per target, no
    subprocess. Targets whose outs can't be parsed (missing/malformed .dvc —
    the fallback-pull shapes) are unverifiable and skipped. Target→outs
    resolution is shared with the cache probe (``resolve_target_outs``):
    this pass stats exactly what that probe promised checkout could
    materialize.
    """
    pipeline_by_target = {o.target: o for o in pipeline_outs}
    still_missing: list[str] = []
    for target in checkout_targets:
        outs = resolve_target_outs(
            project_path, target, remote_name, pipeline_by_target,
        )
        if not outs:
            continue
        if outs_materialized(project_path, outs):
            continue
        logger.warning(
            "dvc checkout exited 0 but %s is missing from the workspace; "
            "retrying with a single-target checkout", target,
        )
        dvc_ops.checkout(targets=[target])
        if not outs_materialized(project_path, outs):
            still_missing.append(target)
    return still_missing


def _report_pull_failure(
    reporter: "Reporter", target: str, why: str, *, hint: str | None = None
) -> None:
    """THE composition site for the per-target pull-failure error: every
    lane that leaves a target absent from the workspace emits the same
    ``cannot pull <target>: <why>`` frame with the targeted-retry hint
    (``pull_retry_hint``); only ``why`` is lane-specific.

    ``hint`` overrides the default ``pull_retry_hint(target)`` — the import
    rescue lane passes its own actionable hint (e.g. "ask the producer to
    re-push") when a plain retry would not help."""
    reporter.error(
        f"cannot pull {target}: {why}",
        hint=hint if hint is not None else pull_retry_hint(target),
    )


def _report_not_materialized(reporter: "Reporter", targets: list[str]) -> None:
    """One pull-failure error per target ``dvc checkout`` claimed to serve
    but left absent from the workspace (even after the single-target
    retry)."""
    for t in targets:
        _report_pull_failure(
            reporter, t,
            "not materialized by dvc checkout (exit 0, but the workspace "
            "path is still missing after a single-target retry)",
        )


def _checkout_pull_verify(
    project_path: Path,
    checkout_targets: list[str],
    pull_targets: list[str],
    pipeline_outs: list[DvcOut],
    remote_name: str,
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
    aws_profile_name: str | None = None,
    import_rescue: "ImportRescueFn" = rescue_import_pull,
) -> list[str]:
    """The degraded-path materialization contract, stated once for both the
    fast-sync result branch and crash recovery:

    1. grouped ``dvc checkout`` of the fully-cached ``checkout_targets`` —
       BEFORE the pull, so a hanging/crashing ``dvc pull`` can never leave
       a fresh clone with zero workspace data;
    2. ``dvc pull`` of ``pull_targets`` — non-imports in one batched fatal
       pull (today's behavior); each dvc-import pulled one-target-per-argv
       with its ``dvc pull`` failure absorbed and the import-rescue lane
       (direct producer-bucket fetch) tried when the import did not
       materialize (see ``_import_rescue_ops``);
    3. verify-and-retry every checkout target — AFTER the pull, so the
       checkout-before-pull ordering above is unchanged;
    4. report the still-missing (when a reporter exists) and return them
       for the caller's error accounting — checkout misses plus any import
       whose rescue also failed.

    ``pipeline_outs`` must be the same out list the checkout candidates
    were resolved against: ``all_pipeline`` on the crash path (candidates =
    targets + every stage out), the fast-syncable subset on the result
    branch (its checkout_targets only ever contain fast-syncable stage
    targets).
    """
    if checkout_targets:
        _checkout_grouped(dvc_ops, checkout_targets)

    rescue_failed = _pull_targets_with_import_rescue(
        project_path, pull_targets, remote_name,
        dvc_ops=dvc_ops, remote=remote, jobs=jobs,
        extra_dvc_args=extra_dvc_args, reporter=reporter,
        aws_profile_name=aws_profile_name, import_rescue=import_rescue,
    )

    not_materialized: list[str] = []
    if checkout_targets:
        not_materialized = _verify_and_retry_checkout(
            project_path, checkout_targets, remote_name, pipeline_outs,
            dvc_ops=dvc_ops,
        )
        # _report_not_materialized fires ONLY for the checkout-target verify
        # list; rescue failures were already reported at the hook site with
        # their own (producer-specific) hint. Keep the two lanes disjoint so
        # no target is double-reported.
        if not_materialized and reporter is not None:
            _report_not_materialized(reporter, not_materialized)
    return not_materialized + rescue_failed


def _pull_targets_with_import_rescue(
    project_path: Path,
    pull_targets: list[str],
    remote_name: str,
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
    aws_profile_name: str | None,
    import_rescue: "ImportRescueFn",
) -> list[str]:
    """Pull ``pull_targets``: non-imports in one batched fatal ``dvc pull``,
    each dvc-import serialized with the rescue lane behind a failed/non-
    materializing pull. Returns the imports whose rescue also failed (already
    reported); each was left absent from the workspace and drives the
    caller's non-zero exit."""
    if not pull_targets:
        return []

    import_targets: list[str] = []
    non_import_targets: list[str] = []
    for t in pull_targets:
        outs = outs_for_target(project_path, t, remote_name)
        if outs and outs[0].is_import:
            import_targets.append(t)
        else:
            non_import_targets.append(t)

    # Non-imports keep today's single batched fatal pull (a failure raises
    # and aborts the whole pull, unchanged).
    if non_import_targets:
        dvc_ops.pull(
            targets=non_import_targets,
            remote=remote, jobs=jobs, extra_args=extra_dvc_args,
        )

    # One producer-resolution cache shared across every import in this pull
    # run so several imports from one producer fetch its config once.
    producer_cache: dict[tuple[str, str], object] = {}
    rescue_failed: list[str] = []
    for t in import_targets:
        pull_raised = False
        try:
            dvc_ops.pull(
                targets=[t], remote=remote, jobs=jobs, extra_args=extra_dvc_args,
            )
        except (DvcPullError, DvcStorageKeyError) as exc:
            # The documented import failure shape: dvc pull cannot materialize
            # a version-aware import whose producer lock recorded no
            # version_id. Absorb it; the rescue lane below completes the pull.
            pull_raised = True
            logger.info(
                "import %r: dvc pull did not materialize it (%s); "
                "trying the import-rescue lane", t, exc,
            )
        # A healthy import that dvc pull materialized (the pull returned
        # cleanly) never touches the rescue lane: the stat probe confirms it.
        # But a RAISED pull cannot be trusted to the stat probe — legacy
        # producers' git-tracked riders (readme, .gitkeep) ride in via the
        # erepo git clone and leave the out dir non-empty while every
        # dvc-tracked file is still missing, so ``outs_materialized`` would
        # wrongly report the import materialized, skip the rescue, and turn the
        # absorbed failure into a silent exit-0 success with the payload gone.
        # When the pull raised, always run the rescue (it skips already-cached
        # blobs and re-runs checkout, so a genuinely-materialized import is
        # cheap and still verified).
        outs = outs_for_target(project_path, t, remote_name)
        if not pull_raised and outs and outs_materialized(project_path, outs):
            continue
        result: RescueResult = import_rescue(
            project_path, t,
            dvc_ops=dvc_ops,
            aws_profile_name=aws_profile_name,
            reporter=reporter,
            _producer_cache=producer_cache,  # type: ignore[arg-type]
        )
        if not result.ok:
            rescue_failed.append(t)
            if reporter is not None:
                _report_pull_failure(reporter, t, result.reason, hint=result.hint)
    return rescue_failed


def _finish_after_crash_recovery(
    project_path: Path,
    targets: list[str],
    all_pipeline: list[DvcOut],
    remote_name: str,
    total_bytes: int,
    start_t: float,
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
    aws_profile_name: str | None = None,
    import_rescue: "ImportRescueFn" = rescue_import_pull,
) -> PullSummary:
    """Fast-sync raised mid-run: materialize what it already paid for,
    ``dvc pull`` the rest, and build the crash-branch ``PullSummary``.

    Checkout candidates are the requested/discovered targets plus every
    dvc.lock stage out (``all_pipeline`` — covered AND uncovered, so
    dvc.yaml pipeline stages aren't silently dropped); whatever is fully
    cached is materialized via ``_checkout_pull_verify``. The fallback pull
    covers the uncached rest — never targets=None: a blanket pull would
    re-validate the just-checked-out version-aware outs and re-trigger DVC
    3.66.1's rehash-on-pull pathology (SLICE-42).

    Accounting bases differ deliberately (see ``PullSummary``): candidates
    include stage outs, but ``targets_pulled`` counts only the REQUESTED/
    discovered .dvc targets — verify failures are subtracted from that same
    basis (or a failed stage out would understate how many user targets
    landed), while ``error_count`` counts every failure, stage outs
    included.
    """
    candidates = list(dict.fromkeys(
        targets + [out.target for out in all_pipeline]
    ))
    cached, rest = _split_cached(project_path, candidates, remote_name, all_pipeline)
    not_materialized = _checkout_pull_verify(
        project_path, cached, rest, all_pipeline, remote_name,
        dvc_ops=dvc_ops, remote=remote, jobs=jobs,
        extra_dvc_args=extra_dvc_args, reporter=reporter,
        aws_profile_name=aws_profile_name, import_rescue=import_rescue,
    )
    requested = set(targets)
    missing_requested = [t for t in not_materialized if t in requested]
    return PullSummary(
        targets_pulled=max(0, len(targets) - len(missing_requested)),
        total_bytes=total_bytes,
        elapsed_s=time.monotonic() - start_t,
        error_count=len(not_materialized),
    )


def _report_unserved_targets(
    reporter: "Reporter",
    result: FastPullResult,
    hard_blocked_targets: list[str],
    *,
    project_path: Path,
    remote_name: str,
) -> None:
    """One ``reporter.error`` per target left absent from the workspace,
    each with the targeted-retry hint.

    Version-aware targets fast-sync could not serve fail LOUDLY: they are
    NEITHER checked out (nothing verified in cache) NOR fed to plain
    ``dvc pull`` (documented broken on version-aware outs). Blocked targets
    carry their producer reason; incomplete targets (per-file download
    failures) end in the same workspace state — the out is absent — so they
    get the same error shape, with the failed-file count as the reason
    (try_fast_pull already named each failed file via reporter.warn).
    """
    unserved: list[tuple[str, str]] = [
        (
            t,
            result.blocked_reasons.get(t)
            or result.reason
            or "fast-sync could not serve this target",
        )
        for t in hard_blocked_targets
    ]
    for t in result.incomplete_targets:
        n_failed = sum(
            1 for f in result.files_dir_failures
            if f.startswith(f"{t}: ")
        )
        count = f"{n_failed} file(s)" if n_failed else "file(s)"
        unserved.append((t, f"{count} failed to download after retries"))
    for t, why in unserved:
        # The version-aware "fallback not attempted" suffix is true only for
        # non-imports: imports are routed to the fallback pull (and then the
        # rescue lane) at classify time, so they never reach this path. Guard
        # defensively so the misleading suffix can never attach to an import.
        outs = outs_for_target(project_path, t, remote_name)
        is_import = bool(outs and outs[0].is_import)
        suffix = (
            ""
            if is_import
            else " — version-aware output, so the plain `dvc pull` "
            "fallback was not attempted"
        )
        _report_pull_failure(reporter, t, f"{why}{suffix}")


@dataclass(frozen=True)
class _Materialized:
    """What the fast-sync result branch landed (or failed to land) on disk —
    exactly the inputs ``data_pull``'s summary arithmetic needs beyond the
    ``FastPullResult`` itself.

    - ``cached_blocked``: blocked targets the cache probe rescued (checked
      out, count as pulled);
    - ``hard_blocked``: blocked targets still unserved (reported, count as
      errors);
    - ``not_materialized``: checkout targets absent from the workspace even
      after the single-target retry (reported, count as errors, subtracted
      from the pulled count).
    """
    cached_blocked: list[str]
    hard_blocked: list[str]
    not_materialized: list[str]


def _materialize_fast_sync_result(
    project_path: Path,
    result: FastPullResult,
    targets: list[str],
    pipeline_outs: list[DvcOut],
    remote_name: str,
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
    aws_profile_name: str | None = None,
    import_rescue: "ImportRescueFn" = rescue_import_pull,
) -> _Materialized:
    """Route a completed ``FastPullResult`` onto disk: split the blocked and
    fallback buckets by the local-cache probe, report the hard-blocked, then
    run the checkout → pull → verify sequence (``_checkout_pull_verify``).
    """
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
    # A blocked target whose pinned blobs are ALL in the local cache is
    # locally satisfiable — `dvc checkout` materializes it with zero
    # network, so the guard/drift reason is moot for it. Probe BEFORE
    # reporting: fully-cached blocked targets join the checkout set and
    # drop out of the error accounting entirely (a prior run or an
    # interrupted fast-sync already paid for their blobs). The rest
    # error loudly and drive the CLI's non-zero exit via
    # PullSummary.error_count.
    cached_blocked, hard_blocked = _split_cached(
        project_path, result.blocked_targets, remote_name, pipeline_outs,
    )
    if reporter is not None:
        _report_unserved_targets(
            reporter, result, hard_blocked,
            project_path=project_path, remote_name=remote_name,
        )

    # Every unserved target is excluded from checkout: fallback targets
    # aren't in cache yet (unless the probe below rescues them),
    # incomplete targets have partial cache blobs, blocked targets have
    # nothing verified. Whatever remains was fast-synced into the cache.
    unserved = (
        set(result.fallback_targets)
        | set(result.incomplete_targets)
        | set(result.blocked_targets)
    )
    pipeline_target_ids = [out.target for out in pipeline_outs]
    candidate_synced = targets + pipeline_target_ids
    synced_targets = [t for t in candidate_synced if t not in unserved]

    # Fallback targets whose pinned blobs are already fully in the
    # local cache (fast-sync fetched them before degrading, or a prior
    # run did) are checked out with the synced set and EXCLUDED from
    # the fallback pull — never hand an already-cached out back to
    # plain dvc pull. Covers the all-fallback guards where
    # synced_targets is empty and checkout used to be skipped entirely.
    # dvc-imports never qualify (ensure_out_cached), so slice 29's
    # route-to-dvc-pull holds — and when that plain dvc pull cannot
    # materialize the import, _checkout_pull_verify's per-import rescue lane
    # (direct producer-bucket fetch) completes it.
    cached_fallback, remaining_fallback = _split_cached(
        project_path, result.fallback_targets, remote_name, pipeline_outs,
    )
    checkout_targets = synced_targets + cached_fallback + cached_blocked

    not_materialized = _checkout_pull_verify(
        project_path, checkout_targets, remaining_fallback, pipeline_outs,
        remote_name, dvc_ops=dvc_ops, remote=remote, jobs=jobs,
        extra_dvc_args=extra_dvc_args, reporter=reporter,
        aws_profile_name=aws_profile_name, import_rescue=import_rescue,
    )
    return _Materialized(
        cached_blocked=cached_blocked,
        hard_blocked=hard_blocked,
        not_materialized=not_materialized,
    )


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
    aws_profile_name: str | None = None,
    import_rescue: "ImportRescueFn" = rescue_import_pull,
) -> PullSummary:
    """Pull dvc-tracked data via fast-sync (boto3 → cache) when available;
    fall back to ``dvc pull`` for anything fast-sync can't handle.

    Slice 26: when ``targets is None`` and fast_sync_ops is available,
    discovers all ``.dvc`` files in the project and routes through
    fast-sync. Without discovery, the call would fall through to
    ``dvc pull`` directly and hit DVC 3.66.1's cache-write bug on
    version_aware buckets.

    Returns a ``PullSummary`` (target count, total bytes, elapsed) so the CLI
    can render an informative completion line (slice 38b).
    """
    start_t = time.monotonic()
    if fast_sync_ops is None:
        dvc_ops.pull(
            targets=targets, remote=remote, jobs=jobs, extra_args=extra_dvc_args,
        )
        return PullSummary(
            targets_pulled=len(targets or []),
            total_bytes=0,
            elapsed_s=time.monotonic() - start_t,
        )

    # Track the original request shape: "pull-all" (None) carries different
    # post-fast-sync semantics than "pull these specific .dvc files."
    pull_all_requested = targets is None
    remote_name = remote or _default_dvc_remote(project_path) or "origin"

    # dvc.lock stage outs are discovered ONLY on pull-all. A targeted pull
    # of a bare stage-out path (e.g. `data pull data/staged`, no .dvc file)
    # therefore never fast-syncs: classify_targets finds no `<path>.dvc`,
    # routes it to `fallback`, and it lands via a scoped `dvc pull <path>`.
    # That is correct and safe (scoped, never targets=None; single
    # homogeneous argv, so the mixed-argv checkout bug can't fire; dvc pull
    # raises loudly on failure) but bypasses fast-sync, so a large
    # version-aware stage out pulled by name uses plain dvc pull rather than
    # the direct version-keyed fetch. Pinned by
    # test_data_pull_targeted_bare_stage_out_routes_to_scoped_fallback.
    # Fast-syncing targeted stage outs is a deferred enhancement.
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

    total_bytes = _compute_total_bytes(
        project_path, targets or [], pipeline_outs, remote_name,
    )

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

    # Fast-sync raised — check out whatever is already fully cached, then
    # fall back to dvc pull for the rest. MUST happen OUTSIDE the progress
    # widget's with-block; otherwise dvc's subprocess output corrupts the
    # active rich.Progress render.
    if fast_sync_failed:
        return _finish_after_crash_recovery(
            project_path, targets or [], all_pipeline, remote_name,
            total_bytes, start_t,
            dvc_ops=dvc_ops, remote=remote, jobs=jobs,
            extra_dvc_args=extra_dvc_args, reporter=reporter,
            aws_profile_name=aws_profile_name, import_rescue=import_rescue,
        )

    mat = _Materialized(cached_blocked=[], hard_blocked=[], not_materialized=[])
    if result is not None:
        mat = _materialize_fast_sync_result(
            project_path, result, targets or [], pipeline_outs, remote_name,
            dvc_ops=dvc_ops, remote=remote, jobs=jobs,
            extra_dvc_args=extra_dvc_args, reporter=reporter,
            aws_profile_name=aws_profile_name, import_rescue=import_rescue,
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
    # Count fast-synced outputs, the dvc-pull fallback, the uncovered
    # stage outs the catch-all pulled, and the blocked targets checkout
    # satisfied from cache — all land on disk, so all belong in the
    # target count. A checkout target that never materialized (even after
    # its single-target retry) did NOT land, so it is subtracted.
    synced_count = (
        result.synced_count
        + len(result.fallback_targets)
        + len(uncovered)
        + len(mat.cached_blocked)
        if result is not None
        else len(targets or []) + len(uncovered)
    )
    synced_count = max(0, synced_count - len(mat.not_materialized))
    # Non-zero exit signal: blocked targets the cache probe couldn't rescue
    # (guard/drift/unsyncable) PLUS incomplete targets PLUS checkout targets
    # that never materialized — all leave the out absent from the workspace.
    error_count = (
        len(mat.hard_blocked)
        + len(result.incomplete_targets)
        + len(mat.not_materialized)
        if result is not None
        else 0
    )
    return PullSummary(
        targets_pulled=synced_count,
        total_bytes=total_bytes,
        elapsed_s=time.monotonic() - start_t,
        error_count=error_count,
    )


def _default_dvc_remote(project_path: Path) -> str | None:
    """Pick the dvc remote name for the project.

    Order:
      1. ``[core] remote = <name>`` if present (DVC's standard default).
      2. The single ``[remote "..."]`` section if there's exactly one
         (covers freshly-cloned data products: their .dvc/config typically
         declares one remote per product and no [core] default).
      3. None — caller defaults to "origin".

    Name-resolution logic is hoisted into
    ``_fast_sync_ops._default_remote_name_from_config`` so it is written once
    (shared with ``parse_remote_config_text``'s default-remote path).
    """
    import configparser

    from ._fast_sync_ops import _default_remote_name_from_config
    config_file = project_path / ".dvc" / "config"
    if not config_file.is_file():
        return None
    cp = configparser.ConfigParser()
    try:
        cp.read(config_file)
    except configparser.Error:
        return None
    return _default_remote_name_from_config(cp)


def data_push(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    remote: str | None = None,
    jobs: int | None = None,
) -> PushSummary:
    # Resolve the effective remote for display only (explicit > .dvc/config >
    # "origin"); the actual dvc push still gets the raw ``remote`` so dvc
    # applies its own default when None. ``targets`` pass straight through to
    # ``dvc push <targets>`` (None = push everything tracked, dvc's default).
    # Push deliberately has no fast-sync/grouping analog: those exist only to
    # work around dvc's pull/checkout-side materialization bugs, whereas push
    # uploads from an intact local cache.
    effective_remote = remote or _default_dvc_remote(project_path) or "origin"
    start_t = time.monotonic()
    result: DvcPushResult = dvc_ops.push(targets=targets, remote=remote, jobs=jobs)
    return PushSummary(
        remote=effective_remote,
        pushed=result.pushed,
        bytes=result.bytes,
        elapsed_s=time.monotonic() - start_t,
        up_to_date=result.up_to_date,
    )


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
