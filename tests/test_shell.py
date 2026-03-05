"""Tests for shell command execution, including live output streaming."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mintd.shell import CommandType, ShellCommand, dvc_command


class TestRunLive:
    """Tests for ShellCommand.run_live() method."""

    @patch("subprocess.run")
    def test_run_live_does_not_capture_output(self, mock_run):
        """run_live() must pass capture_output=False so output streams to terminal."""
        mock_run.return_value = Mock(returncode=0)
        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))

        cmd.run_live("push", "-r", "my_remote")

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["capture_output"] is False

    @patch("subprocess.run")
    def test_run_live_builds_correct_command(self, mock_run):
        """run_live() should prepend the executable to the arguments."""
        mock_run.return_value = Mock(returncode=0)
        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))

        cmd.run_live("push", "-r", "my_remote")

        call_args = mock_run.call_args[0][0]
        assert call_args == ["dvc", "push", "-r", "my_remote"]

    @patch("subprocess.run")
    def test_run_live_passes_cwd(self, mock_run):
        """run_live() should pass the working directory."""
        mock_run.return_value = Mock(returncode=0)
        project = Path("/tmp/my-project")
        cmd = ShellCommand(CommandType.DVC, cwd=project)

        cmd.run_live("status")

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == project

    @patch("subprocess.run")
    def test_run_live_passes_env(self, mock_run):
        """run_live() should forward env variables."""
        mock_run.return_value = Mock(returncode=0)
        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))
        env = {"AWS_PROFILE": "test"}

        cmd.run_live("push", env=env)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["env"] == env

    @patch("subprocess.run")
    def test_run_live_raises_on_failure(self, mock_run):
        """run_live() should raise DVCError when the command exits non-zero."""
        from mintd.exceptions import DVCError

        mock_run.return_value = Mock(returncode=1)
        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))

        with pytest.raises(DVCError):
            cmd.run_live("push")

    @patch("shutil.which", return_value=None)
    def test_run_live_raises_if_executable_missing(self, mock_which):
        """run_live() should raise CommandNotFoundError when executable is missing."""
        from mintd.exceptions import CommandNotFoundError

        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))

        with pytest.raises(CommandNotFoundError):
            cmd.run_live("push")

    @patch("subprocess.run")
    def test_run_live_returns_completed_process(self, mock_run):
        """run_live() should return the CompletedProcess result."""
        expected = Mock(returncode=0)
        mock_run.return_value = expected
        cmd = ShellCommand(CommandType.DVC, cwd=Path("/tmp"))

        result = cmd.run_live("status")

        assert result is expected

    @patch("subprocess.run")
    def test_run_live_works_with_git(self, mock_run):
        """run_live() should work with any CommandType, not just DVC."""
        mock_run.return_value = Mock(returncode=0)
        cmd = ShellCommand(CommandType.GIT, cwd=Path("/tmp"))

        cmd.run_live("push", "origin", "main")

        call_args = mock_run.call_args[0][0]
        assert call_args == ["git", "push", "origin", "main"]


@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path


def _write_metadata(path, remote_name="data_test"):
    """Helper to create a valid metadata.json with DVC remote config."""
    metadata = {
        "project": {"name": "test", "type": "data", "full_name": "data_test"},
        "storage": {
            "dvc": {
                "remote_name": remote_name,
                "remote_url": "s3://bucket/lab/data_test/",
            }
        },
    }
    with open(path / "metadata.json", "w") as f:
        json.dump(metadata, f)


class TestLiveOutputIntegration:
    """Verify that data commands use run_live() for user-visible progress."""

    @patch("mintd.data_import.dvc_command")
    def test_push_data_uses_run_live(self, mock_dvc_cmd, temp_dir):
        """push_data must call run_live() so DVC progress streams to terminal."""
        from mintd.data_import import push_data

        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc
        _write_metadata(temp_dir)

        push_data(project_path=temp_dir)

        mock_dvc.run_live.assert_called_once()
        call_args = mock_dvc.run_live.call_args[0]
        assert "push" in call_args
        assert "-r" in call_args
        assert "data_test" in call_args
        # Must NOT have called run() instead
        mock_dvc.run.assert_not_called()

    @patch("mintd.data_import.dvc_command")
    def test_run_dvc_import_uses_run_live(self, mock_dvc_cmd, temp_dir):
        """run_dvc_import must call run_live() so DVC progress streams to terminal."""
        from mintd.data_import import run_dvc_import

        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        run_dvc_import(
            project_path=temp_dir,
            repo_url="https://github.com/test/repo",
            source_path="data/final/",
            dest_path="data/import/",
        )

        mock_dvc.run_live.assert_called_once()
        call_args = mock_dvc.run_live.call_args[0]
        assert "import" in call_args
        mock_dvc.run.assert_not_called()

    @patch("mintd.data_import.dvc_command")
    def test_run_dvc_update_uses_run_live(self, mock_dvc_cmd, temp_dir):
        """run_dvc_update must call run_live() so DVC progress streams to terminal."""
        from mintd.data_import import run_dvc_update

        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc
        # Create a fake .dvc file so the existence check passes
        (temp_dir / "data.dvc").touch()

        run_dvc_update(project_path=temp_dir, dvc_file="data.dvc")

        mock_dvc.run_live.assert_called_once()
        call_args = mock_dvc.run_live.call_args[0]
        assert "update" in call_args
        mock_dvc.run.assert_not_called()
