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
    ``.dir`` tree because the pointer lists no files.
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

    @property
    def path(self) -> str:
        return f"{self.out}/{self.relpath}" if self.relpath else self.out


@dataclass(frozen=True)
class Scope:
    """``entries`` the pointers record for R; ``unrecoverable`` names legacy
    directory outs whose tree isn't in the local cache, so no entry for them
    can be listed, proven or re-uploaded."""

    entries: tuple[Entry, ...]
    unrecoverable: tuple[str, ...] = ()


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
    cache_dir: Path,
) -> tuple[list[Entry], bool]:
    """Entries of one out; the bool is False when a legacy directory out's
    tree isn't cached, so its files can't be listed."""
    md5 = str(raw.get("md5") or "")
    files = raw.get("files")
    if isinstance(files, list):
        entries = []
        for fe in files:
            if not isinstance(fe, dict):
                continue
            rel = str(fe.get("relpath", ""))
            vid, etag = _cloud(fe, remote)
            entries.append(Entry(
                pointer=pointer, stage=stage, out=out_path, relpath=rel,
                md5=str(fe.get("md5", "")), size=_int(fe.get("size")),
                key=_key(prefix, f"{out_path}/{rel}"), version_id=vid, etag=etag,
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
                legacy_dir=True,
            )
            for fe in tree
        ], True
    vid, etag = _cloud(raw, remote)
    return [Entry(
        pointer=pointer, stage=stage, out=out_path, relpath=None, md5=md5,
        size=_int(raw.get("size")), key=_key(prefix, out_path), version_id=vid, etag=etag,
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
    unrecoverable: list[str] = []
    for rel in dvc_files:
        doc = _load(root / rel)
        if not isinstance(doc, dict) or not isinstance(doc.get("outs"), list):
            continue
        deps = doc.get("deps") or []
        if any(isinstance(d, dict) and isinstance(d.get("repo"), dict) for d in deps):
            continue
        base = posixpath.join(posixpath.dirname(rel) or ".", str(doc.get("wdir") or "."))
        for raw in doc["outs"]:
            if not isinstance(raw, dict) or raw.get("cache") is False or raw.get("push") is False:
                continue
            out_path = resolve_out(base, str(raw.get("path", "")))
            if _escapes(out_path):
                continue
            got, ok = _out_entries(raw, pointer=rel, stage=None, out_path=out_path,
                                   remote=remote, prefix=prefix, cache_dir=cache_dir)
            entries.extend(got)
            if not ok:
                unrecoverable.append(out_path)

    lock = _load(root / "dvc.lock")
    stages = lock.get("stages") if isinstance(lock, dict) else None
    if isinstance(stages, dict):
        yaml_doc = _load(root / "dvc.yaml")
        wdirs = wdir_map(yaml_doc)
        skip = flagged_outs(yaml_doc, "cache")
        for stage, paths in flagged_outs(yaml_doc, "push").items():
            skip.setdefault(stage, set()).update(paths)
        for stage, data in stages.items():
            outs = data.get("outs") if isinstance(data, dict) else None
            wdir = stage_wdir(wdirs, str(stage)) or "."
            declared_skip = skip.get(str(stage), skip.get(str(stage).split("@", 1)[0], set()))
            for raw in outs if isinstance(outs, list) else []:
                if not isinstance(raw, dict):
                    continue
                written = posixpath.normpath(str(raw.get("path", "")))
                if written in declared_skip:
                    continue
                out_path = resolve_out(wdir, written)
                if _escapes(out_path):
                    continue
                got, ok = _out_entries(raw, pointer="dvc.lock", stage=str(stage), out_path=out_path,
                                       remote=remote, prefix=prefix, cache_dir=cache_dir)
                entries.extend(got)
                if not ok:
                    unrecoverable.append(out_path)
    return Scope(tuple(entries), tuple(unrecoverable))


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
