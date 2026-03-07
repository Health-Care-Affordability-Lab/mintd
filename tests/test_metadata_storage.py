"""Tests for metadata.json storage path correctness across project types and classifications.

Verifies that:
1. DVC remote URL uses full_project_name (with prefix), not just the bare name
2. Storage paths follow classification rules:
   - private: lab/{full_project_name}/
   - public: pub/{full_project_name}/
   - contract: contract/{contract_slug}/{full_project_name}/
3. storage.prefix and storage.dvc.remote_url are consistent
4. DVC remote_url in metadata matches what storage.py would actually configure
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mintd.api import create_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_and_load_metadata(tmp_dir, project_type, name, classification="private",
                               team="all-lab", contract_slug=None, contract_info=None):
    """Create a project and return parsed metadata.json."""
    result = create_project(
        project_type=project_type,
        name=name,
        language="python",
        path=tmp_dir,
        init_git=False,
        init_dvc=False,
        classification=classification,
        team=team,
        contract_slug=contract_slug,
        contract_info=contract_info,
    )
    metadata_path = result.path / "metadata.json"
    assert metadata_path.exists(), f"metadata.json not found at {metadata_path}"
    with open(metadata_path) as f:
        return json.load(f), result


# ---------------------------------------------------------------------------
# Test: DVC remote URL uses full_project_name
# ---------------------------------------------------------------------------

class TestDvcRemoteUrlUsesFullName:
    """DVC remote URL must include the type prefix (data_, prj_, etc.)."""

    def test_data_project_dvc_url_has_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, result = _create_and_load_metadata(tmp, "data", "test")
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert "data_test" in dvc_url, f"Expected 'data_test' in DVC URL, got: {dvc_url}"

    def test_project_dvc_url_has_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, result = _create_and_load_metadata(tmp, "project", "test")
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert "prj_test" in dvc_url, f"Expected 'prj_test' in DVC URL, got: {dvc_url}"


# ---------------------------------------------------------------------------
# Test: Storage prefix uses full_project_name
# ---------------------------------------------------------------------------

class TestStoragePrefixUsesFullName:
    """storage.prefix must use the full project name with type prefix."""

    def test_data_private_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "test", "private")
            prefix = metadata["storage"]["prefix"]
            assert "data_test" in prefix, f"Expected 'data_test' in prefix, got: {prefix}"

    def test_project_private_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "project", "test", "private")
            prefix = metadata["storage"]["prefix"]
            assert "prj_test" in prefix, f"Expected 'prj_test' in prefix, got: {prefix}"

    def test_data_public_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "test", "public")
            prefix = metadata["storage"]["prefix"]
            assert "data_test" in prefix, f"Expected 'data_test' in prefix, got: {prefix}"

    def test_data_contract_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(
                tmp, "data", "test", "contract",
                contract_slug="cms-2024", contract_info="CMS contract"
            )
            prefix = metadata["storage"]["prefix"]
            assert "data_test" in prefix, f"Expected 'data_test' in prefix, got: {prefix}"


# ---------------------------------------------------------------------------
# Test: Classification-based path patterns
# ---------------------------------------------------------------------------

class TestClassificationPaths:
    """Storage paths must follow classification-specific patterns."""

    def test_private_uses_lab_prefix(self):
        """Private projects: lab/{full_name}/"""
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "mydata", "private")
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert prefix.startswith("lab/"), f"Private prefix should start with 'lab/', got: {prefix}"
            assert "lab/" in dvc_url, f"Private DVC URL should contain 'lab/', got: {dvc_url}"

    def test_public_uses_pub_prefix(self):
        """Public projects: pub/{full_name}/"""
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "mydata", "public")
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert prefix.startswith("pub/"), f"Public prefix should start with 'pub/', got: {prefix}"
            assert "pub/" in dvc_url, f"Public DVC URL should contain 'pub/', got: {dvc_url}"

    def test_contract_uses_contract_prefix(self):
        """Contract projects: contract/{slug}/{full_name}/"""
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(
                tmp, "data", "mydata", "contract",
                contract_slug="cms-2024", contract_info="Test"
            )
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert prefix.startswith("contract/"), f"Contract prefix should start with 'contract/', got: {prefix}"
            assert "cms-2024" in prefix, f"Contract prefix should contain slug, got: {prefix}"


# ---------------------------------------------------------------------------
# Test: Consistency between storage.prefix and storage.dvc.remote_url
# ---------------------------------------------------------------------------

class TestStorageConsistency:
    """storage.prefix and storage.dvc.remote_url must be consistent."""

    def test_prefix_matches_dvc_url_data_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "test", "private")
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            bucket = metadata["storage"]["bucket"]
            expected_url = f"s3://{bucket}/{prefix}"
            assert dvc_url == expected_url, f"DVC URL mismatch: {dvc_url} != {expected_url}"

    def test_prefix_matches_dvc_url_project_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "project", "test", "public")
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            bucket = metadata["storage"]["bucket"]
            expected_url = f"s3://{bucket}/{prefix}"
            assert dvc_url == expected_url, f"DVC URL mismatch: {dvc_url} != {expected_url}"

    def test_prefix_matches_dvc_url_data_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(
                tmp, "data", "test", "contract",
                contract_slug="slug", contract_info="info"
            )
            prefix = metadata["storage"]["prefix"]
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            bucket = metadata["storage"]["bucket"]
            expected_url = f"s3://{bucket}/{prefix}"
            assert dvc_url == expected_url, f"DVC URL mismatch: {dvc_url} != {expected_url}"


# ---------------------------------------------------------------------------
# Test: Metadata matches what DVC storage.py would actually configure
# ---------------------------------------------------------------------------

class TestMetadataMatchesDvcInit:
    """Metadata DVC info must match what initializers/storage.py would configure."""

    def test_private_data_matches_storage_init(self):
        """Metadata DVC URL should match the URL that init_dvc would actually set."""
        with tempfile.TemporaryDirectory() as tmp:
            metadata, result = _create_and_load_metadata(tmp, "data", "test", "private")
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            bucket = metadata["storage"]["bucket"]
            full_name = result.full_name
            # storage.py uses: s3://{bucket}/lab/{full_project_name}/
            expected = f"s3://{bucket}/lab/{full_name}/"
            assert dvc_url == expected, f"Metadata DVC URL {dvc_url} doesn't match storage.py expected {expected}"

    def test_public_project_matches_storage_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, result = _create_and_load_metadata(tmp, "project", "test", "public")
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            bucket = metadata["storage"]["bucket"]
            full_name = result.full_name
            # storage.py uses: s3://{bucket}/pub/{full_project_name}/
            expected = f"s3://{bucket}/pub/{full_name}/"
            assert dvc_url == expected, f"Metadata DVC URL {dvc_url} doesn't match storage.py expected {expected}"


# ---------------------------------------------------------------------------
# Test: Private paths should NOT include team name
# ---------------------------------------------------------------------------

class TestNoTeamInPath:
    """Storage paths should use lab/{full_name}/, not lab/{team}/{name}/."""

    def test_private_no_team_in_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(
                tmp, "data", "test", "private", team="all-lab"
            )
            prefix = metadata["storage"]["prefix"]
            assert "all-lab" not in prefix, f"Team name should not be in prefix: {prefix}"

    def test_private_no_team_in_dvc_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(
                tmp, "data", "test", "private", team="all-lab"
            )
            dvc_url = metadata["storage"]["dvc"]["remote_url"]
            assert "all-lab" not in dvc_url, f"Team name should not be in DVC URL: {dvc_url}"


# ---------------------------------------------------------------------------
# Test: DVC remote_name uses full_project_name
# ---------------------------------------------------------------------------

class TestDvcRemoteName:
    """DVC remote_name should be the full project name (with prefix)."""

    def test_data_remote_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "data", "test")
            remote_name = metadata["storage"]["dvc"]["remote_name"]
            assert remote_name == "data_test", f"Expected remote_name='data_test', got: {remote_name}"

    def test_project_remote_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata, _ = _create_and_load_metadata(tmp, "project", "test")
            remote_name = metadata["storage"]["dvc"]["remote_name"]
            assert remote_name == "prj_test", f"Expected remote_name='prj_test', got: {remote_name}"
