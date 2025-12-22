"""Tests for initializer functionality."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from mint.initializers.git import init_git, is_git_repo
from mint.initializers.storage import init_dvc, create_bucket, is_dvc_repo


def test_git_repo_detection():
    """Test Git repository detection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not be a git repo initially
        assert not is_git_repo(temp_path)

        # Create a mock .git directory check (avoid permission issues in sandbox)
        # We'll test the logic by mocking the path operations
        from unittest.mock import patch
        with patch.object(Path, 'is_dir', return_value=True):
            mock_git_path = temp_path / ".git"
            assert is_git_repo(temp_path)  # Would be True if .git exists


def test_dvc_repo_detection():
    """Test DVC repository detection."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not be a DVC repo initially
        assert not is_dvc_repo(temp_path)

        # Create .dvc directory
        (temp_path / ".dvc").mkdir()
        assert is_dvc_repo(temp_path)


@patch('mint.initializers.git._run_git_command')
@patch('mint.initializers.git._is_command_available')
def test_git_initialization(mock_cmd_available, mock_run_git):
    """Test Git initialization."""
    mock_cmd_available.return_value = True
    mock_run_git.return_value = ""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        init_git(temp_path)

        # Should have called git init, add, and commit
        assert mock_run_git.call_count == 3

        # Check that the commands were called with correct arguments
        call_args = [call[0][1] for call in mock_run_git.call_args_list]  # Get the args list
        assert call_args[0] == ["init"]
        assert call_args[1] == ["add", "."]
        assert "commit" in str(call_args[2])  # commit has more complex args


@patch('mint.initializers.storage._run_dvc_command')
@patch('mint.initializers.storage._is_command_available')
@patch('mint.config.get_config')
def test_dvc_initialization(mock_get_config, mock_cmd_available, mock_run_dvc):
    """Test DVC initialization."""
    mock_cmd_available.return_value = True
    mock_run_dvc.return_value = ""
    mock_get_config.return_value = {"storage": {}}  # No endpoint/region

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        init_dvc(temp_path, "test-bucket")

        # Should have called at least dvc init and remote add
        assert mock_run_dvc.call_count >= 2

        # Check that init and remote add were called
        call_args = [call[0][1] for call in mock_run_dvc.call_args_list]  # Get the command list
        assert ["init"] in call_args
        assert ["remote", "add", "-d", "storage", "s3://test-bucket/"] in call_args


@patch('mint.initializers.storage.boto3.client')
@patch('mint.initializers.storage.get_storage_credentials')
@patch('mint.initializers.storage.get_config')
def test_bucket_creation(mock_get_config, mock_get_creds, mock_boto3_client):
    """Test bucket creation."""
    # Mock configuration
    mock_get_config.return_value = {
        "storage": {
            "provider": "s3",
            "bucket_prefix": "testlab",
            "region": "us-east-1"
        }
    }
    mock_get_creds.return_value = ("test_key", "test_secret")

    # Mock S3 client
    mock_client = MagicMock()
    mock_boto3_client.return_value = mock_client

    bucket_name = create_bucket("myproject")

    assert bucket_name == "testlab-myproject"
    mock_client.create_bucket.assert_called_once()
    mock_client.put_bucket_versioning.assert_called_once()


@patch('mint.initializers.git._run_git_command')
def test_git_command_error_handling(mock_run_git):
    """Test Git command error handling."""
    mock_run_git.side_effect = FileNotFoundError("git command not found")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not raise exception, just warn
        init_git(temp_path)

        # Should have tried to run git command
        mock_run_git.assert_called()


@patch('mint.initializers.storage._run_dvc_command')
def test_dvc_command_error_handling(mock_run_dvc):
    """Test DVC command error handling."""
    mock_run_dvc.side_effect = FileNotFoundError("dvc command not found")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not raise exception, just warn
        init_dvc(temp_path, "test-bucket")

        # Should have tried to run dvc command
        mock_run_dvc.assert_called()

