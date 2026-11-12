"""What DVC actually tracks, and whether a declared metadata path reaches it.

**Placement invariant — `_fast_sync_ops` must never import this module.**
`_cache_ops` imports `_fast_sync_ops` at its import block, and `_fast_sync_ops`
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

`outs_matching` answers "which outs does this declared path reach"; `delivery`
answers the consumer's question on top of it — "would `mintd data import` of
this path land bytes?". `check`, the import postcondition
(`data._require_deliverable`) and `data push`'s pointer probe
(`judge_pointers`) all call it rather than re-derive it, so the three verbs
cannot disagree about what "delivered" means.
"""
from __future__ import annotations

import configparser
import dataclasses
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml

from ._fast_sync_ops import (
    EMPTY_DIR_MD5,
    DvcOut,
    cache_path_for,
    dir_manifest_md5,
    discover_all_outs,
    normalize_target,
    out_pins_no_files,
    outs_for_target,
    parse_dvc_lock_outs,
    parse_dvc_outs,
    partition_pipeline_outs,
    workspace_path_for,
)


def _all_dvc_outs(project_path: Path, remote_name: str, pointers: list[str] | None = None) -> list:
    """Every DVC out for the project: ``.dvc`` pointers (via
    ``discover_all_outs`` + ``outs_for_target``) plus ``dvc.lock`` stage outs.
    The single enumeration both the collision guard and the tracked-path set
    read from, so neither can drift from the other's view of DVC reality.
    A caller that already walked the tree passes the ``*.dvc`` list in."""
    outs = []
    for target in pointers if pointers is not None else discover_all_outs(project_path):
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


def _covers_exact(parent: str, child: str) -> bool:
    """``path_covers`` without the case-fold: what a case-sensitive consumer's
    filesystem — a Linux clone, the enclave — agrees is the same path."""
    return child == parent or child.startswith(f"{parent}/")


def _workspace_rel(root: Path, out: DvcOut) -> str | None:
    """``out``'s workspace path relative to the project root, posix spelled;
    None when it escapes the project — the same skip ``dvc_tracked_paths``
    applies. Lexical on purpose: dvc checks a file out as a symlink into
    `.dvc/cache/` when reflinks and hardlinks are unavailable, and resolving
    that link would make a delivered output look untracked."""
    rel = os.path.relpath(os.path.normpath(workspace_path_for(root, out)), root)
    if rel == ".." or rel.startswith(f"..{os.sep}"):
        return None
    return Path(rel).as_posix()


def _tracked(root: Path, outs: list[DvcOut]) -> tuple[str, ...]:
    return tuple(sorted({r for o in outs if (r := _workspace_rel(root, o)) is not None}))


def outs_matching(
    project_path: Path,
    declared: str,
    remote_name: str,
    outs: list[DvcOut] | None = None,
) -> list[DvcOut]:
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
    ``index_from_targets`` defect (whose field incident cached ~37 GB and
    materialized ONE out at exit 0), which
    is why ``data_ops._checkout_grouped`` already keeps every argv homogeneous.
    The project-relative form still resolves for a pointer out because
    ``classify_targets`` appends ``.dvc`` itself.

    **Direction 2 over-delivers.** A declared subpath resolves to its
    *enclosing* directory out, so pulling the result also lands that subpath's
    siblings. Correct here — this returns OUTS, and the out is the directory —
    but a caller must not report "delivered exactly what you asked for" off the
    back of it. Narrowing to the pinned ``.dir`` manifest is in neither this
    contract nor the ``outs_materialized`` deepening's scope.

    **The workspace is re-enumerated on every call unless ``outs`` is passed**
    (a full walk plus a reparse of every ``.dvc`` and ``dvc.lock``). A caller
    resolving many declared paths against one project enumerates once with
    ``_all_dvc_outs`` and passes the list in; nothing is memoized here.
    """
    root = Path(os.path.normpath(project_path))
    declared_rel = normalize_target(declared)
    if not declared_rel:
        return []

    matched: list[DvcOut] = []
    for out in outs if outs is not None else _all_dvc_outs(project_path, remote_name):
        rel = _workspace_rel(root, out)
        if rel is None:
            continue
        if path_covers(rel, declared_rel) or path_covers(declared_rel, rel):
            matched.append(dataclasses.replace(out, target=rel))
    return matched


def _names_a_stage(dvc_yaml: Path, name: str) -> bool:
    """Is ``name`` (``build``, or a ``fan@a`` instance) a stage ``dvc_yaml`` declares?"""
    try:
        doc = yaml.safe_load(dvc_yaml.read_text(encoding="utf-8")) if dvc_yaml.is_file() else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    stages = doc.get("stages") if isinstance(doc, dict) else None
    return isinstance(stages, dict) and name.split("@", 1)[0] in stages


def push_targets(
    project_path: Path, targets: list[str], remote_name: str, *, cwd: Path
) -> tuple[list[list[str]], frozenset[str]]:
    """``(argv groups, selection)`` for ``dvc push`` run from the project root.

    A target naming a tracked output, or a file or folder inside or above
    one, becomes every out it reaches (``outs_matching``'s three
    directions), each spelled the way dvc pushes it whole: a pointer out as
    its ``.dvc`` file, a ``dvc.lock`` out as ``dvc.yaml:<stage>``. Naming one
    file of a tracked folder therefore pushes the folder; pushed bare, dvc
    uploads that one file and rewrites the folder's pointer to list only it.
    A ``.dvc`` target and a ``dvc.yaml:<stage>`` key are kept; a stage name
    becomes ``dvc.yaml:<name>``, or ``<dir>/dvc.yaml:<name>`` when typed
    beside a subdirectory's own ``dvc.yaml``; any other target that
    matches nothing reaches dvc unchanged, so dvc rejects or handles it as
    before.

    Targets are read relative to ``cwd`` and re-spelled relative to the root
    (an escaping one stays absolute). The groups are ``.dvc`` paths, stage
    keys, then the rest, never mixed in one argv (see
    ``data_ops._shape_groups``). The selection holds the ``.dvc`` paths and
    stage keys: what this push uploads whole.
    """
    root = Path(os.path.normpath(project_path))
    here = Path(os.path.normpath(cwd))
    dvc_files: dict[str, None] = {}
    stages: dict[str, None] = {}
    rest: dict[str, None] = {}
    all_outs: list[DvcOut] | None = None
    for t in targets:
        absolute = os.path.normpath(here / t)
        try:
            rel = os.path.relpath(absolute, root)
        except ValueError:  # Windows: another drive, so outside the project
            rel = ".."
        if rel == ".." or rel.startswith(f"..{os.sep}"):
            rest[absolute] = None
            continue
        rel = Path(rel).as_posix()
        if rel.endswith(".dvc"):
            dvc_files[rel] = None
            continue
        if rel.startswith("dvc.yaml:"):
            stages[rel] = None
            continue
        if all_outs is None:
            all_outs = _all_dvc_outs(root, remote_name)
        matched = outs_matching(root, rel, remote_name, outs=all_outs)
        if not matched and here == root and _names_a_stage(root / "dvc.yaml", t):
            stages[f"dvc.yaml:{t}"] = None
        elif not matched and here != root and _names_a_stage(here / "dvc.yaml", t):
            # A stage name typed beside a subdirectory's own dvc.yaml: dvc
            # found it from there, and from the root it needs the file named.
            stages[f"{Path(os.path.relpath(here, root)).as_posix()}/dvc.yaml:{t}"] = None
        elif not matched:
            rest[t if here == root else rel] = None
        for o in matched:
            if o.stage is not None:
                stages[f"dvc.yaml:{o.stage}"] = None
            elif o.dvc_file is not None:
                dvc_files[Path(os.path.relpath(root / o.dvc_file, root)).as_posix()] = None
    groups = [list(g) for g in (dvc_files, stages, rest) if g]
    return groups, frozenset([*dvc_files, *stages])


# ---------------------------------------------------------------------------
# Delivery: would `mintd data import <declared>` land bytes?
# ---------------------------------------------------------------------------

DeliveryState = Literal[
    "pinned",                # every file the path reaches carries what a pull needs
    "via_parent",            # a subpath of a pinned `.dir` out; its manifest lives in S3, so this is the ceiling offline
    "untracked",             # DVC tracks nothing at, under or above the path (case-sensitively)
    "not_pullable",          # nothing dvc pushes resolves there: `cache: false` outs, or a `dvc import`
    "unpinned",              # version-aware remote, and no file carries an S3 version ID
    "partial",               # version-aware remote, and some files carry no S3 version ID
    "missing_from_manifest", # a subpath its enclosing files-format out's inline manifest does not hold
]


@dataclasses.dataclass(frozen=True)
class Delivery:
    """The verdict for one declared path. ``detail`` is a sentence fragment the
    caller can print after the path; the tuples carry the paths a repair hint
    needs to name."""

    state: DeliveryState
    detail: str
    unpinned: tuple[str, ...] = ()    # unpinned / partial: the files with no S3 version ID
    tracked: tuple[str, ...] = ()     # untracked: what DVC does track, project-relative
    unpullable: tuple[str, ...] = ()  # not_pullable: the outs dvc never pushes

    @property
    def deliverable(self) -> bool:
        return self.state in ("pinned", "via_parent")


def abbreviate(paths: Sequence[str], limit: int = 8) -> str:
    """``a, b, c …`` — the first ``limit`` paths, for one-line messages."""
    return ", ".join(paths[:limit]) + (" …" if len(paths) > limit else "")


def _never_pushed(out: DvcOut) -> str | None:
    """Why dvc will never push this out, or None. A ``cache: false`` out is
    never cached; a ``dvc import`` out's bytes live in the SOURCE repo's
    bucket, which a consumer of this repo never reaches."""
    if out.is_import:
        return "dvc import"
    if not out.use_cache:
        return "cache: false"
    return None


def _coverage(out: DvcOut) -> tuple[int, int, tuple[str, ...], str]:
    """``(files, files carrying a version ID, the ones without, shape)`` for an
    out dvc does push (callers screen with ``_never_pushed`` first). An out
    with zero pinned files (``files: []``, or the canonical empty-dir manifest
    hash) is deliverable as an empty directory, not a defect."""
    if out_pins_no_files(out):
        return 1, 1, (), "empty directory out"
    if out.files is not None:
        bad = tuple(f"{out.target}/{f.relpath}" for f in out.files if not f.version_id)
        return len(out.files), len(out.files) - len(bad), bad, "files-format directory out"
    pinned = 1 if out.version_id else 0
    shape = "directory out with no files: list" if out.is_dir else "file"
    return 1, pinned, () if pinned else (out.target,), shape


def delivery(
    project_path: Path,
    declared: str,
    remote_name: str,
    *,
    version_aware: bool,
    outs: list[DvcOut] | None = None,
) -> Delivery:
    """Would ``mintd data import`` of ``declared`` land bytes, judged from the
    pointers and ``dvc.lock`` in the working tree alone — no network, no
    cache. Whether they are committed and pushed to git is not checked.

    ``version_aware`` is the remote's ``version_aware = true`` flag. On such a
    remote the S3 key is the file's real path, so a consumer's pull needs the
    per-file S3 version ID the producer's push recorded; a pointer without one
    pulls nothing at exit 0. On an md5-layout remote plain ``dvc pull`` serves
    any pushed out, so missing IDs are not a defect there — which is also why
    a local-directory remote never trips the pin arms. The caller reads the
    flag (``_storage_state.inspect_storage``); this function never touches
    ``.dvc/config`` itself, so it stays usable on a bare out list.

    Matching is case-exact even though ``outs_matching`` folds case: the
    producer's filesystem may treat ``Data/Final`` and ``data/final`` as one
    directory, a Linux consumer's does not, and the declaration is what the
    consumer types.

    A path that resolves only to outs dvc never pushes (``cache: false``,
    ``dvc import``) is undeliverable. Such an out *beside* pullable siblings
    under a declared directory is not counted either way: a lab pipeline
    commonly writes an uncached metric (``cache: false`` — a validation
    report, say) under ``data/final/`` beside the data it pushes; dvc never
    stores it, and the pull lane drops it rather than failing on it
    (``data_ops.data_pull``), so a directory declaration promises what dvc
    stores. A declared subpath that resolves only to an enclosing directory
    out inherits that out's coverage: an unpinned or never-pushed parent
    cannot land anything beneath it. A files-format parent carries its
    manifest inline, so the subpath's presence is decided here; a ``.dir``
    parent's manifest is an S3 object, so a pinned one is reported
    ``via_parent`` — the clone lane's postcondition decides the rest at clone
    time. A ``.dir`` out whose ID is recorded at the top level counts as
    pinned; whether that ID is still current is the push probe's question.

    Two limits, stated: a path only git tracks is reported untracked on
    purpose -- ``dvc import`` would serve it from git, but a product's
    outputs are DVC-tracked by contract, and git is not where the lab's data
    lives. And only the project root's ``dvc.lock`` is read; a pipeline that
    lives in a subdirectory is not enumerated.
    """
    if outs is None:
        outs = _all_dvc_outs(project_path, remote_name)
    root = Path(os.path.normpath(project_path))
    rel = normalize_target(declared)
    matched = outs_matching(project_path, declared, remote_name, outs=outs)
    if not matched:
        return Delivery("untracked", "DVC tracks nothing at, under or above it", tracked=_tracked(root, outs))
    exact = [o for o in matched if _covers_exact(o.target, rel) or _covers_exact(rel, o.target)]
    if not exact:
        return Delivery(
            "untracked",
            f"DVC tracks {abbreviate(sorted(o.target for o in matched))}, which differs from the "
            "declared spelling only by case; a case-sensitive consumer cannot import it",
            tracked=_tracked(root, outs),
        )
    matched = exact

    via_parent = all(o.target != rel and _covers_exact(o.target, rel) for o in matched)
    if via_parent:
        for o in matched:
            # A parent dvc never pushes cannot land anything beneath it
            # whatever its manifest says: it inherits the tally below.
            if _never_pushed(o):
                continue
            if o.files is None:
                # A `.dir` or lock parent has no manifest on disk, so it
                # inherits the tally below -- unless it is known to hold
                # nothing (a single file, or the empty-directory hash).
                if out_pins_no_files(o) or not o.materializes_as_dir:
                    what = "an empty directory out" if out_pins_no_files(o) else "a single-file out"
                    return Delivery(
                        "missing_from_manifest",
                        f"declared under {o.target!r}, which is {what}; nothing can exist beneath it",
                    )
                continue
            sub = rel[len(o.target) + 1:]
            inside = [f for f in o.files if _covers_exact(sub, f.relpath)]
            if not inside:
                return Delivery(
                    "missing_from_manifest",
                    f"declared under {o.target!r}, whose pinned manifest "
                    f"({len(o.files)} files) holds nothing at {sub!r}",
                )
            bad = tuple(f"{o.target}/{f.relpath}" for f in inside if not f.version_id)
            if bad and version_aware:
                return Delivery(
                    "unpinned" if len(bad) == len(inside) else "partial",
                    f"{len(inside) - len(bad)}/{len(inside)} files under {o.target!r} "
                    f"carry an S3 version ID; missing on {abbreviate(bad)}",
                    unpinned=bad,
                )
            return Delivery("pinned", f"{len(inside)} file(s) under {o.target!r} manifest")

    total = pinned = 0
    bad_all: list[str] = []
    never: list[str] = []
    shapes: set[str] = set()
    never_why: set[str] = set()
    for o in matched:
        why = _never_pushed(o)
        if why:
            never.append(o.target)
            never_why.add(why)
            continue
        t, p, b, s = _coverage(o)
        total += t
        pinned += p
        bad_all.extend(b)
        shapes.add(s)
    if never and len(never) == len(matched):
        return Delivery(
            "not_pullable",
            f"{len(matched)} DVC out(s), all {', '.join(sorted(never_why))}",
            unpullable=tuple(never),
        )
    shape = ", ".join(sorted(shapes))
    if version_aware and pinned == 0:
        return Delivery(
            "unpinned", f"0/{total} files carry an S3 version ID ({shape})", unpinned=tuple(bad_all)
        )
    if version_aware and pinned < total:
        return Delivery(
            "partial",
            f"{pinned}/{total} files carry an S3 version ID ({shape}); missing on {abbreviate(bad_all)}",
            unpinned=tuple(bad_all),
        )
    if via_parent:
        return Delivery(
            "via_parent", f"resolves only to the enclosing out {matched[0].target!r}, which is pinned"
        )
    if version_aware:
        return Delivery("pinned", f"{pinned}/{total} files carry an S3 version ID ({shape})")
    return Delivery("pinned", f"remote is not version-aware; plain dvc pull serves it ({shape})")


# ---------------------------------------------------------------------------
# The push probe: may `data push` commit these pointer files?
# ---------------------------------------------------------------------------


def dvc_cache_dir(root: Path) -> Path:
    """The project's DVC cache: ``[cache] dir`` from ``.dvc/config.local``,
    then ``.dvc/config`` (relative to ``.dvc/``), else ``.dvc/cache``."""
    for name in ("config.local", "config"):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(root / ".dvc" / name, encoding="utf-8")
        except configparser.Error:
            continue
        value = parser.get("cache", "dir", fallback="")
        if value:
            return Path(os.path.normpath(root / ".dvc" / value))
    return root / ".dvc" / "cache"


@dataclasses.dataclass(frozen=True)
class PointerJudgement:
    """``blocked``: what a commit would record that importers could not pull.
    ``not_committed``: why the pointers are left for a person to commit."""

    blocked: tuple[str, ...] = ()
    not_committed: tuple[str, ...] = ()


def _load(text: str | None) -> Any:
    try:
        return yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return None


def _hashed(raw_outs: Any) -> list[dict] | None:
    """The raw outs the parsers turn into ``DvcOut``s, in their order; None
    when the list is not a list of mappings."""
    if not isinstance(raw_outs, list) or not all(isinstance(r, dict) for r in raw_outs):
        return None
    return [r for r in raw_outs if r.get("md5") or "files" in r]


def _by_path(raw_outs: Any) -> dict[str, dict]:
    if not isinstance(raw_outs, list):
        return {}
    return {str(r.get("path")): r for r in raw_outs if isinstance(r, dict)}


def _lists_files(raw: dict | None) -> bool:
    if not raw:
        return False
    if isinstance(raw.get("files"), list):
        return bool(raw["files"])
    md5 = str(raw.get("md5", ""))
    return md5.endswith(".dir") and md5 != EMPTY_DIR_MD5


def _judge_out(
    root: Path,
    o: DvcOut,
    raw: dict,
    head_raw: dict | None,
    label: str,
    *,
    remote_name: str,
    version_aware: bool,
    cache: Path,
    blocked: list[str],
    soft: list[str],
) -> None:
    if raw == head_raw:
        return  # committed as it is: this commit does not change what importers get
    rel = _workspace_rel(root, o)
    if rel is None:
        soft.append(f"{label} records an output outside the project")
        return
    o = dataclasses.replace(o, target=rel)
    label = f"{label}: {rel}"
    if _never_pushed(o) or raw.get("cache") is False or raw.get("push") is False:
        return
    if version_aware:
        d = delivery(root, rel, remote_name, version_aware=True, outs=[o])
        if not d.deliverable and d.state != "not_pullable":
            blocked.append(f"{label} ({d.detail})")
            return
    if not o.materializes_as_dir:
        return
    if out_pins_no_files(o):
        ws = root / rel
        if _lists_files(head_raw):
            blocked.append(f"{label} lists no files, but the last commit's pointer lists some")
        elif ws.is_dir() and next(ws.iterdir(), None) is not None:
            blocked.append(f"{label} lists no files, but the folder on disk holds some")
        return
    tree = dir_manifest_md5(o.files) if o.files is not None else o.md5
    if not cache.is_dir():
        soft.append(f"no local DVC cache to check {label} against")
    elif not (cache_path_for(cache, tree).exists() or (cache / tree[:2] / tree[2:]).exists()):
        count = f"{len(o.files)} file(s)" if o.files is not None else f"tree {tree}"
        blocked.append(
            f"{label} lists {count}, a tree DVC never saved in this project's cache: the push "
            "recorded a different file list than `dvc add` / `dvc commit` did"
        )


def judge_pointers(
    project_path: Path,
    pointers: Sequence[str],
    *,
    head: Mapping[str, str | None],
    remote_name: str,
    version_aware: bool,
    selection: frozenset[str] | None,
) -> PointerJudgement:
    """May ``data push`` commit ``pointers`` (root-relative ``.dvc`` /
    ``dvc.lock`` files that differ from HEAD; ``head`` maps each to its text
    at HEAD, None when new)? Judges what a commit would carry, not what the
    push targeted: every out of a changed ``.dvc``, or of a root ``dvc.lock``
    stage whose entry differs from HEAD, unless the out's own entry is the
    same as HEAD's (the commit does not change it). Runs no git and no
    network.

    Blocked, because importers could not pull what the commit records:

    - on a version-aware remote, an out ``delivery`` calls undeliverable
      (version IDs missing under ``remote_name``, the default remote
      importers read);
    - a changed folder out that lists no files while the last commit's
      pointer, or the folder on disk, holds some;
    - a changed folder out whose file list hashes to a tree this project's
      cache never held. dvc's version-aware push digests the list it
      uploaded in memory (``dvc_data.index.save.build_tree``) and never
      saves that tree, so a list cut down by a narrow push names a tree no
      ``dvc add`` or ``dvc commit`` wrote, while a complete list hashes back
      to the one they saved;
    - a ``.dvc`` out with no hash.

    Not committed, for a person to decide: a deleted pointer, a ``dvc
    import``, a nested or unparseable file, an orphan stage or an out
    outside the project, no local cache to check a folder against, and on an
    md5-layout remote a pointer this targeted push did not upload (nothing
    there shows it reached the remote).

    Blind spots, stated: an empty tree once saved beside an empty folder; a
    cut-down list equal to an older tree still in the cache; and a tree
    fast-sync's ``ensure_dir_manifest`` wrote while pulling a damaged pointer.
    """
    root = Path(os.path.normpath(project_path))
    cache = dvc_cache_dir(root)
    blocked: list[str] = []
    soft: list[str] = []

    def judge(o: DvcOut, raw: dict, head_raw: dict | None, label: str) -> None:
        _judge_out(
            root, o, raw, head_raw, label, remote_name=remote_name,
            version_aware=version_aware, cache=cache, blocked=blocked, soft=soft,
        )

    for p in pointers:
        path = root / p
        if not path.is_file():
            soft.append(f"{p} was deleted")
            continue
        if Path(p).name == "dvc.lock" and p != "dvc.lock":
            soft.append(f"{p} is not the project's root dvc.lock")
            continue
        doc = _load(path.read_text(encoding="utf-8", errors="replace"))
        head_doc = _load(head.get(p))
        if not isinstance(doc, dict):
            soft.append(f"{p} does not parse")
            continue
        if p == "dvc.lock":
            stages = doc.get("stages")
            if not isinstance(stages, dict):
                soft.append(f"{p} does not parse")
                continue
            head_stages = head_doc.get("stages") if isinstance(head_doc, dict) else None
            head_stages = head_stages if isinstance(head_stages, dict) else {}
            parsed: dict[str | None, list[DvcOut]] = {}
            for o in parse_dvc_lock_outs(root, remote_name):
                parsed.setdefault(o.stage, []).append(o)
            for stage, entry in stages.items():
                head_entry = head_stages.get(stage)
                if entry == head_entry:
                    continue
                key = f"dvc.yaml:{stage}"
                if not version_aware and selection is not None and key not in selection:
                    soft.append(
                        f"on an md5-layout remote nothing shows dvc.lock stage '{stage}' reached "
                        "S3, and this push did not upload it"
                    )
                    continue
                raw_outs = _hashed((entry.get("outs") or []) if isinstance(entry, dict) else None)
                outs = parsed.get(stage, [])
                if raw_outs is None or len(raw_outs) != len(outs):
                    soft.append(f"dvc.lock stage '{stage}' has outs mintd cannot resolve (an orphan stage, or an out outside the project)")
                    continue
                head_by_path = _by_path(head_entry.get("outs") if isinstance(head_entry, dict) else None)
                for raw, o in zip(raw_outs, outs, strict=True):
                    judge(o, raw, head_by_path.get(str(raw.get("path"))), f"dvc.lock stage '{stage}'")
            continue
        if "deps" in doc:
            soft.append(f"{p} is a dvc import")
            continue
        raw_outs = _hashed(doc.get("outs"))
        if raw_outs is None:
            soft.append(f"{p} does not parse")
            continue
        if any(isinstance(r, dict) and not r.get("md5") and "files" not in r for r in doc.get("outs") or []):
            blocked.append(f"{p} records an output with no hash; re-record it with `dvc commit {p}`")
            continue
        if not version_aware and selection is not None and p not in selection:
            soft.append(f"on an md5-layout remote nothing shows {p} reached S3, and this push did not upload it")
            continue
        outs = parse_dvc_outs(path, remote_name)
        if len(outs) != len(raw_outs):
            soft.append(f"{p} does not parse")
            continue
        head_by_path = _by_path(head_doc.get("outs") if isinstance(head_doc, dict) else None)
        for raw, o in zip(raw_outs, outs, strict=True):
            judge(o, raw, head_by_path.get(str(raw.get("path"))), p)
    return PointerJudgement(blocked=tuple(blocked), not_committed=tuple(soft))
