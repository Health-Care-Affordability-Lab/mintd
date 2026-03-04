"""Tests for Phase 2: registry catalog must include remote_url, endpoint, region."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mintd.registry import LocalRegistry
from mintd.enclave_commands import get_repo_info, configure_dvc_remote


class TestCatalogEntryHasFullDvcFields:
    """_generate_catalog_entry should always include remote_url, endpoint, region."""

    @patch("mintd.config.get_config")
    def test_fallback_storage_includes_remote_url(self, mock_get_config):
        """When storage is not in metadata (fallback path), remote_url should be set."""
        mock_get_config.return_value = {
            "storage": {
                "endpoint": "https://s3.wasabisys.com",
                "region": "us-east-1",
            }
        }

        registry = LocalRegistry("https://github.com/test-org/registry")

        metadata = {
            "project": {
                "name": "test-data",
                "type": "data",
                "full_name": "data_test-data",
            },
            "ownership": {"created_by": "user@example.com"},
            "metadata": {"version": "1.0.0"},
            "status": {"state": "active"},
        }

        entry, _ = registry._generate_catalog_entry(metadata)

        dvc = entry["storage"]["dvc"]
        assert "remote_url" in dvc, "Fallback storage must include remote_url"
        assert dvc["remote_url"] != ""
        assert "endpoint" in dvc
        assert dvc["endpoint"] == "https://s3.wasabisys.com"
        assert "region" in dvc
        assert dvc["region"] == "us-east-1"

    @patch("mintd.config.get_config")
    def test_existing_storage_enriched_with_endpoint_region(self, mock_get_config):
        """When metadata already has storage.dvc, enrich with endpoint and region."""
        mock_get_config.return_value = {
            "storage": {
                "endpoint": "https://s3.wasabisys.com",
                "region": "us-east-1",
            }
        }

        registry = LocalRegistry("https://github.com/test-org/registry")

        metadata = {
            "project": {
                "name": "test-data",
                "type": "data",
                "full_name": "data_test-data",
            },
            "ownership": {"created_by": "user@example.com"},
            "metadata": {"version": "1.0.0"},
            "status": {"state": "active"},
            "storage": {
                "dvc": {
                    "remote_name": "data_test-data",
                    "remote_url": "s3://bucket/lab/data_test-data/",
                },
                "sensitivity": "restricted",
            },
        }

        entry, _ = registry._generate_catalog_entry(metadata)

        dvc = entry["storage"]["dvc"]
        # Should keep existing fields
        assert dvc["remote_name"] == "data_test-data"
        assert dvc["remote_url"] == "s3://bucket/lab/data_test-data/"
        # Should be enriched with endpoint and region
        assert dvc["endpoint"] == "https://s3.wasabisys.com"
        assert dvc["region"] == "us-east-1"


class TestGetRepoInfoExtractsFullFields:
    """get_repo_info should extract endpoint and region from catalog."""

    @patch("mintd.enclave_commands.query_registry_for_product")
    def test_get_repo_info_includes_endpoint_and_region(self, mock_query):
        """get_repo_info result should include endpoint and region from catalog."""
        mock_query.return_value = {
            "exists": True,
            "catalog_data": {
                "repository": {"github_url": "https://github.com/org/repo"},
                "storage": {
                    "dvc": {
                        "remote_name": "data_test",
                        "remote_url": "s3://bucket/lab/data_test/",
                        "endpoint": "https://s3.wasabisys.com",
                        "region": "us-east-1",
                    }
                },
            },
        }

        info = get_repo_info("data_test")

        assert info["dvc_remote_url"] == "s3://bucket/lab/data_test/"
        assert info["endpoint"] == "https://s3.wasabisys.com"
        assert info["region"] == "us-east-1"


class TestConfigureDvcRemoteUsesRegistryFields:
    """configure_dvc_remote should use endpoint/region from params, not just global config."""

    @patch("mintd.enclave_commands.dvc_command")
    def test_configure_uses_explicit_endpoint_and_region(self, mock_dvc_command):
        """When endpoint and region are provided, use them directly."""
        mock_dvc = MagicMock()
        mock_dvc.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_dvc_command.return_value = mock_dvc

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / ".dvc").mkdir()
            # Write a minimal .dvc/config with a remote
            dvc_config = repo_dir / ".dvc" / "config"
            dvc_config.write_text("[core]\n    remote = data_test\n")

            configure_dvc_remote(
                repo_dir,
                repo_name="data_test",
                dvc_remote_url="s3://bucket/lab/data_test/",
                endpoint="https://s3.wasabisys.com",
                region="us-east-1",
            )

            # Should have called remote add and remote modify for endpoint and region
            calls = [str(c) for c in mock_dvc.run.call_args_list]
            assert any("endpointurl" in c for c in calls), (
                f"Expected endpointurl in DVC calls: {calls}"
            )
            assert any("region" in c for c in calls), (
                f"Expected region in DVC calls: {calls}"
            )
