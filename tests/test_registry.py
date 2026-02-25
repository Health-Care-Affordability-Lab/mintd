"""Tests for registry functionality."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mintd.registry import (
    LocalRegistry,
    get_registry_client,
    load_project_metadata,
    save_pending_registration,
    get_pending_registrations,
    clear_pending_registration,
)


class TestLocalRegistry:
    """Test LocalRegistry functionality."""

    def test_parse_registry_url(self):
        """Test URL parsing extracts org and repo correctly."""
        registry = LocalRegistry("https://github.com/test-org/test-repo")
        assert registry.registry_org == "test-org"
        assert registry.registry_name == "test-repo"

    def test_parse_registry_url_with_git_suffix(self):
        """Test URL parsing with .git suffix."""
        registry = LocalRegistry("https://github.com/test-org/test-repo.git")
        assert registry.registry_org == "test-org"
        assert registry.registry_name == "test-repo.git"

    @patch("mintd.shell.ShellCommand.run")
    def test_clone_registry_success(self, mock_shell_run):
        """Test successful registry cloning."""
        mock_shell_run.return_value = Mock(returncode=0, stdout="", stderr="")

        registry = LocalRegistry("https://github.com/test-org/test-repo")
        repo_path = registry._clone_registry()

        # Should have called git clone
        assert mock_shell_run.call_count == 1

        assert registry.repo_path is not None
        assert registry.temp_dir is not None

    @patch("mintd.shell.ShellCommand.run")
    def test_clone_registry_failure(self, mock_shell_run):
        """Test registry cloning failure."""
        from mintd.exceptions import GitError
        mock_shell_run.side_effect = GitError("Permission denied")

        registry = LocalRegistry("https://github.com/test-org/test-repo")

        with pytest.raises(GitError):
            registry._clone_registry()

    @patch("mintd.shell.ShellCommand.run")
    def test_create_branch_success(self, mock_shell_run):
        """Test successful branch creation."""
        mock_shell_run.return_value = Mock(returncode=0, stdout="", stderr="")

        registry = LocalRegistry("https://github.com/test-org/test-repo")
        registry.repo_path = Path("/tmp/test-repo")

        registry._create_branch("test-branch")

        # Should call checkout -b
        mock_shell_run.assert_called_with("checkout", "-b", "test-branch")

    @patch("mintd.shell.ShellCommand.run")
    def test_create_branch_existing(self, mock_shell_run):
        """Test branch creation when branch already exists."""
        from mintd.exceptions import GitError
        # First call (checkout -b) fails, second (checkout) succeeds
        mock_shell_run.side_effect = [
            GitError("branch exists"),
            Mock(returncode=0, stdout="", stderr="")
        ]

        registry = LocalRegistry("https://github.com/test-org/test-repo")
        registry.repo_path = Path("/tmp/test-repo")

        registry._create_branch("existing-branch")

        assert mock_shell_run.call_count == 2

    @patch("mintd.shell.ShellCommand.run")
    def test_commit_and_push_success(self, mock_shell_run):
        """Test successful commit and push."""
        mock_shell_run.return_value = Mock(returncode=0, stdout="", stderr="")

        registry = LocalRegistry("https://github.com/test-org/test-repo")
        registry.repo_path = Path("/tmp/test-repo")

        registry._commit_and_push("test-branch", "Test commit")

        assert mock_shell_run.call_count == 3  # add, commit, push

    @patch("mintd.shell.ShellCommand.run")
    def test_create_pull_request_success(self, mock_shell_run):
        """Test successful PR creation."""
        mock_shell_run.return_value = Mock(returncode=0, stdout="https://github.com/test-org/test-repo/pull/123", stderr="")

        registry = LocalRegistry("https://github.com/test-org/test-repo")
        registry.repo_path = Path("/tmp/test-repo")

        pr_url = registry._create_pull_request("test-branch", "Test PR", "Test body")

        assert pr_url == "https://github.com/test-org/test-repo/pull/123"
        assert mock_shell_run.call_count == 1


class TestLocalRegistryIntegration:
    """Test LocalRegistry integration functionality."""

    @patch("mintd.registry.LocalRegistry._clone_registry")
    @patch("mintd.registry.LocalRegistry._create_branch")
    @patch("mintd.registry.LocalRegistry._write_catalog_entry")
    @patch("mintd.registry.LocalRegistry._commit_and_push")
    @patch("mintd.registry.LocalRegistry._create_pull_request")
    def test_register_project_full_flow(self, mock_pr, mock_push, mock_write, mock_branch, mock_clone):
        """Test full project registration flow."""
        mock_clone.return_value = Path("/tmp/registry")
        mock_write.return_value = Path("/tmp/registry/catalog/data/test.yaml")
        mock_pr.return_value = "https://github.com/test-org/registry/pull/123"

        registry = LocalRegistry("https://github.com/test-org/registry")

        metadata = {
            "project": {
                "name": "test_project",
                "type": "data",
                "full_name": "data_test_project",
                "status": "active"
            },
            "metadata": {
                "version": "1.0.0",
                "mint_version": "0.1.0"
            },
            "ownership": {
                "created_by": "user@example.com"
            },
            "header": {},
            "access_control": {
                "teams": [{"name": "admins", "permission": "admin"}]
            },
            "status": {
                "state": "active"
            },
            "repository": {
                "github_url": "https://github.com/test-org/data_test_project"
            }
        }

        pr_url = registry.register_project(metadata)

        assert pr_url == "https://github.com/test-org/registry/pull/123"
        mock_clone.assert_called_once()
        mock_branch.assert_called_once_with("register-test_project")
        mock_write.assert_called_once()
        mock_push.assert_called_once()
        mock_pr.assert_called_once()

    @patch("mintd.shell.ShellCommand.run")
    def test_check_registration_status_registered(self, mock_shell_run):
        """Test checking status of registered project."""
        # Mock shell command (git clone and gh pr list)
        mock_shell_run.return_value = Mock(returncode=0, stdout='[]', stderr="")

        registry = LocalRegistry("https://github.com/test-org/registry")

        # Mock the catalog file existing
        with patch.object(Path, 'exists', return_value=True):
            status = registry.check_registration_status("test_project")

        assert status["registered"] is True
        assert status["type"] == "data"
        assert "test_project" in status["full_name"]

    @patch("mintd.shell.ShellCommand.run")
    def test_check_registration_status_pending_pr(self, mock_shell_run):
        """Test checking status of project with pending PR."""
        # Mock gh pr list (with pending PR)
        mock_shell_run.return_value = Mock(returncode=0, stdout=json.dumps([
            {
                "title": "Register data project: test_project",
                "url": "https://github.com/test-org/registry/pull/123",
                "headRefName": "register-test_project"
            }
        ]), stderr="")

        registry = LocalRegistry("https://github.com/test-org/registry")

        # Mock the catalog file not existing
        with patch.object(Path, 'exists', return_value=False):
            status = registry.check_registration_status("test_project")

        assert status["registered"] is False
        assert status["pending_pr"] == "https://github.com/test-org/registry/pull/123"

    @patch("mintd.shell.ShellCommand.run")
    def test_check_registration_status_not_found(self, mock_shell_run):
        """Test checking status of non-existent project."""
        # Mock shell command (git clone and gh pr list - no PRs)
        mock_shell_run.return_value = Mock(returncode=0, stdout='[]', stderr="")

        registry = LocalRegistry("https://github.com/test-org/registry")

        # Mock the catalog file not existing
        with patch.object(Path, 'exists', return_value=False):
            status = registry.check_registration_status("nonexistent_project")

        assert status["registered"] is False
        assert status["status"] == "not_found"


class TestRegistryClientFactory:
    """Test registry client factory functions."""

    @patch("mintd.config.get_config")
    def test_get_registry_client(self, mock_get_config):
        """Test getting registry client instance."""
        mock_get_config.return_value = {
            "registry": {"url": "https://github.com/test-org/test-repo"}
        }

        client = get_registry_client()

        assert isinstance(client, LocalRegistry)
        assert client.registry_url == "https://github.com/test-org/test-repo"


class TestCatalogEntryGeneration:
    """Test catalog entry generation for LocalRegistry."""

    @patch.dict(os.environ, {'USER': 'testuser'})
    def test_generate_catalog_entry_data_project(self):
        """Test generating catalog entry for data project."""
        registry = LocalRegistry("https://github.com/test-org/registry")

        metadata = {
            "project": {
                "name": "test_data",
                "type": "data",
                "full_name": "data_test_data"
            },
            "ownership": {
                "created_by": "user@example.com"
            },
            "metadata": {
                "description": "Test data project",
                "tags": ["test"]
            },
            "status": {
                "state": "active"
            },
            "schema_version": "1.0"
        }

        entry, project_name = registry._generate_catalog_entry(metadata)

        assert entry["schema_version"] == "1.0"
        assert entry["project"]["name"] == "test_data"
        assert entry["project"]["type"] == "data"
        assert entry["project"]["full_name"] == "data_test_data"
        assert entry["metadata"]["description"] == "Test data project"
        assert "test" in entry["metadata"]["tags"]
        assert entry["ownership"]["created_by"] == "user@example.com"
        assert "storage" in entry  # Data projects should have storage
        assert entry["storage"]["dvc"]["bucket"] == "lab-data"

    def test_generate_catalog_entry_project(self):
        """Test generating catalog entry for analysis project."""
        registry = LocalRegistry("https://github.com/test-org/registry")

        metadata = {
            "project": {
                "name": "analysis_1",
                "type": "project",
                "full_name": "prj_analysis_1",
                "status": "active"
            },
            "ownership": {
                "created_by": "user@example.com"
            },
            "status": {
                "state": "active"
            },
            "metadata": {
                "version": "1.0.0"
            }
        }

        entry, project_name = registry._generate_catalog_entry(metadata)

        assert entry["project"]["full_name"] == "prj_analysis_1"
        assert "storage" in entry  # Project types should have storage
        assert entry["storage"]["dvc"]["bucket"] == "lab-projects"
        assert "data_dependencies" in entry["metadata"]

    def test_write_catalog_entry(self, tmp_path):
        """Test writing catalog entry to file system."""
        registry = LocalRegistry("https://github.com/test-org/registry")
        registry.repo_path = tmp_path

        entry = {
            "project": {"type": "data"},
            "schema_version": "1.0"
        }

        file_path = registry._write_catalog_entry(entry, "test_project")

        assert file_path.exists()
        assert file_path.name == "test_project.yaml"

        # Verify content
        with open(file_path) as f:
            content = f.read()
            assert "schema_version:" in content
            assert "project:" in content


class TestProjectMetadata:
    """Test project metadata loading."""

    def test_load_project_metadata_success(self):
        """Test successful metadata loading."""
        metadata = {
            "schema_version": "1.0",
            "methodology": "Test methodology",
            "project": {
                "name": "test_project",
                "type": "data",
                "full_name": "data_test_project"
            },
            "metadata": {
                "version": "1.0.0",
                "mint_version": "0.1.0"
            },
            "ownership": {
                "created_by": "user@example.com",
                "created_at": "2025-01-15T10:30:00Z",
                "maintainers": []
            },
            "access_control": {
                "teams": [{"name": "admins", "permission": "admin"}]
            },
            "status": {
                "state": "active"
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "test_project"
            project_path.mkdir()

            metadata_file = project_path / "metadata.json"
            with open(metadata_file, "w") as f:
                json.dump(metadata, f)

            loaded = load_project_metadata(project_path)

            assert loaded["project"]["name"] == "test_project"
            assert loaded["project"]["type"] == "data"

    def test_load_project_metadata_missing_file(self):
        """Test loading metadata from non-existent file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "missing_project"

            with pytest.raises(FileNotFoundError, match="metadata.json not found"):
                load_project_metadata(project_path)

    def test_load_project_metadata_invalid_json(self):
        """Test loading invalid JSON metadata."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "test_project"
            project_path.mkdir()

            metadata_file = project_path / "metadata.json"
            with open(metadata_file, "w") as f:
                f.write("invalid json content")

            with pytest.raises(ValueError):
                load_project_metadata(project_path)


class TestPendingRegistrations:
    """Test pending registration functionality."""

    @patch("pathlib.Path.home")
    def test_save_and_get_pending_registrations(self, mock_home):
        """Test saving and retrieving pending registrations."""
        temp_dir = Path(tempfile.gettempdir()) / ".mint_test"
        temp_dir.mkdir(exist_ok=True)
        mock_home.return_value = temp_dir

        metadata = {
            "project": {
                "full_name": "data_test_project",
                "name": "test_project",
                "type": "data"
            },
            "ownership": {
                "created_by": "user@example.com"
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "test_project"
            project_path.mkdir()

            # Save pending registration
            save_pending_registration(project_path, metadata)

            # Retrieve pending registrations
            pending = get_pending_registrations()

            assert len(pending) == 1
            assert pending[0]["metadata"]["project"]["full_name"] == "data_test_project"
            assert pending[0]["project_path"] == str(project_path)

    @patch("pathlib.Path.home")
    def test_clear_pending_registration(self, mock_home):
        """Test clearing pending registrations."""
        temp_dir = Path(tempfile.gettempdir()) / ".mint_test"
        temp_dir.mkdir(exist_ok=True)
        mock_home.return_value = temp_dir

        metadata = {
            "project": {
                "full_name": "data_test_project",
                "name": "test_project",
                "type": "data"
            },
            "ownership": {
                "created_by": "user@example.com"
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "test_project"
            project_path.mkdir()

            # Save pending registration
            save_pending_registration(project_path, metadata)

            # Verify it was saved
            pending = get_pending_registrations()
            assert len(pending) == 1

            # Clear it
            clear_pending_registration("data_test_project")

            # Verify it was removed
            pending = get_pending_registrations()
            assert len(pending) == 0

    @patch("pathlib.Path.home")
    def test_get_pending_registrations_empty(self, mock_home):
        """Test getting pending registrations when none exist."""
        temp_dir = Path(tempfile.gettempdir()) / ".mint_test_empty"
        temp_dir.mkdir(exist_ok=True)
        mock_home.return_value = temp_dir

        pending = get_pending_registrations()
        assert pending == []