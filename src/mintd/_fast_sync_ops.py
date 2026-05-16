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


@dataclass(frozen=True)
class DvcOut:
    target: str
    path: str
    md5: str
    is_dir: bool
    version_id: str | None = None
    is_files_format: bool = False
    files: list[DvcFileEntry] | None = None


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
        ))
    return outs


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
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix


def s3_key_for(prefix: str, md5: str) -> str:
    parts = [prefix] if prefix else []
    parts.extend(["files", "md5", md5[:2], md5[2:]])
    return "/".join(parts)


def s3_key_for_out(prefix: str, out: DvcOut) -> str:
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
    """
    del project_path  # unused; reserved for future relative-key construction
    to_check = random.sample(outs, min(n, len(outs)))
    for out in to_check:
        if not out.version_id:
            continue
        key = s3_key_for(prefix, out.md5)
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
    tmp_path.rename(manifest_path)
    parent_fd = os.open(str(manifest_path.parent), os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return full_md5


def fetch_to_cache(s3: Any, bucket: str, key: str, cache_path: Path, expected_md5: str, version_id: str | None = None) -> bool:
    """Download an object to the DVC cache atomically.

    Sequence: download to ``cache_path.tmp`` → md5 verify → fsync the tmp file →
    rename to ``cache_path`` → fsync the parent directory. Parent-dir fsync is
    required for durability across crashes (POSIX rename atomicity does not
    guarantee the directory entry itself is durable).

    ``version_id`` is passed via ``ExtraArgs={"VersionId": ...}`` — boto3's
    ``download_file`` does not accept ``VersionId`` as a top-level kwarg.
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
            tmp_path.rename(cache_path)
            parent_fd = os.open(str(cache_path.parent), os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
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
) -> list[str]:
    """Concurrent download of md5-keyed-dir constituents.

    Deduplicates entries by md5 before submitting to the thread pool:
    ``fetch_to_cache`` uses a static ``.tmp`` sibling per cache_path,
    so two threads downloading entries with the same md5 would race
    on the same temp file. Any two entries with the same md5 yield the
    same bytes from S3, so one download serves every duplicate relpath.
    First-occurrence wins for test determinism.
    """
    if not entries:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in entries:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                s3_key_for(prefix, entry.md5),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                None,
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
) -> list[str]:
    """Concurrent download of files-format dir constituents.

    Like ``fetch_dir_contents`` but with real-path S3 keys and per-file
    version_ids. md5 dedup applies for the same .tmp-race reason. The
    *manifest* (written by ``ensure_dir_manifest``) receives the full
    un-deduped list so ``dvc checkout`` can materialize every relpath.
    """
    del remote_name
    if not out.files:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in out.files:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    def _key(rel: str) -> str:
        parts = [p for p in (prefix, out.path, rel) if p]
        return "/".join(parts)

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                _key(entry.relpath),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                entry.version_id,
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
    ) -> FastPullResult: ...


class SubprocessFastSyncOps:
    def __init__(
        self,
        *,
        jobs: int = 8,
        progress: Callable[[DvcOut, bool], None] | None = None,
        aws_profile_name: str | None = None,
    ) -> None:
        self._default_jobs = jobs
        self._progress = progress
        self._aws_profile_name = aws_profile_name

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
    ) -> FastPullResult:
        if not _dvc_version_ok():
            return _push_all_to_fallback(targets, "dvc version mismatch")
            
        try:
            remote_cfg = get_remote_config(project_path, remote_name)
        except (FileNotFoundError, KeyError) as exc:
            return _push_all_to_fallback(targets, f"remote config not found: {exc}")
            
        try:
            bucket, prefix = parse_s3_url(remote_cfg.get("url", ""))
        except ValueError as exc:
            return _push_all_to_fallback(targets, f"non-S3 remote: {exc}")

        if boto3 is None:
            return _push_all_to_fallback(targets, "boto3 not importable")

        s3 = _create_s3_client(remote_cfg, self._aws_profile_name)
        if not check_bucket_versioning(s3, bucket):
            return _push_all_to_fallback(targets, "bucket versioning disabled")

        all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)

        fallback.extend(hash_missing)

        if not spot_check_versions(s3, bucket, prefix, all_outs, project_path):
            return _push_all_to_fallback(targets, "version_id spot-check drift")

        cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
        synced = 0
        files_dir_failures: list[str] = []
        # Progress callback is reserved for slice 19's dir/files-format paths
        # (`self._progress`); slice 18's single-file orchestrator doesn't
        # invoke it directly. Keeping the field for forward compatibility.
        _ = self._progress

        for out in all_outs:
            try:
                if out.is_files_format:
                    assert out.files is not None
                    failures = fetch_files_dir_contents(
                        s3, bucket, prefix, out, cache_dir, jobs, remote_name
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
                            s3, bucket, prefix, entries, cache_dir, jobs
                        )
                        if failures:
                            files_dir_failures.extend(failures)
                            fallback.append(out.target)
                            continue
                else:
                    if not is_cached(cache_dir, out.md5):
                        fetch_to_cache(
                            s3, bucket,
                            s3_key_for(prefix, out.md5),
                            cache_path_for(cache_dir, out.md5),
                            out.md5,
                            out.version_id,
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