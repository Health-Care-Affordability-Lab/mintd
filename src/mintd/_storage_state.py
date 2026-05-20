"""Storage state inspection + canonical prefix computation.

Pure-function classifier reading ``metadata.json`` and ``.dvc/config`` to
detect storage configuration drift, plus the single source of truth for
mapping a classification tier (labonly / public / licensed) to its S3
prefix layout. No network, no subprocess, no DVC invocation.

Slice 30:
- Ports v1's ``utils/storage_state.py`` classifier, adds a 7th state
  (``BUCKET_EMPTY``) for the lab's actual drift mode.
- Adds ``compute_storage_prefix`` (replaces v1's three-tier
  ``api.compute_storage_prefix`` with the slice-30 three-tier shape
  where ``licensed`` puts the slug at the bucket root).
"""

from __future__ import annotations

import configparser
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


SLUG_REGEX = re.compile(r"^[a-zA-Z0-9._-]+$")


class StorageState(StrEnum):
    FRESH = "fresh"
    INITIALIZED = "initialized"
    PARTIAL_META_ONLY = "partial_meta_only"
    PARTIAL_DVC_ONLY = "partial_dvc_only"
    NAME_MISMATCH = "name_mismatch"
    URL_MISMATCH = "url_mismatch"
    BUCKET_EMPTY = "bucket_empty"


@dataclass(frozen=True)
class StorageInspection:
    state: StorageState
    metadata_remote_name: str | None
    metadata_url: str | None
    metadata_bucket: str | None
    metadata_prefix: str | None
    dvc_remote_name: str | None
    dvc_url: str | None
    metadata_path: Path
    dvc_config_path: Path


def inspect_storage(project_path: Path) -> StorageInspection:
    """Classify a project's storage state. Pure function.

    Reads ``metadata.json`` and ``.dvc/config``; returns a
    ``StorageInspection`` whose ``state`` is one of seven values per
    ``StorageState``. The classification ladder (order matters, per
    slice-30 review fix):

    1. No metadata storage block AND no DVC remote -> FRESH.
    2. Metadata storage block present AND no DVC remote ->
       PARTIAL_META_ONLY. Hint can't reference a DVC host.
    3. DVC remote present AND no metadata storage block ->
       PARTIAL_DVC_ONLY.
    4. Both present, metadata bucket == "" -> BUCKET_EMPTY. Guaranteed
       dvc_url is set so the hint can name the real bucket.
    5. Both present, remote names differ -> NAME_MISMATCH.
    6. Both present, URLs differ (after rstrip("/")) -> URL_MISMATCH.
    7. Otherwise -> INITIALIZED.
    """
    metadata_path = project_path / "metadata.json"
    dvc_config_path = project_path / ".dvc" / "config"

    meta_bucket, meta_prefix, meta_name, meta_block_present = _read_metadata_storage(metadata_path)
    dvc_name, dvc_url = _read_dvc_config(dvc_config_path)

    meta_url: str | None = None
    if meta_bucket and meta_prefix:
        meta_url = f"s3://{meta_bucket}/{meta_prefix}"

    n_meta = _normalize_url(meta_url)
    n_dvc = _normalize_url(dvc_url)

    state: StorageState
    if not meta_block_present and dvc_name is None:
        state = StorageState.FRESH
    elif meta_block_present and dvc_name is None:
        state = StorageState.PARTIAL_META_ONLY
    elif not meta_block_present and dvc_name is not None:
        state = StorageState.PARTIAL_DVC_ONLY
    elif meta_bucket == "":
        state = StorageState.BUCKET_EMPTY
    elif meta_name and dvc_name and meta_name != dvc_name:
        state = StorageState.NAME_MISMATCH
    elif n_meta and n_dvc and n_meta != n_dvc:
        state = StorageState.URL_MISMATCH
    else:
        state = StorageState.INITIALIZED

    return StorageInspection(
        state=state,
        metadata_remote_name=meta_name,
        metadata_url=meta_url,
        metadata_bucket=meta_bucket,
        metadata_prefix=meta_prefix,
        dvc_remote_name=dvc_name,
        dvc_url=dvc_url,
        metadata_path=metadata_path,
        dvc_config_path=dvc_config_path,
    )


def repair_hint(inspection: StorageInspection) -> str | None:
    """Return the manual-repair instruction for a non-healthy state.

    Returns None for FRESH and INITIALIZED. For other states, returns a
    short instruction naming the user's actual on-disk values so they
    can edit ``metadata.json`` or ``.dvc/config`` by hand. No DVC
    commands are run by mintd.
    """
    state = inspection.state
    if state in (StorageState.FRESH, StorageState.INITIALIZED):
        return None

    if state == StorageState.BUCKET_EMPTY:
        host = urlparse(inspection.dvc_url).netloc if inspection.dvc_url else ""
        return (
            f"storage.bucket is empty in metadata.json. From .dvc/config "
            f"the bucket is {host!r}. Edit metadata.json and set "
            f"storage.bucket to {host!r}."
        )

    if state == StorageState.PARTIAL_META_ONLY:
        return (
            f"metadata.storage is configured but .dvc/config has no remote. "
            f"Run: dvc remote add -d {inspection.metadata_remote_name or '<remote>'} "
            f"{inspection.metadata_url or '<reconstructed-url>'}"
        )

    if state == StorageState.PARTIAL_DVC_ONLY:
        host = ""
        path = ""
        if inspection.dvc_url:
            parsed = urlparse(inspection.dvc_url)
            host = parsed.netloc
            path = parsed.path.lstrip("/")
        return (
            f".dvc/config has remote {inspection.dvc_remote_name!r} at "
            f"{inspection.dvc_url!r} but metadata.json has no storage "
            f"block. Edit metadata.json to add a storage object with "
            f"bucket={host!r}, prefix={path!r}, "
            f"dvc.remote_name={inspection.dvc_remote_name!r}."
        )

    if state == StorageState.NAME_MISMATCH:
        return (
            f"metadata.storage.dvc.remote_name is "
            f"{inspection.metadata_remote_name!r} but .dvc/config has "
            f"remote {inspection.dvc_remote_name!r}. Pick one: edit "
            f"metadata.json or run 'dvc remote rename "
            f"{inspection.dvc_remote_name} {inspection.metadata_remote_name}'."
        )

    if state == StorageState.URL_MISMATCH:
        return (
            f"metadata reconstructs to {inspection.metadata_url!r} but "
            f".dvc/config has {inspection.dvc_url!r}. Decide which is "
            f"correct, then edit the other to match."
        )

    return None


def compute_storage_prefix(
    *,
    classification: Literal["labonly", "public", "licensed"],
    project_name: str,
    slug: str | None = None,
) -> str:
    """Build the canonical S3 path segment for a project.

    Single source of truth for ``metadata.storage.prefix`` and the DVC
    remote URL suffix. Slice 30 collapses v1's separate ``licensed`` /
    ``contract`` tiers into one ``licensed`` tier where the slug sits
    at the bucket root (no wrapper segment).

    labonly  -> lab/<project>/
    public   -> pub/<project>/
    licensed -> <slug>/<project>/   (slug required; URL-safe)
    """
    if classification == "labonly":
        return f"lab/{project_name}/"
    if classification == "public":
        return f"pub/{project_name}/"
    if classification == "licensed":
        if not slug:
            raise ValueError("classification 'licensed' requires a slug")
        if not SLUG_REGEX.match(slug):
            raise ValueError(
                f"licensed slug {slug!r} must match {SLUG_REGEX.pattern}"
            )
        return f"{slug}/{project_name}/"
    raise ValueError(
        f"unknown classification {classification!r}; expected one of "
        f"labonly / public / licensed"
    )


# ---------- helpers --------------------------------------------------


def _read_metadata_storage(
    metadata_path: Path,
) -> tuple[str | None, str | None, str | None, bool]:
    """Return (bucket, prefix, remote_name, storage_block_present)."""
    if not metadata_path.is_file():
        return None, None, None, False
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None, None, False
    storage = data.get("storage")
    if not isinstance(storage, dict):
        return None, None, None, False
    bucket = storage.get("bucket")
    prefix = storage.get("prefix")
    dvc = storage.get("dvc") or {}
    name = dvc.get("remote_name") if isinstance(dvc, dict) else None
    return (
        bucket if isinstance(bucket, str) else None,
        prefix if isinstance(prefix, str) else None,
        name if isinstance(name, str) and name else None,
        True,
    )


def _read_dvc_config(config_path: Path) -> tuple[str | None, str | None]:
    """Return (default_remote_name, url) from .dvc/config.

    Handles both DVC section-header formats: ``[remote "name"]`` and
    ``['remote "name"']``. Falls back to the single remote section when
    ``[core] remote = ...`` isn't set (clone-time config shape).
    """
    if not config_path.is_file():
        return None, None
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error:
        return None, None

    name = parser.get("core", "remote", fallback="") or None
    if not name:
        for section in parser.sections():
            sec_name = _extract_remote_section_name(section)
            if sec_name is not None:
                name = sec_name
                break
        if not name:
            return None, None

    for section in (f'remote "{name}"', f'\'remote "{name}"\''):
        if parser.has_section(section):
            url = parser.get(section, "url", fallback="") or None
            return name, url

    for section in parser.sections():
        if _extract_remote_section_name(section) == name:
            url = parser.get(section, "url", fallback="") or None
            return name, url
    return name, None


def _extract_remote_section_name(section: str) -> str | None:
    s = section
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    if not s.startswith("remote "):
        return None
    rest = s[len("remote ") :].strip()
    if rest.startswith('"') and rest.endswith('"'):
        return rest[1:-1]
    return rest or None


def _normalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    return url.rstrip("/")
