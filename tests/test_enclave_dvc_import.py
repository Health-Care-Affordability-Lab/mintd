"""Tests for Phase 3: enclave pull_enclave_data should use run_dvc_import."""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from mintd.enclave_commands import pull_enclave_data


@pytest.fixture
def enclave_workspace():
    """Create a minimal enclave project workspace with manifest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        enclave_path = Path(tmpdir)

        manifest = {
            "enclave_name": "test_enclave",
            "approved_products": [
                {"repo": "data_test-product", "stage": "final"}
            ],
        }

        with open(enclave_path / "enclave_manifest.yaml", "w") as f:
            yaml.dump(manifest, f)

        yield enclave_path


class TestPullEnclaveDataUsesDvcImport:
    """pull_enclave_data should use run_dvc_import instead of clone + pull."""

    @patch("mintd.enclave_commands.copy_to_downloads")
    @patch("mintd.enclave_commands.get_dvc_hash")
    @patch("mintd.enclave_commands.run_dvc_import")
    @patch("mintd.enclave_commands.get_repo_info")
    def test_pull_uses_dvc_import(
        self, mock_get_info, mock_dvc_import, mock_get_hash, mock_copy,
        enclave_workspace,
    ):
        """pull_enclave_data should call run_dvc_import, not clone_or_update_repo."""
        mock_get_info.return_value = {
            "repo_url": "https://github.com/org/data_test-product",
            "dvc_remote_name": "data_test-product",
            "dvc_remote_url": "s3://bucket/lab/data_test-product/",
            "endpoint": "https://s3.wasabisys.com",
            "region": "us-east-1",
            "data_stage": "final",
        }

        staging_dir = enclave_workspace / "data" / "staging" / "data_test-product"
        staging_dir.mkdir(parents=True)
        # Simulate DVC import creates a data/final directory in staging
        (staging_dir / "final").mkdir()
        (staging_dir / "final" / "data.csv").write_text("test")

        mock_dvc_import.return_value = str(staging_dir / "final.dvc")
        mock_get_hash.return_value = ("abc1234", "gitcommit123")
        mock_copy.return_value = enclave_workspace / "data" / "downloads" / "data_test-product" / "abc1234-2026-03-04"

        pull_enclave_data(enclave_workspace, repo_name="data_test-product")

        # Should have called run_dvc_import
        mock_dvc_import.assert_called_once()
        call_kwargs = mock_dvc_import.call_args
        # The source repo URL should be passed
        assert "github.com" in str(call_kwargs)

    @patch("mintd.enclave_commands.copy_to_downloads")
    @patch("mintd.enclave_commands.get_dvc_hash")
    @patch("mintd.enclave_commands.run_dvc_import")
    @patch("mintd.enclave_commands.get_repo_info")
    def test_pull_updates_manifest_after_import(
        self, mock_get_info, mock_dvc_import, mock_get_hash, mock_copy,
        enclave_workspace,
    ):
        """Manifest should be updated with downloaded entry after successful import."""
        mock_get_info.return_value = {
            "repo_url": "https://github.com/org/data_test-product",
            "dvc_remote_name": "data_test-product",
            "dvc_remote_url": "s3://bucket/lab/data_test-product/",
            "endpoint": "",
            "region": "",
            "data_stage": "final",
        }

        staging_dir = enclave_workspace / "data" / "staging" / "data_test-product"
        staging_dir.mkdir(parents=True)

        mock_dvc_import.return_value = str(staging_dir / "final.dvc")
        mock_get_hash.return_value = ("abc1234", "gitcommit123")

        downloads_dir = enclave_workspace / "data" / "downloads" / "data_test-product" / "abc1234-2026-03-04"
        downloads_dir.mkdir(parents=True)
        mock_copy.return_value = downloads_dir

        pull_enclave_data(enclave_workspace, repo_name="data_test-product")

        # Read manifest and check downloaded section
        with open(enclave_workspace / "enclave_manifest.yaml") as f:
            manifest = yaml.safe_load(f)

        assert "downloaded" in manifest
        assert len(manifest["downloaded"]) == 1
        assert manifest["downloaded"][0]["repo"] == "data_test-product"
        assert manifest["downloaded"][0]["dvc_hash"] == "abc1234"

    @patch("mintd.enclave_commands.copy_to_downloads")
    @patch("mintd.enclave_commands.get_dvc_hash")
    @patch("mintd.enclave_commands.run_dvc_import")
    @patch("mintd.enclave_commands.get_repo_info")
    def test_pull_does_not_call_clone_or_update_repo(
        self, mock_get_info, mock_dvc_import, mock_get_hash, mock_copy,
        enclave_workspace,
    ):
        """clone_or_update_repo should NOT be called in the new implementation."""
        mock_get_info.return_value = {
            "repo_url": "https://github.com/org/data_test-product",
            "dvc_remote_name": "data_test-product",
            "dvc_remote_url": "s3://bucket/lab/data_test-product/",
            "endpoint": "",
            "region": "",
            "data_stage": "final",
        }

        staging_dir = enclave_workspace / "data" / "staging" / "data_test-product"
        staging_dir.mkdir(parents=True)

        mock_dvc_import.return_value = str(staging_dir / "final.dvc")
        mock_get_hash.return_value = ("abc1234", "gitcommit123")
        mock_copy.return_value = enclave_workspace / "data" / "downloads" / "v1"

        with patch("mintd.enclave_commands.clone_or_update_repo") as mock_clone:
            pull_enclave_data(enclave_workspace, repo_name="data_test-product")
            mock_clone.assert_not_called()
