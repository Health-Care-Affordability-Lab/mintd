"""Tests for Phase 5: mintd check consistency validation subcommand."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from mintd.cli import main


@pytest.fixture
def consistent_project():
    """Create a project dir where metadata.json and .dvc/config agree."""
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
                    "remote_url": "s3://bucket/lab/data_test-project/",
                },
            },
        }
        with open(project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # Create .dvc/config that matches
        dvc_dir = project_dir / ".dvc"
        dvc_dir.mkdir()
        dvc_config = dvc_dir / "config"
        dvc_config.write_text(
            "[core]\n"
            "    remote = data_test-project\n"
            '[remote "data_test-project"]\n'
            "    url = s3://bucket/lab/data_test-project/\n"
        )

        yield project_dir


@pytest.fixture
def drifted_project():
    """Create a project dir where metadata.json and .dvc/config disagree."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        # metadata.json has OLD url
        metadata = {
            "project": {
                "name": "test-project",
                "type": "data",
                "full_name": "data_test-project",
            },
            "storage": {
                "dvc": {
                    "remote_name": "data_test-project",
                    "remote_url": "s3://old-bucket/lab/data_test-project/",
                },
            },
        }
        with open(project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        # .dvc/config has NEW url
        dvc_dir = project_dir / ".dvc"
        dvc_dir.mkdir()
        dvc_config = dvc_dir / "config"
        dvc_config.write_text(
            "[core]\n"
            "    remote = data_test-project\n"
            '[remote "data_test-project"]\n'
            "    url = s3://new-bucket/lab/data_test-project/\n"
        )

        yield project_dir


class TestCheckCommand:
    """mintd check should validate consistency between metadata.json and .dvc/config."""

    def test_check_command_exists(self):
        """The check subcommand should be registered."""
        runner = CliRunner()
        result = runner.invoke(main, ["check", "--help"])
        assert result.exit_code == 0
        assert "consistency" in result.output.lower() or "validate" in result.output.lower()

    def test_check_consistent_project_passes(self, consistent_project):
        """A consistent project should show no warnings."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["check", "--path", str(consistent_project)]
        )

        assert result.exit_code == 0
        # Should not contain "mismatch" or "warning"
        output_lower = result.output.lower()
        assert "mismatch" not in output_lower

    def test_check_drifted_project_warns(self, drifted_project):
        """A drifted project should report a remote_url mismatch."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["check", "--path", str(drifted_project)]
        )

        # Should still exit 0 (read-only, advisory)
        assert result.exit_code == 0
        # But output should mention the mismatch
        assert "mismatch" in result.output.lower() or "differ" in result.output.lower()

    def test_check_no_metadata(self):
        """Should error if metadata.json is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            result = runner.invoke(main, ["check", "--path", tmpdir])

            assert result.exit_code != 0

    def test_check_no_dvc(self):
        """Should warn if .dvc is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            metadata = {
                "project": {"name": "test", "type": "data", "full_name": "data_test"},
                "storage": {"dvc": {"remote_name": "x", "remote_url": "s3://x/"}},
            }
            with open(project_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

            runner = CliRunner()
            result = runner.invoke(main, ["check", "--path", tmpdir])

            assert result.exit_code == 0
            assert "not initialized" in result.output.lower() or "dvc" in result.output.lower()

    def test_check_is_read_only(self, consistent_project):
        """check command should never modify any files."""
        # Record file contents before
        meta_before = (consistent_project / "metadata.json").read_text()
        dvc_before = (consistent_project / ".dvc" / "config").read_text()

        runner = CliRunner()
        runner.invoke(main, ["check", "--path", str(consistent_project)])

        # Verify nothing changed
        assert (consistent_project / "metadata.json").read_text() == meta_before
        assert (consistent_project / ".dvc" / "config").read_text() == dvc_before
