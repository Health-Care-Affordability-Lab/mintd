"""Read-only S3 listing for `mintd data ls`.

Reuses `_create_s3_client` and `parse_s3_url` from `_fast_sync_ops`.
Md5-keyed support is deferred for v1.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional
from botocore.exceptions import ClientError
from ._fast_sync_ops import _create_s3_client

@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    last_modified: Optional[datetime]
    version_count: int
    is_dir: bool = False

@dataclass(frozen=True)
class S3ListingResult:
    bucket: str
    prefix: str
    endpoint: str
    objects: List[S3Object]
    truncated_to_prefix: Optional[str]

class S3ListingError(Exception):
    pass

class BucketNotFound(S3ListingError):
    pass

class BucketAccessError(S3ListingError):
    pass

_SUB_PATH_FORBIDDEN = ("..", "\\")

def _normalise_sub_path(sub_path: Optional[str]) -> str:
    if sub_path is None:
        return ""
    if sub_path.startswith("/") or any(token in sub_path for token in _SUB_PATH_FORBIDDEN):
        raise ValueError(f"invalid sub_path: {sub_path!r}")
    return sub_path.strip("/") + "/" if sub_path.strip("/") else ""

def list_product_objects(
    *,
    bucket: str,
    prefix: str,
    endpoint: str,
    sub_path: Optional[str],
    recursive: bool,
    include_versions: bool,
    aws_profile_name: Optional[str],
    s3_client_factory: Callable[[dict[str, str], Optional[str]], Any] = _create_s3_client,
) -> S3ListingResult:
    normalised_sub_path = _normalise_sub_path(sub_path)
    parts = [p.strip("/") for p in [prefix, normalised_sub_path] if p and p.strip("/")]
    effective_prefix = "/".join(parts) + "/" if parts else ""

    remote_cfg = {"endpoint": endpoint}
    s3 = s3_client_factory(remote_cfg, aws_profile_name)

    listing: List[S3Object] = []
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": effective_prefix}
    if not recursive:
        kwargs["Delimiter"] = "/"

    try:
        if include_versions:
            # Plan reviewer P1: harvest both Versions[] and DeleteMarkers[],
            # track which key has IsLatest. Skip keys whose latest entry is
            # a DeleteMarker (logically deleted). version_count counts live
            # versions only.
            paginator = s3.get_paginator("list_object_versions")
            latest_by_key: dict[str, dict[str, Any]] = {}
            latest_is_delete: dict[str, bool] = {}
            version_count_by_key: dict[str, int] = {}
            directories: set[str] = set()
            for page in paginator.paginate(**kwargs):
                for p in page.get("CommonPrefixes", []):
                    directories.add(p["Prefix"][len(effective_prefix):])
                for v in page.get("Versions", []):
                    key = v["Key"]
                    version_count_by_key[key] = version_count_by_key.get(key, 0) + 1
                    if v.get("IsLatest"):
                        latest_by_key[key] = v
                        latest_is_delete[key] = False
                for d in page.get("DeleteMarkers", []):
                    key = d["Key"]
                    if d.get("IsLatest"):
                        latest_by_key[key] = d
                        latest_is_delete[key] = True
            for d in sorted(directories):
                listing.append(S3Object(
                    key=d, size=0, last_modified=None, version_count=0, is_dir=True,
                ))
            for key in sorted(latest_by_key):
                if latest_is_delete.get(key):
                    continue
                latest = latest_by_key[key]
                rel = key[len(effective_prefix):]
                listing.append(S3Object(
                    key=rel,
                    size=latest.get("Size", 0),
                    last_modified=latest.get("LastModified"),
                    version_count=version_count_by_key.get(key, 1),
                    is_dir=False,
                ))
        else:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(**kwargs):
                for p in page.get("CommonPrefixes", []):
                    rel_key = p["Prefix"][len(effective_prefix):]
                    listing.append(S3Object(
                        key=rel_key, size=0, last_modified=None,
                        version_count=0, is_dir=True,
                    ))
                for o in page.get("Contents", []):
                    rel_key = o["Key"][len(effective_prefix):]
                    if not rel_key:
                        # Skip placeholder "folder" key equal to the prefix.
                        continue
                    listing.append(S3Object(
                        key=rel_key, size=o["Size"],
                        last_modified=o["LastModified"],
                        version_count=1, is_dir=False,
                    ))
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket", "404"):
            raise BucketNotFound(f"bucket {bucket!r} not found") from e
        if code in ("AccessDenied", "403"):
            raise BucketAccessError(f"access denied for bucket {bucket!r}") from e
        raise BucketAccessError(str(e)) from e

    # Lexicographic sort: directories first then files (existing UX).
    listing.sort(key=lambda o: (not o.is_dir, o.key))
    return S3ListingResult(
        bucket=bucket, prefix=effective_prefix, endpoint=endpoint,
        objects=listing, truncated_to_prefix=sub_path,
    )
