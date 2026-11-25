"""What DVC actually tracks, and whether a declared metadata path reaches it.

It holds the readers of DVC's own files that fast sync, pull, push, check and the
cache lane share: `.dvc` pointers, `dvc.yaml`, `dvc.lock`, `.dvc/config`, the
`.gitignore` rules DVC applies, and the cache's layout. It only reads (no
network, no writes, no spawning), and the only mintd module it imports is the
leaf `mintd.errors`, which imports none, so any module may import it without a
cycle (pinned by `tests/test_dvc_state.py::test_dvc_state_has_no_import_cycle`).
Writing the cache (`ensure_dir_manifest`) and everything that talks to S3 stay
in `_fast_sync_ops`.

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

import dataclasses
import hashlib
import importlib
import json
import logging
import os
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Literal

import pathspec

from .errors import MintdError


logger = logging.getLogger(__name__)
_DEFAULT_DVC_CACHE_REL = Path(".dvc/cache")


@dataclass(frozen=True)
class DvcFileEntry:
    md5: str
    relpath: str
    size: int = 0
    version_id: str | None = None
    # Slice 27: True when the entry's version_id lives ONLY under cloud[]
    # (DVC's version_aware mode). The S3 key is then the file's real path,
    # not files/md5/XX/YYYY. See _is_path_based_file_entry.
    is_path_based: bool = False


@dataclass(frozen=True)
class DvcOut:
    target: str
    path: str
    md5: str
    is_dir: bool
    version_id: str | None = None
    is_files_format: bool = False
    files: list[DvcFileEntry] | None = None
    size: int = 0  # aggregate bytes for the out (sum across files for dirs); used by Reporter.progress
    # Slice 27: True when the out's version_id lives ONLY under cloud[]
    # (DVC's version_aware mode). The S3 key is the .dvc file's parent
    # directory (relative to project_path) joined with out.path, not
    # files/md5/XX/YYYY. See _is_path_based_entry + s3_key_for_out.
    is_path_based: bool = False
    # The path of the .dvc file this out was parsed from. Required for
    # path-based key reconstruction; None on synthetic DvcOuts (tests).
    dvc_file: Path | None = None
    # Slice 29: True when the parent .dvc has any deps[].repo block,
    # i.e. the file was produced by `dvc import`. The data lives in the
    # source repo's bucket — not this consumer repo's — so fast-sync
    # cannot serve it and routes the target straight to `dvc pull`.
    is_import: bool = False
    # False for a dvc.lock out dvc.yaml declares `cache: false`: dvc never
    # caches or pushes it, so no pull can land it. It is still DVC-tracked
    # (clone selectors and the cache-lane screens must keep seeing it); only
    # data_pull's discovery drops it.
    use_cache: bool = True
    # The dvc.lock stage key (``build``, ``fan@a``) a lock out was parsed
    # from; None for a ``.dvc`` out. Push pushes a lock out by this key.
    stage: str | None = None

    @property
    def materializes_as_dir(self) -> bool:
        """``dvc checkout`` writes this out as a DIRECTORY: ``.dir``-md5
        outs AND files-format outs (which have no top-level md5, so
        ``is_dir`` stays False). Every dir-vs-file dispatch on workspace
        shape must use this predicate, never ``is_dir`` alone."""
        return self.is_dir or self.is_files_format


def _extract_version_id(out_dict: dict[str, Any]) -> str | None:
    # A null ID (a pointer cut short right after the key) is unpinned, never "None".
    if out_dict.get("version_id") is not None:
        return str(out_dict["version_id"])
    cloud = out_dict.get("cloud")
    if isinstance(cloud, dict):  # null in a pointer cut short right after `cloud:`
        for cloud_info in cloud.values():
            if isinstance(cloud_info, dict) and cloud_info.get("version_id") is not None:
                return str(cloud_info["version_id"])
    return None


def _extract_version_id_from_file_entry(entry: dict[str, Any], remote_name: str) -> str | None:
    cloud = entry.get("cloud")
    if not isinstance(cloud, dict):
        return None
    remote_block = cloud.get(remote_name)
    if isinstance(remote_block, dict) and remote_block.get("version_id") is not None:
        return str(remote_block["version_id"])
    return None


def _is_path_based_entry(entry: dict[str, Any], remote_name: str | None = None) -> bool:
    """True iff the entry's version_id comes ONLY from cloud[] metadata —
    meaning the S3 object is keyed by its file path (DVC's version_aware
    mode). False when a top-level version_id is present.

    Mirrors v1 ``mintd/utils/fast_sync.py:_is_path_based_entry``.
    Mixed entries (both top-level AND cloud-nested ``version_id``) are
    treated md5-keyed: top-level wins (v1 contract).
    """
    if entry.get("version_id"):
        return False
    cloud = entry.get("cloud") or {}
    if remote_name and isinstance(cloud.get(remote_name), dict):
        return bool(cloud[remote_name].get("version_id"))
    return any(
        isinstance(v, dict) and v.get("version_id")
        for v in cloud.values()
    )


def _is_path_based_file_entry(entry: dict[str, Any], remote_name: str | None = None) -> bool:
    """Parallel to ``_is_path_based_entry`` for files-format file entries.

    Today the rule is identical; the alias exists so call sites don't lie
    about which kind of dict they're inspecting and so we can diverge later
    without touching callers.
    """
    return _is_path_based_entry(entry, remote_name)


class PointerCutShort(MintdError):
    """A pointer file isn't whole: a null or wrong type where DVC writes a
    mapping, a list, a number or a string, an output with no path or no hash,
    or no YAML at all.
    A write to it stopped partway (dvc killed mid-dump, say). Read on, it
    lists less than it records: DVC pushes nothing for it at exit 0, and a
    pull lands an empty folder. So every reader stops on it, naming the file;
    ``cli.main`` renders it, exit 1. Not a ValueError, so no handler that
    degrades on bad values swallows it."""

    hint = ("restore it from git, or record the data again (mintd data add <path>); "
            "if it came from someone else's repo, ask them to")

    def __init__(self, pointer: Path | str, detail: str) -> None:
        super().__init__(f"{pointer} is cut short or damaged: {detail}")
        self.pointer = pointer
        self.detail = detail


_DVC_HASH = re.compile(r"[0-9a-f]{32}(\.dir)?")


def hash_in_dvc_shape(md5: object) -> bool:
    """Whether ``md5`` is a hash as dvc writes one: 32 lowercase hex, with or
    without ``.dir``. Joined onto the cache unchecked, ``ab/home/u/notes.dir``
    names ``/home/u/notes.dir`` and ``ab/../../x`` climbs out of it."""
    return isinstance(md5, str) and _DVC_HASH.fullmatch(md5) is not None


def load12(text: str) -> Any:
    """``text`` read as DVC reads ``.dvc``, ``dvc.lock`` and ``dvc.yaml``:
    YAML 1.2, with ruamel (DVC's own library), so an unquoted ``on``,
    ``no`` or ``1:30`` stays a string, where YAML 1.1 (PyYAML) reads a
    boolean or a number. Raises ``ruamel.yaml.YAMLError``, also for a value
    its type can't hold (``!!bool x``, ``!!int ''``, ``!!int x``, a date
    like ``2020-02-30``, a number too long to read), on which ruamel's own
    constructor raises KeyError, IndexError or ValueError."""
    from ruamel.yaml import YAML, YAMLError

    try:
        return YAML(typ="safe", pure=True).load(text)
    except (LookupError, ValueError) as exc:
        raise YAMLError(f"a value its type can't hold: {exc!r}") from exc


def load_pointer(path: Path) -> Any:
    """The YAML in pointer file ``path``, read as DVC reads it (``load12``);
    ``PointerCutShort`` when it isn't valid YAML. A missing file raises
    ``FileNotFoundError``."""
    from ruamel.yaml import YAMLError

    try:
        # dvc writes UTF-8 whatever the locale; the platform default (cp1252
        # on Windows) misreads a non-ASCII relpath, and the tree it hashes to.
        return load12(Path(path).read_text(encoding="utf-8"))
    except (YAMLError, UnicodeDecodeError) as exc:
        raise PointerCutShort(path, "it isn't valid YAML") from exc


def _scalars_ok(rec: dict[str, Any]) -> bool:
    """A count DVC writes is a number, and a hash or version ID is no mapping
    or list (a null one is left to the rules above). ``path`` and ``relpath``
    aren't checked: DVC writes a name like ``on`` bare, which YAML 1.1 reads
    as a boolean."""
    return (all(type(rec[k]) is int for k in ("size", "nfiles") if k in rec)
            and not any(isinstance(rec.get(k), (dict, list)) for k in ("md5", "hash", "etag", "version_id")))


def _cloud_ok(rec: dict[str, Any]) -> bool:
    """A ``cloud`` block as DVC writes it: a mapping of remote to mapping. A
    null block or remote entry is no version ID (unpinned), not damage."""
    cloud = rec.get("cloud")
    return cloud is None or (isinstance(cloud, dict) and all(
        v is None or (isinstance(v, dict) and _scalars_ok(v)) for v in cloud.values()))


def _check_out(pointer: Path | str, out: Any, *, hashed: bool, unrun_ok: bool = False) -> None:
    if not isinstance(out, dict):
        raise PointerCutShort(pointer, "an output entry is empty" if out is None
                              else "an output entry is not in the shape dvc writes")
    name = out.get("path")
    if name is None:
        raise PointerCutShort(pointer, "an output has no path")
    if hashed and not out.get("md5") and "files" not in out and not (unrun_ok and out.get("hash")):
        raise PointerCutShort(pointer, f"{name} records no hash")
    if out.get("md5") is not None and not hash_in_dvc_shape(out["md5"]):
        raise PointerCutShort(pointer, f"the hash of {name} is not in the shape dvc writes: {out['md5']!r}")
    if not _scalars_ok(out):
        raise PointerCutShort(pointer, f"a value of {name} is not in the shape dvc writes")
    if not _cloud_ok(out):
        raise PointerCutShort(pointer, f"the version block of {name} is incomplete")
    if "files" in out:
        files = out["files"]
        if not isinstance(files, list):
            raise PointerCutShort(pointer, f"the file list of {name} is missing")
        # dvc writes no nfiles beside a file list, so one cut between two
        # entries reads as a smaller folder; the file alone can't show it.
        for fe in files:
            if (not isinstance(fe, dict) or fe.get("md5") is None or fe.get("relpath") is None
                    or not _scalars_ok(fe) or not _cloud_ok(fe)):
                raise PointerCutShort(pointer, f"a file entry of {name} is incomplete")
            if not hash_in_dvc_shape(fe["md5"]):
                raise PointerCutShort(
                    pointer, f"a file entry of {name} records a hash not in the shape dvc writes: {fe['md5']!r}")


def check_dvc_file(pointer: Path | str, doc: Any) -> None:
    """``PointerCutShort`` unless ``doc`` reads as a whole ``.dvc`` file: at
    least one output, each with a path and, outside a dvc import, a hash."""
    if not isinstance(doc, dict):
        raise PointerCutShort(pointer, "it is empty" if doc is None else "it doesn't read as a DVC file")
    outs = doc.get("outs")
    if not isinstance(outs, list) or not outs:
        raise PointerCutShort(pointer, "it lists no outputs")
    for out in outs:
        _check_out(pointer, out, hashed="deps" not in doc)


def check_lock(pointer: Path | str, doc: Any) -> None:
    """``PointerCutShort`` unless ``doc`` reads as a whole ``dvc.lock``: its
    stages a mapping, each stage a mapping, and any ``outs`` a non-empty list
    of outputs that each record a hash, or at least ``hash: md5``. DVC hashes
    every out of a stage that ran (a ``cache: false`` one too), but a push to
    a version-aware remote writes a declared stage that never ran as ``path``
    and ``hash`` only; a cut right after ``hash: md5`` reads the same. The
    parser skips such an out, and the push counts its stage as behind
    (``_dvc_state.stages_behind``). An out with no ``hash`` at all is cut:
    DVC 3 always writes one."""
    stages = doc.get("stages") if isinstance(doc, dict) else None
    if not isinstance(stages, dict):
        raise PointerCutShort(pointer, "it lists no stages")
    for stage, entry in stages.items():
        if not isinstance(entry, dict):
            raise PointerCutShort(pointer, f"stage {stage} is empty")
        if "outs" in entry:
            outs = entry["outs"]
            if not isinstance(outs, list) or not outs:
                raise PointerCutShort(pointer, f"stage {stage} lists no outputs")
            for out in outs:
                _check_out(pointer, out, hashed=True, unrun_ok=True)


def parse_dvc_outs(dvc_path: Path, remote_name: str) -> list[DvcOut]:
    """Parse a .dvc file into DvcOut entries.

    DVC marks directories by suffixing the md5 hash with ``.dir`` — that's
    the only signal; there is no separate ``is_dir`` YAML field. Files-format
    directories use an inline ``files:`` list with no top-level md5.
    A dvc import's output with no md5 and no files list is hash-missing:
    left out, so the caller routes it to fallback. A file that isn't whole
    raises ``PointerCutShort`` (``check_dvc_file``); a missing one is ``[]``.
    """
    try:
        data = load_pointer(dvc_path)
    except FileNotFoundError:
        return []
    check_dvc_file(dvc_path, data)

    outs = []
    # Slice 29: `dvc import` produces a .dvc with `deps[].repo` blocks
    # pointing at the source repo. The data lives in that source repo's
    # bucket, not this one — fast-sync cannot serve it. Detect once per
    # file; stamp every out so classify_targets can short-circuit.
    deps = data.get("deps") or []
    is_import = any(
        isinstance(d, dict) and isinstance(d.get("repo"), dict)
        for d in deps
    )
    for out in data["outs"]:
        has_md5 = bool(out.get("md5"))
        has_files = "files" in out
        if not has_md5 and not has_files:
            continue
        md5 = str(out.get("md5", ""))
        files_list: list[DvcFileEntry] | None = None
        if has_files:
            files_list = [
                DvcFileEntry(
                    md5=str(fe.get("md5", "")),
                    relpath=str(fe.get("relpath", "")),
                    size=int(fe.get("size", 0) or 0),
                    version_id=_extract_version_id_from_file_entry(fe, remote_name),
                    is_path_based=_is_path_based_file_entry(fe, remote_name),
                )
                for fe in out["files"]
            ]
        outs.append(DvcOut(
            target="",
            path=str(out.get("path", "")),
            md5=md5,
            is_dir=md5.endswith(".dir"),
            version_id=_extract_version_id(out),
            is_files_format=has_files and not has_md5,
            files=files_list,
            size=int(out.get("size", 0) or 0),
            is_path_based=_is_path_based_entry(out, remote_name),
            dvc_file=dvc_path,
            is_import=is_import,
        ))
    return outs


def wdir_map(dvc_yaml_doc: object) -> dict[str, str | None]:
    """``stage name -> wdir`` from an already-parsed ``dvc.yaml`` document.

    Pure: takes the parsed document, not a path, because the two callers
    acquire it differently — fast-sync ``open()``s a local checkout, while the
    drift comparator has only ``Fetcher.fetch_path_at`` bytes at a remote rev
    and no working tree to open. Sharing the *parsing* is the reuse that is
    actually available; sharing the acquisition is not.

    A stage with no ``wdir`` maps to ``"."``. An absolute ``wdir`` maps to
    ``None`` — unresolvable against a project root, so callers skip the stage
    rather than guess at it.
    """
    if not isinstance(dvc_yaml_doc, dict):
        return {}
    stages = dvc_yaml_doc.get("stages")
    if not isinstance(stages, dict):
        return {}
    out: dict[str, str | None] = {}
    for stage, stage_data in stages.items():
        # A `foreach` stage nests the real stage body (wdir included) under
        # `do:`; a `matrix` stage keeps it at the top level.
        body = stage_data.get("do", stage_data) if isinstance(stage_data, dict) else {}
        wdir = body.get("wdir", ".") if isinstance(body, dict) else "."
        if posixpath.isabs(str(wdir)) or Path(str(wdir)).is_absolute():
            logger.warning(
                "absolute wdir in dvc.yaml stage %s: %s; skipping stage", stage, wdir
            )
            out[str(stage)] = None
        else:
            out[str(stage)] = str(wdir)
    return out


def flagged_outs(dvc_yaml_doc: object, flag: str) -> dict[str, set[str]]:
    """``stage name -> {out path}`` for every out ``dvc.yaml`` declares
    ``<flag>: false`` (under ``outs``, ``metrics`` or ``plots``). Paths stay as
    written (relative to the stage ``wdir``, normalized) to compare against
    the lock's raw ``path``.

    A `${...}` path never equals the lock's expanded one, so a templated
    flagged out is missed; expand it if a product ever declares one.
    """
    if not isinstance(dvc_yaml_doc, dict):
        return {}
    stages = dvc_yaml_doc.get("stages")
    if not isinstance(stages, dict):
        return {}
    out: dict[str, set[str]] = {}
    for stage, stage_data in stages.items():
        body = stage_data.get("do", stage_data) if isinstance(stage_data, dict) else {}
        if not isinstance(body, dict):
            continue
        for key in ("outs", "metrics", "plots"):
            entries = body.get(key)
            for entry in entries if isinstance(entries, list) else []:
                for path, opts in entry.items() if isinstance(entry, dict) else ():
                    if isinstance(opts, dict) and opts.get(flag) is False:
                        out.setdefault(str(stage), set()).add(posixpath.normpath(str(path)))
    return out


def uncached_outs(dvc_yaml_doc: object) -> dict[str, set[str]]:
    """``stage name -> {out path}`` for every out ``dvc.yaml`` declares
    ``cache: false``.

    ``dvc.lock`` records such an out like any other -- path, md5, size, no
    ``cache`` key -- but dvc never caches or pushes it, so ``dvc pull`` exits
    0 and lands nothing.
    """
    return flagged_outs(dvc_yaml_doc, "cache")


def stage_wdir(stage_wdirs: dict[str, str | None], stage: str) -> str | None:
    """The `wdir` for one **dvc.lock** stage name.

    `dvc.yaml` names a `foreach`/`matrix` stage `build`; `dvc.lock` names its
    instances `build@main`. Looking the lock name up directly missed, every
    such stage silently fell back to `wdir="."`, and a scaffold-shaped out
    (`../data/final/`) then resolved outside the repo root and was dropped —
    reported as "not published" at whichever rev the fan-out existed at.
    """
    return stage_wdirs.get(stage, stage_wdirs.get(stage.split("@", 1)[0], "."))


def resolve_out(wdir: str, out_path: str) -> str:
    """Resolve a stage ``out`` (recorded relative to ``wdir``) to a
    project-root-relative POSIX path.

    ``wdir="code", out="../data/final/"`` -> ``"data/final"``. Lexical on
    purpose: the drift comparator resolves paths for a *remote* rev where
    nothing exists on disk to `.resolve()` against. A result that still starts
    with ``../`` escaped the project root and the caller must reject it.
    """
    return posixpath.normpath(posixpath.join(wdir or ".", out_path))


def parse_dvc_lock_outs(
    project_path: Path, remote_name: str, *, declared: Collection[str] | None = None,
    skipped: list[str] | None = None,
) -> list[DvcOut]:
    """Parse ``project_path/dvc.lock`` into ``DvcOut`` entries for pipeline-
    stage outputs (no per-output ``.dvc`` pointer files). Slice 37 — gives
    fast-sync a way to enumerate pipeline products so they bypass DVC's
    rehash-on-pull cost on version_aware remotes.

    Reads ``dvc.yaml`` (optional) to build the ``stage → wdir`` map so
    lock-relative ``out.path`` resolves correctly to project-relative.
    When ``dvc.yaml`` is missing, every stage's ``wdir`` defaults to ``.``.

    Once ``dvc.yaml`` declares any stage at all, a lock stage whose name AND
    ``@``-base are both undeclared there is skipped: real ``dvc pull`` rejects
    its outs ("does not exist as an output or a stage name"), so enumerating
    them turns one stale stage into a failed pull. When ``dvc.yaml`` declares
    nothing — missing, ``vars:``-only, ``stages: {}``, or unparseable — the
    skip stays off rather than dropping every out on the strength of a
    document that taught us nothing.

    ``declared``, when given, is dvc's own list of the stages ``dvc.yaml``
    declares (``DvcOps.stage_names``, foreach and matrix expanded): a lock
    stage it doesn't hold is skipped too, and its name appended to
    ``skipped``. Without it, a ``foreach``/``matrix`` list that SHRANK is not
    caught: ``fan@b`` left in the lock while ``dvc.yaml`` still declares
    ``fan`` keeps its base, so it survives here and dvc still rejects it. Closing that needs the instance
    names, and ``foreach`` may be a ``${vars}`` reference only dvc's
    templating can expand — guessing it would drop live instances instead.

    An out ``dvc.yaml`` declares ``cache: false`` is kept, marked
    ``use_cache=False`` (see ``DvcOut.use_cache``).

    Returns ``[]`` when ``dvc.lock`` is missing. One that isn't whole raises
    ``PointerCutShort`` (``check_lock``), unless no ``dvc.yaml`` is beside it:
    DVC never reads such a lock, so it stays ``[]``.
    """
    yaml_path = project_path / "dvc.yaml"
    lock_path = project_path / "dvc.lock"

    # Shared with the drift comparator (`check._pointer_md5`), which
    # reaches the same document through `Fetcher.fetch_path_at` instead of
    # `open()`. Only the acquisition differs; the walk is identical.
    stage_wdirs: dict[str, str | None] = {}
    stage_uncached: dict[str, set[str]] = {}
    from ruamel.yaml import YAMLError

    try:
        if yaml_path.exists():
            yaml_doc = load12(yaml_path.read_text(encoding="utf-8"))
            stage_wdirs = wdir_map(yaml_doc)
            stage_uncached = uncached_outs(yaml_doc)
    except (OSError, UnicodeDecodeError, YAMLError):
        pass

    try:
        lock_data = load_pointer(lock_path)
        check_lock(lock_path, lock_data)
    except OSError:  # missing or unreadable
        return []
    except PointerCutShort:
        if yaml_path.exists():
            raise
        return []

    # dvc keeps a foreach item's trailing space in the lock key (`fan@t `) but prints the
    # step as `fan@t` in `dvc stage list`, so the two are matched without it.
    listed = None if declared is None else {name.rstrip() for name in declared}
    outs: list[DvcOut] = []
    for key, stage_data in lock_data["stages"].items():
        stage = str(key)  # dvc writes a name like `on` bare; YAML 1.1 reads it as a boolean
        if listed is not None and stage.rstrip() not in listed:
            logger.info("dvc.lock stage %s is not a stage dvc lists; skipping", stage)
            if skipped is not None:
                skipped.append(stage)
            continue
        if stage_wdirs and stage not in stage_wdirs and stage.split("@", 1)[0] not in stage_wdirs:
            logger.info("dvc.lock stage %s no longer in dvc.yaml; skipping", stage)
            continue
        wdir = stage_wdir(stage_wdirs, stage)
        if wdir is None:
            continue  # absolute wdir: unresolvable against the project root
        if "outs" not in stage_data:
            continue
        uncached = stage_uncached.get(stage, stage_uncached.get(stage.split("@", 1)[0], set()))
        for out in stage_data["outs"]:
            has_md5 = bool(out.get("md5"))
            has_files = "files" in out
            if not has_md5 and not has_files:
                continue
            # Lexical, like `resolve_out`'s other callers: dvc checks a file
            # out as a symlink into `.dvc/cache/` when reflinks and hardlinks
            # are unavailable, and resolving that link made the out vanish
            # (or point at the cache) instead of keeping its workspace path.
            raw_path = out.get("path")
            rel = resolve_out(wdir, str(raw_path)) if raw_path is not None else ".."
            # Outside the project: escaped through `..`, or absolute (a dvc
            # 2.x external out) in either platform's spelling.
            if rel == ".." or rel.startswith("../") or posixpath.isabs(rel) or PureWindowsPath(str(raw_path)).drive:
                logger.warning(
                    "path resolution failed for stage %s, out %s; skipping",
                    stage, out.get("path"),
                )
                continue

            cloud_block = out.get("cloud") or {}
            remote_cloud = cloud_block.get(remote_name) or {}
            version_id_raw = remote_cloud.get("version_id")
            version_id = str(version_id_raw) if version_id_raw else None
            is_path_based = bool(version_id) and not out.get("version_id")
            md5 = str(out.get("md5", ""))

            if has_files:
                is_files_format = True
                is_dir = True
                files_list: list[DvcFileEntry] | None = [
                    DvcFileEntry(
                        md5=str(fe.get("md5", "")),
                        relpath=str(fe.get("relpath", "")),
                        size=int(fe.get("size", 0) or 0),
                        version_id=_extract_version_id_from_file_entry(fe, remote_name),
                        is_path_based=_is_path_based_file_entry(fe, remote_name),
                    )
                    for fe in out["files"]
                ]
            else:
                is_files_format = False
                is_dir = md5.endswith(".dir")
                files_list = None

            outs.append(DvcOut(
                target=rel,
                path=rel,
                md5=md5,
                is_dir=is_dir,
                version_id=version_id,
                is_files_format=is_files_format,
                files=files_list,
                size=int(out.get("size", 0) or 0),
                is_path_based=is_path_based,
                dvc_file=lock_path,
                is_import=False,
                use_cache=posixpath.normpath(str(raw_path)) not in uncached,
                stage=stage,
            ))
    return outs


def _is_fast_syncable_pipeline_out(out: DvcOut) -> bool:
    """Whether fast-sync can serve this ``dvc.lock`` stage out.

    Two shapes qualify:

    1. Single-file outs whose top-level ``cloud.<remote>`` block carries a
       ``version_id`` — the straightforward case.
    2. ``files:``-format directory outs whose per-file entries each carry
       ``cloud.<remote>.version_id``. Real-world DVC lockfiles don't emit a
       top-level ``cloud`` block on dir-outs (only per-file ones inside
       ``files:``), so this branch is what makes pipeline dir-outs reachable
       in practice. ``fetch_files_dir_contents`` operates on per-file entries
       and doesn't need the top-level version_id.

    Outs that fit neither shape route to ``dvc pull`` instead.
    """
    if out.version_id:
        return True
    return bool(out.is_files_format and out.files and all(fe.version_id for fe in out.files))


def partition_pipeline_outs(
    project_path: Path, remote_name: str, *, declared: Collection[str] | None = None,
    skipped: list[str] | None = None,
) -> tuple[list[DvcOut], list[DvcOut]]:
    """Parse ``dvc.lock`` once and partition its stage outs.

    Returns ``(fast_syncable, all_outs)``: the first is the subset fast-sync
    can serve (see ``_is_fast_syncable_pipeline_out``); the second is every
    stage out in the lockfile. Callers diff the two to find the outs that must
    route to ``dvc pull``. One parse, so ``data_pull`` doesn't read the lock
    twice. ``declared`` and ``skipped``: see :func:`parse_dvc_lock_outs`.
    """
    all_outs = parse_dvc_lock_outs(project_path, remote_name, declared=declared, skipped=skipped)
    fast_syncable = [o for o in all_outs if _is_fast_syncable_pipeline_out(o)]
    return fast_syncable, all_outs


def discover_all_outs(project_path: Path) -> list[str]:
    """Walk project_path recursively for ``*.dvc`` files; return paths
    relative to project_path, sorted lexicographically.

    Excludes the ``.dvc/`` internals directory (DVC metadata, not data
    pointers), ``.git/`` and ``dvc.lock`` (pipeline lockfile). Mirrors v1's
    ``discover_dvc_targets`` (mintd/utils/dvc_guards.py) for the .dvc-file
    half; pipeline-stage names from ``dvc.yaml`` are NOT included because
    fast-sync's ``classify_targets`` cannot consume them — such targets
    fall through to ``dvc pull`` as the normal fallback path.

    Used by ``data_pull`` when ``targets is None`` to route through
    fast-sync (boto3 → cache) instead of straight to ``dvc pull``, which
    hits a cache-write bug in DVC 3.66.1 on version_aware buckets.

    Only the ``.dvc`` files DVC reads: one that DVC skips because of
    ``.gitignore`` is left out (see ``_walk_pointers``), so push, pull and
    check see what DVC sees.
    """
    return [rel for rel, ignored in _walk_pointers(project_path) if not ignored]


def pipeline_files(project_path: Path) -> list[str]:
    """The ``dvc.yaml`` files DVC reads, by the walk ``discover_all_outs``
    makes: none in a folder ``.gitignore`` excludes."""
    return [rel for rel, ignored in _walk_pointers(project_path, lambda name: name == "dvc.yaml") if not ignored]


def gitignored_pointers(project_path: Path) -> list[str]:
    """The ``.dvc`` files ``discover_all_outs`` leaves out because DVC reads
    ``.gitignore`` as excluding them. ``data push`` names them."""
    return [rel for rel, ignored in _walk_pointers(project_path) if ignored]


def skipped_targets(project_path: Path, targets: list[str], *, base: Path) -> list[tuple[str, str]]:
    """``(target, pointer)`` for each target that names a ``.dvc`` file DVC
    skips because of ``.gitignore``, as ``x.dvc`` or as ``x`` for ``x.dvc``.

    A target is read from ``base`` the way its command reads it (push from
    the folder it runs in, pull from the project root) and compared in
    project-relative form, lexically: ``..`` is collapsed and an absolute
    path mapped, but symlinks are not followed. A target outside the project
    never matches.
    """
    skipped = set(gitignored_pointers(project_path))
    if not skipped:
        return []
    root = os.path.abspath(project_path)
    found: list[tuple[str, str]] = []
    for t in targets:
        absolute = os.path.join(os.path.abspath(base), normalize_target(t))  # relpath normalises `..`
        try:
            rel = Path(os.path.relpath(absolute, root)).as_posix()
        except ValueError:  # Windows: another drive, so outside the project
            continue
        if rel == ".." or rel.startswith("../"):
            continue
        pointer = rel if rel.endswith(".dvc") else f"{rel}.dvc"
        if pointer in skipped:
            found.append((t, pointer))
    return found


class _NothingIgnored:
    """Outside a git repository, or without scmrepo: DVC ignores nothing."""

    def __enter__(self) -> "_NothingIgnored":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def is_ignored(self, path: str) -> bool:
        return False


def _walk_pointers(
    project_path: Path, match: Callable[[str], bool] = lambda name: name.endswith(".dvc"),
) -> list[tuple[str, bool]]:
    """Every ``*.dvc`` file (or file whose name ``match`` accepts) under
    project_path, relative and posix, with whether DVC skips it because of
    ``.gitignore``.

    It mirrors DVC's own walk (``dvc/repo/index.py`` ``collect_files``): a
    folder DVC reads as ignored is never read, so everything under it is
    skipped, and the check is by pattern only, so a pointer git tracks (one
    added with ``git add -f``) still counts as ignored. It asks scmrepo, the
    library DVC asks, rather than ``git check-ignore``: the two disagree on
    some re-include layouts (one is in the oracle test in
    ``tests/test_harness_contract.py``) and, on older dulwich, on a folder
    line under the scaffold's ``!/data/**/``; and DVC's answer changes with
    the installed dulwich. Asked in-process, from the environment whose dvc
    mintd runs, it can't drift from DVC. Ignored folders are still walked, so
    push can name what it skipped. It doesn't apply ``.dvcignore``,
    ``core.no_scm``, or DVC's pruning of folders that are outputs.
    """
    root = os.path.abspath(project_path)
    scm: Any = _NothingIgnored()
    try:
        from scmrepo.exceptions import SCMError
        from scmrepo.git import Git
    except ImportError:  # no scmrepo means no dvc, whose own missing-dvc error comes next
        pass
    else:
        try:
            scm = Git(root, search_parent_directories=True)  # how dvc.scm.SCM opens it
        except SCMError:  # not in a git repo: DVC without git ignores nothing
            pass
    found: list[tuple[str, bool]] = []
    ignored_dirs: set[str] = set()
    with scm:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune .dvc/ internals in-place so os.walk doesn't descend; dvc never
            # reads .git/ either, where a branch or tag named `*.dvc` is a ref file.
            dirnames[:] = [d for d in dirnames if d not in (".dvc", ".git")]
            inside = dirpath in ignored_dirs
            for d in dirnames:
                sub = os.path.join(dirpath, d)
                if inside or scm.is_ignored(sub + os.sep):  # trailing sep: how DVC asks about a folder
                    ignored_dirs.add(sub)
            for name in filenames:
                if not match(name):
                    continue
                path = os.path.join(dirpath, name)
                found.append((Path(os.path.relpath(path, root)).as_posix(), inside or scm.is_ignored(path)))
    return sorted(found)


def cache_path_for(cache_dir: Path, md5: str) -> Path:
    """Where DVC keeps the object ``md5`` under ``cache_dir``.

    ``PointerCutShort`` unless ``hash_in_dvc_shape(md5)``, so no caller gets
    a path outside the cache, whose cleanup would delete what's there. The
    floor: the pointer readers (``_check_out``) and the folder-list readers
    refuse such a hash first, naming where it came from."""
    if not hash_in_dvc_shape(md5):
        raise PointerCutShort("a pointer or folder list", f"the hash {md5!r} is not in the shape dvc writes")
    return cache_dir / "files" / "md5" / md5[:2] / md5[2:]


def is_cached(cache_dir: Path, md5: str) -> bool:
    if not md5:
        return False
    return cache_path_for(cache_dir, md5).exists()


def _listed(entries: "list[DvcFileEntry]", source: str) -> "list[DvcFileEntry]":
    """``entries``, read from the folder list ``source``, unless one records
    a hash dvc never writes: then ``PointerCutShort`` naming ``source``."""
    for e in entries:
        if not hash_in_dvc_shape(e.md5):
            raise PointerCutShort(source, f"the hash of {e.relpath!r} is not in the shape dvc writes: {e.md5!r}")
    return entries


def read_cached_dir_manifest(cache_dir: Path, dir_md5: str) -> "list[DvcFileEntry] | None":
    """Parse a locally-cached ``.dir`` manifest into file entries.

    ``dir_md5`` may arrive with or without the ``.dir`` suffix; the cache
    filename always carries it (DVC's own layout — see ensure_dir_manifest).
    Returns ``None`` when the manifest is absent or malformed in any way
    (bad JSON, non-list payload, non-dict entries): callers treat that as
    "manifest not usable from cache" and fetch or route accordingly. An
    entry whose hash dvc never writes is ``PointerCutShort`` (``_listed``).
    """
    full_md5 = dir_md5 if dir_md5.endswith(".dir") else f"{dir_md5}.dir"
    manifest_path = cache_path_for(cache_dir, full_md5)
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_bytes())
        if not isinstance(payload, list):
            return None
        entries = [
            DvcFileEntry(
                md5=str(e.get("md5", "")),
                relpath=str(e.get("relpath", "")),
                size=int(e.get("size", 0) or 0),
            )
            for e in payload
        ]
    # Unreadable or a folder, bad JSON or YAML, wrong-typed fields, an infinite size, deep nesting.
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError, MemoryError):
        return None
    return _listed(entries, f"the folder list {full_md5} in the cache")


# DVC's content address for the empty directory: md5 of the empty-list
# JSON manifest (b"[]"). An md5-keyed dir out pinning this hash legally
# tracks a directory with zero files.
EMPTY_DIR_MD5 = "d751713988987e9331980363e24189ce.dir"


def workspace_path_for(project_path: Path, out: "DvcOut") -> Path:
    """Where ``out`` lives in the workspace.

    ``.dvc``-file outs record ``path`` relative to the ``.dvc`` file's own
    directory; ``dvc.lock`` stage outs (and synthetic test outs with no
    ``dvc_file``) already carry a project-relative path. S3-side twin of
    this anchoring convention: :func:`s3_key_for_out`.
    """
    if out.dvc_file is not None and out.dvc_file.name != "dvc.lock":
        return out.dvc_file.parent / out.path
    return project_path / out.path


def out_pins_no_files(out: "DvcOut") -> bool:
    """True when the out's pinned content is *known* to be zero files, so
    ``dvc checkout`` correctly materializes it as an EMPTY directory:
    a files-format out with an empty ``files:`` list, or an md5-keyed dir
    out pinning the canonical empty-dir manifest hash."""
    if out.files is not None:
        return not out.files
    return out.md5 == EMPTY_DIR_MD5


def outs_materialized(project_path: Path, outs: "list[DvcOut]") -> bool:
    """Stat-only materialization probe: every dir-out exists (and is
    non-empty unless the out pins zero files), every file-out exists.

    Workspace-side twin of :func:`ensure_out_cached` — that one asks "CAN
    checkout materialize this out from cache?", this one asks "DID checkout
    materialize it?". Dir-ness dispatches on ``DvcOut.materializes_as_dir``
    (what ``dvc checkout`` writes), never ``is_dir`` alone.
    """
    for out in outs:
        ws = workspace_path_for(project_path, out)
        if out.materializes_as_dir:
            if not ws.is_dir():
                return False
            if out_pins_no_files(out):
                continue  # legitimately empty dir: existence suffices
            try:
                next(iter(ws.iterdir()))
            except StopIteration:
                return False
        elif not ws.is_file():
            return False
    return True


def outs_for_target(project_path: Path, target: str, remote_name: str) -> "list[DvcOut]":
    """Normalize ``target``, resolve its ``.dvc`` file, parse its outs.

    Returns ``[]`` for missing or malformed ``.dvc`` files — including
    YAML-valid files with wrong-typed fields, where parse_dvc_outs raises.
    The degraded pull paths treat such a target as simply "not cached" /
    zero bytes so it keeps its fallback route instead of crashing the whole
    pull with a raw traceback.
    """
    nt = normalize_target(target)
    dvc_path = (
        project_path / nt if nt.endswith(".dvc") else project_path / f"{nt}.dvc"
    )
    try:
        return parse_dvc_outs(dvc_path, remote_name)
    # Unreadable or a folder, a bad date, a `deps` that isn't a list, deep nesting. A file
    # cut short raises PointerCutShort (check_dvc_file), which is none of these, so it stops.
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        return []


def resolve_target_outs(
    project_path: Path,
    target: str,
    remote_name: str,
    pipeline_by_target: "dict[str, DvcOut]",
) -> "list[DvcOut]":
    """Resolve a checkout candidate to its outs: prefer the already-parsed
    ``dvc.lock`` pipeline out, else parse the target's ``.dvc`` file.

    INVARIANT: the cache probe (:func:`cached_targets`) and the workspace
    verify pass (data_ops) MUST share this resolution — the verify pass
    stats exactly what the cache probe promised ``dvc checkout`` could
    materialize. Returns ``[]`` for missing/malformed ``.dvc`` targets
    (treated as "not cached" / unverifiable).
    """
    pipeline_out = pipeline_by_target.get(target)
    if pipeline_out is not None:
        return [pipeline_out]
    return outs_for_target(project_path, target, remote_name)


def normalize_target(target: str) -> str:
    """Normalize a catalog-supplied target into the posix-relative form
    that the ``.dvc`` lookup (classify_targets / data_pull) expects.

    The no-flag clone path discovers targets via ``discover_all_outs``,
    which already emits ``rel.as_posix()`` strings. ``--primary`` is the
    only caller that turns a hand-written ``data_products.primary`` string
    into a target, so a value stored on Windows (backslash separators), or
    written with a leading ``./`` or a trailing ``/``, would otherwise miss
    the on-disk ``.dvc`` file. Normalize once, at the boundary.

    Backslashes are treated as path separators. On POSIX a backslash is a
    legal filename character, but mintd data products never use them (DVC
    stores posix outs), and the value being normalized is a hand-edited
    catalog metadata string whose intent is a path separator.
    """
    t = target.replace("\\", "/")
    if t.startswith("./"):
        t = t[2:]
    return t.rstrip("/")


def classify_targets(project_path: Path, targets: list[str], remote_name: str) -> tuple[list[DvcOut], list[str], list[str]]:
    """Resolve each user-supplied target to a list of single-file DvcOut entries.

    Each user target's outs are appended to ``all_outs`` regardless of shape;
    the orchestrator's per-out loop dispatches on ``is_dir`` / ``is_files_format``.
    Hash-missing entries (entries declaring a hash type but no value) are
    surfaced in ``hash_missing`` and the orchestrator merges them into
    ``fallback`` so they never silently mark as synced.
    """
    all_outs = []
    fallback = []
    hash_missing = []

    for target in targets:
        lookup = normalize_target(target)
        dvc_path = project_path / lookup if lookup.endswith(".dvc") else project_path / f"{lookup}.dvc"
        outs = parse_dvc_outs(dvc_path, remote_name)
        if not outs:
            if dvc_path.exists():
                hash_missing.append(target)
            else:
                fallback.append(target)
            continue

        # Slice 29: `dvc import` files (deps[].repo) have data in the
        # source repo's bucket; fast-sync cannot serve them. Route to
        # fallback before any S3 client / spot-check / manifest-fetch.
        # `is_import` is a per-file flag (deps applies to the whole
        # .dvc), so we add `target` to fallback exactly once and break.
        # A mixed import+non-import .dvc would over-route the file —
        # perf regression at worst, never a correctness loss.
        if outs[0].is_import:
            logger.info(
                "fast-sync: skipping %r — dvc-import (data lives in source repo)",
                target,
            )
            fallback.append(target)
            continue

        for out in outs:
            all_outs.append(dataclasses.replace(out, target=target))

    return all_outs, fallback, hash_missing


def remote_key(name: str) -> str:
    """A DVC remote's name as dvc compares it: lowercased. dvc lowercases section names and
    ``core.remote`` when it reads a config (``Config.load_file``, and the schema's ``Lower``),
    and looks a named remote up as ``name.lower()`` (``dvc.fs.get_fs_config``), so
    ``Storage`` and ``storage`` are one remote. Every lookup and comparison of a remote's
    name goes through it."""
    return name.lower()


def read_dvc_config_text(text: str, source: str) -> dict[str, Any]:
    """A DVC config file's text, read the way dvc reads it (``dvc.config.Config.load_file``):
    configobj, which strips one pair of matching quotes and an unquoted inline ``#``
    comment and keeps ``;``; every key and section name lowercased, and ``core.remote``'s
    value too; and each
    ``remote "<name>"`` section gathered under ``conf["remote"][<name>]``. Unlike dvc,
    ``%(name)s`` is read literally. A file configobj can't parse, or one with an unquoted
    ``[remote <name>]`` header (dvc's schema refuses it: "extra keys not allowed"), is a
    ``MintdError``, since dvc can't read it either; ``source`` names it in the message."""
    # dvc's own reader, installed with dvc (which requires configobj >= 5.0.9). It ships no type
    # information, and the type-ignore count may only shrink, so it is imported untyped.
    configobj = importlib.import_module("configobj")
    try:
        parsed = configobj.ConfigObj(text.splitlines(), interpolation=False).dict()
    except configobj.ConfigObjError as exc:
        raise MintdError(f"can't read {source}: {exc}", hint="fix or remove it; dvc can't read it either") from exc
    conf: dict[str, Any] = {"remote": {}}
    for section, values in parsed.items():
        if not isinstance(values, dict):
            continue  # a key outside any section: dvc's schema refuses it, and nothing here reads one
        # A comma-separated value comes back as a list; dvc's schema would refuse it as a remote setting.
        keys = {k.lower(): ", ".join(v) if isinstance(v, list) else v for k, v in values.items()}
        name = section.lower()
        bare = re.fullmatch(r"\s*remote\s+([^\s\"]+)\s*", section, re.IGNORECASE)
        if bare:
            raise MintdError(
                f"can't read {source}: an unquoted [remote {bare.group(1)}] header",
                hint=f'write [remote "{bare.group(1)}"]; dvc can\'t read this config either',
            )
        named = re.fullmatch(r'\s*remote\s*"(.*)"\s*', name)
        if named:
            conf["remote"][remote_key(named.group(1))] = keys
        elif name.strip() == "remote":
            continue  # a [remote] with no name: dvc's schema refuses it; main read past it, and so does this
        else:
            conf[name] = keys
    if isinstance(conf.get("core", {}).get("remote"), str):
        conf["core"]["remote"] = remote_key(conf["core"]["remote"])  # dvc's schema lowercases it too
    return conf


def read_dvc_config(root: Path, level: Literal["repo", "local"]) -> dict[str, Any]:
    """``.dvc/config`` (``"repo"``) or ``.dvc/config.local`` (``"local"``), read by
    :func:`read_dvc_config_text`. A missing file reads as empty; one that can't be read is
    a ``MintdError``."""
    path = root / ".dvc" / ("config.local" if level == "local" else "config")
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"remote": {}}
    except (OSError, UnicodeDecodeError) as exc:
        raise MintdError(f"can't read {path}: {exc}", hint="fix or remove it; dvc can't read it either") from exc
    return read_dvc_config_text(text, str(path))


def default_remote_in(*confs: dict[str, Any]) -> str | None:
    """The default remote across parsed configs given highest level first, as dvc merges
    them: the first ``core.remote`` set, where an empty ``remote =`` sets none. ``None`` when
    none sets it: dvc then picks no remote either (``dvc pull`` and ``dvc push`` stop with "No
    remote provided and no default remote set"), even when the configs name exactly one."""
    for conf in confs:
        core = conf.get("core", {})
        if "remote" in core:
            return str(core["remote"]) or None
    return None


def default_remote_name(root: Path) -> str | None:
    """The project's default DVC remote: ``.dvc/config.local`` over ``.dvc/config``
    (:func:`default_remote_in`)."""
    return default_remote_in(read_dvc_config(root, "local"), read_dvc_config(root, "repo"))


def require_default_remote(root: Path, *, local_ok: bool = False) -> str:
    """The project's default remote, or the ``MintdError`` dvc's own refusal calls for, with a
    hint that names the remote when the project has exactly one. ``local_ok``: the caller
    only reads (pull, clone), so a default kept in ``config.local`` serves it; push, the cache
    lane and publish need it committed, where importers and the storage check read it."""
    name = default_remote_name(root)
    if name is not None:
        return name
    remotes = sorted({*read_dvc_config(root, "repo")["remote"], *read_dvc_config(root, "local")["remote"]})
    if len(remotes) == 1:
        hint = (f"set a default: dvc remote default {remotes[0]} (add --local to keep it out of git)" if local_ok
                else f"set a default: dvc remote default {remotes[0]}, and commit .dvc/config")
    elif remotes:
        hint = "set a default: dvc remote default <name>"
    else:
        hint = "add one: dvc remote add -d <name> <url>"
    raise MintdError("no default DVC remote in .dvc/config", hint=hint)


def parse_remote_config_text(text: str, remote_name: str | None) -> dict[str, str]:
    """One remote's settings from a DVC config given as text (the import-rescue lane reads a
    producer's committed config from git). ``remote_name=None`` takes the default
    (:func:`default_remote_in`); no default, or a named remote that is absent, raises
    ``KeyError`` so callers can map it to a hint."""
    conf = read_dvc_config_text(text, ".dvc/config")
    if remote_name is None:
        remote_name = default_remote_in(conf)
        if remote_name is None:
            raise KeyError("no default DVC remote: config sets no [core] remote")
    if remote_key(remote_name) not in conf["remote"]:
        raise KeyError(f"remote {remote_name} not found in DVC config")
    return dict(conf["remote"][remote_key(remote_name)])


def get_remote_config(project_path: Path, remote_name: str) -> dict[str, str]:
    """The remote as fast sync, the cache lane and import's location check read it: the
    committed ``.dvc/config`` section, with one per-machine override from
    ``.dvc/config.local``, the remote's ``profile`` (``dvc remote modify --local <remote>
    profile <name>``), which dvc uses too. Every other key stays the committed one: pointers
    record S3 version IDs against the committed location, the one every importer and fresh
    clone reads, so a ``config.local`` that moves ``url``, ``endpointurl`` or ``version_aware``
    points where those IDs don't belong (``data_ops._local_redirect`` refuses to publish
    from there). A remote with no committed section uses its ``config.local`` section: there
    is no location of record then, and dvc reads that one. The push check reads the other
    view, :func:`merged_remote`."""
    config_path = project_path / ".dvc" / "config"
    if not config_path.exists():
        raise FileNotFoundError(f"no .dvc/config at {config_path}")
    committed = read_dvc_config(project_path, "repo")["remote"].get(remote_key(remote_name))
    local = read_dvc_config(project_path, "local")["remote"].get(remote_key(remote_name))
    if committed is None and local is None:
        raise KeyError(f"remote {remote_name} not found in DVC config ({config_path})")
    if committed is None:
        return dict(local)
    cfg = dict(committed)
    if local is not None and "profile" in local:
        cfg["profile"] = local["profile"]
    return cfg


def merged_remote(root: Path, remote: str) -> dict[str, str]:
    """The remote as dvc pushes with it, for the push check: every key of
    ``.dvc/config.local`` laid over ``.dvc/config``. The check asks whether dvc's upload
    landed, so it must look where dvc wrote; whether that is the location of record is a
    separate check (``data_ops._local_redirect``). Fast sync reads the committed view,
    :func:`get_remote_config`."""
    merged = dict(read_dvc_config(root, "repo")["remote"].get(remote_key(remote), {}))
    merged.update(read_dvc_config(root, "local")["remote"].get(remote_key(remote), {}))
    return merged


def parse_s3_url(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        raise ValueError(f"not an s3 url: {url}")
    parts = url[5:].split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    return bucket, prefix


def s3_key_for(prefix: str, md5: str) -> str:
    parts = [prefix] if prefix else []
    parts.extend(["files", "md5", md5[:2], md5[2:]])
    return "/".join(parts)


def s3_key_for_out(prefix: str, out: DvcOut, project_path: Path) -> str:
    """S3 object key for ``out``.

    Slice 27: branches on ``out.is_path_based``:
      - Path-based (version_aware): ``<prefix>/<dvc_file.parent_rel>/<out.path>``.
        ``dvc_file.parent_rel`` is ``Path(".")`` when the .dvc file sits at
        the project root, in which case the rel-dir segment is omitted.
      - Md5-keyed: ``<prefix>/files/md5/XX/YYYY`` (delegates to ``s3_key_for``).

    Raises ``ValueError`` when ``is_path_based=True`` and ``dvc_file is None``
    (synthetic out misuse); the orchestrator's per-out try/except routes
    such outs to the dvc-pull fallback.
    """
    if out.is_path_based:
        if out.dvc_file is None:
            raise ValueError(f"path-based DvcOut {out.path!r} missing dvc_file")
        rel_dir = out.dvc_file.parent.relative_to(project_path)
        rel = out.path if rel_dir == Path(".") else f"{rel_dir.as_posix()}/{out.path}"
        return f"{prefix}/{rel}" if prefix else rel
    return s3_key_for(prefix, out.md5)


def is_dir_fully_cached(cache_dir: Path, entries: list[DvcFileEntry]) -> bool:
    return all(is_cached(cache_dir, e.md5) for e in entries)


def _dir_manifest_bytes(entries: list[DvcFileEntry]) -> bytes:
    """The ``.dir`` manifest DVC writes for ``entries``; see ``ensure_dir_manifest``."""
    payload = [{"md5": e.md5, "relpath": e.relpath} for e in sorted(entries, key=lambda e: e.relpath)]
    return json.dumps(payload, sort_keys=True).encode()


def dir_manifest_md5(entries: list[DvcFileEntry]) -> str:
    """``<md5>.dir``: the tree hash DVC gives a directory holding ``entries``,
    so ``dir_manifest_md5([]) == EMPTY_DIR_MD5``. Pure; nothing is written."""
    return f"{hashlib.md5(_dir_manifest_bytes(entries), usedforsecurity=False).hexdigest()}.dir"


def _all_dvc_outs(
    project_path: Path, remote_name: str, pointers: list[str] | None = None, *,
    declared: Collection[str] | None = None, skipped: list[str] | None = None,
) -> list:
    """Every DVC out for the project: ``.dvc`` pointers (via
    ``discover_all_outs`` + ``outs_for_target``) plus ``dvc.lock`` stage outs.
    The single enumeration both the collision guard and the tracked-path set
    read from, so neither can drift from the other's view of DVC reality.
    A caller that already walked the tree passes the ``*.dvc`` list in;
    ``declared`` and ``skipped`` go to ``partition_pipeline_outs``."""
    outs = []
    for target in pointers if pointers is not None else discover_all_outs(project_path):
        outs.extend(outs_for_target(project_path, target, remote_name))
    _, lock_outs = partition_pipeline_outs(project_path, remote_name, declared=declared, skipped=skipped)
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
    ``parse_dvc_outs`` hard-codes ``target=""``; only
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
    """Is ``name`` (``build``, or a ``fan@a`` instance) a stage ``dvc_yaml``
    declares, its keys read as DVC reads them (``load12``)?"""
    doc = _load(dvc_yaml.read_text(encoding="utf-8", errors="replace")) if dvc_yaml.is_file() else None
    stages = doc.get("stages") if isinstance(doc, dict) else None
    return isinstance(stages, dict) and name.split("@", 1)[0] in stages


def _nested_pipeline_outs(root: Path, remote_name: str) -> list[DvcOut]:
    """The stage outs of each pipeline below the root (a ``dvc.yaml`` DVC
    reads, not in a subrepo), their paths project-relative."""
    found = []
    for yaml_rel in pipeline_files(root):
        parts = Path(yaml_rel).parent.parts
        if not parts or any((root.joinpath(*parts[:k]) / ".dvc").is_dir() for k in range(1, len(parts) + 1)):
            continue
        base = Path(*parts).as_posix()
        found += [dataclasses.replace(o, path=f"{base}/{o.path}", target=f"{base}/{o.target}")
                  for o in parse_dvc_lock_outs(root.joinpath(*parts), remote_name)]
    return found


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
        if re.fullmatch(r"(?:.+/)?dvc\.yaml:.+", rel):  # a stage of the root or a nested pipeline
            stages[rel] = None
            continue
        if all_outs is None:
            all_outs = [*_all_dvc_outs(root, remote_name), *_nested_pipeline_outs(root, remote_name)]
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
                lock = Path(os.path.relpath(o.dvc_file, root)) if o.dvc_file is not None else Path("dvc.lock")
                stages[f"{lock.with_name('dvc.yaml').as_posix()}:{o.stage}"] = None
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
    """The project's DVC cache, as dvc resolves it: ``[cache] dir`` from ``.dvc/config.local``,
    then ``.dvc/config``, with ``~`` expanded and relative to ``.dvc/`` (so an empty ``dir =``
    is ``.dvc/`` itself, where dvc then writes), else ``.dvc/cache``."""
    for level in ("local", "repo"):
        cache = read_dvc_config(root, level).get("cache", {})
        if "dir" in cache:
            return Path(os.path.normpath(root / ".dvc" / os.path.expanduser(str(cache["dir"]))))
    return root / ".dvc" / "cache"


@dataclasses.dataclass(frozen=True)
class PointerJudgement:
    """``blocked``: what a commit would record that importers could not pull.
    ``not_committed``: why the pointers are left for a person to commit."""

    blocked: tuple[str, ...] = ()
    not_committed: tuple[str, ...] = ()


def _load(text: str | None) -> Any:
    """``text`` read as DVC reads it (``load12``); None when absent or not YAML."""
    from ruamel.yaml import YAMLError

    try:
        return load12(text) if text else None
    except YAMLError:
        return None


def _hashed(raw_outs: Any) -> list[dict] | None:
    """The raw outs the parsers turn into ``DvcOut``s, in their order; None
    when the list is not a list of mappings."""
    if not isinstance(raw_outs, list) or not all(isinstance(r, dict) for r in raw_outs):
        return None
    return [r for r in raw_outs if r.get("md5") or "files" in r]


def does_not_parse(root: Path, rel: str) -> bool:
    """Whether the pointer file ``rel`` is cut short by the rule every reader
    applies (``check_dvc_file`` / ``check_lock``). DVC reads one as tracking
    less than it records, and pushes nothing for the rest at exit 0."""
    path = root / rel
    if not path.is_file():
        return False
    try:
        (check_lock if Path(rel).name == "dvc.lock" else check_dvc_file)(rel, load_pointer(path))
    except PointerCutShort:
        return True
    return False


#: Files an OS drops into a folder on its own (Finder's ``.DS_Store`` whenever
#: a folder is opened, its ``._*`` resource forks, Windows' thumbnail and
#: folder settings). Left out of the folder comparison only, never out of
#: what DVC hashes, so opening a folder never stops a push.
_OS_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def _dvcignore(root: Path, dirs: list[Path]) -> pathspec.GitIgnoreSpec:
    """The rules of each ``.dvcignore`` in ``dirs`` (root first, deeper
    later), re-rooted at the project root so one spec applies them all as
    DVC does: a pattern with a slash is anchored to its file's folder, one
    without matches at any depth below it, and a later rule wins. Only the
    root and the folder's ancestors count: ``dvc add`` refuses a folder with
    a ``.dvcignore`` inside it."""
    lines = []
    for d in dirs:
        f = d / ".dvcignore"
        if not f.is_file():
            continue
        base = d.relative_to(root).as_posix()
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            neg, body = ("!", line[1:]) if line.startswith("!") else ("", line)
            if base != ".":
                body = f"{base}/{body.lstrip('/')}" if "/" in body.rstrip("/") else f"{base}/**/{body}"
            lines.append(neg + body)
    return pathspec.GitIgnoreSpec.from_lines(lines)


def _files_on_disk(root: Path, folder: str) -> list[str]:
    """What DVC would hash into ``folder`` (project-relative), relative to
    it and without OS junk; none when it isn't on disk. DVC skips
    ``.git``, ``.hg`` and ``.dvc`` entries, a subrepo (a folder with its own
    ``.dvc/``), and what a ``.dvcignore`` above it excludes."""
    top = root / folder
    parts = Path(folder).parts
    found = []
    for dirpath, dirnames, filenames in os.walk(top):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", ".hg", ".dvc") and not (here / d / ".dvc").is_dir())
        found += [(here / f).relative_to(root).as_posix() for f in filenames if f != ".git"]
    ignored = _dvcignore(root, [root, *(root.joinpath(*parts[:k]) for k in range(1, len(parts)))])
    prefix = "" if Path(folder) == Path(".") else f"{Path(folder).as_posix()}/"  # the project itself
    return sorted(p[len(prefix):] for p in found
                  if not ignored.match_file(p) and not (Path(p).name in _OS_JUNK or Path(p).name.startswith("._")))


def stages_behind(root: Path, lock: str, names: Sequence[str], *, stages: Collection[str] | None = None) -> list[str]:
    """The stages of ``lock``'s ``dvc.yaml`` that a push would find behind:
    of ``names`` (dvc's own list, ``DvcOps.stage_names``, foreach and matrix
    instances expanded), each declaring outputs whose lock entry is missing
    (never run, as ``dvc repro <one>`` leaves the others, or no ``dvc.lock``
    at all) or records fewer hashed outs than it declares (an out with no
    hash doesn't count: a version-aware push writes a stage that never ran
    that way). Only the ``stages`` being pushed count, by name or
    foreach/matrix base, all when None. DVC pushes nothing for the missing
    outs, at exit 0. The push's check alone: a pull must not refuse a stage
    that simply hasn't run."""
    path = root / lock
    declared = _declared_outs(path.with_name("dvc.yaml"))
    named = None if stages is None else set(stages)
    entries = {str(n): e for n, e in load_pointer(path)["stages"].items()} if path.is_file() else {}
    return [n for n in names
            if (base := n.split("@")[0]) in declared
            and (named is None or n in named or base in named)
            and len(_hashed((entries.get(n) or {}).get("outs") or []) or []) < declared[base]]


def with_outputs(dvc_yaml: Path, names: Sequence[str]) -> list[str]:
    """The stages of ``names`` that declare outputs in ``dvc_yaml``: the ones
    a push of their name uploads anything for."""
    declared = _declared_outs(dvc_yaml)
    return [n for n in names if n.split("@")[0] in declared]


def _declared_outs(dvc_yaml: Path) -> dict[str, int]:
    """How many outputs each ``dvc.yaml`` stage declares, counting outs,
    metrics and plots, which DVC all records under the lock entry's ``outs``
    (a foreach stage's under ``do:``). Stages declaring none are left out."""
    doc = _load(dvc_yaml.read_text(encoding="utf-8", errors="replace")) if dvc_yaml.is_file() else None
    stages = doc.get("stages") if isinstance(doc, dict) else None
    if not isinstance(stages, dict):
        return {}
    found = {}
    for name, stage in stages.items():
        body = stage.get("do") if isinstance(stage, dict) and isinstance(stage.get("do"), dict) else stage
        if isinstance(body, dict):
            n = sum(len(body[k]) for k in ("outs", "metrics", "plots") if isinstance(body.get(k), list))
            if n:
                found[str(name)] = n
    return found


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
    cut-down list equal to a tree still in the cache, or committed as it is,
    when the folder isn't on disk (with it on disk the push's folder
    comparison, ``unlisted_on_disk``, refuses it before upload); and a tree
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
        try:
            doc = load_pointer(path)
            (check_lock if p == "dvc.lock" else check_dvc_file)(p, doc)
        except PointerCutShort as exc:
            blocked.append(f"{p} is cut short or damaged: {exc.detail}")
            continue
        head_doc = _load(head.get(p))
        if p == "dvc.lock":
            stages = doc["stages"]
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
                outs = parsed.get(str(stage), [])
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
        raw_outs = doc["outs"]  # whole: every out has a path and a hash (check_dvc_file)
        if not version_aware and selection is not None and p not in selection:
            soft.append(f"on an md5-layout remote nothing shows {p} reached S3, and this push did not upload it")
            continue
        outs = parse_dvc_outs(path, remote_name)
        head_by_path = _by_path(head_doc.get("outs") if isinstance(head_doc, dict) else None)
        for raw, o in zip(raw_outs, outs, strict=True):
            judge(o, raw, head_by_path.get(str(raw.get("path"))), p)
    return PointerJudgement(blocked=tuple(blocked), not_committed=tuple(soft))
