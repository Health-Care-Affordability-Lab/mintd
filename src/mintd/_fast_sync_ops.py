"""DVC FastSync implementation.

Coupled to DVC cache layout (3.66.x).
"""

import hashlib
import json
import logging
import random
import shlex
import stat
import subprocess
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol


from mintd._atomic import _try_fsync_file, _try_fsync_parent_dir
from mintd._dvc_invoke import dvc_cmd, dvc_env
from mintd._dvc_state import (
    _DEFAULT_DVC_CACHE_REL,
    DvcFileEntry,
    DvcOut,
    PointerCutShort,
    _dir_manifest_bytes,
    _listed,
    cache_path_for,
    classify_targets,
    dir_manifest_md5,
    get_remote_config,
    is_cached,
    is_dir_fully_cached,
    parse_s3_url,
    read_cached_dir_manifest,
    resolve_target_outs,
    s3_key_for,
    s3_key_for_out,
)
from mintd._s3 import (
    _DRIFT_404_CODES,
    ClientError,
    NoCredentialsError,
    available,
    check_bucket_versioning,
    download,
    retry_transient,
)
from mintd._s3 import create_client as _create_s3_client  # tests patch it here
from mintd.model import FastPullResult

if TYPE_CHECKING:
    from mintd._console import Reporter

logger = logging.getLogger(__name__)

_DVC_FLOOR = (3, 66)
_DVC_CEILING = (4, 0)  # exclusive
_SPOT_CHECK_N = 5

def _check_dvc() -> tuple[bool, str | None]:
    """Probe the bundled dvc. Return (ok, reason_if_not_ok)."""
    # Same "subprocess argv:" prefix and shlex quoting as run_streaming, so
    # -vv output has one grep-able, copy-pasteable format for every spawn.
    logger.debug("subprocess argv: %s", shlex.join([*dvc_cmd(), "--version"]))
    try:
        result = subprocess.run(
            [*dvc_cmd(), "--version"],
            env=dvc_env(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False, "dvc not installed"
    except subprocess.TimeoutExpired:
        return False, "dvc version probe timed out"
    if result.returncode != 0:
        # `sys.executable -m dvc` returns exit 1 + "No module named 'dvc'" on
        # stderr when dvc isn't installed in mintd's env — re-emit the
        # honest reason rather than the opaque "probe failed" string.
        if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
            return False, "dvc not installed"
        return False, f"dvc version probe failed (exit {result.returncode})"
    version = result.stdout.strip()
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return False, f"dvc version unparseable: {version!r}"
    if (major, minor) < _DVC_FLOOR:
        return False, f"dvc {major}.{minor} below floor {_DVC_FLOOR[0]}.{_DVC_FLOOR[1]}"
    if (major, minor) >= _DVC_CEILING:
        return False, f"dvc {major}.{minor} above ceiling {_DVC_CEILING[0]}.{_DVC_CEILING[1]}"
    return True, None


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    expected: str
    actual: str


def is_version_aware(out: "DvcOut") -> bool:
    """Pure format predicate: does the out pin S3 object versions (a
    ``version_id`` at any level — top-level, cloud-nested/path-based, or on
    any files-format entry)?

    Routing decisions should call :func:`dvc_pull_can_serve` instead; this
    predicate says nothing about which command can restore the out.
    """
    if out.version_id or out.is_path_based:
        return True
    if not out.files:
        return False
    return any(fe.version_id or fe.is_path_based for fe in out.files)


def dvc_pull_can_serve(out: "DvcOut") -> bool:
    """Routing policy: can plain ``dvc pull`` restore this out?

    - dvc-imports: yes, always — their data lives in the source repo's
      bucket, which ``dvc pull`` reaches and fast-sync cannot (slice 29).
      A pinned version_id on an import does not change that. When even plain
      ``dvc pull`` cannot materialize an import (a version-aware producer
      bucket whose committed lock recorded no version_id), the downstream
      consumer-side rescue lane (``_import_rescue_ops``) fetches the
      producer's objects directly — this routing is unchanged; the rescue
      fires only after the fallback pull has already failed.
    - version-aware outs: no — plain ``dvc pull`` is documented broken on
      these (rehash-on-pull re-downloads, StorageKeyError tuple crashes;
      see the fallback-scope comments in data_ops.py). When fast-sync
      cannot serve them they must fail loudly instead.
    - everything else (md5-keyed): yes.

    This is THE fallback-vs-blocked split; every routing site goes through
    it (directly or via :func:`_route_out`) so the policy cannot diverge.
    """
    return out.is_import or not is_version_aware(out)


def _route_out(
    out: "DvcOut",
    why: str,
    *,
    fallback: list[str],
    blocked_targets: list[str],
    blocked_reasons: dict[str, str],
) -> None:
    """Route one out fast-sync could not serve: fallback when plain
    ``dvc pull`` can restore it, otherwise a loud per-target error with
    ``why`` as its reason."""
    if dvc_pull_can_serve(out):
        fallback.append(out.target)
    else:
        blocked_targets.append(out.target)
        blocked_reasons.setdefault(out.target, why)


def _degrade_all_targets(
    project_path: Path,
    targets: list[str],
    remote_name: str,
    pipeline_outs: list["DvcOut"] | None,
    reason: str,
) -> FastPullResult:
    """All-or-nothing degradation: a guard stopped the whole try_fast_pull
    call (dvc version mismatch, remote config missing, non-S3 remote, boto3
    missing, credentials/versioning probes, versioning disabled).

    Degrading must NOT dump every target into one plain ``dvc pull`` — that
    command is documented broken on version-aware outs. Classify targets
    FIRST (pure .dvc parsing: no S3, no subprocess, works even when boto3
    is absent), then split per :func:`dvc_pull_can_serve`:

    - imports, md5-keyed outs, unparseable and hash-missing targets keep the
      fallback route (plain ``dvc pull`` genuinely serves them);
    - version-aware outs land in ``blocked_targets`` with the guard's reason —
      the caller reports each loudly and exits non-zero.
    """
    all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)
    fallback = fallback + hash_missing
    by_target: dict[str, list[DvcOut]] = {}
    for out in all_outs:
        by_target.setdefault(out.target, []).append(out)
    blocked_targets: list[str] = []
    blocked_reasons: dict[str, str] = {}
    for target, outs in by_target.items():
        if all(dvc_pull_can_serve(o) for o in outs):
            fallback.append(target)
        else:
            blocked_targets.append(target)
            blocked_reasons.setdefault(target, reason)
    for out in pipeline_outs or []:
        _route_out(
            out, reason,
            fallback=fallback,
            blocked_targets=blocked_targets,
            blocked_reasons=blocked_reasons,
        )
    blocked_targets = list(dict.fromkeys(blocked_targets))
    fallback = list(dict.fromkeys(fallback))
    return FastPullResult(
        success=False,
        fallback_targets=fallback,
        reason=reason,
        blocked_targets=blocked_targets,
        blocked_reasons=blocked_reasons,
    )








def ensure_out_cached(cache_dir: Path, out: "DvcOut") -> bool:
    """True when every blob this out pins is verifiably in the local DVC cache.

    Used by the degraded pull paths (data_ops) to decide what `dvc checkout`
    can materialize WITHOUT any fallback `dvc pull`. The cache is
    content-addressed against the md5s pinned in the `.dvc` / `dvc.lock`
    entry, so a fully-cached out is materializable regardless of what
    happened on the remote.

    Workspace-side twin: :func:`outs_materialized` ("DID checkout
    materialize it?"). Its dir-vs-file dispatch is
    ``DvcOut.materializes_as_dir``; the arm ordering below (files-format
    first, then ``.dir`` md5, then single file) encodes the same fact.

    "ensure" because the files-format arm has a repair side effect:

    - dvc-imports are never "cached": their data lives in the source repo's
      bucket and they route to plain `dvc pull` intentionally (slice 29).
    - files-format dir-outs: all per-file blobs must be present. When they
      are, the synthetic ``.dir`` manifest is (re)written so `dvc checkout`
      can find the directory — idempotent, local-only, and the exact repair
      fast-sync itself performs after fetching (slice 27).
    - md5-keyed dir-outs: the ``.dir`` manifest must be in cache AND every
      entry it lists must be cached.
    - single-file outs: the pinned blob must be in cache.
    """
    if out.is_import:
        return False
    if out.is_files_format:
        if out.files is None:
            return False
        if not is_dir_fully_cached(cache_dir, out.files):
            return False
        ensure_dir_manifest(cache_dir, out.files)
        return True
    if not out.md5:
        return False
    if out.is_dir:
        entries = read_cached_dir_manifest(cache_dir, out.md5)
        return entries is not None and is_dir_fully_cached(cache_dir, entries)
    return is_cached(cache_dir, out.md5)


def cached_targets(
    project_path: Path,
    candidates: list[str],
    remote_name: str,
    pipeline_outs: "list[DvcOut] | None" = None,
) -> list[str]:
    """Subset of ``candidates`` whose pinned blobs are ALL in the local cache.

    Both degraded branches of ``data_pull`` previously skipped `dvc checkout`
    entirely, dropping data fast-sync had already downloaded into the cache.
    This probe is filesystem-only (parse the `.dvc` / reuse the
    already-parsed ``dvc.lock`` out, stat cache blobs) — no S3, no
    subprocess. Order is preserved; unparseable / missing / hash-missing
    targets are simply not cached.
    """
    cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
    pipeline_by_target = {o.target: o for o in pipeline_outs or []}
    cached: list[str] = []
    for target in candidates:
        outs = resolve_target_outs(
            project_path, target, remote_name, pipeline_by_target,
        )
        if outs and all(ensure_out_cached(cache_dir, o) for o in outs):
            cached.append(target)
    return cached


def spot_check_versions(
    s3: Any, bucket: str, prefix: str, outs: list[DvcOut], project_path: Path, n: int = _SPOT_CHECK_N
) -> list[tuple[str, str]]:
    """Randomly sample up to ``n`` outs and HEAD them to detect version_id drift.

    Returns ``[(target, why), ...]`` for outs with VERIFIED drift only: an
    HTTP 404 / NoSuchVersion on the pinned version, or a VersionId mismatch
    in a successful response. The orchestrator demotes ONLY those targets
    (previously any transient ``ClientError`` here demoted all targets under
    the misleading reason "version_id spot-check drift").

    Each HEAD is retried via :func:`retry_transient`. A probe that still
    fails after retries is INCONCLUSIVE — logged and skipped, never drift
    (the md5 verify after download stays the safety net).
    ``NoCredentialsError`` propagates so the orchestrator can name it as a
    non-retried degradation reason. Outs without a ``version_id``
    (md5-keyed, content-addressed) are skipped as before.

    Slice 27: uses ``s3_key_for_out`` so path-based (version_aware) outs
    are HEAD'd at their real file path, not at ``files/md5/...``.
    """
    drift: list[tuple[str, str]] = []
    to_check = random.sample(outs, min(n, len(outs)))
    for out in to_check:
        if not out.version_id:
            continue
        try:
            key = s3_key_for_out(prefix, out, project_path)
        except ValueError as exc:
            # Per-out misuse (synthetic out without dvc_file) — demote just
            # this out; the rest of the batch is unaffected.
            drift.append((out.target, f"cannot build S3 key: {exc}"))
            continue
        def _head(key: str = key, vid: str | None = out.version_id) -> Any:
            # Defaults bind the loop variables at definition time.
            return s3.head_object(Bucket=bucket, Key=key, VersionId=vid)

        try:
            resp = retry_transient(_head)
        except NoCredentialsError:
            raise
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in _DRIFT_404_CODES or status == 404:
                drift.append((out.target, f"HEAD 404 for pinned version {out.version_id} of {key}"))
            else:
                logger.warning(
                    "fast-sync: spot-check inconclusive for %r (%s); not treating as drift",
                    out.target, exc,
                )
            continue
        except Exception as exc:
            logger.warning(
                "fast-sync: spot-check inconclusive for %r after retries (%s); not treating as drift",
                out.target, exc,
            )
            continue
        if resp.get("VersionId") != out.version_id:
            drift.append((
                out.target,
                f"version drift: pinned {out.version_id}, remote returned {resp.get('VersionId')}",
            ))
    return drift










def verify_download(cache_path: Path, expected_md5: str) -> VerifyResult:
    """Stream-verify a downloaded file's md5 against the expected hash.

    Streams in 1 MiB chunks so we don't OOM on multi-GB parquet files. On
    mismatch or read error, the partial download is unlinked.
    """
    try:
        h = hashlib.md5(usedforsecurity=False)
        with open(cache_path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        actual = h.hexdigest()
        ok = (actual == expected_md5)
        if not ok:
            cache_path.unlink(missing_ok=True)
        return VerifyResult(ok=ok, expected=expected_md5, actual=actual)
    except Exception:
        cache_path.unlink(missing_ok=True)
        return VerifyResult(ok=False, expected=expected_md5, actual="")


def ensure_dir_manifest(cache_dir: Path, entries: list[DvcFileEntry]) -> str:
    """Write a synthetic .dir manifest into the local cache.

    Byte-exact match against real DVC's output: sorted by relpath,
    ``[{"md5": ..., "relpath": ...}]`` only (no size or other fields),
    ``json.dumps(..., sort_keys=True)`` with otherwise-default kwargs
    (``separators=(", ", ": ")``, ``ensure_ascii=True``, no trailing
    newline). ``sort_keys=True`` is a defensive measure — Python preserves
    dict insertion order so the bytes happen to match without it, but a
    future refactor that adds a field or reorders keys would silently
    diverge.

    **Cache filename convention:** the manifest lives at
    ``.dvc/cache/files/md5/XX/YYYY.dir`` (with the literal ``.dir`` suffix
    on the filename). This matches DVC's own layout
    (``dvc_data.hashfile.db.local.LocalHashFileDB.oid_to_path``); any
    deviation breaks `dvc checkout`'s cache lookup. Returns ``{md5}.dir``.
    """
    serialized = _dir_manifest_bytes(entries)
    full_md5 = dir_manifest_md5(entries)
    manifest_path = cache_path_for(cache_dir, full_md5)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_bytes(serialized)
    _try_fsync_file(tmp_path)
    tmp_path.replace(manifest_path)
    _try_fsync_parent_dir(manifest_path)
    return full_md5


def _writable(path: Path) -> Path:
    """``path``, made writable when it's there and ours to change: dvc leaves
    its cache objects read-only, and Windows won't replace or delete a
    read-only file. Anything else (a shared cache's file owned by someone
    else) is left to the replace or delete, as before."""
    try:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
    return path


def fetch_to_cache(
    s3: Any, bucket: str, key: str, cache_path: Path, expected_md5: str,
    version_id: str | None = None,
    *,
    progress: Callable[[int], None] | None = None,
    check: Callable[[Path], None] | None = None,
) -> bool:
    """Download an object to the DVC cache atomically.

    Sequence: download to ``cache_path.tmp`` → md5 verify → fsync the tmp file →
    rename to ``cache_path`` → fsync the parent directory. Parent-dir fsync is
    required for durability across crashes (POSIX rename atomicity does not
    guarantee the directory entry itself is durable).

    ``version_id`` is passed via ``ExtraArgs={"VersionId": ...}`` — boto3's
    ``download_file`` does not accept ``VersionId`` as a top-level kwarg.

    ``progress``, if provided, is passed as boto3's ``Callback=`` — invoked per
    transferred chunk with ``bytes_delta: int`` (default 8 MB chunks; may be
    smaller on the final chunk or for individual multipart parts). MUST be
    thread-safe — boto3's transfer manager runs multipart parts on its own
    internal pool.

    ``check``, if provided, reads the verified tmp file before the rename; what
    it raises propagates the same way, so an object it refuses never lands.

    Retry: ``_s3.download`` retries transient S3/network errors (3 attempts,
    capped backoff), a 5xx botocore mislabels among them. Non-transient errors
    (incl. md5 mismatch) propagate immediately, and the tmp file is unlinked.
    """
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    def _attempt() -> bool:
        try:
            download(s3, bucket, key, tmp_path, version_id=version_id, progress=progress)

            vr = verify_download(tmp_path, expected_md5)
            if not vr.ok:
                raise ValueError(f"md5 mismatch: expected {vr.expected}, got {vr.actual}")
            if check is not None:
                check(tmp_path)

            # fsync the tmp file's data, rename, then fsync the parent dir.
            # Both fsyncs are best-effort (no-op on platforms that reject
            # them, e.g. Windows) — the rename is what makes the write
            # visible; fsync only hardens durability.
            _try_fsync_file(tmp_path)
            tmp_path.replace(_writable(cache_path))
            _try_fsync_parent_dir(cache_path)
            return True
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return _attempt()


def fetch_dir_manifest(
    s3: Any, bucket: str, prefix: str, dir_md5: str, cache_dir: Path
) -> list[DvcFileEntry] | None:
    """Fetch a .dir manifest from S3 to local cache; return parsed entries.

    DVC stores manifests at ``prefix/files/md5/XX/YYYY.dir`` on S3 and
    ``.dvc/cache/files/md5/XX/YYYY.dir`` locally — the ``.dir`` suffix
    survives in the filename. ``raw_md5`` (without the suffix) is used
    only for the md5 verify, since that's the actual md5 of the manifest
    bytes; the cache/S3 paths use ``full_md5``.
    """
    full_md5 = dir_md5 if dir_md5.endswith(".dir") else f"{dir_md5}.dir"
    raw_md5 = full_md5[:-4]
    key = s3_key_for(prefix, full_md5)
    cache_path = cache_path_for(cache_dir, full_md5)
    entries: list[DvcFileEntry] = []

    def _read(tmp: Path) -> None:
        # read before it lands in the cache, so a list that isn't one, or that
        # records a hash dvc never writes, never does
        payload = json.loads(tmp.read_bytes())
        if not isinstance(payload, list):
            raise ValueError("the folder list is not a list")
        entries.extend(_listed([
            DvcFileEntry(
                md5=str(e.get("md5", "")),
                relpath=str(e.get("relpath", "")),
                size=int(e.get("size", 0) or 0),
                version_id=None,
            )
            for e in payload
        ], f"the folder list {full_md5} from storage"))

    try:
        fetch_to_cache(s3, bucket, key, cache_path, raw_md5, version_id=None, check=_read)
    except PointerCutShort:
        raise
    except Exception as exc:
        logger.warning("fast-sync: dir manifest fetch failed for %s: %s", full_md5, exc)
        _writable(cache_path).unlink(missing_ok=True)
        return None
    return entries



def fetch_dir_contents(
    s3: Any, bucket: str, prefix: str, entries: list[DvcFileEntry],
    cache_dir: Path, jobs: int,
    *,
    progress: Callable[[int], None] | None = None,
) -> list[str]:
    """Concurrent download of md5-keyed-dir constituents.

    Deduplicates entries by md5 before submitting to the thread pool:
    ``fetch_to_cache`` uses a static ``.tmp`` sibling per cache_path,
    so two threads downloading entries with the same md5 would race
    on the same temp file. Any two entries with the same md5 yield the
    same bytes from S3, so one download serves every duplicate relpath.
    First-occurrence wins for test determinism.

    ``progress``, if provided, fires (a) per-chunk via boto3's Callback during
    each entry's download (from boto3's transfer threads) and (b) once per
    cache-hit with ``entry.size``. MUST be thread-safe — fetches run in a
    ThreadPoolExecutor.
    """
    if not entries:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in entries:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    # Slice 28: progress total = sum(out.size) which counts duplicate-content
    # entries by all their relpaths. Fire advance for each duplicate that
    # dedup dropped so the bar reaches 100% on dirs with repeated content.
    if progress is not None:
        for e in entries:
            if e.md5 in unique and unique[e.md5] is not e:
                try:
                    progress(e.size)
                except Exception:
                    pass

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            if progress is not None:
                try:
                    progress(entry.size)
                except Exception:
                    pass
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                s3_key_for(prefix, entry.md5),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                None,
                progress=progress,
            )
            return (entry.relpath, None)
        except PointerCutShort:
            raise  # a hash that would name a path outside the cache: the user's to fix
        except Exception as exc:
            return (entry.relpath, f"{entry.relpath}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = [ex.submit(_one, e) for e in unique_entries]
        for fut in as_completed(futures):
            _, err = fut.result()
            if err:
                failures.append(err)
    return failures


def fetch_files_dir_contents(
    s3: Any, bucket: str, prefix: str, out: DvcOut,
    cache_dir: Path, jobs: int, remote_name: str,
    *,
    project_path: Path,
    progress: Callable[[int], None] | None = None,
) -> list[str]:
    """Concurrent download of files-format dir constituents.

    Like ``fetch_dir_contents`` but with real-path S3 keys and per-file
    version_ids. md5 dedup applies for the same .tmp-race reason. The
    *manifest* (written by ``ensure_dir_manifest``) receives the full
    un-deduped list so ``dvc checkout`` can materialize every relpath.

    Slice 27: branches per ``entry.is_path_based``. The non-path-based
    branch preserves pre-slice-27 behavior: ``<prefix>/<out.path>/<relpath>``.
    The path-based branch reconstructs ``<prefix>/<dvc_file.parent_rel>/<out.path>/<relpath>``
    so files-format dirs in nested .dvc files (e.g. ``data/raw/x.dvc``)
    fetch from the correct S3 location. ``project_path`` is required to
    compute ``dvc_file.parent_rel`` for path-based entries.

    Slice 28: ``progress``, if provided, fires (a) per-chunk via boto3's
    Callback during each entry's download and (b) once per cache-hit with
    ``entry.size``. MUST be thread-safe (ThreadPoolExecutor + boto3 transfer
    threads).
    """
    del remote_name
    if not out.files:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in out.files:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    # Slice 28: see fetch_dir_contents — fire advance for dedup-dropped
    # entries so the bar reaches 100% when out.files has duplicate md5s.
    if progress is not None:
        for e in out.files:
            if e.md5 in unique and unique[e.md5] is not e:
                try:
                    progress(e.size)
                except Exception:
                    pass

    def _key(entry: DvcFileEntry) -> str:
        if entry.is_path_based:
            if out.dvc_file is None:
                raise ValueError(
                    f"path-based DvcFileEntry under {out.path!r} missing parent dvc_file"
                )
            rel_dir = out.dvc_file.parent.relative_to(project_path)
            base = out.path if rel_dir == Path(".") else f"{rel_dir.as_posix()}/{out.path}"
            parts = [p for p in (prefix, base, entry.relpath) if p]
        else:
            # Preserve pre-slice-27 behaviour for non-path-based files-format
            # entries: prefix/out.path/<entry.relpath>. Existing tests at
            # tests/test_fast_sync.py:564, :767 pin this shape.
            parts = [p for p in (prefix, out.path, entry.relpath) if p]
        return "/".join(parts)

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            if progress is not None:
                try:
                    progress(entry.size)
                except Exception:
                    pass
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                _key(entry),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                entry.version_id,
                progress=progress,
            )
            return (entry.relpath, None)
        except PointerCutShort:
            raise  # a hash that would name a path outside the cache: the user's to fix
        except Exception as exc:
            return (entry.relpath, f"{entry.relpath}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = [ex.submit(_one, e) for e in unique_entries]
        for fut in as_completed(futures):
            _, err = fut.result()
            if err:
                failures.append(err)
    return failures



class _UnsyncableOut(Exception):
    """Raised by :func:`_fetch_out` when the out cannot be served for a
    named, expected reason (vs. an unexpected error, which propagates and
    is logged by the caller before routing)."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _fetch_out(
    s3: Any,
    bucket: str,
    prefix: str,
    out: DvcOut,
    cache_dir: Path,
    jobs: int,
    remote_name: str,
    project_path: Path,
    advance: Callable[[int], None],
) -> list[str]:
    """Fetch one out's blobs into the local cache; return per-file failures.

    Pure mechanics — no routing decisions. Three out shapes:

    - files-format dirs: fetch constituents; on full success (re)write the
      synthetic ``.dir`` manifest so `dvc checkout` can find the directory.
    - md5-keyed dirs: reuse a cached ``.dir`` manifest when present, else
      fetch it (raises ``_UnsyncableOut`` when the remote doesn't have it);
      then fetch any uncached constituents. When everything was already
      cached, fire the aggregate ``advance`` so the progress bar still
      moves on warm-cache re-runs.
    - single files: fetch unless already cached (aggregate ``advance``).

    Returns the (possibly empty) list of per-file failures that survived
    the transient retries; the caller routes those. Out-level failures
    raise: ``_UnsyncableOut`` for the named manifest-unavailable case,
    anything else propagates as-is.
    """
    if out.is_files_format:
        assert out.files is not None
        failures = fetch_files_dir_contents(
            s3, bucket, prefix, out, cache_dir, jobs, remote_name,
            project_path=project_path,
            progress=advance,
        )
        if failures:
            return failures
        ensure_dir_manifest(cache_dir, out.files)
        return []
    if out.is_dir:
        entries = read_cached_dir_manifest(cache_dir, out.md5)
        if entries is None:
            entries = fetch_dir_manifest(s3, bucket, prefix, out.md5, cache_dir)
            if entries is None:
                raise _UnsyncableOut("dir manifest unavailable on remote")
        if not is_dir_fully_cached(cache_dir, entries):
            return fetch_dir_contents(
                s3, bucket, prefix, entries, cache_dir, jobs,
                progress=advance,
            )
        advance(out.size)
        return []
    if is_cached(cache_dir, out.md5):
        advance(out.size)
    else:
        fetch_to_cache(
            s3, bucket,
            s3_key_for_out(prefix, out, project_path),
            cache_path_for(cache_dir, out.md5),
            out.md5,
            out.version_id,
            progress=advance,
        )
    return []


def _build_fast_pull_result(
    *,
    synced: int,
    fallback: list[str],
    incomplete_targets: list[str],
    blocked_targets: list[str],
    blocked_reasons: dict[str, str],
    drift_notes: list[str],
    files_dir_failures: list[str],
) -> FastPullResult:
    """Assemble try_fast_pull's result: dedupe the buckets (first occurrence
    wins) and compose the human-readable ``reason`` summary."""
    unique_incomplete = list(dict.fromkeys(incomplete_targets))
    unique_blocked = list(dict.fromkeys(blocked_targets))
    reason_bits: list[str] = []
    if fallback:
        reason_bits.append("partial fallback")
    if drift_notes:
        reason_bits.append("version_id spot-check drift: " + "; ".join(drift_notes))
    if unique_incomplete:
        reason_bits.append(
            "per-file download failures (not demoted to dvc pull): "
            + ", ".join(unique_incomplete)
        )
    if unique_blocked:
        reason_bits.append(
            "version-aware target(s) failed (never routed to dvc pull): "
            + ", ".join(unique_blocked)
        )
    return FastPullResult(
        success=not fallback and not unique_incomplete and not unique_blocked,
        synced_count=synced,
        fallback_targets=fallback,
        incomplete_targets=unique_incomplete,
        reason="; ".join(reason_bits),
        files_dir_failures=files_dir_failures,
        blocked_targets=unique_blocked,
        blocked_reasons=blocked_reasons,
    )




class FastSyncOps(Protocol):
    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
        reporter: Optional["Reporter"] = None,
    ) -> FastPullResult: ...


class SubprocessFastSyncOps:
    def __init__(
        self,
        *,
        jobs: int = 8,
        progress: Callable[[int], None] | None = None,
        aws_profile_name: str | None = None,
    ) -> None:
        # Progress callback: ``Callable[[int], None]`` invoked with bytes_delta.
        # Fires per-chunk during S3 transfers (via boto3's ``Callback=`` on
        # ``download_file``) AND once per cache-hit / dir-fully-cached path with
        # the out's / entry's declared size. See ``set_progress`` for the
        # thread-safety + retry contract.
        self._default_jobs = jobs
        self._progress = progress
        self._aws_profile_name = aws_profile_name

    def set_progress(self, progress: Callable[[int], None] | None) -> None:
        """Install an ``advance(n_bytes)`` callback for subsequent
        ``try_fast_pull`` invocations. Pass ``None`` to disable. Idempotent.

        The callback fires from boto3's per-chunk ``Callback`` during S3
        transfers (default 8 MB chunks; may be smaller for the final chunk
        or for individual multipart parts) AND once per cache-hit with the
        fully-cached out's / entry's declared size, AND once per
        dir-fully-cached short-circuit with the dir's aggregate size.

        Thread-safety: ``fetch_{dir,files_dir}_contents`` submit per-entry
        work to a ``ThreadPoolExecutor``, and boto3's transfer manager runs
        multipart parts on its own internal pool. The callback therefore
        runs on worker threads. ``rich.Progress.update`` is thread-safe and
        satisfies this contract; custom callables must be thread-safe too.

        On retry: ``fetch_to_cache`` makes up to 3 attempts on transient S3
        errors. Bytes from a failed attempt are NOT rolled back from the
        advance total; the bar may visually lap past 100% within a single
        out across retries. This is intentional — the bar reflects
        bytes-on-the-wire, not bytes-committed.
        """
        self._progress = progress

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
        reporter: Optional["Reporter"] = None,
    ) -> FastPullResult:
        def _degrade_all(reason: str) -> FastPullResult:
            """Curry this call's constants into :func:`_degrade_all_targets`
            (the all-or-nothing guard degradation, split per
            :func:`dvc_pull_can_serve`)."""
            return _degrade_all_targets(
                project_path, targets, remote_name, pipeline_outs, reason,
            )

        ok, reason = _check_dvc()
        if not ok:
            return _degrade_all(reason or "dvc version mismatch")

        try:
            remote_cfg = get_remote_config(project_path, remote_name)
        except (FileNotFoundError, KeyError) as exc:
            return _degrade_all(f"remote config not found: {exc}")

        try:
            bucket, prefix = parse_s3_url(remote_cfg.get("url", ""))
        except ValueError as exc:
            return _degrade_all(f"non-S3 remote: {exc}")

        if not available():
            return _degrade_all("boto3 not importable")

        all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)
        if pipeline_outs:
            all_outs.extend(pipeline_outs)
        fallback.extend(hash_missing)
        if not all_outs:
            # Nothing on this project's remote to fetch (all imports, say), so no
            # client: its profile can't stop a pull that never reads that bucket.
            return _build_fast_pull_result(
                synced=0, fallback=fallback, incomplete_targets=[], blocked_targets=[],
                blocked_reasons={}, drift_notes=[], files_dir_failures=[],
            )

        s3 = _create_s3_client(remote_cfg, self._aws_profile_name, pool=jobs)
        try:
            versioning_enabled = check_bucket_versioning(s3, bucket)
        except NoCredentialsError as exc:
            # Named, non-retried degradation: retrying cannot mint credentials.
            return _degrade_all(f"AWS credentials unavailable (not retried): {exc}")
        except Exception as exc:
            # Probe failed even after transient retries — say so, instead of
            # the old (misleading) "bucket versioning disabled".
            return _degrade_all(f"bucket versioning probe failed: {exc}")
        if not versioning_enabled:
            return _degrade_all("bucket versioning disabled")

        # Spot-check drift demotes ONLY the affected outs (named in the
        # reason), never the whole batch. Probe errors are retried inside
        # spot_check_versions; still-failing probes are inconclusive and
        # demote nothing. A drifted out is by definition version-aware
        # (only outs with a version_id are probed), so it becomes a loud
        # error — never a plain `dvc pull`.
        try:
            drift = spot_check_versions(s3, bucket, prefix, all_outs, project_path)
        except NoCredentialsError as exc:
            return _degrade_all(f"AWS credentials unavailable (not retried): {exc}")
        blocked_targets: list[str] = []
        blocked_reasons: dict[str, str] = {}
        drift_notes: list[str] = []
        if drift:
            drifted_targets = list(dict.fromkeys(t for t, _ in drift))
            drift_notes = [f"{t} ({why})" for t, why in drift]
            for t, why in drift:
                logger.warning("fast-sync: spot-check drift for %r: %s", t, why)
                blocked_reasons.setdefault(t, f"version_id spot-check drift: {why}")
            blocked_targets.extend(drifted_targets)
            drifted_set = set(drifted_targets)
            all_outs = [o for o in all_outs if o.target not in drifted_set]

        cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
        synced = 0
        files_dir_failures: list[str] = []
        incomplete_targets: list[str] = []

        def _record_per_file_failures(out: DvcOut, failures: list[str]) -> None:
            # A per-file failure that survives the transient retries fails
            # THAT FILE by name. Same split as _route_out, except the
            # unservable bucket differs: an out plain `dvc pull` can restore
            # (md5-keyed) keeps the fallback route, while a version-aware
            # out lands in incomplete_targets (its cache blobs are partial)
            # so the caller skips both its checkout and any fallback pull,
            # reports it loudly, and exits non-zero.
            files_dir_failures.extend(f"{out.target}: {f}" for f in failures)
            if dvc_pull_can_serve(out):
                fallback.append(out.target)
                logger.warning(
                    "fast-sync: %d file(s) of %r failed after retries; "
                    "md5-keyed out, demoting to the dvc pull fallback",
                    len(failures), out.target,
                )
                return
            incomplete_targets.append(out.target)
            logger.warning(
                "fast-sync: %d file(s) of %r failed after retries; NOT demoting to dvc pull",
                len(failures), out.target,
            )
            if reporter is not None:
                reporter.warn(
                    f"fast-sync: {len(failures)} file(s) of {out.target} failed to download; "
                    f"retry with: mintd data pull {out.target}"
                )

        def _advance(n: int) -> None:
            # Slice 28: piecewise progress accounting. Fires from boto3's
            # Callback during transfers (per-chunk), from cache-hit branches
            # below, and from the dir-fully-cached short-circuit. Bound to
            # ``self._progress`` at call time so set_progress(None) mid-loop
            # still disables it. Bare except keeps UI bugs from aborting pulls.
            if self._progress is not None:
                try:
                    self._progress(n)
                except Exception:
                    pass

        # The loop body makes routing decisions only; fetch mechanics live
        # in _fetch_out. Per-file failures route via _record_per_file_failures
        # (fallback vs incomplete); out-level failures route via _route_out
        # (fallback vs blocked).
        for i, out in enumerate(all_outs, start=1):
            if reporter is not None:
                target = out.target if len(out.target) <= 50 else out.target[:47] + "..."
                reporter.update_progress_desc(f"Pulling {target} ({i}/{len(all_outs)})...")
            try:
                failures = _fetch_out(
                    s3, bucket, prefix, out, cache_dir, jobs, remote_name,
                    project_path, _advance,
                )
                if failures:
                    _record_per_file_failures(out, failures)
                    continue
                synced += 1
            except PointerCutShort:
                raise  # a hash that would name a path outside the cache: the user's to fix, not dvc pull's
            except _UnsyncableOut as exc:
                _route_out(
                    out, exc.why,
                    fallback=fallback,
                    blocked_targets=blocked_targets,
                    blocked_reasons=blocked_reasons,
                )
            except Exception as exc:
                logger.warning("fast-sync target %r failed: %s", out.target, exc)
                _route_out(
                    out, str(exc),
                    fallback=fallback,
                    blocked_targets=blocked_targets,
                    blocked_reasons=blocked_reasons,
                )

        return _build_fast_pull_result(
            synced=synced,
            fallback=fallback,
            incomplete_targets=incomplete_targets,
            blocked_targets=blocked_targets,
            blocked_reasons=blocked_reasons,
            drift_notes=drift_notes,
            files_dir_failures=files_dir_failures,
        )