"""Tests for CLI functionality."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from mintd.cli import main


def test_cli_main():
    """Test that the main CLI command works."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "mintd - Lab Project Scaffolding Tool" in result.output


def test_create_data_help():
    """Test the create data command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "data", "--help"])
    assert result.exit_code == 0
    assert "data_{name}" in result.output


def test_create_project_help():
    """Test the create project command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "project", "--help"])
    assert result.exit_code == 0
    assert "prj__{name}" in result.output


def test_create_infra_help():
    """Test the create infra command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "infra", "--help"])
    assert result.exit_code == 0
    assert "infra_{name}" in result.output


class TestUpdateStorageRemoteName:
    """Tests for update storage command remote naming convention."""

    @patch('mintd.shell.dvc_command')
    @patch('mintd.config.get_config')
    @patch('mintd.initializers.storage.is_dvc_repo')
    def test_update_storage_uses_full_name_for_remote(self, mock_is_dvc_repo, mock_get_config, mock_dvc_command):
        """Test that update storage uses full_name (e.g., data_test) as remote_name, not project_name (test)."""
        mock_is_dvc_repo.return_value = True
        mock_get_config.return_value = {
            "storage": {
                "bucket_prefix": "cooper-globus",
                "endpoint": "https://s3.wasabisys.com",
                "region": "us-east-1",
                "versioning": True
            }
        }
        mock_dvc = MagicMock()
        mock_dvc_command.return_value = mock_dvc

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            (project_path / ".dvc").mkdir()

            # Create metadata.json with both name and full_name
            metadata = {
                "project": {
                    "name": "cms-pps-weights",
                    "type": "data",
                    "full_name": "data_cms-pps-weights"
                },
                "storage": {
                    "sensitivity": "restricted"
                }
            }
            with open(project_path / "metadata.json", "w") as f:
                json.dump(metadata, f)

            runner = CliRunner()
            result = runner.invoke(main, ["update", "storage", "--path", str(project_path), "-y"])

            # Should use full_name (data_cms-pps-weights) as remote name, not name (cms-pps-weights)
            # Check that dvc remote commands were called with the full_name
            dvc_calls = mock_dvc.run.call_args_list
            remote_calls = [c for c in dvc_calls if "remote" in str(c)]

            # The remote name in DVC calls should be the full_name
            assert any("data_cms-pps-weights" in str(c) for c in remote_calls), \
                f"Expected remote name 'data_cms-pps-weights', but got calls: {remote_calls}"
            # Should NOT use just the short name
            assert not any(
                "cms-pps-weights" in str(c) and "data_cms-pps-weights" not in str(c)
                for c in remote_calls
            ), "Remote name should be full_name, not short name"