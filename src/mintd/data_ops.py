"""`mintd data` family — thin pass-throughs over DvcOps with fast_sync hook.

Distinct module from slice-7's `data.py` (which holds `import_product`
and `bump_import`). The naming asymmetry is deliberate: `data.py` is the
*catalog-aware* family (uses CatalogClient + DvcOps), while `data_ops.py`
is the *project-local* family (the user's own data): DVC, plus the git
commit and push of the pointer files `data push` rewrites
(`commit_pushed_pointers`, through `RegistryGitOps`). They could share a module
in a slice-19 cleanup; for now, the rename would touch slice 7-12 tests.

``data_pull``'s degraded-path behavior (checkout-before-pull ordering, the
fallback/blocked/incomplete routing, the scoped catch-all) implements the
pull-all audit.
"""

from __future__ import annotations

import logging
import os
import re
import hashlib
import shlex
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from ._dvc_ops import (
    DvcOpError,
    DvcOps,
    DvcPullError,
    DvcPushResult,
    DvcStorageKeyError,
    pull_retry_hint,
)
from ._dvc_state import (
    DvcOut,
    PointerCutShort,
    discover_all_outs,
    outs_for_target,
    outs_materialized,
    partition_pipeline_outs,
    resolve_target_outs,
    skipped_targets,
)
from ._fast_sync_ops import (
    FastSyncOps,
    cached_targets,
    ensure_out_cached,
)
from ._dvc_state import (
    _all_dvc_outs,
    _names_a_stage,
    abbreviate,
    does_not_parse,
    dvc_cache_dir,
    judge_pointers,
    outs_matching,
    push_targets,
    require_default_remote,
    stages_behind,
    with_outputs,
)
from ._pin_proof import unlisted_on_disk
from ._import_rescue_ops import RescueResult, rescue_import_pull
from ._registry_git_ops import GitOpError, GitPushRejected, RegistryGitOps, is_pointer_path
from ._storage_state import inspect_storage
from ._subprocess import WallTimeoutExceeded
from .errors import MintdError
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
      ``dvc pull`` reports no per-target count).

    ``error_count`` drives the CLI's non-zero exit — every target left
    absent from the workspace:

    - ``FastPullResult.blocked_targets`` still unserved after the
      cache-rescue probe (version-aware, so no plain ``dvc pull`` fallback);
    - ``incomplete_targets`` (per-file download failures);
    - checkout targets ``dvc checkout`` claimed (exit 0) but never
      materialized, even after a single-target retry — any out shape, not
      only version-aware;
    - DVC-lane pull failures (the non-import batch and the catch-all): a
      target whose ``dvc pull`` raised even when retried alone, or which
      exited 0 without materializing (``_pull_isolating_failures``). A
      per-target ``DvcPullError`` never escapes ``data_pull``; only
      ``error_count`` signals it.

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
    # The targets behind ``error_count``, each already reported, so a caller
    # with its own post-pull check (clone's sub-path check) does not report
    # the same target a second time.
    failed_targets: tuple[str, ...] = ()


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
    # The ``.dvc`` paths and ``dvc.yaml:<stage>`` keys a targeted push
    # uploaded whole (``_dvc_state.push_targets``); None when untargeted.
    selection: frozenset[str] | None = None
    # ``_push_verify.Prepared`` on a version-aware remote: the repair it did,
    # and what ``judge`` checks after the push. None elsewhere.
    verification: Any = None


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


def _shape_groups(targets: list[str]) -> list[list[str]]:
    """``targets`` split into non-empty ``[.dvc paths]`` then ``[bare paths]``,
    so no argv ever mixes the two shapes.

    dvc checkout AND pull (pull's fetch half shares ``index_from_targets``)
    exit 0 without materializing a bare target that follows a ``.dvc`` one in
    the same argv — measured: ``dvc pull b.dvc x.dvc y.dvc s1 s2`` lands only
    ``y``, losing both stage outs AND the other pointers. Why, and the
    removal criterion: ``_checkout_grouped``.

    The suffix is the whole test, which leaves two known-narrow leaks that
    predate this split and stay open: a bare argv is only homogeneous to dvc
    while no entry resolves as a dvc.yaml STAGE NAME, so a root-level out
    whose path equals its stage name (out ``model`` of stage ``model``), or a
    stage name the user types before a path (``mintd data pull s1
    data/s2.txt``), still takes the leaking fast path. On the pull and checkout
    lanes both leaks fail LOUDLY — the ``outs_materialized`` probe names what
    did not land. Push no longer uses this split: ``_dvc_state.push_targets``
    spells every tracked target as a ``.dvc`` file or a ``dvc.yaml:<stage>``
    key, so only targets that match nothing reach dvc bare."""
    groups = ([t for t in targets if t.endswith(".dvc")],
              [t for t in targets if not t.endswith(".dvc")])
    return [g for g in groups if g]


def _checkout_grouped(dvc_ops: DvcOps, targets: list[str], *, cwd: Path) -> None:
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
    fallback.
    """
    for group in _shape_groups(targets):
        dvc_ops.checkout(targets=group, cwd=cwd)


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
        dvc_ops.checkout(targets=[target], cwd=project_path)
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


def _dvc_failure_why(exc: Exception) -> str:
    """The lane-specific ``why`` for a ``DvcPullError``: one line, without the
    ``dvc pull failed (exit N): `` framing (the CLI's summary line already says
    the pull failed) and from dvc's own ``ERROR:`` onward when it has one, so
    the preceding WARNING chatter does not bury the reason."""
    msg = " ".join(str(exc).split())
    msg = re.sub(r"^dvc pull failed \(exit \d+\): ", "", msg)
    i = msg.find("ERROR: ")
    return msg[i + len("ERROR: "):] if i >= 0 else msg


def _pull_isolating_failures(
    project_path: Path,
    targets: list[str],
    remote_name: str,
    pipeline_by_target: dict[str, DvcOut],
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
) -> list[str]:
    """Batch-first, isolate-on-failure ``dvc pull``: one batched argv per
    ``_shape_groups`` group on the happy path (one group unless this call
    really holds both shapes); only when a group raises is each of ITS
    members retried alone (idempotent — dvc skips what the batch already
    landed), so survivors land and each failure is named once. One failing
    group never stops the others. A single-target batch is not retried (the
    retry would be the same argv). Then every target that did not raise is stat-
    probed (``outs_materialized``, the same probe the checkout lane and the
    import rescue use): a zero-exit pull that left the out absent is a failure
    too; unresolvable targets (no ``.dvc``, no lock entry) stay unverifiable.
    Returns the failed targets, each already reported via
    ``_report_pull_failure`` when a reporter exists."""
    if not targets:
        return []

    def _pull(argv: list[str]) -> None:
        dvc_ops.pull(
            targets=argv, cwd=project_path, remote=remote, jobs=jobs,
            extra_args=extra_dvc_args,
        )

    failed: dict[str, str] = {}
    for group in _shape_groups(targets):
        try:
            _pull(group)
        except DvcPullError as exc:
            if len(group) == 1:
                failed[group[0]] = _dvc_failure_why(exc)
            else:
                # The exception text is dvc's raw stderr; each per-target retry
                # reports its own trimmed reason, so it is not repeated here.
                logger.warning(
                    "batched dvc pull of %d target(s) failed; retrying each target alone",
                    len(group),
                )
                for t in group:
                    try:
                        _pull([t])
                    except DvcPullError as exc_t:
                        failed[t] = _dvc_failure_why(exc_t)
    for t in targets:
        if t in failed:
            continue
        outs = resolve_target_outs(project_path, t, remote_name, pipeline_by_target)
        if outs and not outs_materialized(project_path, outs):
            failed[t] = (
                "not materialized by dvc pull (exit 0, but the workspace path "
                "is still missing)"
            )
    if reporter is not None:
        for t, why in failed.items():
            _report_pull_failure(reporter, t, why)
    return list(failed)


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
    2. ``dvc pull`` of ``pull_targets`` — non-imports batched per shape group
       (``_shape_groups``), failures isolated per target and verified on disk
       (``_pull_isolating_failures``); each dvc-import pulled one-target-per-argv
       with its ``dvc pull`` failure absorbed and the import-rescue lane
       (direct producer-bucket fetch) tried when the import did not
       materialize (see ``_import_rescue_ops``);
    3. verify-and-retry every checkout target — AFTER the pull, so the
       checkout-before-pull ordering above is unchanged;
    4. report the still-missing (when a reporter exists) and return them
       for the caller's error accounting — checkout misses, non-import pull
       failures, plus any import whose rescue also failed.

    ``pipeline_outs`` must be the same out list the checkout candidates
    were resolved against: ``all_pipeline`` on the crash path (candidates =
    targets + every stage out), the fast-syncable subset on the result
    branch (its checkout_targets only ever contain fast-syncable stage
    targets).
    """
    checkout_failed: DvcOpError | None = None
    if checkout_targets:
        try:
            _checkout_grouped(dvc_ops, checkout_targets, cwd=project_path)
        except DvcOpError as exc:  # raised once the pull's other steps, imports included, have run
            checkout_failed = exc

    pull_failed = _pull_targets_with_import_rescue(
        project_path, pull_targets, remote_name, pipeline_outs,
        dvc_ops=dvc_ops, remote=remote, jobs=jobs,
        extra_dvc_args=extra_dvc_args, reporter=reporter,
        aws_profile_name=aws_profile_name, import_rescue=import_rescue,
    )
    if checkout_failed is not None:
        raise checkout_failed

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
    return not_materialized + pull_failed


def _pull_targets_with_import_rescue(
    project_path: Path,
    pull_targets: list[str],
    remote_name: str,
    pipeline_outs: list[DvcOut],
    *,
    dvc_ops: DvcOps,
    remote: str | None,
    jobs: int | None,
    extra_dvc_args: list[str] | None,
    reporter: "Reporter | None",
    aws_profile_name: str | None,
    import_rescue: "ImportRescueFn",
) -> list[str]:
    """Pull ``pull_targets``: non-imports in a batched ``dvc pull`` per shape
    group, with per-target failure isolation; each dvc-import serialized with
    the rescue lane behind a failed/non-materializing pull. Returns the non-imports that
    failed plus the imports whose rescue also failed (all already reported);
    each was left absent from the workspace and drives the caller's non-zero
    exit."""
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

    # Non-imports: one batched pull per shape group (never a mixed argv),
    # failures isolated per target and verified on disk (never raises for a
    # per-target failure).
    pull_failed = _pull_isolating_failures(
        project_path, non_import_targets, remote_name,
        {o.target: o for o in pipeline_outs},
        dvc_ops=dvc_ops, remote=remote, jobs=jobs,
        extra_dvc_args=extra_dvc_args, reporter=reporter,
    )

    # One producer-resolution cache shared across every import in this pull
    # run so several imports from one producer fetch its config once.
    producer_cache: dict[tuple[str, str], object] = {}
    rescue_failed: list[str] = []
    for t in import_targets:
        pull_raised = False
        try:
            dvc_ops.pull(
                targets=[t], cwd=project_path, remote=remote, jobs=jobs,
                extra_args=extra_dvc_args,
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
    return pull_failed + rescue_failed


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
        failed_targets=tuple(not_materialized),
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


def _dvc_stage_names(project_path: Path, dvc_ops: DvcOps) -> set[str] | None:
    """The stages dvc lists for the root ``dvc.yaml`` (``DvcOps.stage_names``),
    or None when there's no ``dvc.lock`` to sort, dvc can't list them, or it
    lists none (an empty or vars-only ``dvc.yaml`` teaches nothing): every lock
    out is then kept, and dvc's own checkout says what's wrong."""
    if not (project_path / "dvc.lock").is_file():
        return None
    try:
        return set(dvc_ops.stage_names(cwd=project_path)) or None
    except (DvcOpError, WallTimeoutExceeded):
        return None


def data_pull(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    fast_sync_ops: FastSyncOps,
    remote: str | None = None,
    jobs: int | None = None,
    extra_dvc_args: list[str] | None = None,
    reporter: "Reporter | None" = None,
    aws_profile_name: str | None = None,
    import_rescue: "ImportRescueFn" = rescue_import_pull,
) -> PullSummary:
    """Pull dvc-tracked data via fast-sync (boto3 → cache), falling back to
    ``dvc pull`` for anything fast-sync can't handle.

    ``fast_sync_ops`` is REQUIRED (issue18). It was optional, and a ``None``
    meant "skip fast-sync entirely, issue one blanket ``dvc pull``, and
    report the number of targets REQUESTED as the number landed". That lane
    is DELETED, not disabled -- see ``cli._require_fast_sync_ops``, which is
    what now stops a boto3-less install before it reaches this function.

    Slice 26: when ``targets is None``, discovers all ``.dvc`` files in the
    project and routes through fast-sync. Without discovery, the call would
    fall through to ``dvc pull`` directly and hit DVC 3.66.1's cache-write
    bug on version_aware buckets.

    Returns a ``PullSummary`` (target count, total bytes, elapsed) so the CLI
    can render an informative completion line (slice 38b).
    """
    start_t = time.monotonic()
    # Track the original request shape: "pull-all" (None) carries different
    # post-fast-sync semantics than "pull these specific .dvc files."
    pull_all_requested = targets is None
    # DVC refuses a pointer .gitignore excludes; refuse it first, so fast-sync
    # never fetches what DVC would never pull. Pull reads targets from the root.
    for target, pointer in skipped_targets(project_path, targets or [], base=project_path):
        raise DvcPullError(
            f"{target}: DVC skips {pointer} because .gitignore rules exclude it, so it can't be pulled; "
            "change the .gitignore, or drop this target"
        )
    default_remote = remote.lower() if remote else _default_dvc_remote(project_path)  # dvc lowercases names too
    # "" names no remote: a pull of imports alone reads only their producers' buckets, and
    # dvc pull needs no remote of this project for them either. Anything else does (below).
    remote_name = default_remote or ""

    # dvc.lock stage outs are discovered ONLY on pull-all. A targeted pull
    # of a bare stage-out path (e.g. `data pull data/staged`, no .dvc file)
    # therefore never fast-syncs: classify_targets finds no `<path>.dvc`,
    # routes it to `fallback`, and it lands via a scoped `dvc pull <path>`.
    # That is correct and safe (scoped, never targets=None; single
    # homogeneous argv, so the mixed-argv checkout bug can't fire; a pull
    # failure is reported per target and counted) but bypasses fast-sync, so a large
    # version-aware stage out pulled by name uses plain dvc pull rather than
    # the direct version-keyed fetch. Pinned by
    # test_data_pull_targeted_bare_stage_out_routes_to_scoped_fallback.
    # Fast-syncing targeted stage outs is a deferred enhancement.
    pipeline_outs: list[DvcOut] = []
    all_pipeline: list[DvcOut] = []
    if pull_all_requested:
        targets = discover_all_outs(project_path)
        # A lock entry for a stage dvc.yaml no longer declares (a foreach that
        # shrank, a stage renamed) is one dvc checkout and dvc pull refuse, and
        # plain dvc pull skips: it is never listed, so never downloaded.
        stale: list[str] = []
        pipeline_outs, all_pipeline = partition_pipeline_outs(
            project_path, remote_name, declared=_dvc_stage_names(project_path, dvc_ops), skipped=stale,
        )
        if stale:
            note = (f"skipped dvc.lock entries for {len(stale)} stage(s) dvc.yaml no longer declares, "
                    f"as dvc pull does: {abbreviate(sorted(stale))}")
            if reporter is not None:
                reporter.warn(note)
            else:
                logger.warning(note)
        # dvc never pushes a `cache: false` out, so no pull can land it;
        # enumerating one turned every pull into a permanent failure.
        pipeline_outs = [o for o in pipeline_outs if o.use_cache]
        all_pipeline = [o for o in all_pipeline if o.use_cache]

        n_dvc = len(targets)
        n_pipe = len(pipeline_outs)
        if n_dvc + n_pipe:
            logger.info(
                "fast-sync: discovered %d .dvc target(s) + %d pipeline output(s)",
                n_dvc, n_pipe,
            )
        else:
            logger.info("no .dvc targets and no pipeline outs discovered")

    if default_remote is None:
        # dvc pull needs no remote of this project for imports or for what its cache holds; for other
        # data this asks for one before dvc runs, saying how to set one. A target that reaches no out
        # with a hash (a typo, an import made with --no-download, a stage named by its stage name) is
        # left to dvc, which lands it or says why. Stricter than dvc for an out with its own `remote:`.
        cache_dir = dvc_cache_dir(project_path)
        tracked: list[DvcOut] | None = None
        needed = list(all_pipeline)
        for t in targets or []:
            outs = outs_for_target(project_path, t, remote_name)
            if not outs:  # a path at, inside or above an out, a pointer's or dvc.lock's
                tracked = _all_dvc_outs(project_path, remote_name) if tracked is None else tracked
                outs = [o for o in outs_matching(project_path, t, remote_name, outs=tracked) if o.use_cache]
            needed += outs
        if not all(o.is_import or ensure_out_cached(cache_dir, o) for o in needed):
            require_default_remote(project_path, local_ok=True)

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
                except MintdError:
                    raise  # the user's to fix (a missing named AWS profile, a pointer cut short): no dvc pull fallback
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
    catch_all_failed: list[str] = []
    if pull_all_requested:
        covered = {out.target for out in pipeline_outs}
        uncovered = sorted({out.target for out in all_pipeline} - covered)
        if uncovered:
            logger.info("dvc pull for %d stage out(s) fast-sync can't serve", len(uncovered))
            catch_all_failed = _pull_isolating_failures(
                project_path, uncovered, remote_name,
                {o.target: o for o in all_pipeline},
                dvc_ops=dvc_ops, remote=remote, jobs=jobs,
                extra_dvc_args=extra_dvc_args, reporter=reporter,
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
    synced_count = max(0, synced_count - len(mat.not_materialized) - len(catch_all_failed))
    # Non-zero exit signal: blocked targets the cache probe couldn't rescue
    # (guard/drift/unsyncable) PLUS incomplete targets PLUS checkout targets
    # that never materialized (mat.not_materialized also carries the
    # non-import pull failures) PLUS catch-all pull failures — all leave the
    # out absent from the workspace. The catch-all runs on the result-less
    # branch too, so its failures are added outside the `result` guard.
    failed = (
        [*mat.hard_blocked, *result.incomplete_targets, *mat.not_materialized]
        if result is not None
        else []
    ) + catch_all_failed
    return PullSummary(
        targets_pulled=synced_count,
        total_bytes=total_bytes,
        elapsed_s=time.monotonic() - start_t,
        error_count=len(failed),
        failed_targets=tuple(failed),
    )


def _default_dvc_remote(project_path: Path) -> str | None:
    """The project's default DVC remote, as dvc picks it (``_dvc_state.default_remote_name``:
    ``core.remote`` from ``.dvc/config.local`` over ``.dvc/config``); ``None`` when there is
    none. A config dvc can't read stops here with its ``MintdError``."""
    from ._dvc_state import default_remote_name

    return default_remote_name(project_path)


def _cut_short_hint(pointers: list[str]) -> str:
    locks = [p for p in pointers if not p.endswith(".dvc")]
    ways = [*(["re-record a .dvc file with mintd data add <path>, or restore it from git"]
              if len(locks) < len(pointers) else []),
            *(["restore a dvc.lock from git, or delete it and run dvc commit -f when every stage's outputs "
               "are in the workspace"] if locks else [])]
    return "; ".join(ways) + "; then push"


def data_push(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
    remote: str | None = None,
    jobs: int | None = None,
    cwd: Path | None = None,
    aws_profile: str | None = None,
    memo_path: Path | None = None,
) -> PushSummary:
    # The effective remote is the explicit one, else .dvc/config's default; with
    # neither, push stops here, as dvc push does. dvc still gets the raw ``remote``
    # and applies its own default when None. ``targets=None`` pushes everything
    # tracked (dvc's default) in one argv.
    #
    # Push needs no fast-sync analog — it uploads from an intact local cache.
    # Targets are read relative to ``cwd`` (default: the project) and pushed
    # from the project root as whole outputs: ``_dvc_state.push_targets``
    # spells each tracked target as its ``.dvc`` file or ``dvc.yaml:<stage>``
    # key. Pushed bare, one file of a tracked folder uploads alone and dvc
    # rewrites the folder's pointer to list only it; and an argv mixing
    # ``.dvc`` paths with bare paths uploads only the last ``.dvc`` target's
    # outs at exit 0 (the ``index_from_targets`` leak ``_shape_groups``
    # documents), so the groups are never mixed.
    remote = remote.lower() if remote else remote  # dvc lowercases remote names when it looks one up
    effective_remote = remote or require_default_remote(project_path)  # dvc push refuses with no default too
    groups: list[list[str] | None] = [None]
    selection: frozenset[str] | None = None
    if targets:
        # DVC refuses a pointer .gitignore excludes; say so before any upload,
        # reading each target from the folder push_targets reads it from.
        for target, pointer in skipped_targets(project_path, targets, base=cwd or project_path):
            if not target.endswith(".dvc") and _names_a_stage(Path(cwd or project_path) / "dvc.yaml", target):
                continue  # push_targets reads a bare stage name as the stage, as dvc does
            from ._push_verify import PushRefused

            raise PushRefused(
                f"{target}: DVC skips {pointer} because .gitignore rules exclude it, so it can't be pushed",
                hint="change the .gitignore, or drop this target",
            )
        try:
            resolved, selection = push_targets(
                project_path, targets, effective_remote, cwd=cwd or project_path
            )
        except PointerCutShort as exc:  # a path target is found by reading every pointer
            from ._push_verify import PushRefused

            pointer = Path(exc.pointer).resolve().relative_to(Path(project_path).resolve()).as_posix()
            raise PushRefused(
                f"nothing was written or pushed: {pointer} is cut short or damaged ({exc.detail}), and a push "
                "naming a path reads every pointer to find what it names",
                hint=_cut_short_hint([pointer]),
            ) from exc
        groups = list(resolved)
    # On a version-aware remote: refuse what can't be checked, then repair
    # the pins in this push's scope before DVC sees them (``_push_verify``).
    # Raises ``PushRefused`` before any write or upload.
    from ._push_verify import PushRefused, prepare, pushed_dvc_files, pushed_lockfiles, pushed_pipelines, uploading

    # A target push_targets couldn't resolve reaches dvc as typed, so which
    # outs it uploads is unknown: repair (and allow changes to) every out.
    unresolved = selection is not None and any(t not in selection for g in groups if g for t in g)
    # A pointer cut short reads to DVC as tracking nothing: it would push
    # nothing for it and exit 0, so stop before anything is written.
    if selection is None or unresolved:
        scope = [*pushed_dvc_files(project_path), *pushed_lockfiles(project_path)]
    else:
        scope = sorted({k if k.endswith(".dvc") else Path(k.split(":", 1)[0]).with_name("dvc.lock").as_posix()
                        for k in selection})
    cut = [p for p in scope if does_not_parse(project_path, p)]
    if cut:
        raise PushRefused(
            f"nothing was written or pushed: {len(cut)} pointer file(s) are cut short or damaged, "
            f"so DVC would skip what they record: {abbreviate(cut)}",
            hint=_cut_short_hint(cut),
        )
    # A stage behind its dvc.yaml stops only a push that includes it: a push
    # naming other stages, or .dvc files, goes ahead. dvc names the stages
    # (foreach and matrix instances, templating), so mintd never guesses.
    stage_targets = None if selection is None or unresolved else [k for k in selection if not k.endswith(".dvc")]
    pipelines = pushed_pipelines(project_path) if stage_targets is None else sorted(
        {y for k in stage_targets if (project_path / (y := k.split(":", 1)[0])).is_file()})
    behind: list[str] = []
    others: list[str] = []
    for pipeline in pipelines:
        try:
            names = dvc_ops.stage_names(cwd=project_path / Path(pipeline).parent)
        except DvcOpError as exc:
            raise PushRefused(f"nothing was written or pushed: dvc can't read {pipeline}, so mintd can't tell "
                              f"which of its stages a push would skip: {exc}",
                              hint=f"fix {pipeline}, then push") from exc
        named = None if stage_targets is None else {
            k.split(":", 1)[1] for k in stage_targets if k.split(":", 1)[0] == pipeline}
        late = stages_behind(project_path, Path(pipeline).with_name("dvc.lock").as_posix(), names, stages=named)
        behind += [f"{pipeline}:{s}" for s in late]
        others += [f"{pipeline}:{s}" for s in with_outputs(project_path / pipeline, names) if s not in late]
    if behind:
        rest = [] if stage_targets is not None else [*others, *(p for p in scope if p.endswith(".dvc"))]
        here = Path(cwd or project_path)

        def from_here(target: str) -> str:  # the hints' commands run where mintd did
            path, colon, stage = (target, "", "") if target.endswith(".dvc") else target.partition(":")
            return f"{Path(os.path.relpath(Path(project_path) / path, here)).as_posix()}{colon}{stage}"

        raise PushRefused(
            f"nothing was written or pushed: dvc.lock records fewer outputs than dvc.yaml declares for "
            f"{abbreviate(behind)}, so DVC would push nothing for the missing ones: each stage hasn't run "
            "since, or dvc.lock was cut short",
            hint=f"run dvc repro {shlex.join(map(from_here, behind))}, then push"
            + (f"; or push the rest by naming it, for example mintd data push {shlex.join(map(from_here, rest[:3]))}"
               if rest else "")
            + "; if dvc.lock was cut short, restore it from git",
        )
    # A folder pointer lists no count, so one cut at the end of an entry, or
    # recorded before files were added, reads as a smaller folder; DVC would
    # push the shorter list at exit 0 while the rest sits on disk.
    unlisted = unlisted_on_disk(project_path, scope, remote=effective_remote)
    if unlisted:
        raise PushRefused(
            "nothing was written or pushed: " + "; ".join(u.message() for u in unlisted),
            hint=(unlisted[0].hint() if len(unlisted) == 1 else
                  "record each folder again (mintd data add <folder>, or dvc commit <stage> for a stage out), "
                  "or list files DVC should skip in .dvcignore; if a pointer was cut, restore it from git")
            + "; then push",
        )
    prepared = prepare(project_path, remote_arg=remote, selection=None if unresolved else selection,
                       aws_profile=aws_profile, memo_path=memo_path)
    # No per-group failure isolation, unlike ``_pull_isolating_failures``: a
    # raising group aborts the rest and surfaces as a non-zero exit. Deliberate
    # — push is idempotent (the re-run skips what already landed) and its
    # failure modes (credentials, remote unreachable) hit every group alike, so
    # there is nothing for a survivor list to add. A failed or interrupted
    # push leaves every pointer file as it was before this command (``uploading``).
    with uploading(prepared):
        env = prepared.env if prepared is not None else None
        start_t = time.monotonic()
        results: list[DvcPushResult] = [
            dvc_ops.push(targets=group, cwd=project_path, remote=remote, jobs=jobs, env=env)
            for group in groups
        ]
    # Best-effort counts stay best-effort: a group dvc reported no number for
    # drops out rather than counting as zero, and the push is "up to date" only
    # when every group was.
    pushed = [r.pushed for r in results if r.pushed is not None]
    byte_counts = [r.bytes for r in results if r.bytes is not None]
    return PushSummary(
        remote=effective_remote,
        pushed=sum(pushed) if pushed else None,
        bytes=sum(byte_counts) if byte_counts else None,
        elapsed_s=time.monotonic() - start_t,
        up_to_date=all(r.up_to_date for r in results),
        selection=selection,
        verification=prepared,
    )


COMMIT_MESSAGE = "Record pushed data versions"


@dataclass(frozen=True)
class PointerCommit:
    """What ``commit_pushed_pointers`` did with the pointer files.

    ``left``: nothing committed, ``reason`` says why, for a person to commit.
    ``blocked``: nothing committed, because ``blocked`` names outputs those
    files record that importers could not pull. ``commit_failed``: git commit
    failed. ``push_failed``: committed as ``commit``, not pushed. ``pushed``:
    committed and pushed to ``pushed_to``, with ``earlier`` unpushed commits
    before it. ``hint`` is the command to finish by hand; ``note`` says when
    importers will see a branch other than the one they read.
    """

    state: Literal["left", "blocked", "commit_failed", "push_failed", "pushed"]
    pointers: tuple[str, ...]
    reason: str | None = None
    blocked: tuple[str, ...] = ()
    commit: str | None = None
    pushed_to: str | None = None
    earlier: int = 0
    hint: str | None = None
    note: str | None = None


def _sha256_text(path: Path) -> str | None:
    """sha256 of the file's text with CRLF read as LF, as git stores it."""
    try:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def _last_line(exc: GitOpError) -> str:
    lines = [ln.strip() for ln in exc.stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else "git printed no reason"


def _local_redirect(root: Path, default: str, *, named: bool) -> str | None:
    """Why ``.dvc/config.local`` sends this push somewhere other than where
    importers pull, or None. dvc merges ``config.local`` over ``.dvc/config``;
    a consumer's clone has only the latter. So another ``[core] remote``, or
    the default remote's ``url``, ``version_aware`` or endpoint set locally,
    records version IDs (or an object layout) importers cannot use, or checks
    them against a service importers don't read. ``named``: the
    push named its remote (``--remote``), so ``[core] remote`` did not pick it."""
    from ._dvc_state import read_dvc_config

    local = read_dvc_config(root, "local")  # dvc can't push with an unreadable one either: its MintdError stops here
    redirected = local.get("core", {}).get("remote", "")
    if redirected and redirected != default and not named:
        return f".dvc/config.local sends pushes to {redirected}; importers pull from the default remote {default}"
    section = local["remote"].get(default, {})
    keys = sorted(k for k in ("url", "version_aware", "endpointurl", "endpoint") if k in section)
    if keys:
        return f".dvc/config.local overrides {default}'s {' and '.join(keys)}; importers use .dvc/config's"
    return None


def commit_pushed_pointers(
    root: Path,
    pointers: list[str],
    *,
    git_ops: RegistryGitOps,
    remote: str | None,
    selection: frozenset[str] | None,
    cwd: Path | None = None,
    frozen: dict[str, str | None] | None = None,
) -> PointerCommit:
    """Commit and push ``pointers`` (repo-relative ``.dvc`` / ``dvc.lock``
    files that differ from HEAD) when it is safe, after ``data push``.

    Importers read the pushed commit, so the S3 version IDs a version-aware
    push writes into these files reach nobody until they are committed and
    pushed. Every check comes before any write. Nothing is committed when the
    push went to a remote importers do not read, when the DVC project is not
    its git repository's top level or ``cwd`` is in another repository (a
    nested project or a nested clone must not commit the repo around it),
    when a pointer records something importers could not pull
    (``_dvc_state.judge_pointers``), or when any other tracked file differs
    from HEAD (a person mid-edit decides what goes in). The commit names its
    paths, so nothing else is swept in; the push goes to the branch's
    upstream of the same name, as git's own ``push.default=simple`` requires,
    with an explicit refspec, and never pulls, rebases or forces.

    ``frozen`` (``pointer -> sha256``) is what the post-push check judged.
    A pointer whose bytes differ from it now is not committed; and after the
    commit, before any ``git push``, the commit must hold exactly these
    paths with exactly these bytes, or it stays local (a commit hook that
    rewrote or added a file).
    """
    pts = tuple(pointers)

    def left(reason: str) -> PointerCommit:
        return PointerCommit("left", pts, reason=reason)

    storage = inspect_storage(root)
    default = storage.dvc_remote_name
    if default is None:
        return left("no default DVC remote in .dvc/config")
    if remote is not None and remote.lower() != default:
        return left(f"pushed to --remote {remote}; importers pull from the default remote {default}")
    redirect = _local_redirect(root, default, named=remote is not None)
    if redirect is not None:
        return left(redirect)
    try:
        top = git_ops.repo_toplevel(root).resolve()
        if top != root.resolve():
            return left("the DVC project is not the root of its git repository")
        if cwd is not None and git_ops.repo_toplevel(cwd).resolve() != top:
            return left(f"you are in another git repository than the DVC project's ({root})")
        try:
            before = git_ops.current_commit(root)
        except GitOpError:
            return left("the repository has no commit yet")
        head = {p: git_ops.head_text(root, p) for p in pts}
        judged = judge_pointers(
            root, pts, head=head, remote_name=default,
            version_aware=storage.dvc_version_aware, selection=selection,
        )
        if judged.blocked:
            return PointerCommit("blocked", pts, blocked=judged.blocked)
        if judged.not_committed:
            return left(judged.not_committed[0])
        changes = git_ops.tracked_changes(root)
        others = [path for _, path in changes if not is_pointer_path(path)]
        if others:
            return left(f"{abbreviate(others)} also differs from the last commit")
        deleting = [path for xy, path in changes if "D" in xy]
        if deleting:
            return left(f"{deleting[0]} is staged for deletion")
        if git_ops.head_text(root, ".dvc/config") is None:
            return left(".dvc/config is not committed, so importers cannot find the remote")
        if "dvc.lock" in pts and git_ops.head_text(root, "dvc.yaml") is None:
            return left("dvc.yaml is not committed")
        info = git_ops.branch_info(root)
        if info.branch is None:
            return left("HEAD is detached")
        if info.merge_ref is not None and info.merge_ref.removeprefix("refs/heads/") != info.branch:
            upstream = info.merge_ref.removeprefix("refs/heads/")
            return left(
                f"{info.branch} tracks {info.remote}/{upstream}, a branch of another name; "
                "git will not push it there either"
            )
    except GitOpError as exc:
        return left(f"git could not answer: {_last_line(exc)}")

    if frozen is not None:
        drifted = [p for p in sorted({*pts, *frozen}) if _sha256_text(root / p) != frozen.get(p)]
        if drifted:
            return left(f"{abbreviate(drifted)} changed after the push was checked")
    try:
        git_ops.commit_paths(root, list(pts), COMMIT_MESSAGE)
    except GitOpError as exc:
        # A hook or the timeout can kill git after the commit landed.
        try:
            moved = git_ops.current_commit(root) != before
        except GitOpError:
            moved = False
        if not moved:
            return PointerCommit("commit_failed", pts, reason=_last_line(exc))
    try:
        sha = git_ops.current_commit(root)
    except GitOpError as exc:
        return PointerCommit("commit_failed", pts, reason=_last_line(exc))
    if frozen is not None:
        try:
            committed = git_ops.head_paths(root)
            wrong = sorted(set(committed) ^ set(pts)) or [
                p for p in pts
                if hashlib.sha256((git_ops.head_text(root, p) or "").replace("\r\n", "\n").encode()).hexdigest()
                != frozen.get(p)
            ]
        except GitOpError as exc:
            wrong = [f"(git could not answer: {_last_line(exc)})"]
        if wrong:
            return PointerCommit(
                "push_failed", pts, commit=sha,
                reason=f"the commit doesn't hold exactly the checked pointer files: {abbreviate(wrong)} "
                "(a commit hook?); it stays local",
                hint="inspect it (git show), then git push",
            )
    if info.remote is None or info.merge_ref is None:
        return PointerCommit(
            "push_failed", pts, reason=f"{info.branch} has no upstream", commit=sha,
            hint=f"git push -u origin {info.branch}",
        )
    try:
        git_ops.push_to(root, info.remote, info.merge_ref)
    except GitPushRejected:
        return PointerCommit(
            "push_failed", pts, reason="the remote branch has commits this clone does not",
            commit=sha, hint="git pull --rebase, then git push (mintd never pulls, rebases or forces)",
        )
    except GitOpError as exc:
        return PointerCommit("push_failed", pts, reason=_last_line(exc), commit=sha, hint="git push")
    upstream = info.merge_ref.removeprefix("refs/heads/")
    note = None
    reads = git_ops.remote_default_branch(root, info.remote)
    if reads and reads != upstream:
        note = f"importers read {reads}: they see this once {upstream} merges into {reads}"
    return PointerCommit(
        "pushed", pts, commit=sha, pushed_to=f"{info.remote}/{upstream}",
        earlier=info.ahead or 0, note=note,
    )


def data_add(path: Path, *, project_path: Path, dvc_ops: DvcOps) -> Path:
    return dvc_ops.add(path, cwd=project_path)


def data_verify(
    project_path: Path,
    *,
    targets: list[str] | None = None,
    dvc_ops: DvcOps,
) -> dict[str, str]:
    return dvc_ops.status(targets=targets, cwd=project_path)


def data_remove(name: str, *, project_path: Path, dvc_ops: DvcOps) -> None:
    dvc_ops.remove(name, cwd=project_path)
