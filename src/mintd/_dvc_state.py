"""What DVC actually tracks, and whether a declared metadata path reaches it.

**Placement invariant — `_fast_sync_ops` must never import this module.**
`_cache_ops` imports `_fast_sync_ops` (`_cache_ops.py:35`), and `_fast_sync_ops`
imports `_cache_ops` nowhere, so `_cache_ops -> _dvc_state -> _fast_sync_ops` is
a chain. A resolver living in `_fast_sync_ops` while calling
`_cache_ops._all_dvc_outs` would close that cycle, which is why the placement was
a decision (Option A, 2026-08-14) rather than a preference. Pinned by
`tests/test_dvc_state.py::test_dvc_state_has_no_import_cycle`.

`metadata.json` carries a human's CLAIM about a product's outputs
(`data_products.primary`, `data_products.outputs[].path`); DVC carries the truth.
The two are spelled differently on purpose: a producer declares a directory
(`data/final/`), while DVC tracks whatever is beneath it — per-file `.dvc`
pointers, `dvc.lock` stage outs, or both in the same prefix.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from ._fast_sync_ops import (
    DvcOut,
    discover_all_outs,
    normalize_target,
    outs_for_target,
    partition_pipeline_outs,
    workspace_path_for,
)


def _all_dvc_outs(project_path: Path, remote_name: str) -> list:
    """Every DVC out for the project: ``.dvc`` pointers (via
    ``discover_all_outs`` + ``outs_for_target``) plus ``dvc.lock`` stage outs.
    The single enumeration both the collision guard and the tracked-path set
    read from, so neither can drift from the other's view of DVC reality."""
    outs = []
    for target in discover_all_outs(project_path):
        outs.extend(outs_for_target(project_path, target, remote_name))
    _, lock_outs = partition_pipeline_outs(project_path, remote_name)
    outs.extend(lock_outs)
    return outs


def path_covers(parent: str, child: str) -> bool:
    """True iff ``child`` is ``parent`` or lives beneath it, case-folded.

    The one containment rule, relocated from ``_cache_ops._is_tracked`` rather
    than paraphrased beside it. Case-folded for the reason that predicate
    states: on a case-insensitive filesystem ``data/FINAL.parquet`` is the same
    file as ``data/final.parquet``.

    ``outs_matching``'s directions 1 and 2 are this call with
    ``(tracked, declared)``; direction 3 is the same call with the arguments
    swapped.
    """
    p, c = parent.casefold(), child.casefold()
    return c == p or c.startswith(f"{p}/")


def outs_matching(project_path: Path, declared: str, remote_name: str) -> list[DvcOut]:
    """Every DVC out that ``declared`` reaches, in three directions:

    1. ``declared`` **equals** a tracked out;
    2. ``declared`` **lives under** a tracked directory out;
    3. **at least one tracked out lives under** ``declared``.

    Direction 3 is the one every other matcher in the tree omits, and it is the
    ordinary lab shape: a product declares ``data/final/`` while DVC tracks 26
    per-file outs beneath it and nothing at the directory itself.

    Three boundaries, stated because the check gate, the clone selectors and the
    delivery postcondition all consume this:

    **The ``target`` is stamped, not filtered, and always project-relative.**
    ``parse_dvc_outs`` hard-codes ``target=""`` (`_fast_sync_ops.py:429`); only
    ``classify_targets`` stamps it. A resolver that merely filtered
    ``_all_dvc_outs`` would hand ``dvc pull`` an empty string, and ``dvc pull
    ''`` does not fail — it pulls the WHOLE repo at exit 0, turning a selector
    into a silent full-product download reported as success. The spelling is the
    project-relative workspace path for pointer outs AND lock outs alike, never
    ``X.dvc`` for pointers: a mixed argv trips dvc 3.67.1's
    ``index_from_targets`` defect (`notes/issues/issue-dvc-checkout-mixed-argv.md`,
    whose field incident cached ~37 GB and materialized ONE out at exit 0), which
    is why ``data_ops._checkout_grouped`` already keeps every argv homogeneous.
    The project-relative form still resolves for a pointer out because
    ``classify_targets`` appends ``.dvc`` itself.

    **Direction 2 over-delivers.** A declared subpath resolves to its
    *enclosing* directory out, so pulling the result also lands that subpath's
    siblings. Correct here — this returns OUTS, and the out is the directory —
    but a caller must not report "delivered exactly what you asked for" off the
    back of it. Narrowing to the pinned ``.dir`` manifest is in neither this
    contract nor the ``outs_materialized`` deepening's scope.

    **The workspace is re-enumerated on every call** (a full walk plus a reparse
    of every ``.dvc`` and ``dvc.lock``). Harmless today; whoever adds a caller
    that resolves many declared paths against one project should pass the out
    list in rather than memoizing here.
    """
    root = project_path.resolve()
    declared_rel = normalize_target(declared)
    if not declared_rel:
        return []

    matched: list[DvcOut] = []
    for out in _all_dvc_outs(project_path, remote_name):
        try:
            rel = workspace_path_for(project_path, out).resolve().relative_to(root).as_posix()
        except ValueError:
            continue  # resolves outside the project; same skip as dvc_tracked_paths
        if path_covers(rel, declared_rel) or path_covers(declared_rel, rel):
            matched.append(dataclasses.replace(out, target=rel))
    return matched
