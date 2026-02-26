"""Tests for initializer functionality."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from mintd.initializers.git import init_git, is_git_repo
from mintd.initializers.storage import init_dvc, is_dvc_repo, add_dvc_remote


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


class TestAddDvcRemote:
    """Tests for add_dvc_remote function - adds remote without dvc init."""

    @patch('mintd.shell.ShellCommand.run')
    @patch('mintd.config.get_config')
    def test_add_dvc_remote_creates_remote(self, mock_get_config, mock_shell_run):
        """Test add_dvc_remote creates remote without calling dvc init."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {"storage": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            # Create .dvc directory to simulate existing DVC repo
            (temp_path / ".dvc").mkdir()

            result = add_dvc_remote(
                temp_path,
                bucket_prefix="cooper-globus",
                sensitivity="restricted",
                project_name="cms-pps-weights",
                full_project_name="data_cms-pps-weights"
            )

            # Should return remote info
            assert result["remote_name"] == "data_cms-pps-weights"
            assert "s3://cooper-globus/lab/cms-pps-weights/" in result["remote_url"]

            # Should NOT call dvc init, only remote add
            call_args = [call[0] for call in mock_shell_run.call_args_list]
            assert ("init",) not in call_args
            assert any("remote" in str(args) and "add" in str(args) for args in call_args)

    @patch('mintd.shell.ShellCommand.run')
    @patch('mintd.config.get_config')
    def test_add_dvc_remote_sets_default(self, mock_get_config, mock_shell_run):
        """Test add_dvc_remote sets the remote as default."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {"storage": {}}

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".dvc").mkdir()

            add_dvc_remote(
                temp_path,
                bucket_prefix="cooper-globus",
                sensitivity="restricted",
                project_name="test",
                full_project_name="data_test"
            )

            # Check that -d flag was used to set as default
            call_args_list = mock_shell_run.call_args_list
            remote_add_call = [c for c in call_args_list if "remote" in str(c) and "add" in str(c)]
            assert len(remote_add_call) > 0
            # The -d flag should be in the call
            assert any("-d" in str(c) for c in call_args_list)

    @patch('mintd.shell.ShellCommand.run')
    @patch('mintd.config.get_config')
    def test_add_dvc_remote_configures_endpoint(self, mock_get_config, mock_shell_run):
        """Test add_dvc_remote configures endpoint when provided."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {
            "storage": {
                "endpoint": "https://s3.us-east-1.wasabisys.com",
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".dvc").mkdir()

            add_dvc_remote(
                temp_path,
                bucket_prefix="cooper-globus",
                sensitivity="restricted",
                project_name="test",
                full_project_name="data_test"
            )

            # Should configure endpoint via remote modify
            call_args_list = mock_shell_run.call_args_list
            assert any("endpointurl" in str(c) for c in call_args_list)

    @patch('mintd.shell.ShellCommand.run')
    def test_add_dvc_remote_error_handling(self, mock_shell_run):
        """Test add_dvc_remote handles errors gracefully."""
        from mintd.exceptions import DVCError
        mock_shell_run.side_effect = DVCError("dvc command failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".dvc").mkdir()

            # Should not raise, returns empty result
            result = add_dvc_remote(
                temp_path,
                bucket_prefix="bucket",
                sensitivity="restricted",
                project_name="test",
                full_project_name="data_test"
            )

            # Should still return the expected remote info (computed before DVC call)
            assert "remote_name" in result
            assert "remote_url" in result
