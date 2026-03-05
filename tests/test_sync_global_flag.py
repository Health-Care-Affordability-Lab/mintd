"""Tests for Phase 4: storage.sync_global flag guards --global DVC commands."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from mintd.initializers.storage import init_dvc, add_dvc_remote


class TestSyncGlobalFlagInitDvc:
    """init_dvc should skip --global commands when sync_global is False."""

    @patch("mintd.shell.ShellCommand.run")
    @patch("mintd.initializers.storage.get_config")
    def test_init_dvc_skips_global_when_sync_global_false(
        self, mock_get_config, mock_shell_run
    ):
        """When sync_global is False, no --global DVC commands should be run."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {
            "storage": {
                "endpoint": "https://s3.wasabisys.com",
                "region": "us-east-1",
                "versioning": True,
                "sync_global": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            init_dvc(Path(tmpdir), "bucket", "restricted", "test", "data_test")

        # No call should contain "--global"
        calls = [str(c) for c in mock_shell_run.call_args_list]
        assert not any("--global" in c for c in calls), (
            f"Found --global in calls when sync_global=False: {calls}"
        )

    @patch("mintd.shell.ShellCommand.run")
    @patch("mintd.initializers.storage.get_config")
    def test_init_dvc_includes_global_when_sync_global_true(
        self, mock_get_config, mock_shell_run
    ):
        """When sync_global is True (default), --global DVC commands should run."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {
            "storage": {
                "endpoint": "",
                "versioning": True,
                "sync_global": True,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            init_dvc(Path(tmpdir), "bucket", "restricted", "test", "data_test")

        calls = [str(c) for c in mock_shell_run.call_args_list]
        assert any("--global" in c for c in calls), (
            f"Expected --global in calls when sync_global=True: {calls}"
        )

    @patch("mintd.shell.ShellCommand.run")
    @patch("mintd.initializers.storage.get_config")
    def test_init_dvc_defaults_to_sync_global_true(
        self, mock_get_config, mock_shell_run
    ):
        """When sync_global is absent, default to True (backward compat)."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {
            "storage": {}  # No sync_global key at all
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            init_dvc(Path(tmpdir), "bucket", "restricted", "test", "data_test")

        calls = [str(c) for c in mock_shell_run.call_args_list]
        assert any("--global" in c for c in calls), (
            f"Expected --global by default: {calls}"
        )


class TestSyncGlobalFlagAddDvcRemote:
    """add_dvc_remote should also respect sync_global flag."""

    @patch("mintd.shell.ShellCommand.run")
    @patch("mintd.initializers.storage.get_config")
    def test_add_remote_skips_global_when_sync_global_false(
        self, mock_get_config, mock_shell_run
    ):
        """add_dvc_remote should skip --global when sync_global is False."""
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_get_config.return_value = {
            "storage": {
                "sync_global": False,
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".dvc").mkdir()
            add_dvc_remote(
                Path(tmpdir), "bucket", "restricted", "test", "data_test"
            )

        calls = [str(c) for c in mock_shell_run.call_args_list]
        assert not any("--global" in c for c in calls), (
            f"Found --global when sync_global=False: {calls}"
        )


class TestSyncGlobalFlagUpdateStorage:
    """mintd update storage should respect sync_global flag."""

    @patch("mintd.shell.dvc_command")
    @patch("mintd.config.get_config")
    def test_update_storage_skips_global_when_sync_global_false(
        self, mock_get_config, mock_dvc_command
    ):
        """update storage CLI should skip --global when sync_global is False."""
        from click.testing import CliRunner
        from mintd.cli import main

        mock_get_config.return_value = {
            "storage": {
                "bucket_prefix": "bucket",
                "versioning": True,
                "sync_global": False,
            }
        }
        mock_dvc = MagicMock()
        mock_dvc.run.return_value = MagicMock(stdout="", stderr="", returncode=0)
        mock_dvc_command.return_value = mock_dvc

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            metadata = {
                "project": {
                    "name": "test",
                    "type": "data",
                    "full_name": "data_test",
                },
                "storage": {"sensitivity": "restricted"},
            }
            with open(project_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)
            (project_dir / ".dvc").mkdir()

            runner = CliRunner()
            result = runner.invoke(
                main, ["update", "storage", "--path", str(project_dir), "-y"]
            )

            assert result.exit_code == 0, f"CLI failed: {result.output}"

        calls = [str(c) for c in mock_dvc.run.call_args_list]
        assert not any("--global" in c for c in calls), (
            f"Found --global when sync_global=False: {calls}"
        )
