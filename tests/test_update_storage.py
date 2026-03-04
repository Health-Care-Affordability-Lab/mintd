"""Tests for mintd update storage command - metadata.json sync after DVC update."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mintd.cli import main


@pytest.fixture
def project_dir_with_dvc():
    """Create a mock project with metadata.json and .dvc directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        metadata = {
            "project": {
                "name": "test-project",
                "type": "data",
                "full_name": "data_test-project",
            },
            "storage": {
                "sensitivity": "restricted",
                "dvc": {
                    "remote_name": "data_test-project",
                    "remote_url": "s3://old-bucket/lab/data_test-project/",
                },
            },
        }

        with open(project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # Simulate DVC repo
        (project_dir / ".dvc").mkdir()

        yield project_dir


class TestUpdateStorageSyncsMetadata:
    """Phase 1: mintd update storage must sync metadata.json after DVC update."""

    @patch("mintd.shell.dvc_command")
    @patch("mintd.config.get_config")
    def test_storage_update_syncs_metadata_json(
        self, mock_get_config, mock_dvc_command, project_dir_with_dvc
    ):
        """After DVC remote update, metadata.json should reflect new remote_url."""
        mock_get_config.return_value = {
            "storage": {
                "bucket_prefix": "new-bucket",
                "endpoint": "https://s3.wasabisys.com",
                "region": "us-east-1",
                "versioning": True,
            }
        }
        mock_dvc = MagicMock()
        mock_dvc.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_dvc_command.return_value = mock_dvc

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["update", "storage", "--path", str(project_dir_with_dvc), "-y"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        # Read updated metadata.json
        with open(project_dir_with_dvc / "metadata.json") as f:
            metadata = json.load(f)

        # The remote_url should now point to new-bucket
        dvc_info = metadata["storage"]["dvc"]
        assert dvc_info["remote_name"] == "data_test-project"
        assert "new-bucket" in dvc_info["remote_url"]
        assert "lab/data_test-project/" in dvc_info["remote_url"]

    @patch("mintd.shell.dvc_command")
    @patch("mintd.config.get_config")
    def test_storage_update_creates_dvc_section_if_missing(
        self, mock_get_config, mock_dvc_command, project_dir_with_dvc
    ):
        """metadata.json without storage.dvc should get one added after update."""
        # Remove the dvc section from metadata
        with open(project_dir_with_dvc / "metadata.json") as f:
            metadata = json.load(f)
        del metadata["storage"]["dvc"]
        with open(project_dir_with_dvc / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        mock_get_config.return_value = {
            "storage": {
                "bucket_prefix": "my-bucket",
                "versioning": True,
            }
        }
        mock_dvc = MagicMock()
        mock_dvc.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_dvc_command.return_value = mock_dvc

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["update", "storage", "--path", str(project_dir_with_dvc), "-y"],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        with open(project_dir_with_dvc / "metadata.json") as f:
            metadata = json.load(f)

        assert "dvc" in metadata["storage"]
        assert metadata["storage"]["dvc"]["remote_name"] == "data_test-project"

    @patch("mintd.shell.dvc_command")
    @patch("mintd.config.get_config")
    def test_storage_update_preserves_other_storage_fields(
        self, mock_get_config, mock_dvc_command, project_dir_with_dvc
    ):
        """Updating storage should not clobber other storage fields like sensitivity."""
        mock_get_config.return_value = {
            "storage": {"bucket_prefix": "bucket", "versioning": True}
        }
        mock_dvc = MagicMock()
        mock_dvc.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_dvc_command.return_value = mock_dvc

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["update", "storage", "--path", str(project_dir_with_dvc), "-y"],
        )

        assert result.exit_code == 0

        with open(project_dir_with_dvc / "metadata.json") as f:
            metadata = json.load(f)

        # sensitivity should still be there
        assert metadata["storage"]["sensitivity"] == "restricted"
