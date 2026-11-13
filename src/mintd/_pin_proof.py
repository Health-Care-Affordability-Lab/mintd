"""Which S3 file versions a push answers for, and whether S3 holds them.

On a version-aware remote an importer finds a file only through the version ID
its pointer records under ``cloud.<remote>``. An *entry* is one such file
version: a file out, or one ``files:`` entry of a directory out. Its S3 key is
the file's repo path under the remote's prefix, computed from the pointer's
location and the stage's ``wdir``, never from whether an ID is present.

Read from the pointer files themselves, with plain S3 calls to check them, so
the checks don't depend on DVC's Python API.
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ._fast_sync_ops import (
    HeadVerdict,
    cache_path_for,
    flagged_outs,
    read_cached_dir_manifest,
    resolve_out,
    stage_wdir,
    wdir_map,
)

_READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class Entry:
    """One file version a pointer records for remote R.

    ``pointer`` is the repo-relative pointer file (``data/final.dvc`` or
    ``dvc.lock``); ``out`` the out's repo-relative path; ``relpath`` the
    file's path inside a directory out, else None. ``version_id`` / ``etag``
    come only from ``cloud[R]``. ``legacy_dir``: enumerated from a cached
    ``.dir`` tree because the pointer lists no files. ``record`` locates the
    YAML record the ID lives in: ``(out index, files index or None)`` within
    the ``.dvc`` file's ``outs`` or the lock stage's ``outs``.
    """

    pointer: str
    stage: str | None
    out: str
    relpath: str | None
    md5: str
    size: int | None
    key: str
    version_id: str | None = None
    etag: str | None = None
    legacy_dir: bool = False
    record: tuple[int, int | None] = (0, None)

    @property
    def path(self) -> str:
        return f"{self.out}/{self.relpath}" if self.relpath else self.out


@dataclass(frozen=True)
class Out:
    """An out located in its pointer: ``(pointer, stage, index)`` and its
    repo-relative ``path``."""

    pointer: str
    stage: str | None
    index: int
    path: str


@dataclass(frozen=True)
class Scope:
    """``entries`` the pointers record for R. ``unrecoverable``: legacy
    directory outs whose tree isn't in the local cache, so no entry for them
    can be listed, proven or re-uploaded. ``empty``: directory outs whose
    ``files:`` list is empty, which have no entry to check at all."""

    entries: tuple[Entry, ...]
    unrecoverable: tuple[Out, ...] = ()
    empty: tuple[Out, ...] = ()


def _load(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _cloud(raw: dict, remote: str) -> tuple[str | None, str | None]:
    block = (raw.get("cloud") or {}).get(remote) if isinstance(raw.get("cloud"), dict) else None
    if not isinstance(block, dict):
        return None, None
    vid, etag = block.get("version_id"), block.get("etag")
    return (str(vid) if vid else None), (str(etag) if etag else None)


def _key(prefix: str, repo_path: str) -> str:
    return f"{prefix}/{repo_path}" if prefix else repo_path


def _out_entries(
    raw: dict, *, pointer: str, stage: str | None, out_path: str, remote: str, prefix: str,
    cache_dir: Path, index: int,
) -> tuple[list[Entry], bool]:
    """Entries of one out; the bool is False when a legacy directory out's
    tree isn't cached, so its files can't be listed."""
    md5 = str(raw.get("md5") or "")
    files = raw.get("files")
    if isinstance(files, list):
        entries = []
        for j, fe in enumerate(files):
            if not isinstance(fe, dict):
                continue
            rel = str(fe.get("relpath", ""))
            vid, etag = _cloud(fe, remote)
            entries.append(Entry(
                pointer=pointer, stage=stage, out=out_path, relpath=rel,
                md5=str(fe.get("md5", "")), size=_int(fe.get("size")),
                key=_key(prefix, f"{out_path}/{rel}"), version_id=vid, etag=etag,
                record=(index, j),
            ))
        return entries, True
    if md5.endswith(".dir"):
        tree = read_cached_dir_manifest(cache_dir, md5)
        if tree is None:
            return [], False
        return [
            Entry(
                pointer=pointer, stage=stage, out=out_path, relpath=fe.relpath, md5=fe.md5,
                size=fe.size or None, key=_key(prefix, f"{out_path}/{fe.relpath}"),
                legacy_dir=True, record=(index, None),
            )
            for fe in tree
        ], True
    vid, etag = _cloud(raw, remote)
    return [Entry(
        pointer=pointer, stage=stage, out=out_path, relpath=None, md5=md5,
        size=_int(raw.get("size")), key=_key(prefix, out_path), version_id=vid, etag=etag,
        record=(index, None),
    )], True


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _escapes(repo_path: str) -> bool:
    return repo_path == ".." or repo_path.startswith("../") or posixpath.isabs(repo_path)


def publishable_scope(
    root: Path, *, remote: str, prefix: str, cache_dir: Path, dvc_files: list[str],
) -> Scope:
    """Every entry a ``dvc push`` to ``remote`` uploads and records.

    DVC's push predicate: outs in the repo, cached, not ``push: false``, from
    stages that are not repo imports (``deps[].repo``). Stages declared in
    ``dvc.yaml`` but never run have no lock entry, so they never appear.
    Only the root ``dvc.lock`` is read; nested lock files are out of scope.
    """
    entries: list[Entry] = []
    unrecoverable: list[Out] = []
    empty: list[Out] = []
    for rel in dvc_files:
        doc = _load(root / rel)
        if not isinstance(doc, dict) or not isinstance(doc.get("outs"), list):
            continue
        deps = doc.get("deps") or []
        if any(isinstance(d, dict) and isinstance(d.get("repo"), dict) for d in deps):
            continue
        base = posixpath.join(posixpath.dirname(rel) or ".", str(doc.get("wdir") or "."))
        for i, raw in enumerate(doc["outs"]):
            if not isinstance(raw, dict) or raw.get("cache") is False or raw.get("push") is False:
                continue
            out_path = resolve_out(base, str(raw.get("path", "")))
            if _escapes(out_path):
                continue
            got, ok = _out_entries(raw, pointer=rel, stage=None, out_path=out_path,
                                   remote=remote, prefix=prefix, cache_dir=cache_dir, index=i)
            entries.extend(got)
            if not ok:
                unrecoverable.append(Out(rel, None, i, out_path))
            elif not got:
                empty.append(Out(rel, None, i, out_path))

    lock = _load(root / "dvc.lock")
    stages = lock.get("stages") if isinstance(lock, dict) else None
    if isinstance(stages, dict):
        yaml_doc = _load(root / "dvc.yaml")
        wdirs = wdir_map(yaml_doc)
        skip = flagged_outs(yaml_doc, "cache")
        for stage, paths in flagged_outs(yaml_doc, "push").items():
            skip.setdefault(stage, set()).update(paths)
        declared = set((yaml_doc.get("stages") or {}) if isinstance(yaml_doc, dict) else ())
        for stage, data in stages.items():
            # DVC pushes from dvc.yaml: a lock stage it no longer declares
            # (renamed, removed) is never uploaded or re-pinned.
            if declared and str(stage) not in declared and str(stage).split("@", 1)[0] not in declared:
                continue
            outs = data.get("outs") if isinstance(data, dict) else None
            wdir = stage_wdir(wdirs, str(stage))
            if wdir is None:  # an absolute wdir: outside the project
                continue
            declared_skip = skip.get(str(stage), skip.get(str(stage).split("@", 1)[0], set()))
            for i, raw in enumerate(outs if isinstance(outs, list) else []):
                if not isinstance(raw, dict):
                    continue
                written = posixpath.normpath(str(raw.get("path", "")))
                if written in declared_skip:
                    continue
                out_path = resolve_out(wdir, written)
                if _escapes(out_path):
                    continue
                got, ok = _out_entries(raw, pointer="dvc.lock", stage=str(stage), out_path=out_path,
                                       remote=remote, prefix=prefix, cache_dir=cache_dir, index=i)
                entries.extend(got)
                if not ok:
                    unrecoverable.append(Out("dvc.lock", str(stage), i, out_path))
                elif not got:
                    empty.append(Out("dvc.lock", str(stage), i, out_path))
    return Scope(tuple(entries), tuple(unrecoverable), tuple(empty))


# --- content proof ----------------------------------------------------------


def _part_size(size: int) -> int:
    """The part size s3fs uploads a ``size``-byte file with: 50 MiB, raised to
    stay within S3's 10,000 parts. dvc-s3 passes no chunk size of its own."""
    from s3fs.core import calculate_chunksize  # type: ignore[import-untyped]

    return int(calculate_chunksize(size))


def single_put(size: int) -> bool:
    """s3fs sends a file below this size as one PUT, whose ETag is its md5."""
    return size < min(5 * 2**30, 2 * _part_size(size))


def hash_cache_object(path: Path) -> tuple[str, str | None, int] | None:
    """``(md5, multipart ETag or None, size)`` of a cache file, in one read.
    The multipart ETag is what s3fs's upload of this file would produce;
    None when s3fs would send it as a single PUT. None if it can't be read."""
    try:
        size = path.stat().st_size
        part = _part_size(size)
        whole = hashlib.md5()
        parts: list[bytes] = []
        current = hashlib.md5()
        filled = 0
        with open(path, "rb") as f:
            while chunk := f.read(min(_READ_CHUNK, part - filled)):
                whole.update(chunk)
                current.update(chunk)
                filled += len(chunk)
                if filled == part:
                    parts.append(current.digest())
                    current, filled = hashlib.md5(), 0
        if filled:
            parts.append(current.digest())
    except OSError:
        return None
    if single_put(size):
        return whole.hexdigest(), None, size
    return whole.hexdigest(), f"{hashlib.md5(b''.join(parts)).hexdigest()}-{len(parts)}", size


@dataclass
class CacheHashes:
    """Each cache object is read at most once per command."""

    cache_dir: Path
    _seen: dict[str, tuple[str, str | None, int] | None]

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._seen = {}

    def get(self, md5: str) -> tuple[str, str | None, int] | None:
        if md5 not in self._seen:
            path = cache_path_for(self.cache_dir, md5)
            self._seen[md5] = hash_cache_object(path) if path.is_file() else None
        return self._seen[md5]

    def holds(self, md5: str) -> bool:
        got = self.get(md5)
        return got is not None and got[0] == md5


def prove(entry: Entry, verdict: HeadVerdict, cache: CacheHashes) -> str:
    """Does S3's version hold the bytes ``entry`` records?

    ``ok``: proven. ``wrong_bytes``: a single-part ETag that isn't the md5.
    ``unproven``: multipart and the cache can't settle it (no cache file, or a
    recomputed ETag that differs, which other chunking of the same bytes would
    also give). ``size_only``: multipart, no cache file, and the size matches
    the recorded one; an allowance, never a proof.
    Anything but ``found`` is returned as the verdict's own kind.

    A single-part ETag equal to the md5, or a recomputed multipart ETag equal
    to S3's, already pins the size, so size matters only for the allowance.
    """
    if verdict.kind != "found":
        return verdict.kind
    etag = (verdict.etag or "").strip('"')
    if "-" not in etag:
        return "ok" if etag == entry.md5 else "wrong_bytes"
    if cache.holds(entry.md5):
        cached = cache.get(entry.md5)
        return "ok" if cached is not None and cached[1] == etag else "unproven"
    if entry.size is not None and verdict.size == entry.size:
        return "size_only"
    return "unproven"


def pinned(entry: Entry, verdict: HeadVerdict) -> Entry:
    """``entry`` re-pinned to the version ``verdict`` found."""
    return replace(entry, version_id=verdict.version_id, etag=(verdict.etag or "").strip('"'))


# --- repair (before the push) -----------------------------------------------


@dataclass(frozen=True)
class RepairPlan:
    """What push does with each entry in repair scope, decided before any write.

    ``reattach``: ``(entry, re-pinned entry, reason)``; the object already at
    the key proves the file, so its ID goes into the pointer and DVC uploads
    nothing. ``strip``: ``(entry, reason)``; the cache holds the file, so this
    remote's ID is removed and DVC uploads it. ``unrecoverable``:
    ``(entry, why)``. ``inconclusive``: S3 didn't answer, left unchanged.
    ``observed``: the version at each stripped or legacy entry's key before
    the push (None: no object), which the upload must replace.
    """

    keep: tuple[Entry, ...] = ()
    reattach: tuple[tuple[Entry, Entry, str], ...] = ()
    strip: tuple[tuple[Entry, str], ...] = ()
    unrecoverable: tuple[tuple[Entry, str], ...] = ()
    inconclusive: tuple[Entry, ...] = ()
    observed: dict[str, str | None] | None = None

    @property
    def changes(self) -> bool:
        return bool(self.reattach or self.strip)


NO_ID = "no ID"
STALE_ID = "stale ID"


def plan_repair(
    s3: Any, bucket: str, entries: tuple[Entry, ...], cache: CacheHashes,
    memo: "ProofMemo | None" = None,
) -> RepairPlan:
    """Decide, for every entry, keep / re-attach / strip / unrecoverable.

    1. Its own version proves the file (a multipart pin with no cache file
       passes on size alone; the push didn't set it): keep.
    2. Otherwise the current object at its key, if that proves the file:
       re-attach.
    3. Otherwise, the cache holds bytes that hash to its md5, or the entry is
       a legacy ``.dir`` file (never re-attached; DVC uploads it): strip.
    4. Otherwise: unrecoverable.
    An entry S3 gave no clear answer for is left as it is.
    """
    from ._fast_sync_ops import probe_versions

    pins = prove_pins(s3, bucket, [e for e in entries if e.version_id and not e.legacy_dir], cache, memo)
    keep: list[Entry] = []
    rest: list[Entry] = []
    inconclusive: list[Entry] = []
    for e in entries:
        if e.legacy_dir or not e.version_id:
            rest.append(e)
            continue
        verdict = pins[(e.key, e.version_id)]
        if verdict in ("ok", "size_only"):
            keep.append(e)
        elif verdict == "inconclusive":
            inconclusive.append(e)
        else:
            rest.append(e)

    current = probe_versions(s3, bucket, [(e.key, None) for e in rest])
    reattach: list[tuple[Entry, Entry, str]] = []
    strip: list[tuple[Entry, str]] = []
    unrecoverable: list[tuple[Entry, str]] = []
    observed: dict[str, str | None] = {}
    for e in rest:
        now = current[(e.key, None)]
        reason = NO_ID if not e.version_id else STALE_ID
        if now.kind == "inconclusive":
            inconclusive.append(e)
            continue
        if not e.legacy_dir and prove(e, now, cache) == "ok":
            reattach.append((e, pinned(e, now), reason))
            if memo is not None:
                memo.remember(bucket, pinned(e, now), now.size)
            continue
        # A legacy entry's pointer isn't written (DVC rewrites the out as a
        # files list when it uploads), so nothing needs stopping before the
        # push; the gate's upload check proves each file arrived.
        if e.legacy_dir or cache.holds(e.md5):
            strip.append((e, reason))
            observed[e.key] = now.version_id if now.kind == "found" else None
            continue
        cached = cache.get(e.md5)
        why = (
            f"the cache file {cache_path_for(cache.cache_dir, e.md5)} doesn't hash to {e.md5}"
            if cached is not None
            else "not provably on S3 at its current path and not in the local cache"
        )
        unrecoverable.append((e, why))
    return RepairPlan(tuple(keep), tuple(reattach), tuple(strip), tuple(unrecoverable),
                      tuple(inconclusive), observed)


def cause_lines(plan: RepairPlan, location: str) -> list[str]:
    """One line per likely cause, so a cause that keeps recurring is noticed."""
    lines: list[str] = []
    # Only a re-attached file's ID was lost: its bytes were already on S3. A
    # stripped file with no ID is usually new data that was never pushed.
    no_id = [e for e, _, r in plan.reattach if r == NO_ID]
    if any(e.pointer == "dvc.lock" for e in no_id):
        lines.append("dvc.lock lost these IDs; `dvc commit -f` or `dvc repro` usually does this")
    if any(e.pointer != "dvc.lock" and not e.legacy_dir for e in no_id):
        lines.append("`dvc add` or `dvc commit` rewrote these .dvc files without their IDs")
    if any(r == STALE_ID for _, _, r in plan.reattach) or any(r == STALE_ID for _, r in plan.strip):
        lines.append(f"the recorded versions aren't at {location}; the remote has probably moved")
    return lines


class PointerWriteError(Exception):
    """A pointer write changed more than this remote's version IDs."""

    def __init__(self, pointer: str) -> None:
        super().__init__(
            f"rewriting {pointer} changed more than its S3 version IDs; every pointer was "
            "put back as it was and nothing was pushed"
        )
        self.pointer = pointer


def _records(doc: Any, stage: str | None) -> list:
    if stage is None:
        outs = doc.get("outs") if isinstance(doc, dict) else None
    else:
        outs = ((doc.get("stages") or {}).get(stage) or {}).get("outs") if isinstance(doc, dict) else None
    return outs if isinstance(outs, list) else []


def _without_ids(doc: Any, remote: str) -> Any:
    """``doc`` with ``cloud[remote]`` (and a ``cloud`` left empty) removed
    from every out and files entry, for comparing two versions of a pointer."""
    import copy

    doc = copy.deepcopy(doc)
    bodies = [doc] if isinstance(doc, dict) and "outs" in doc else [
        s for s in ((doc.get("stages") or {}).values() if isinstance(doc, dict) else ()) if isinstance(s, dict)
    ]
    for body in bodies:
        for out in body.get("outs") or []:
            for rec in [out, *((out.get("files") or []) if isinstance(out, dict) else [])]:
                if isinstance(rec, dict) and isinstance(rec.get("cloud"), dict):
                    rec["cloud"].pop(remote, None)
                    if not rec["cloud"]:
                        del rec["cloud"]
    return doc


def _yaml12(rt: bool) -> Any:
    """ruamel (DVC's own YAML 1.2 library): ``rt`` round-trips a file keeping
    its formatting and quoting; otherwise a plain 1.2 reader for comparing.
    PyYAML reads YAML 1.1, where DVC's unquoted ``no`` / ``on`` / ``1:30``
    are booleans and numbers, so a PyYAML rewrite changes them."""
    from ruamel.yaml import YAML

    y = YAML(typ="rt" if rt else "safe", pure=True)
    y.width = 4096
    y.preserve_quotes = True
    return y


def load_rt(text: str) -> Any:
    return _yaml12(True).load(text)


def load12(text: str) -> Any:
    return _yaml12(False).load(text)


def _dump(doc: Any) -> str:
    import io

    buf = io.StringIO()
    _yaml12(True).dump(doc, buf)
    return buf.getvalue()


def write_repair(root: Path, plan: RepairPlan, remote: str, *, dump: Any = _dump) -> list[str]:
    """Write the plan's re-attached IDs and strips into the pointer files.

    Only ``cloud[remote]`` changes. Each written file is re-read and compared
    with its original, IDs aside; any other difference puts every written
    file back to its original bytes and raises ``PointerWriteError``.
    Returns the pointers written.
    """
    from ._atomic import _atomic_write_text

    by_pointer: dict[str, list[tuple[Entry, Entry | None]]] = {}
    for was, now, _ in plan.reattach:
        by_pointer.setdefault(was.pointer, []).append((was, now))
    for was, _ in plan.strip:
        if not was.legacy_dir:
            by_pointer.setdefault(was.pointer, []).append((was, None))
    originals: dict[str, bytes] = {}
    try:
        for pointer, changes in sorted(by_pointer.items()):
            path = root / pointer
            raw = path.read_bytes()
            doc = load_rt(raw.decode("utf-8"))
            for old, new in changes:
                out_i, file_i = old.record
                rec = _records(doc, old.stage)[out_i]
                if file_i is not None:
                    rec = rec["files"][file_i]
                if new is None:
                    cloud = rec.get("cloud") if isinstance(rec.get("cloud"), dict) else {}
                    cloud.pop(remote, None)
                    if not cloud:
                        rec.pop("cloud", None)
                else:
                    rec.setdefault("cloud", {})[remote] = {"etag": new.etag, "version_id": new.version_id}
            originals[pointer] = raw
            _atomic_write_text(path, dump(doc))
            if _without_ids(load12(path.read_text(encoding="utf-8")), remote) != _without_ids(
                load12(raw.decode("utf-8")), remote
            ):
                raise PointerWriteError(pointer)
    except Exception:
        for pointer, raw in originals.items():
            _atomic_write_text(root / pointer, raw.decode("utf-8"), newline="")  # bytes as they were
        raise
    return sorted(originals)


# --- proof memo ---------------------------------------------------------------

MEMO_SAMPLE = 16


class ProofMemo:
    """Versions this machine has proven to hold their bytes:
    ``(endpoint, bucket, key, version_id) -> (md5, size)``.

    A version's bytes never change, so a full proof stays true; only a
    deletion could break it, and the lab never deletes on S3. Read access can
    change, though, so each push still HEADs a random sample of what it
    remembers (``MEMO_SAMPLE`` per bucket and prefix) and ignores the memo
    for the rest of the command if any of them fails. Only full proofs are
    written, never the multipart size-only allowance.
    """

    def __init__(self, path: Path, endpoint: str) -> None:
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS proven (endpoint TEXT, bucket TEXT, key TEXT, version_id TEXT,"
            " md5 TEXT, size INTEGER, PRIMARY KEY (endpoint, bucket, key, version_id))"
        )
        self._endpoint = endpoint
        self.disabled = False
        self.hits = 0

    def knows(self, bucket: str, e: Entry) -> bool:
        import sqlite3

        if self.disabled or not e.version_id:
            return False
        try:
            row = self._db.execute(
                "SELECT md5, size FROM proven WHERE endpoint=? AND bucket=? AND key=? AND version_id=?",
                (self._endpoint, bucket, e.key, e.version_id),
            ).fetchone()
        except sqlite3.Error:  # locked or damaged: HEAD everything instead
            self.disabled = True
            return False
        return row is not None and row[0] == e.md5 and (e.size is None or row[1] == e.size)

    def remember(self, bucket: str, e: Entry, size: int | None) -> None:
        import sqlite3

        if self.disabled:
            return
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO proven VALUES (?, ?, ?, ?, ?, ?)",
                (self._endpoint, bucket, e.key, e.version_id, e.md5, size),
            )
            self._db.commit()
        except sqlite3.Error:
            self.disabled = True


def prove_pins(
    s3: Any, bucket: str, entries: list[Entry], cache: CacheHashes, memo: ProofMemo | None,
) -> dict[tuple, str]:
    """``prove`` for each entry's own pin, by HEAD or from the memo.

    Remembered entries skip the HEAD, apart from a random sample that must
    still prove; one that fails disables the memo and every entry is HEADed.
    Every full proof is remembered. Keyed by ``(key, version_id)``.
    """
    import random

    from ._fast_sync_ops import probe_versions

    remembered = [e for e in entries if memo is not None and memo.knows(bucket, e)]
    sample = random.sample(remembered, min(MEMO_SAMPLE, len(remembered)))
    skipped = {(e.key, e.version_id) for e in remembered} - {(e.key, e.version_id) for e in sample}
    heads = probe_versions(s3, bucket, [(e.key, e.version_id) for e in entries
                                        if (e.key, e.version_id) not in skipped])
    verdicts = {(e.key, e.version_id): prove(e, heads[(e.key, e.version_id)], cache)
                for e in entries if (e.key, e.version_id) in heads}
    if memo is not None and any(verdicts[(e.key, e.version_id)] != "ok" for e in sample):
        memo.disabled = True
        rest = [e for e in entries if (e.key, e.version_id) in skipped]
        more = probe_versions(s3, bucket, [(e.key, e.version_id) for e in rest])
        heads.update(more)
        verdicts.update({(e.key, e.version_id): prove(e, more[(e.key, e.version_id)], cache) for e in rest})
        skipped = set()
    for pin in skipped:
        verdicts[pin] = "ok"
    if memo is not None:
        memo.hits += len(skipped)
        for e in entries:
            pin = (e.key, e.version_id)
            if pin in heads and verdicts[pin] == "ok":
                memo.remember(bucket, e, heads[pin].size)
    return verdicts
