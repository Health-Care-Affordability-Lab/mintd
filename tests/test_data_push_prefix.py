"""Test that mintd data push uses the correct S3 prefix.

This test verifies that when pushing data via `mintd data push`,
the data is uploaded to the correct S3 path using the full project name
(with type prefix like data_, prj_).
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mintd.api import create_project
from mintd.data_import import push_data, get_project_remote
from mintd.exceptions import DataImportError


class TestDataPushPrefix:
    """Test mintd data push uses correct storage paths."""

    def test_get_project_remote_returns_full_name(self):
        """Test that get_project_remote returns the full project name with prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a data project
            result = create_project(
                project_type="data",
                name="test-dataset",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="private"
            )

            # Test get_project_remote returns the full name
            remote_name = get_project_remote(result.path)
            assert remote_name == "data_test-dataset", (
                f"Expected remote_name='data_test-dataset', got: {remote_name}"
            )

    @patch('mintd.data_import.dvc_command')
    def test_push_data_uses_correct_remote(self, mock_dvc_command):
        """Test that push_data uses the remote name from metadata.json."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a project project
            result = create_project(
                project_type="project",
                name="my-analysis",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="private"
            )

            # Setup mock DVC command
            mock_dvc = MagicMock()
            mock_dvc_command.return_value = mock_dvc

            # Call push_data
            push_data(project_path=result.path)

            # Verify dvc push was called with the correct remote name
            mock_dvc.run_live.assert_called_once_with(
                "push", "-r", "prj_my-analysis"
            )

    @patch('mintd.data_import.dvc_command')
    def test_push_data_with_targets_and_jobs(self, mock_dvc_command):
        """Test push_data with specific targets and parallel jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a data project
            result = create_project(
                project_type="data",
                name="dataset",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="public"
            )

            # Setup mock DVC command
            mock_dvc = MagicMock()
            mock_dvc_command.return_value = mock_dvc

            # Call push_data with targets and jobs
            targets = ["data/raw.dvc", "data/processed.dvc"]
            push_data(
                project_path=result.path,
                targets=targets,
                jobs=4
            )

            # Verify dvc push was called with correct arguments
            mock_dvc.run_live.assert_called_once_with(
                "push", "-r", "data_dataset",
                "-j", "4",
                "data/raw.dvc", "data/processed.dvc"
            )

    def test_push_data_fails_without_metadata(self):
        """Test that push_data fails gracefully when metadata.json is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "no-metadata-project"
            project_path.mkdir()

            # Should raise DataImportError
            with pytest.raises(DataImportError) as exc_info:
                get_project_remote(project_path)

            assert "Not a mintd project" in str(exc_info.value)

    def test_push_data_fails_without_remote_config(self):
        """Test that push_data fails when metadata.json has no DVC remote."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp) / "no-remote-project"
            project_path.mkdir()

            # Create metadata.json without DVC remote config
            metadata = {
                "project": {"name": "test"},
                "storage": {}  # No dvc section
            }
            metadata_file = project_path / "metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f)

            # Should raise DataImportError
            with pytest.raises(DataImportError) as exc_info:
                get_project_remote(project_path)

            assert "No DVC remote configured" in str(exc_info.value)

    def test_storage_prefix_and_dvc_url_consistency(self):
        """Test that storage.prefix and DVC remote URL use the same path."""
        test_cases = [
            ("data", "test-data", "private", "lab/data_test-data/"),
            ("project", "test-proj", "private", "lab/prj_test-proj/"),
            ("data", "public-data", "public", "pub/data_public-data/"),
            ("project", "public-proj", "public", "pub/prj_public-proj/"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            for project_type, name, classification, expected_prefix in test_cases:
                result = create_project(
                    project_type=project_type,
                    name=name,
                    language="python",
                    path=tmp,
                    init_git=False,
                    init_dvc=False,
                    classification=classification
                )

                # Load metadata
                metadata_file = result.path / "metadata.json"
                with open(metadata_file) as f:
                    metadata = json.load(f)

                # Check storage.prefix
                storage_prefix = metadata["storage"]["prefix"]
                assert storage_prefix == expected_prefix, (
                    f"For {project_type}/{classification}, expected prefix "
                    f"'{expected_prefix}', got: {storage_prefix}"
                )

                # Check DVC remote URL contains the same prefix
                dvc_url = metadata["storage"]["dvc"]["remote_url"]
                bucket = metadata["storage"]["bucket"]
                expected_url = f"s3://{bucket}/{expected_prefix}"
                assert dvc_url == expected_url, (
                    f"DVC URL should be '{expected_url}', got: {dvc_url}"
                )

                # Check remote name is the full project name
                remote_name = metadata["storage"]["dvc"]["remote_name"]
                expected_remote = f"{project_type[:4] if project_type == 'data' else 'prj'}_{name}"
                assert remote_name == expected_remote, (
                    f"Remote name should be '{expected_remote}', got: {remote_name}"
                )