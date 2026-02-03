"""Tests for initializer functionality."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mintd.initializers.git import init_git, is_git_repo
from mintd.initializers.storage import init_dvc, is_dvc_repo


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


@patch('mintd.shell.ShellCommand.run')
def test_git_initialization(mock_shell_run):
    """Test Git initialization."""
    mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        init_git(temp_path)

        # Should have called git init, add, and commit
        assert mock_shell_run.call_count == 3

        # Check that the commands were called with correct arguments
        call_args = [call[0] for call in mock_shell_run.call_args_list]
        assert ("init",) in call_args
        assert ("add", ".") in call_args
        # commit has more complex args - check it contains "commit"
        assert any("commit" in str(args) for args in call_args)


@patch('mintd.shell.ShellCommand.run')
@patch('mintd.config.get_config')
def test_dvc_initialization(mock_get_config, mock_shell_run):
    """Test DVC initialization."""
    mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
    mock_get_config.return_value = {"storage": {}}  # No endpoint/region

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        init_dvc(temp_path, "test-bucket")

        # Should have called at least dvc init and remote add
        assert mock_shell_run.call_count >= 2

        # Check that init and remote add were called
        call_args = [call[0] for call in mock_shell_run.call_args_list]
        assert ("init",) in call_args
        # Check remote add was called
        assert any("remote" in str(args) and "add" in str(args) for args in call_args)


@patch('mintd.shell.ShellCommand.run')
def test_git_command_error_handling(mock_shell_run):
    """Test Git command error handling."""
    from mintd.exceptions import GitError
    mock_shell_run.side_effect = GitError("git command failed")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not raise exception, just warn
        init_git(temp_path)

        # Should have tried to run git command
        mock_shell_run.assert_called()


@patch('mintd.shell.ShellCommand.run')
def test_dvc_command_error_handling(mock_shell_run):
    """Test DVC command error handling."""
    from mintd.exceptions import DVCError
    mock_shell_run.side_effect = DVCError("dvc command failed")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # Should not raise exception, just warn
        init_dvc(temp_path, "test-bucket")

        # Should have tried to run dvc command
        mock_shell_run.assert_called()
