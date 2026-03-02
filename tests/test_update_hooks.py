"""Tests for mintd update hooks command."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from mintd.cli import main


@pytest.fixture
def mock_project_dir():
    """Create a mock project directory with metadata.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        metadata = {
            "project": {
                "name": "test_project",
                "type": "data",
                "full_name": "data_test_project"
            },
            "language": "python",
            "storage": {"provider": "s3"},
            "mint": {"version": "0.1.0", "commit_hash": "abc123"}
        }

        with open(project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        yield project_dir


@pytest.fixture
def stata_project_dir():
    """Create a mock Stata project directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)

        metadata = {
            "project": {
                "name": "stata_project",
                "type": "data",
                "full_name": "data_stata_project"
            },
            "language": "stata",
        }

        with open(project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        yield project_dir


class TestUpdateHooksCommand:
    """Tests for mintd update hooks command."""

    def test_update_hooks_help(self):
        """Test update hooks command help."""
        runner = CliRunner()
        result = runner.invoke(main, ["update", "hooks", "--help"])
        assert result.exit_code == 0
        assert "pre-commit" in result.output.lower()

    def test_update_hooks_creates_all_files(self, mock_project_dir):
        """Test that hooks command creates all required files."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "update", "hooks",
            "--path", str(mock_project_dir)
        ])

        assert result.exit_code == 0

        # Check all files were created
        assert (mock_project_dir / ".pre-commit-config.yaml").exists()
        assert (mock_project_dir / "scripts" / "check-dvc-sync.sh").exists()
        assert (mock_project_dir / "scripts" / "check-env-lockfiles.sh").exists()

    def test_update_hooks_scripts_executable(self, mock_project_dir):
        """Test that generated scripts are executable."""
        runner = CliRunner()
        runner.invoke(main, ["update", "hooks", "--path", str(mock_project_dir)])

        dvc_script = mock_project_dir / "scripts" / "check-dvc-sync.sh"
        env_script = mock_project_dir / "scripts" / "check-env-lockfiles.sh"

        # Check executable bit is set
        assert os.access(dvc_script, os.X_OK)
        assert os.access(env_script, os.X_OK)

    def test_update_hooks_template_rendering(self, mock_project_dir):
        """Test that templates are rendered with correct variables."""
        runner = CliRunner()
        runner.invoke(main, ["update", "hooks", "--path", str(mock_project_dir)])

        # Check check-env-lockfiles.sh contains project_name
        env_script = mock_project_dir / "scripts" / "check-env-lockfiles.sh"
        content = env_script.read_text()
        assert "test_project" in content  # project_name should be in header

        # Check check-dvc-sync.sh has correct source_dir for Python
        dvc_script = mock_project_dir / "scripts" / "check-dvc-sync.sh"
        content = dvc_script.read_text()
        assert "src/" in content  # Python uses src/

    def test_update_hooks_stata_source_dir(self, stata_project_dir):
        """Test that Stata projects use 'code' as source directory."""
        runner = CliRunner()
        runner.invoke(main, ["update", "hooks", "--path", str(stata_project_dir)])

        dvc_script = stata_project_dir / "scripts" / "check-dvc-sync.sh"
        content = dvc_script.read_text()
        assert "code/" in content  # Stata uses code/

    def test_update_hooks_no_metadata(self):
        """Test error when metadata.json doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = CliRunner()
            result = runner.invoke(main, ["update", "hooks", "--path", tmpdir])

            assert result.exit_code != 0
            assert "metadata.json" in result.output.lower()

    def test_update_hooks_existing_config_no_force(self, mock_project_dir):
        """Test that existing config blocks without --force."""
        # Create existing pre-commit config
        (mock_project_dir / ".pre-commit-config.yaml").write_text("existing: config")

        runner = CliRunner()
        result = runner.invoke(main, ["update", "hooks", "--path", str(mock_project_dir)])

        assert result.exit_code != 0
        assert "force" in result.output.lower()

    def test_update_hooks_force_overwrites(self, mock_project_dir):
        """Test that --force overwrites existing config."""
        # Create existing pre-commit config
        (mock_project_dir / ".pre-commit-config.yaml").write_text("existing: config")

        runner = CliRunner()
        result = runner.invoke(main, [
            "update", "hooks",
            "--path", str(mock_project_dir),
            "--force"
        ])

        assert result.exit_code == 0

        # Check content was replaced
        content = (mock_project_dir / ".pre-commit-config.yaml").read_text()
        assert "existing: config" not in content
        assert "check-dvc-sync" in content

    def test_update_hooks_precommit_config_content(self, mock_project_dir):
        """Test that pre-commit config references both hooks."""
        runner = CliRunner()
        runner.invoke(main, ["update", "hooks", "--path", str(mock_project_dir)])

        config = (mock_project_dir / ".pre-commit-config.yaml").read_text()

        # Both hook scripts should be referenced
        assert "check-dvc-sync.sh" in config
        assert "check-env-lockfiles.sh" in config


class TestUpdateHooksEdgeCases:
    """Edge case tests for hooks command."""

    def test_hooks_with_missing_language_defaults_python(self):
        """Test default language is python when not specified."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # metadata without language key
            metadata = {
                "project": {"name": "test", "type": "data", "full_name": "data_test"}
            }
            with open(project_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

            runner = CliRunner()
            result = runner.invoke(main, ["update", "hooks", "--path", str(project_dir)])

            assert result.exit_code == 0

            # Check it defaulted to src/ (Python default)
            dvc_script = project_dir / "scripts" / "check-dvc-sync.sh"
            assert "src/" in dvc_script.read_text()

    def test_hooks_creates_scripts_directory(self):
        """Test scripts directory is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            metadata = {
                "project": {"name": "test", "type": "data", "full_name": "data_test"}
            }
            with open(project_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

            # Ensure scripts dir doesn't exist
            scripts_dir = project_dir / "scripts"
            assert not scripts_dir.exists()

            runner = CliRunner()
            runner.invoke(main, ["update", "hooks", "--path", str(project_dir)])

            assert scripts_dir.exists()

    def test_hooks_with_missing_project_name_defaults(self):
        """Test default project_name when not specified in metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)

            # metadata without project.name
            metadata = {
                "project": {"type": "data"},
                "language": "python"
            }
            with open(project_dir / "metadata.json", "w") as f:
                json.dump(metadata, f)

            runner = CliRunner()
            result = runner.invoke(main, ["update", "hooks", "--path", str(project_dir)])

            assert result.exit_code == 0

            # Check it used default project name
            env_script = project_dir / "scripts" / "check-env-lockfiles.sh"
            assert "project" in env_script.read_text()
