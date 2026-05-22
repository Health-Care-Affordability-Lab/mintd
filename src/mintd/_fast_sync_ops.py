"""DVC FastSync implementation.

Coupled to DVC cache layout (3.66.x).
"""

import configparser
import dataclasses
import hashlib
import json
import logging
import os
import random
import subprocess
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

try:
    import boto3
    from botocore.exceptions import ClientError, ProfileNotFound
except ImportError:
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment,misc]
    ProfileNotFound = Exception  # type: ignore[assignment,misc]

from mintd._atomic import _try_fsync_parent_dir
from mintd.model import FastPullResult

logger = logging.getLogger(__name__)

_EXPECTED_DVC_MINOR = "3.66"
_RETRYABLE_S3_ERRORS = {"503", "500", "RequestTimeout", "SlowDown"}
_SPOT_CHECK_N = 5
_DEFAULT_DVC_CACHE_REL = Path(".dvc/cache")


def _dvc_version_ok() -> bool:
    try:
        result = subprocess.run(
            ["dvc", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    version = result.stdout.strip()
    parts = version.split(".")
    if len(parts) < 2:
        return False
    return ".".join(parts[:2]) == _EXPECTED_DVC_MINOR


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


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    expected: str
    actual: str


def _push_all_to_fallback(targets: list[str], reason: str) -> FastPullResult:
    return FastPullResult(
        success=False,
        fallback_targets=targets,
        reason=reason,
    )


def _is_retryable_s3_error(exc: Exception) -> bool:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return str(code) in _RETRYABLE_S3_ERRORS
    return False


def _extract_version_id(out_dict: dict[str, Any]) -> str | None:
    if "version_id" in out_dict:
        return str(out_dict["version_id"])
    if "cloud" in out_dict:
        for cloud_info in out_dict["cloud"].values():
            if "version_id" in cloud_info:
                return str(cloud_info["version_id"])
    return None


def _extract_version_id_from_file_entry(entry: dict[str, Any], remote_name: str) -> str | None:
    cloud = entry.get("cloud")
    if not isinstance(cloud, dict):
        return None
    remote_block = cloud.get(remote_name)
    if isinstance(remote_block, dict) and "version_id" in remote_block:
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


def parse_dvc_outs(dvc_path: Path, remote_name: str) -> list[DvcOut]:
    """Parse a .dvc file into DvcOut entries.

    DVC marks directories by suffixing the md5 hash with ``.dir`` — that's
    the only signal; there is no separate ``is_dir`` YAML field. Files-format
    directories use an inline ``files:`` list with no top-level md5.
    Entries with no md5 AND no files list are hash-missing — returned as
    empty so the caller routes them to fallback.
    """
    try:
        with open(dvc_path) as f:
            data = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError):
        return []

    outs = []
    if not isinstance(data, dict) or "outs" not in data:
        return []
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


def parse_dvc_lock_outs(project_path: Path, remote_name: str) -> list[DvcOut]:
    """Parse ``project_path/dvc.lock`` into ``DvcOut`` entries for pipeline-
    stage outputs (no per-output ``.dvc`` pointer files). Slice 37 — gives
    fast-sync a way to enumerate pipeline products so they bypass DVC's
    rehash-on-pull cost on version_aware remotes.

    Reads ``dvc.yaml`` (optional) to build the ``stage → wdir`` map so
    lock-relative ``out.path`` resolves correctly to project-relative.
    When ``dvc.yaml`` is missing, every stage's ``wdir`` defaults to ``.``.

    Returns ``[]`` when ``dvc.lock`` is missing or malformed.
    """
    yaml_path = project_path / "dvc.yaml"
    lock_path = project_path / "dvc.lock"

    wdir_map: dict[str, str] = {}
    try:
        if yaml_path.exists():
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "stages" in data:
                    for stage, stage_data in data["stages"].items():
                        wdir = stage_data.get("wdir", ".")
                        if Path(wdir).is_absolute():
                            logger.warning(
                                "absolute wdir in dvc.yaml stage %s: %s; skipping stage",
                                stage, wdir,
                            )
                            wdir_map[stage] = "SKIP"
                        else:
                            wdir_map[stage] = wdir
    except (FileNotFoundError, yaml.YAMLError, OSError):
        pass

    try:
        with open(lock_path) as f:
            lock_data = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError, OSError):
        return []

    if not isinstance(lock_data, dict) or "stages" not in lock_data:
        return []

    outs: list[DvcOut] = []
    for stage, stage_data in lock_data["stages"].items():
        if wdir_map.get(stage) == "SKIP":
            continue
        if "outs" not in stage_data:
            continue
        wdir = wdir_map.get(stage, ".")
        for out in stage_data["outs"]:
            has_md5 = bool(out.get("md5"))
            has_files = "files" in out
            if not has_md5 and not has_files:
                continue
            try:
                raw_path = out["path"]
                abs_path = (project_path / wdir / raw_path).resolve()
                rel = abs_path.relative_to(project_path.resolve()).as_posix()
            except (ValueError, KeyError):
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
            ))
    return outs


def discover_pipeline_outs(project_path: Path, remote_name: str) -> list[DvcOut]:
    """Pipeline outs from ``dvc.lock`` that fast-sync can handle.

    Filters ``parse_dvc_lock_outs`` to entries whose top-level
    ``cloud.<remote>`` block carries a ``version_id``. Outs without one
    (never pushed, or pushed under a different remote name) route to
    ``dvc pull`` instead.
    """
    return [
        out
        for out in parse_dvc_lock_outs(project_path, remote_name)
        if out.version_id
    ]


def discover_all_outs(project_path: Path) -> list[str]:
    """Walk project_path recursively for ``*.dvc`` files; return paths
    relative to project_path, sorted lexicographically.

    Excludes the ``.dvc/`` internals directory (DVC metadata, not data
    pointers) and ``dvc.lock`` (pipeline lockfile). Mirrors v1's
    ``discover_dvc_targets`` (mintd/utils/dvc_guards.py) for the .dvc-file
    half; pipeline-stage names from ``dvc.yaml`` are NOT included because
    fast-sync's ``classify_targets`` cannot consume them — such targets
    fall through to ``dvc pull`` as the normal fallback path.

    Used by ``data_pull`` when ``targets is None`` to route through
    fast-sync (boto3 → cache) instead of straight to ``dvc pull``, which
    hits a cache-write bug in DVC 3.66.1 on version_aware buckets.
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_path, followlinks=False):
        # Prune .dvc/ internals in-place so os.walk doesn't descend.
        if ".dvc" in dirnames:
            dirnames.remove(".dvc")
        for name in filenames:
            if not name.endswith(".dvc") or name == "dvc.lock":
                continue
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(project_path)
            results.append(rel.as_posix())
    return sorted(results)


def cache_path_for(cache_dir: Path, md5: str) -> Path:
    if not md5 or len(md5) < 3:
        return cache_dir / "files" / "md5" / md5
    return cache_dir / "files" / "md5" / md5[:2] / md5[2:]


def is_cached(cache_dir: Path, md5: str) -> bool:
    if not md5:
        return False
    return cache_path_for(cache_dir, md5).exists()


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
        dvc_path = project_path / target if target.endswith(".dvc") else project_path / f"{target}.dvc"
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


def get_remote_config(project_path: Path, remote_name: str) -> dict[str, str]:
    """Read a DVC remote section from .dvc/config.

    DVC has shipped two formats for remote sections in its config files:
    the older quoted form ``['remote "origin"']`` (literal single quotes
    in the section header) and the unquoted form ``[remote "origin"]`` /
    ``[remote origin]``. Probe all three so the slice works across DVC
    versions.
    """
    config_path = project_path / ".dvc" / "config"
    if not config_path.exists():
        raise FileNotFoundError(f"no .dvc/config at {config_path}")

    cp = configparser.ConfigParser()
    cp.read(config_path)

    candidates = (
        f"'remote \"{remote_name}\"'",
        f'remote "{remote_name}"',
        f"remote {remote_name}",
    )
    for section in candidates:
        if cp.has_section(section):
            return dict(cp[section])

    raise KeyError(f"remote {remote_name} not found in {config_path}")


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


def check_bucket_versioning(s3: Any, bucket: str) -> bool:
    try:
        resp = s3.get_bucket_versioning(Bucket=bucket)
        return resp.get("Status") == "Enabled"
    except ClientError:
        return False


def spot_check_versions(s3: Any, bucket: str, prefix: str, outs: list[DvcOut], project_path: Path, n: int = _SPOT_CHECK_N) -> bool:
    """Randomly sample up to ``n`` outs and HEAD them to detect version_id drift.

    Returns False on the first mismatch or NoSuchKey. Outs without a
    ``version_id`` (md5-keyed, content-addressed) are skipped — there's
    nothing to check, the md5 verify post-download is the safety net.

    Slice 27: uses ``s3_key_for_out`` so path-based (version_aware) outs
    are HEAD'd at their real file path, not at ``files/md5/...``.
    """
    to_check = random.sample(outs, min(n, len(outs)))
    for out in to_check:
        if not out.version_id:
            continue
        try:
            key = s3_key_for_out(prefix, out, project_path)
        except ValueError:
            return False
        try:
            resp = s3.head_object(Bucket=bucket, Key=key, VersionId=out.version_id)
            if resp.get("VersionId") != out.version_id:
                return False
        except ClientError:
            return False
    return True


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


def is_dir_fully_cached(cache_dir: Path, entries: list[DvcFileEntry]) -> bool:
    return all(is_cached(cache_dir, e.md5) for e in entries)


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
    sorted_entries = sorted(entries, key=lambda e: e.relpath)
    payload = [{"md5": e.md5, "relpath": e.relpath} for e in sorted_entries]
    serialized = json.dumps(payload, sort_keys=True).encode()
    manifest_md5 = hashlib.md5(serialized, usedforsecurity=False).hexdigest()
    full_md5 = f"{manifest_md5}.dir"
    manifest_path = cache_path_for(cache_dir, full_md5)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_bytes(serialized)
    fd = os.open(str(tmp_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp_path.replace(manifest_path)
    _try_fsync_parent_dir(manifest_path)
    return full_md5


def fetch_to_cache(
    s3: Any, bucket: str, key: str, cache_path: Path, expected_md5: str,
    version_id: str | None = None,
    *,
    progress: Callable[[int], None] | None = None,
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
    """
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    extra_args: dict[str, str] | None = (
        {"VersionId": version_id} if version_id else None
    )

    for attempt in range(4):
        try:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
            if extra_args is not None:
                kwargs["ExtraArgs"] = extra_args
            if progress is not None:
                kwargs["Callback"] = progress
            s3.download_file(Filename=str(tmp_path), **kwargs)

            vr = verify_download(tmp_path, expected_md5)
            if not vr.ok:
                raise ValueError(f"md5 mismatch: expected {vr.expected}, got {vr.actual}")

            # fsync the tmp file's data, rename, then fsync the parent dir.
            fd = os.open(str(tmp_path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            tmp_path.replace(cache_path)
            _try_fsync_parent_dir(cache_path)
            return True
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            if _is_retryable_s3_error(e) and attempt < 3:
                time.sleep(0.5 * (2**attempt))
                continue
            raise
    return False


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
    try:
        fetch_to_cache(s3, bucket, key, cache_path, raw_md5, version_id=None)
    except Exception as exc:
        logger.warning("fast-sync: dir manifest fetch failed for %s: %s", full_md5, exc)
        cache_path.unlink(missing_ok=True)
        return None
    try:
        payload = json.loads(cache_path.read_bytes())
    except Exception as exc:
        logger.warning("fast-sync: dir manifest JSON parse failed for %s: %s", full_md5, exc)
        cache_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, list):
        cache_path.unlink(missing_ok=True)
        return None
    return [
        DvcFileEntry(
            md5=str(e.get("md5", "")),
            relpath=str(e.get("relpath", "")),
            size=int(e.get("size", 0) or 0),
            version_id=None,
        )
        for e in payload
    ]



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
        except Exception as exc:
            return (entry.relpath, f"{entry.relpath}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = [ex.submit(_one, e) for e in unique_entries]
        for fut in as_completed(futures):
            _, err = fut.result()
            if err:
                failures.append(err)
    return failures



def _create_s3_client(remote_cfg: dict[str, str], aws_profile_name: str | None) -> Any:
    try:
        session = boto3.Session(profile_name=aws_profile_name)
    except ProfileNotFound:
        session = boto3.Session()

    endpoint_url = remote_cfg.get("endpointurl")
    if not endpoint_url:
        endpoint_url = remote_cfg.get("endpoint")
    return session.client("s3", endpoint_url=endpoint_url, region_name=remote_cfg.get("region"))


class FastSyncOps(Protocol):
    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
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

        On retry: ``fetch_to_cache`` retries up to 4 times on retryable S3
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
    ) -> FastPullResult:
        def _all_target_ids() -> list[str]:
            """Every target identifier this call is responsible for routing
            to fallback when fast-sync aborts — both .dvc and pipeline."""
            ids: list[str] = list(targets)
            if pipeline_outs:
                ids.extend(o.target for o in pipeline_outs)
            return ids

        if not _dvc_version_ok():
            return _push_all_to_fallback(_all_target_ids(), "dvc version mismatch")

        try:
            remote_cfg = get_remote_config(project_path, remote_name)
        except (FileNotFoundError, KeyError) as exc:
            return _push_all_to_fallback(_all_target_ids(), f"remote config not found: {exc}")

        try:
            bucket, prefix = parse_s3_url(remote_cfg.get("url", ""))
        except ValueError as exc:
            return _push_all_to_fallback(_all_target_ids(), f"non-S3 remote: {exc}")

        if boto3 is None:
            return _push_all_to_fallback(_all_target_ids(), "boto3 not importable")

        s3 = _create_s3_client(remote_cfg, self._aws_profile_name)
        if not check_bucket_versioning(s3, bucket):
            return _push_all_to_fallback(_all_target_ids(), "bucket versioning disabled")

        all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)

        if pipeline_outs:
            all_outs.extend(pipeline_outs)

        fallback.extend(hash_missing)

        if not spot_check_versions(s3, bucket, prefix, all_outs, project_path):
            return _push_all_to_fallback(_all_target_ids(), "version_id spot-check drift")

        cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
        synced = 0
        files_dir_failures: list[str] = []

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

        for out in all_outs:
            try:
                if out.is_files_format:
                    assert out.files is not None
                    failures = fetch_files_dir_contents(
                        s3, bucket, prefix, out, cache_dir, jobs, remote_name,
                        project_path=project_path,
                        progress=_advance,
                    )
                    if failures:
                        logger.warning(
                            "fast-sync files-format dir %r had %d failure(s); falling back",
                            out.target, len(failures),
                        )
                        files_dir_failures.extend(failures)
                        fallback.append(out.target)
                        continue
                    ensure_dir_manifest(cache_dir, out.files)
                elif out.is_dir:
                    # Local manifest lives at ...XX/YYYY.dir (with suffix).
                    # raw_md5 is used only when we need the md5-without-suffix
                    # form; the cache lookup keeps the .dir suffix.
                    full_md5 = out.md5 if out.md5.endswith(".dir") else f"{out.md5}.dir"
                    cached_manifest = cache_path_for(cache_dir, full_md5)
                    entries: list[DvcFileEntry] | None = None
                    if cached_manifest.exists():
                        try:
                            payload = json.loads(cached_manifest.read_bytes())
                            if isinstance(payload, list):
                                entries = [
                                    DvcFileEntry(
                                        md5=str(e.get("md5", "")),
                                        relpath=str(e.get("relpath", "")),
                                        size=int(e.get("size", 0) or 0),
                                    )
                                    for e in payload
                                ]
                        except Exception:
                            entries = None
                    if entries is None:
                        entries = fetch_dir_manifest(s3, bucket, prefix, out.md5, cache_dir)
                        if entries is None:
                            fallback.append(out.target)
                            continue
                    if not is_dir_fully_cached(cache_dir, entries):
                        failures = fetch_dir_contents(
                            s3, bucket, prefix, entries, cache_dir, jobs,
                            progress=_advance,
                        )
                        if failures:
                            files_dir_failures.extend(failures)
                            fallback.append(out.target)
                            continue
                    else:
                        # Dir already fully cached — fetch_dir_contents short-
                        # circuited above, so per-entry advance never fired.
                        # Fire the aggregate here so the bar still progresses
                        # on warm-cache re-runs.
                        _advance(out.size)
                else:
                    if is_cached(cache_dir, out.md5):
                        _advance(out.size)
                    else:
                        fetch_to_cache(
                            s3, bucket,
                            s3_key_for_out(prefix, out, project_path),
                            cache_path_for(cache_dir, out.md5),
                            out.md5,
                            out.version_id,
                            progress=_advance,
                        )
                synced += 1
            except Exception as exc:
                logger.warning("fast-sync target %r failed: %s", out.target, exc)
                fallback.append(out.target)

        return FastPullResult(
            success=not fallback,
            synced_count=synced,
            fallback_targets=fallback,
            reason="" if not fallback else "partial fallback",
            files_dir_failures=files_dir_failures,
        )