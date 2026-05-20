"""Slice 30: storage-state classifier tests.

10 classifier cases + 5 compute_storage_prefix cases. Each inline tempdir
exercises one state to pin the classification ladder + hint generator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mintd._storage_state import (
    StorageState,
    compute_storage_prefix,
    inspect_storage,
    repair_hint,
)


def _write_metadata(tmp_path: Path, storage: dict | None) -> None:
    body: dict = {"project": {"name": "p", "type": "data"}}
    if storage is not None:
        body["storage"] = storage
    (tmp_path / "metadata.json").write_text(json.dumps(body))


def _write_dvc_config(tmp_path: Path, *, remote: str, url: str, quoted: bool = False) -> None:
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    section = f"'remote \"{remote}\"'" if quoted else f'remote "{remote}"'
    cfg.write_text(
        f"[core]\n    remote = {remote}\n"
        f"[{section}]\n    url = {url}\n"
    )


# ---------- classifier ----------------------------------------------

def test_inspect_storage_fresh(tmp_path: Path) -> None:
    """No metadata.json, no .dvc/config => FRESH."""
    inspection = inspect_storage(tmp_path)
    assert inspection.state == StorageState.FRESH


def test_inspect_storage_initialized_matching(tmp_path: Path) -> None:
    """Both sides present, name + URL match => INITIALIZED."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "b", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://b/lab/p/")
    assert inspect_storage(tmp_path).state == StorageState.INITIALIZED


def test_inspect_storage_partial_meta_only(tmp_path: Path) -> None:
    """metadata.storage present, no .dvc/config => PARTIAL_META_ONLY.

    Critical ladder-order regression: bucket=="" with NO .dvc/config
    must be PARTIAL_META_ONLY (not BUCKET_EMPTY) so the hint doesn't
    dereference a nonexistent dvc_url.
    """
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    assert inspect_storage(tmp_path).state == StorageState.PARTIAL_META_ONLY


def test_inspect_storage_partial_dvc_only(tmp_path: Path) -> None:
    """No metadata.storage, .dvc/config present => PARTIAL_DVC_ONLY."""
    _write_metadata(tmp_path, None)
    _write_dvc_config(tmp_path, remote="p", url="s3://b/lab/p/")
    assert inspect_storage(tmp_path).state == StorageState.PARTIAL_DVC_ONLY


def test_inspect_storage_name_mismatch(tmp_path: Path) -> None:
    """Both present, names differ => NAME_MISMATCH."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "b", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "old_name"},
    })
    _write_dvc_config(tmp_path, remote="new_name", url="s3://b/lab/p/")
    assert inspect_storage(tmp_path).state == StorageState.NAME_MISMATCH


def test_inspect_storage_url_mismatch(tmp_path: Path) -> None:
    """Both present, names match, URLs differ => URL_MISMATCH."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "b", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://b/lab/different/")
    assert inspect_storage(tmp_path).state == StorageState.URL_MISMATCH


def test_inspect_storage_bucket_empty(tmp_path: Path) -> None:
    """bucket="" + .dvc/config populated => BUCKET_EMPTY."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://cooper-globus/lab/p/")
    inspection = inspect_storage(tmp_path)
    assert inspection.state == StorageState.BUCKET_EMPTY
    hint = repair_hint(inspection)
    assert hint is not None
    assert "cooper-globus" in hint


def test_inspect_storage_bucket_empty_overrides_url_mismatch(tmp_path: Path) -> None:
    """bucket="" wins over URL_MISMATCH when both apply."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "", "prefix": "lab/p/different/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://cooper-globus/lab/p/")
    assert inspect_storage(tmp_path).state == StorageState.BUCKET_EMPTY


def test_inspect_storage_handles_single_quoted_dvc_section(tmp_path: Path) -> None:
    """['remote "name"'] section header (some DVC builds)."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "b", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://b/lab/p/", quoted=True)
    assert inspect_storage(tmp_path).state == StorageState.INITIALIZED


def test_inspect_storage_normalizes_trailing_slash(tmp_path: Path) -> None:
    """Trailing slash on one side only must NOT trigger URL_MISMATCH."""
    _write_metadata(tmp_path, {
        "provider": "s3", "bucket": "b", "prefix": "lab/p/",
        "endpoint": "", "versioning": True,
        "dvc": {"remote_name": "p"},
    })
    _write_dvc_config(tmp_path, remote="p", url="s3://b/lab/p")
    assert inspect_storage(tmp_path).state == StorageState.INITIALIZED


# ---------- compute_storage_prefix ----------------------------------

def test_compute_storage_prefix_labonly() -> None:
    assert compute_storage_prefix(
        classification="labonly", project_name="data_foo"
    ) == "lab/data_foo/"


def test_compute_storage_prefix_public() -> None:
    assert compute_storage_prefix(
        classification="public", project_name="data_foo"
    ) == "pub/data_foo/"


def test_compute_storage_prefix_licensed_requires_slug() -> None:
    with pytest.raises(ValueError, match="requires a slug"):
        compute_storage_prefix(
            classification="licensed", project_name="data_foo", slug=None
        )


def test_compute_storage_prefix_licensed_with_slug() -> None:
    assert compute_storage_prefix(
        classification="licensed", project_name="data_foo", slug="optum"
    ) == "optum/data_foo/"


def test_compute_storage_prefix_licensed_rejects_bad_slug() -> None:
    """Slug becomes a top-level S3 segment so it MUST be URL-safe."""
    with pytest.raises(ValueError, match="must match"):
        compute_storage_prefix(
            classification="licensed", project_name="data_foo", slug="bad slug"
        )


def test_compute_storage_prefix_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError, match="unknown classification"):
        compute_storage_prefix(
            classification="contract",  # type: ignore[arg-type]
            project_name="data_foo",
        )
