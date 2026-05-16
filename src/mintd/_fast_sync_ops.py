"""DVC FastSync implementation.

Coupled to DVC cache layout (3.66.x).
"""

import configparser
import dataclasses
import hashlib
import logging
import os
import random
import subprocess
import time
from dataclasses import dataclass
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
class DvcOut:
    target: str
    path: str
    md5: str
    is_dir: bool
    version_id: str | None = None
    is_files_format: bool = False


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
        outs.append(DvcOut(
            target="",
            path=str(out.get("path", "")),
            md5=md5,
            is_dir=md5.endswith(".dir"),
            version_id=_extract_version_id(out),
            is_files_format=has_files and not has_md5,
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

    Slice 18 fast-sync handles single-file (md5-keyed) targets only. Targets
    whose .dvc has a directory entry (md5 ending in ``.dir``) or a
    files-format inline ``files:`` list are routed to ``fallback`` so vanilla
    ``dvc pull`` materializes them correctly. Slice 19 will port the dir/
    files-format paths once the single-file path is proven in production.
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

        # Any directory-shaped entry on this target → fallback the whole target.
        # Mixing partial-fast-sync and partial-fallback within a single .dvc file
        # is the recipe for the partial-checkout invariant bug. Keep it simple.
        if any(out.is_dir or out.is_files_format for out in outs):
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


# Slice 19 candidates: fetch_dir_manifest, ensure_dir_manifest,
# fetch_dir_contents, fetch_files_dir_contents — port from legacy
# mintd/utils/fast_sync.py once the single-file path is proven in production.
# Until then, classify_targets routes dir-shaped entries to fallback.


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

        # all_outs only contains single-file targets; dir-shaped entries
        # routed to fallback in classify_targets.
        for out in all_outs:
            try:
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