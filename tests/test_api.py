"""Tests for API functionality."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from mintd.api import (
    create_project,
    ProjectResult,
    _init_git,
    _init_dvc,
    _update_metadata_with_dvc_info,
    _register_project,
)
from mintd.initializers.storage import add_dvc_remote


def test_create_project_data():
    """Test creating a data project."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="data",
            name="test_api",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False
        )

        assert result.name == "test_api"
        assert result.full_name == "data_test_api"
        assert result.project_type == "data"
        assert result.path.exists()
        assert (result.path / "README.md").exists()
        assert (result.path / "metadata.json").exists()


def test_create_project_project():
    """Test creating a project."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="project",
            name="test_api",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False
        )

        assert result.name == "test_api"
        assert result.full_name == "prj_test_api"
        assert result.project_type == "project"
        assert result.path.exists()
        # AEA-compliant structure with config at code/ level
        assert (result.path / "code" / "02_analysis" / "__init__.py").exists()
        assert (result.path / "code" / "config.py").exists()
        assert (result.path / "code" / "_mintd_utils.py").exists()
        assert (result.path / "run_all.py").exists()
        assert (result.path / "citations.md").exists()
        assert (result.path / "data" / "analysis").exists()
        assert (result.path / "results" / "estimates").exists()


def test_create_project_infra():
    """Test creating an infra project."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="infra",
            name="test_api",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False
        )

        assert result.name == "test_api"
        assert result.full_name == "infra_test_api"
        assert result.project_type == "infra"
        assert result.path.exists()
        assert (result.path / "code" / "test_api" / "__init__.py").exists()


def test_create_project_invalid_type():
    """Test creating a project with invalid type."""
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            create_project(
                project_type="invalid",
                name="test",
                language="python",
                path=temp_dir,
                init_git=False,
                init_dvc=False
            )
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unknown project type" in str(e)


def test_create_project_invalid_name():
    """Test creating a project with invalid name."""
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            create_project(
                project_type="data",
                language="python",
                name="test project",  # Invalid name with space
                path=temp_dir,
                init_git=False,
                init_dvc=False
            )
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Invalid project name" in str(e)


def test_create_project_enclave():
    """Test creating an enclave project."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="enclave",
            name="test_enclave",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False,
            registry_url="https://github.com/test/registry"
        )

        assert result.name == "test_enclave"
        assert result.full_name == "enclave_test_enclave"
        assert result.project_type == "enclave"
        assert result.path.exists()
        assert (result.path / "enclave_manifest.yaml").exists()


def test_create_project_prj_alias():
    """Test creating a project using 'prj' alias."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="prj",
            name="test_prj_alias",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False
        )

        assert result.project_type == "project"  # Should be normalized
        assert result.full_name == "prj_test_prj_alias"


def test_create_project_with_classification_public():
    """Test creating a project with public classification."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="data",
            name="public_data",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False,
            classification="public"
        )

        assert result.path.exists()


def test_create_project_with_classification_contract():
    """Test creating a project with contract classification."""
    with tempfile.TemporaryDirectory() as temp_dir:
        result = create_project(
            project_type="data",
            name="contract_data",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False,
            classification="contract",
            contract_slug="cms-2024",
            contract_info="CMS contract for 2024"
        )

        assert result.path.exists()


def test_create_project_use_current_repo_not_in_git():
    """Test error when using --use-current-repo outside git repo."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError, match="not in a git repository"):
            create_project(
                project_type="data",
                name="test",
                language="python",
                path=temp_dir,
                init_git=False,
                init_dvc=False,
                use_current_repo=True
            )


@patch("mintd.api.is_git_repo")
def test_create_project_use_current_repo_with_conflicts(mock_is_git_repo):
    """Test warning when using --use-current-repo with conflicting files."""
    mock_is_git_repo.return_value = True

    with tempfile.TemporaryDirectory() as temp_dir:
        # Create conflicting files
        (Path(temp_dir) / "README.md").write_text("Existing README")
        (Path(temp_dir) / "metadata.json").write_text("{}")

        # Should not raise, but will print warning
        result = create_project(
            project_type="data",
            name="test",
            language="python",
            path=temp_dir,
            init_git=False,
            init_dvc=False,
            use_current_repo=True
        )

        assert result.path.exists()


class TestInitGit:
    """Tests for _init_git function."""

    @patch("mintd.api.is_git_repo")
    @patch("mintd.api.init_git")
    def test_init_git_new_repo(self, mock_init_git, mock_is_git_repo):
        """Test initializing git in a new directory."""
        mock_is_git_repo.return_value = False

        with tempfile.TemporaryDirectory() as temp_dir:
            _init_git(Path(temp_dir), use_current_repo=False)

        mock_init_git.assert_called_once()

    @patch("mintd.api.is_git_repo")
    @patch("mintd.shell.ShellCommand.run")
    def test_init_git_use_current_repo(self, mock_shell_run, mock_is_git_repo):
        """Test using git in existing repo."""
        mock_is_git_repo.return_value = True
        mock_shell_run.return_value = MagicMock(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            _init_git(Path(temp_dir), use_current_repo=True)

        # Should attempt to add and commit via ShellCommand
        assert mock_shell_run.call_count >= 1


class TestInitDvc:
    """Tests for _init_dvc function."""

    @patch("mintd.api.is_dvc_repo")
    @patch("mintd.api.init_dvc")
    def test_init_dvc_with_bucket(self, mock_init_dvc, mock_is_dvc_repo):
        """Test initializing DVC with bucket name."""
        mock_is_dvc_repo.return_value = False
        mock_init_dvc.return_value = {"remote_name": "s3", "remote_url": "s3://test-bucket/data_test"}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _init_dvc(Path(temp_dir), "custom-bucket", "restricted", "test", "data_test")

        mock_init_dvc.assert_called_once()

    @patch("mintd.api.is_dvc_repo")
    @patch("mintd.config.get_config")
    def test_init_dvc_no_bucket_configured(self, mock_config, mock_is_dvc_repo):
        """Test DVC init skipped when no bucket configured."""
        mock_is_dvc_repo.return_value = False
        mock_config.return_value = {"storage": {"bucket_prefix": ""}}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _init_dvc(Path(temp_dir))

        assert result == {"remote_name": "", "remote_url": ""}

    @patch("mintd.api.add_dvc_remote")
    @patch("mintd.api.is_dvc_repo")
    def test_init_dvc_already_initialized_adds_remote(self, mock_is_dvc_repo, mock_add_remote):
        """Test DVC adds remote when already initialized (fix for --use-current-repo)."""
        mock_is_dvc_repo.return_value = True
        mock_add_remote.return_value = {
            "remote_name": "data_test",
            "remote_url": "s3://bucket/lab/test/"
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _init_dvc(Path(temp_dir), "bucket", "restricted", "test", "data_test")

        # Should call add_dvc_remote for existing DVC repos
        mock_add_remote.assert_called_once()
        assert result["remote_name"] == "data_test"
        assert result["remote_url"] == "s3://bucket/lab/test/"


class TestUpdateMetadataWithDvcInfo:
    """Tests for _update_metadata_with_dvc_info function."""

    def test_update_metadata_no_file(self):
        """Test update when metadata.json doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Should not raise
            _update_metadata_with_dvc_info(
                Path(temp_dir),
                {"remote_name": "s3", "remote_url": "s3://bucket/path"}
            )

    def test_update_metadata_success(self):
        """Test successful metadata update."""
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata_path = Path(temp_dir) / "metadata.json"
            metadata_path.write_text('{"project": {"name": "test"}}')

            _update_metadata_with_dvc_info(
                Path(temp_dir),
                {"remote_name": "s3", "remote_url": "s3://bucket/path"}
            )

            with open(metadata_path) as f:
                updated = json.load(f)

            assert "storage" in updated
            assert updated["storage"]["dvc"]["remote_name"] == "s3"

    def test_update_metadata_with_existing_storage(self):
        """Test update when storage section already exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata_path = Path(temp_dir) / "metadata.json"
            metadata_path.write_text('{"project": {"name": "test"}, "storage": {"other": "data"}}')

            _update_metadata_with_dvc_info(
                Path(temp_dir),
                {"remote_name": "s3", "remote_url": "s3://bucket/path"}
            )

            with open(metadata_path) as f:
                updated = json.load(f)

            assert updated["storage"]["dvc"]["remote_url"] == "s3://bucket/path"


class TestRegisterProject:
    """Tests for _register_project function."""

    @patch("mintd.registry.get_registry_client")
    @patch("mintd.registry.load_project_metadata")
    def test_register_project_success(self, mock_load_metadata, mock_get_client):
        """Test successful project registration."""
        mock_load_metadata.return_value = {"project": {"name": "test"}}
        mock_client = MagicMock()
        mock_client.register_project.return_value = "https://github.com/org/registry/pull/1"
        mock_get_client.return_value = mock_client

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _register_project(Path(temp_dir))

        assert result == "https://github.com/org/registry/pull/1"

    @patch("mintd.registry.save_pending_registration")
    @patch("mintd.registry.load_project_metadata")
    @patch("mintd.registry.get_registry_client")
    def test_register_project_failure(self, mock_get_client, mock_load_metadata, mock_save_pending):
        """Test project registration failure saves pending."""
        mock_load_metadata.return_value = {"project": {"name": "test"}}
        mock_get_client.side_effect = Exception("Registration failed")

        with tempfile.TemporaryDirectory() as temp_dir:
            result = _register_project(Path(temp_dir))

        assert result is None
        mock_save_pending.assert_called_once()


class TestProjectResult:
    """Tests for ProjectResult dataclass."""

    def test_project_result_creation(self):
        """Test creating a ProjectResult."""
        result = ProjectResult(
            name="test",
            full_name="data_test",
            project_type="data",
            path=Path("/tmp/test")
        )

        assert result.name == "test"
        assert result.registration_url is None

    def test_project_result_with_registration(self):
        """Test ProjectResult with registration URL."""
        result = ProjectResult(
            name="test",
            full_name="data_test",
            project_type="data",
            path=Path("/tmp/test"),
            registration_url="https://github.com/org/registry/pull/1"
        )

        assert result.registration_url == "https://github.com/org/registry/pull/1"

