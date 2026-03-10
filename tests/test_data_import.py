"""Tests for data import functionality."""

import pytest
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from click.testing import CliRunner

from mintd.data_import import (
    ImportTransaction, ImportResult, UpdateResult, RemoveResult, DataImportError,
    RegistryError, DVCImportError, DVCUpdateError, DependencyNotFoundError,
    DependencyRemovalError,
    query_data_product, validate_project_directory,
    run_dvc_import, run_dvc_update, update_project_metadata, update_dependency_metadata,
    update_single_import, update_all_imports,
    import_data_product, pull_data_product, push_data, get_project_remote,
    list_data_products,
    remove_dependency_from_metadata, check_dvc_yaml_references, remove_data_import,
    list_remote_data_paths, validate_source_path, prompt_stage_selection,
    list_remote_dvc_files,
)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_project_dir(temp_dir):
    """Create a mock project directory with metadata.json."""
    project_dir = temp_dir / "test_project"
    project_dir.mkdir()

    metadata = {
        "project": {
            "name": "test_project",
            "type": "project",
            "full_name": "prj__test_project"
        },
        "metadata": {},
        "ownership": {"created_by": "test"},
        "access_control": {"teams": [{"name": "test", "permission": "admin"}]},
        "status": {"lifecycle": "active"}
    }

    with open(project_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    return project_dir


@pytest.fixture
def mock_data_product():
    """Mock data product catalog entry."""
    return {
        "schema_version": "1.0",
        "mint": {"version": "0.1.0", "commit_hash": "abc123"},
        "project": {
            "name": "cms-provider-data-service",
            "type": "data",
            "full_name": "data_cms-provider-data-service",
            "created_at": "2025-01-01T00:00:00Z",
            "created_by": "test"
        },
        "metadata": {"description": "Test data product"},
        "ownership": {"created_by": "test"},
        "repository": {
            "github_url": "https://github.com/test/data_cms-provider-data-service"
        },
        "storage": {
            "provider": "s3",
            "bucket": "test-bucket"
        },
        "access_control": {"teams": [{"name": "test", "permission": "read"}]},
        "status": {"lifecycle": "active"}
    }


# =============================================================================
# ImportTransaction Tests
# =============================================================================

class TestImportTransaction:

    def test_init(self, temp_dir):
        """Test ImportTransaction initialization."""
        transaction = ImportTransaction(temp_dir)
        assert transaction.project_path == temp_dir
        assert transaction.completed == []
        assert transaction.failed == []
        assert len(transaction.rollback_actions) == 0

    def test_add_success(self, temp_dir):
        """Test adding successful import."""
        transaction = ImportTransaction(temp_dir)
        result = ImportResult(
            product_name="test_product",
            success=True,
            dvc_file="test.dvc",
            local_path="data/test/"
        )

        transaction.add_success(result)
        assert len(transaction.completed) == 1
        assert transaction.completed[0]["product_name"] == "test_product"

    def test_add_failure(self, temp_dir):
        """Test adding failed import."""
        transaction = ImportTransaction(temp_dir)
        result = ImportResult(
            product_name="test_product",
            success=False,
            error_message="Network error"
        )

        transaction.add_failure(result)
        assert len(transaction.failed) == 1
        assert transaction.failed[0]["error_message"] == "Network error"

    def test_save_and_load_state(self, temp_dir):
        """Test saving and loading transaction state."""
        transaction = ImportTransaction(temp_dir)

        # Add some data
        transaction.add_success(ImportResult("prod1", True, "prod1.dvc"))
        transaction.add_failure(ImportResult("prod2", False, error_message="error"))

        # Save state
        transaction.save_state()
        assert transaction.state_file.exists()

        # Create new transaction and load state
        new_transaction = ImportTransaction(temp_dir)
        assert new_transaction.load_state()

        assert len(new_transaction.completed) == 1
        assert len(new_transaction.failed) == 1
        assert new_transaction.completed[0]["product_name"] == "prod1"

    def test_cleanup_state(self, temp_dir):
        """Test cleaning up state file."""
        transaction = ImportTransaction(temp_dir)
        transaction.save_state()
        assert transaction.state_file.exists()

        transaction.cleanup_state()
        assert not transaction.state_file.exists()


# =============================================================================
# Registry Query Tests
# =============================================================================

class TestRegistryQuery:

    @patch('mintd.data_import.get_registry_client')
    def test_query_data_product_success(self, mock_get_client, mock_data_product):
        """Test successful data product query."""
        mock_client = Mock()
        mock_client.query_data_product.return_value = mock_data_product
        mock_get_client.return_value = mock_client

        result = query_data_product("test_product")

        assert result == mock_data_product
        mock_client.query_data_product.assert_called_once_with("test_product")

    @patch('mintd.data_import.get_registry_client')
    def test_query_data_product_registry_error(self, mock_get_client):
        """Test registry error handling."""
        mock_client = Mock()
        mock_client.query_data_product.side_effect = Exception("Network error")
        mock_get_client.return_value = mock_client

        with pytest.raises(RegistryError, match="Failed to query registry"):
            query_data_product("test_product")


# =============================================================================
# Project Validation Tests
# =============================================================================

class TestProjectValidation:

    def test_validate_project_directory_valid_project(self, mock_project_dir):
        """Test validation of valid project directory."""
        # Should not raise exception
        validate_project_directory(mock_project_dir)

    def test_validate_project_directory_no_metadata(self, temp_dir):
        """Test validation fails without metadata.json."""
        with pytest.raises(DataImportError, match="Not a mintd project directory"):
            validate_project_directory(temp_dir)

    def test_validate_project_directory_valid_data_type(self, temp_dir):
        """Test validation passes for data project type."""
        metadata = {
            "project": {
                "name": "test_data",
                "type": "data",
                "full_name": "data_test_data"
            },
            "metadata": {},
            "ownership": {},
            "access_control": {"teams": [{"name": "test", "permission": "admin"}]},
            "status": {}
        }

        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Should not raise exception
        validate_project_directory(temp_dir)

    def test_validate_project_directory_invalid_type(self, temp_dir):
        """Test validation fails for unsupported project type."""
        metadata = {
            "project": {
                "name": "test_project",
                "type": "code",
                "full_name": "test_project"
            },
            "metadata": {},
            "ownership": {},
            "access_control": {"teams": [{"name": "test", "permission": "admin"}]},
            "status": {}
        }

        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        with pytest.raises(DataImportError, match="Data import only supported"):
            validate_project_directory(temp_dir)


# =============================================================================
# DVC Import Tests
# =============================================================================

class TestDVCImport:

    @patch('subprocess.run')
    def test_run_dvc_import_success(self, mock_run, temp_dir):
        """Test successful DVC import."""
        mock_run.return_value = Mock(returncode=0, stdout="")

        result = run_dvc_import(
            project_path=temp_dir,
            repo_url="https://github.com/test/repo",
            source_path="data/final/",
            dest_path="data/import/"
        )

        assert result == "data/import.dvc"
        mock_run.assert_called_once()

    @patch('subprocess.run')
    def test_run_dvc_import_failure(self, mock_run, temp_dir):
        """Test DVC import failure."""
        mock_run.side_effect = subprocess.CalledProcessError(1, ["dvc", "import"], stderr="Import failed")

        with pytest.raises(DVCImportError, match="DVC import failed"):
            run_dvc_import(
                project_path=temp_dir,
                repo_url="https://github.com/test/repo",
                source_path="data/final/",
                dest_path="data/import/"
            )


# =============================================================================
# Metadata Update Tests
# =============================================================================

class TestMetadataUpdate:

    def test_update_metadata_adds_dependency(self, mock_project_dir, mock_data_product):
        """Test adding new dependency to metadata."""
        result = ImportResult(
            product_name="data_cms-provider-data-service",
            success=True,
            dvc_file="cms.dvc",
            local_path="data/imports/cms/",
            source_commit="abc123"
        )

        update_project_metadata(mock_project_dir, result, mock_data_product)

        # Check metadata was updated
        with open(mock_project_dir / "metadata.json", "r") as f:
            metadata = json.load(f)

        dependencies = metadata["metadata"]["data_dependencies"]
        assert len(dependencies) == 1
        assert dependencies[0]["source"] == "data_cms-provider-data-service"
        assert dependencies[0]["dvc_file"] == "cms.dvc"

    def test_update_metadata_updates_existing(self, mock_project_dir, mock_data_product):
        """Test updating existing dependency."""
        # First add a dependency
        result1 = ImportResult(
            product_name="data_cms-provider-data-service",
            success=True,
            dvc_file="cms.dvc",
            local_path="data/imports/cms/",
            source_commit="abc123"
        )
        update_project_metadata(mock_project_dir, result1, mock_data_product)

        # Update the same dependency
        result2 = ImportResult(
            product_name="data_cms-provider-data-service",
            success=True,
            dvc_file="cms.dvc",
            local_path="data/imports/cms/",
            source_commit="def456"
        )
        update_project_metadata(mock_project_dir, result2, mock_data_product)

        # Check only one entry exists with updated info
        with open(mock_project_dir / "metadata.json", "r") as f:
            metadata = json.load(f)

        dependencies = metadata["metadata"]["data_dependencies"]
        assert len(dependencies) == 1
        assert dependencies[0]["source_commit"] == "def456"


# =============================================================================
# Import Data Product Tests
# =============================================================================

class TestImportDataProduct:

    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.validate_source_path')
    def test_import_data_product_success(
        self, mock_validate_src, mock_update_metadata, mock_run_dvc, mock_validate, mock_query,
        mock_project_dir, mock_data_product
    ):
        """Test successful data product import."""
        mock_query.return_value = mock_data_product
        mock_run_dvc.return_value = "test.dvc"
        mock_validate_src.return_value = (True, ["data/final"])

        result = import_data_product(
            product_name="data_cms-provider-data-service",
            project_path=mock_project_dir,
            stage="final"
        )

        assert result.success == True
        assert result.product_name == "data_cms-provider-data-service"
        assert result.dvc_file == "test.dvc"
        assert "data/imports/" in result.local_path

    @patch('mintd.data_import.query_data_product')
    def test_import_data_product_registry_error(self, mock_query, mock_project_dir):
        """Test import fails with registry error."""
        mock_query.side_effect = RegistryError("Registry not found")

        result = import_data_product(
            product_name="data_cms-provider-data-service",
            project_path=mock_project_dir,
            stage="final"
        )

        assert result.success == False
        assert "Registry not found" in result.error_message


# =============================================================================
# Pull Data Product Tests
# =============================================================================

class TestPullDataProduct:

    @patch('mintd.data_import.query_data_product')
    @patch('tempfile.mkdtemp')
    @patch('git.Repo.clone_from')
    @patch('shutil.copytree')
    def test_pull_data_product_success(
        self, mock_copytree, mock_clone, mock_mkdtemp, mock_query,
        temp_dir, mock_data_product
    ):
        """Test successful data product pull."""
        mock_query.return_value = mock_data_product
        mock_mkdtemp.return_value = str(temp_dir / "temp_repo")

        # Create mock repo structure
        temp_repo_dir = temp_dir / "temp_repo"
        temp_repo_dir.mkdir()
        (temp_repo_dir / "data" / "final").mkdir(parents=True)

        success = pull_data_product(
            product_name="data_cms-provider-data-service",
            destination=str(temp_dir / "output")
        )

        assert success == True


# =============================================================================
# Push Data Tests
# =============================================================================

class TestGetProjectRemote:
    """Tests for get_project_remote."""

    def test_get_remote_from_metadata(self, temp_dir):
        """Test reading remote name from metadata.json."""
        metadata = {
            "project": {"name": "test", "type": "data", "full_name": "data_test"},
            "storage": {
                "dvc": {
                    "remote_name": "data_test",
                    "remote_url": "s3://bucket/lab/data_test/"
                }
            }
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        remote = get_project_remote(temp_dir)
        assert remote == "data_test"

    def test_get_remote_no_metadata(self, temp_dir):
        """Test error when metadata.json is missing."""
        with pytest.raises(DataImportError, match="Not a mintd project"):
            get_project_remote(temp_dir)

    def test_get_remote_no_dvc_config(self, temp_dir):
        """Test error when DVC remote is not configured in metadata."""
        metadata = {
            "project": {"name": "test", "type": "data"},
            "storage": {}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        with pytest.raises(DataImportError, match="No DVC remote configured"):
            get_project_remote(temp_dir)

    def test_get_remote_empty_remote_name(self, temp_dir):
        """Test error when remote_name is empty string."""
        metadata = {
            "project": {"name": "test", "type": "data"},
            "storage": {"dvc": {"remote_name": "", "remote_url": ""}}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        with pytest.raises(DataImportError, match="No DVC remote configured"):
            get_project_remote(temp_dir)


class TestPushData:
    """Tests for push_data function."""

    @patch('subprocess.run')
    def test_push_data_success(self, mock_run, temp_dir):
        """Test successful DVC push."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        metadata = {
            "project": {"name": "test", "type": "data", "full_name": "data_test"},
            "storage": {
                "dvc": {
                    "remote_name": "data_test",
                    "remote_url": "s3://bucket/lab/data_test/"
                }
            }
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        result = push_data(project_path=temp_dir)
        assert result is True

        # Verify dvc push was called with correct remote
        call_args = mock_run.call_args[0][0]
        assert "push" in call_args
        assert "-r" in call_args
        assert "data_test" in call_args

    @patch('subprocess.run')
    def test_push_data_with_targets(self, mock_run, temp_dir):
        """Test push with specific targets."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        metadata = {
            "project": {"name": "test", "type": "data", "full_name": "data_test"},
            "storage": {"dvc": {"remote_name": "data_test", "remote_url": "s3://x/"}}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        push_data(project_path=temp_dir, targets=["data/raw.dvc"])

        call_args = mock_run.call_args[0][0]
        assert "data/raw.dvc" in call_args

    @patch('subprocess.run')
    def test_push_data_with_jobs(self, mock_run, temp_dir):
        """Test push with parallel jobs option."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        metadata = {
            "project": {"name": "test", "type": "data", "full_name": "data_test"},
            "storage": {"dvc": {"remote_name": "data_test", "remote_url": "s3://x/"}}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        push_data(project_path=temp_dir, jobs=4)

        call_args = mock_run.call_args[0][0]
        assert "-j" in call_args
        assert "4" in call_args

    def test_push_data_no_remote(self, temp_dir):
        """Test push fails gracefully when no remote configured."""
        metadata = {"project": {"name": "test"}, "storage": {}}
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        with pytest.raises(DataImportError, match="No DVC remote configured"):
            push_data(project_path=temp_dir)


class TestPushCLI:
    """Tests for data push CLI command."""

    def test_data_push_help(self):
        """Test data push command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "push", "--help"])
        assert result.exit_code == 0
        assert "Push DVC-tracked data" in result.output

    @patch('mintd.data_import.push_data')
    def test_data_push_success(self, mock_push, temp_dir):
        """Test successful push via CLI."""
        from mintd.cli import main
        mock_push.return_value = True

        # Create metadata so the path exists
        metadata = {
            "project": {"name": "test", "type": "data"},
            "storage": {"dvc": {"remote_name": "data_test", "remote_url": "s3://x/"}}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        runner = CliRunner()
        result = runner.invoke(main, ["data", "push", "-p", str(temp_dir)])

        assert result.exit_code == 0
        mock_push.assert_called_once()

    @patch('mintd.data_import.push_data')
    def test_data_push_with_targets(self, mock_push, temp_dir):
        """Test push with specific targets via CLI."""
        from mintd.cli import main
        mock_push.return_value = True

        metadata = {
            "project": {"name": "test", "type": "data"},
            "storage": {"dvc": {"remote_name": "data_test", "remote_url": "s3://x/"}}
        }
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "push", "data/raw.dvc", "data/clean.dvc", "-p", str(temp_dir)
        ])

        assert result.exit_code == 0
        call_kwargs = mock_push.call_args[1]
        assert call_kwargs["targets"] == ["data/raw.dvc", "data/clean.dvc"]

    @patch('mintd.data_import.push_data')
    def test_data_push_failure(self, mock_push, temp_dir):
        """Test CLI handles push failure."""
        from mintd.cli import main
        mock_push.side_effect = DataImportError("No DVC remote configured")

        metadata = {"project": {"name": "test"}, "storage": {}}
        with open(temp_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        runner = CliRunner()
        result = runner.invoke(main, ["data", "push", "-p", str(temp_dir)])

        assert result.exit_code != 0


# =============================================================================
# List Data Products Tests
# =============================================================================

class TestListDataProducts:

    @patch('mintd.data_import.load_project_metadata')
    def test_list_imported_dependencies(self, mock_load_metadata, mock_project_dir):
        """Test listing imported dependencies."""
        # Mock metadata with dependencies
        mock_metadata = {
            "metadata": {
                "data_dependencies": [
                    {
                        "source": "data_product_1",
                        "local_path": "data/imports/prod1/",
                        "imported_at": "2025-01-01T00:00:00Z"
                    }
                ]
            }
        }
        mock_load_metadata.return_value = mock_metadata

        # This should not raise an exception
        list_data_products(show_imported=True, project_path=mock_project_dir)

    @patch('mintd.data_import.get_registry_client')
    def test_list_available_products(self, mock_get_client):
        """Test listing available products from registry."""
        mock_client = Mock()
        mock_client.list_data_products.return_value = [
            {"name": "data_product_1", "description": "Test product"}
        ]
        mock_get_client.return_value = mock_client

        # This should not raise an exception
        list_data_products(show_imported=False)


# =============================================================================
# CLI Tests
# =============================================================================

class TestCLI:

    def test_data_command_help(self):
        """Test data command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "--help"])
        assert result.exit_code == 0
        assert "Manage data products and dependencies" in result.output

    def test_data_pull_help(self):
        """Test data pull command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "pull", "--help"])
        assert result.exit_code == 0
        assert "Pull/download data from a registered data product" in result.output

    def test_data_import_help(self):
        """Test data import command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "import", "--help"])
        assert result.exit_code == 0
        assert "Import data product as DVC dependency" in result.output

    def test_data_list_help(self):
        """Test data list command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "list", "--help"])
        assert result.exit_code == 0
        assert "List available data products or imported dependencies" in result.output

    def test_data_update_help(self):
        """Test data update command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "update", "--help"])
        assert result.exit_code == 0
        assert "Update DVC data imports" in result.output

    @patch('mintd.data_import.update_all_imports')
    def test_data_update_all(self, mock_update, mock_project_dir):
        """Test data update command updates all imports."""
        from mintd.cli import main
        mock_update.return_value = [
            UpdateResult(dvc_file="test.dvc", success=True)
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["data", "update", "-p", str(mock_project_dir)])

        assert result.exit_code == 0
        mock_update.assert_called_once()

    @patch('mintd.data_import.update_single_import')
    def test_data_update_single(self, mock_update, mock_project_dir):
        """Test data update command with specific path."""
        from mintd.cli import main
        mock_update.return_value = UpdateResult(
            dvc_file="data/test.dvc", success=True
        )

        # Create the dvc file
        dvc_file = mock_project_dir / "data" / "test.dvc"
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        dvc_file.write_text("md5: abc\n")

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "update", "data/test.dvc", "-p", str(mock_project_dir)
        ])

        assert result.exit_code == 0
        mock_update.assert_called_once()


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:

    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.validate_source_path')
    def test_import_e2e_workflow(
        self, mock_validate_src, mock_update, mock_dvc_import, mock_query,
        mock_project_dir, mock_data_product
    ):
        """Test full import workflow end-to-end."""
        mock_query.return_value = mock_data_product
        mock_dvc_import.return_value = "test.dvc"
        mock_validate_src.return_value = (True, ["data/final"])

        result = import_data_product(
            product_name="data_cms-provider-data-service",
            project_path=mock_project_dir,
            stage="final"
        )

        assert result.success == True
        mock_query.assert_called_once_with("data_cms-provider-data-service")
        mock_dvc_import.assert_called_once()
        mock_update.assert_called_once()


# =============================================================================
# Error Handling Tests
# =============================================================================

# =============================================================================
# DVC Update Tests
# =============================================================================

class TestDVCUpdate:
    """Tests for DVC update functionality."""

    def test_update_result_creation(self):
        """Test UpdateResult dataclass creation."""
        result = UpdateResult(
            dvc_file="data/imports/test.dvc",
            success=True,
            previous_commit="abc123",
            new_commit="def456"
        )
        assert result.dvc_file == "data/imports/test.dvc"
        assert result.success is True
        assert result.previous_commit == "abc123"
        assert result.new_commit == "def456"
        assert result.skipped is False

    def test_update_result_skipped(self):
        """Test UpdateResult when already up-to-date."""
        result = UpdateResult(
            dvc_file="test.dvc",
            success=True,
            skipped=True,
            previous_commit="abc123",
            new_commit="abc123"
        )
        assert result.skipped is True

    @patch('subprocess.run')
    def test_run_dvc_update_success(self, mock_run, temp_dir):
        """Test successful DVC update."""
        mock_run.return_value = Mock(returncode=0, stdout="Updated 'data/test.dvc'")

        # Create a mock .dvc file
        dvc_file = temp_dir / "data" / "imports" / "test.dvc"
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        dvc_file.write_text("md5: abc123\n")

        result = run_dvc_update(
            project_path=temp_dir,
            dvc_file=str(dvc_file.relative_to(temp_dir))
        )

        assert result.success is True
        mock_run.assert_called_once()

    @patch('subprocess.run')
    def test_run_dvc_update_with_revision(self, mock_run, temp_dir):
        """Test DVC update with specific revision."""
        mock_run.return_value = Mock(returncode=0, stdout="")

        dvc_file = temp_dir / "test.dvc"
        dvc_file.write_text("md5: abc123\n")

        run_dvc_update(
            project_path=temp_dir,
            dvc_file="test.dvc",
            rev="v1.0.0"
        )

        # Check --rev flag was passed
        call_args = mock_run.call_args
        assert "--rev" in call_args[0][0] or any("--rev" in str(arg) for arg in call_args[0][0])

    def test_run_dvc_update_missing_file(self, temp_dir):
        """Test DVC update fails for missing .dvc file."""
        with pytest.raises(DVCUpdateError, match="not found"):
            run_dvc_update(
                project_path=temp_dir,
                dvc_file="nonexistent.dvc"
            )

    @patch('subprocess.run')
    def test_run_dvc_update_failure(self, mock_run, temp_dir):
        """Test DVC update failure handling."""
        mock_run.side_effect = subprocess.CalledProcessError(
            1, ["dvc", "update"], stderr="Update failed"
        )

        dvc_file = temp_dir / "test.dvc"
        dvc_file.write_text("md5: abc123\n")

        with pytest.raises(DVCUpdateError, match="DVC update failed"):
            run_dvc_update(project_path=temp_dir, dvc_file="test.dvc")


class TestUpdateSingleImport:
    """Tests for updating a single import."""

    @patch('mintd.data_import.run_dvc_update')
    def test_update_single_import_success(self, mock_update, mock_project_dir):
        """Test updating a single import by path."""
        # Create the .dvc file
        dvc_file = mock_project_dir / "data" / "imports" / "test.dvc"
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        dvc_file.write_text("md5: abc123\n")

        mock_update.return_value = UpdateResult(
            dvc_file="data/imports/test.dvc",
            success=True,
            previous_commit="abc123",
            new_commit="def456"
        )

        result = update_single_import(
            project_path=mock_project_dir,
            dvc_file_path="data/imports/test.dvc"
        )

        assert result.success is True
        mock_update.assert_called_once()

    def test_update_single_import_not_found(self, mock_project_dir):
        """Test error when .dvc file not found."""
        with pytest.raises(DependencyNotFoundError):
            update_single_import(
                project_path=mock_project_dir,
                dvc_file_path="nonexistent.dvc"
            )


class TestUpdateAllImports:
    """Tests for updating all imports."""

    @patch('mintd.data_import.run_dvc_update')
    @patch('mintd.data_import.load_project_metadata')
    def test_update_all_imports_success(self, mock_load, mock_update, mock_project_dir):
        """Test updating all imports."""
        mock_load.return_value = {
            "metadata": {
                "data_dependencies": [
                    {"dvc_file": "data/imports/prod1.dvc", "source": "prod1"},
                    {"dvc_file": "data/imports/prod2.dvc", "source": "prod2"}
                ]
            }
        }
        mock_update.return_value = UpdateResult(
            dvc_file="test.dvc", success=True
        )

        # Create the dvc files
        for f in ["data/imports/prod1.dvc", "data/imports/prod2.dvc"]:
            dvc_path = mock_project_dir / f
            dvc_path.parent.mkdir(parents=True, exist_ok=True)
            dvc_path.write_text("md5: abc\n")

        results = update_all_imports(project_path=mock_project_dir)

        assert len(results) == 2
        assert all(r.success for r in results)

    @patch('mintd.data_import.load_project_metadata')
    def test_update_all_imports_no_dependencies(self, mock_load, mock_project_dir):
        """Test when no dependencies to update."""
        mock_load.return_value = {"metadata": {}}

        results = update_all_imports(project_path=mock_project_dir)

        assert len(results) == 0

    @patch('mintd.data_import.run_dvc_update')
    @patch('mintd.data_import.load_project_metadata')
    def test_update_all_imports_dry_run(self, mock_load, mock_update, mock_project_dir):
        """Test dry-run mode doesn't actually update."""
        mock_load.return_value = {
            "metadata": {
                "data_dependencies": [
                    {"dvc_file": "test.dvc", "source": "prod1"}
                ]
            }
        }

        # Create the dvc file
        (mock_project_dir / "test.dvc").write_text("md5: abc\n")

        results = update_all_imports(project_path=mock_project_dir, dry_run=True)

        mock_update.assert_not_called()
        assert len(results) == 1


class TestUpdateDependencyMetadata:
    """Tests for updating metadata after update."""

    def test_update_dependency_metadata(self, mock_project_dir):
        """Test metadata is updated after successful update."""
        # First add a dependency
        metadata = {
            "project": {"name": "test", "type": "project", "full_name": "prj_test"},
            "metadata": {
                "data_dependencies": [{
                    "source": "prod1",
                    "dvc_file": "test.dvc",
                    "source_commit": "abc123",
                    "local_path": "data/imports/prod1/"
                }]
            },
            "ownership": {},
            "access_control": {"teams": []},
            "status": {}
        }
        with open(mock_project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        update_result = UpdateResult(
            dvc_file="test.dvc",
            success=True,
            previous_commit="abc123",
            new_commit="def456"
        )

        update_dependency_metadata(mock_project_dir, update_result)

        # Check metadata was updated
        with open(mock_project_dir / "metadata.json", "r") as f:
            updated = json.load(f)

        deps = updated["metadata"]["data_dependencies"]
        assert deps[0]["source_commit"] == "def456"


class TestErrorHandling:

    def test_import_result_creation(self):
        """Test ImportResult creation."""
        result = ImportResult(
            product_name="test",
            success=True,
            dvc_file="test.dvc",
            local_path="data/test/",
            source_commit="abc123"
        )

        assert result.product_name == "test"
        assert result.success == True
        assert result.dvc_file == "test.dvc"

    def test_import_result_failure(self):
        """Test ImportResult with failure."""
        result = ImportResult(
            product_name="test",
            success=False,
            error_message="Network timeout"
        )

        assert result.success == False
        assert result.error_message == "Network timeout"
        assert result.dvc_file is None

    @patch('mintd.data_import.import_data_product')
    def test_multi_import_partial_failure_simulation(self, mock_import, mock_project_dir):
        """Test handling multiple imports with partial failure."""
        # Mock first import success, second failure
        def mock_import_func(product_name, **kwargs):
            if "product1" in product_name:
                return ImportResult(product_name, True, dvc_file="prod1.dvc")
            else:
                return ImportResult(product_name, False, error_message="Import failed")

        mock_import.side_effect = mock_import_func

        # Simulate multiple imports
        transaction = ImportTransaction(mock_project_dir)

        result1 = import_data_product("product1", mock_project_dir, stage="final")
        result2 = import_data_product("product2", mock_project_dir, stage="final")

        transaction.add_success(result1)
        transaction.add_failure(result2)

        summary = transaction.get_summary()
        assert summary["successful"] == 1
        assert len(summary["failed"]) == 1


# =============================================================================
# Remove Data Import Tests
# =============================================================================

class TestRemoveResult:
    """Tests for RemoveResult dataclass."""

    def test_remove_result_creation(self):
        """Test RemoveResult dataclass creation."""
        result = RemoveResult(
            product_name="cms-pps-weights",
            success=True,
            removed_path="data/imports/cms-pps-weights/",
            removed_dvc_file="data/imports/cms-pps-weights.dvc"
        )
        assert result.product_name == "cms-pps-weights"
        assert result.success is True
        assert result.removed_path == "data/imports/cms-pps-weights/"
        assert result.warnings == []

    def test_remove_result_with_warnings(self):
        """Test RemoveResult with warnings."""
        result = RemoveResult(
            product_name="test",
            success=True,
            warnings=["dvc.yaml still references data/imports/test/"]
        )
        assert len(result.warnings) == 1

    def test_remove_result_failure(self):
        """Test RemoveResult with failure."""
        result = RemoveResult(
            product_name="test",
            success=False,
            error_message="Dependency not found"
        )
        assert result.success is False
        assert "not found" in result.error_message


class TestRemoveDependencyFromMetadata:
    """Tests for removing dependency from metadata.json."""

    def test_remove_dependency_success(self, mock_project_dir):
        """Test successful removal of dependency from metadata."""
        # Setup: add a dependency first
        metadata = {
            "project": {"name": "test", "type": "project", "full_name": "prj_test"},
            "metadata": {
                "data_dependencies": [{
                    "source": "data_cms-pps-weights",
                    "local_path": "data/imports/cms-pps-weights/",
                    "dvc_file": "data/imports/cms-pps-weights.dvc",
                    "imported_at": "2025-01-01T00:00:00Z"
                }]
            },
            "ownership": {"created_by": "test"},
            "access_control": {"teams": []},
            "status": {}
        }
        with open(mock_project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Remove the dependency
        removed = remove_dependency_from_metadata(
            mock_project_dir, "data_cms-pps-weights"
        )

        # Verify it was removed
        assert removed["source"] == "data_cms-pps-weights"

        with open(mock_project_dir / "metadata.json", "r") as f:
            updated = json.load(f)
        assert len(updated["metadata"]["data_dependencies"]) == 0

    def test_remove_dependency_not_found(self, mock_project_dir):
        """Test error when dependency doesn't exist."""
        with pytest.raises(DependencyNotFoundError):
            remove_dependency_from_metadata(
                mock_project_dir, "nonexistent-import"
            )

    def test_remove_dependency_partial_match(self, mock_project_dir):
        """Test removal matches by source name without data_ prefix."""
        metadata = {
            "project": {"name": "test", "type": "project", "full_name": "prj_test"},
            "metadata": {
                "data_dependencies": [{
                    "source": "data_cms-pps-weights",
                    "local_path": "data/imports/cms-pps-weights/",
                    "dvc_file": "data/imports/cms-pps-weights.dvc"
                }]
            },
            "ownership": {}, "access_control": {"teams": []}, "status": {}
        }
        with open(mock_project_dir / "metadata.json", "w") as f:
            json.dump(metadata, f)

        # Should match without data_ prefix
        removed = remove_dependency_from_metadata(
            mock_project_dir, "cms-pps-weights"
        )
        assert removed["source"] == "data_cms-pps-weights"


class TestCheckDvcYamlReferences:
    """Tests for checking dvc.yaml references."""

    def test_no_dvc_yaml(self, mock_project_dir):
        """Test when dvc.yaml doesn't exist."""
        warnings = check_dvc_yaml_references(
            mock_project_dir, ["data/imports/test/"]
        )
        assert len(warnings) == 0

    def test_no_references(self, mock_project_dir):
        """Test when dvc.yaml has no references to removed paths."""
        dvc_yaml = mock_project_dir / "dvc.yaml"
        dvc_yaml.write_text("stages:\n  build:\n    cmd: echo hello\n")

        warnings = check_dvc_yaml_references(
            mock_project_dir, ["data/imports/test/"]
        )
        assert len(warnings) == 0

    def test_has_references(self, mock_project_dir):
        """Test when dvc.yaml references removed paths."""
        dvc_yaml = mock_project_dir / "dvc.yaml"
        dvc_yaml.write_text(
            "stages:\n  build:\n    deps:\n      - data/imports/test/file.dta\n"
        )

        warnings = check_dvc_yaml_references(
            mock_project_dir, ["data/imports/test/"]
        )
        assert len(warnings) == 1
        assert "dvc.yaml" in warnings[0]


class TestRemoveDataImport:
    """Tests for full remove workflow."""

    @patch('mintd.data_import.remove_dependency_from_metadata')
    def test_remove_success(self, mock_remove_meta, mock_project_dir):
        """Test successful removal of data import."""
        # Setup directory and dvc file
        import_dir = mock_project_dir / "data" / "imports" / "cms-pps-weights"
        import_dir.mkdir(parents=True)
        (import_dir / "test.dta").write_text("test data")
        dvc_file = mock_project_dir / "data" / "imports" / "cms-pps-weights.dvc"
        dvc_file.write_text("md5: abc123\n")

        # Populate data_dependencies so the inline lookup finds the dependency
        dep_entry = {
            "source": "data_cms-pps-weights",
            "local_path": "data/imports/cms-pps-weights/",
            "dvc_file": "data/imports/cms-pps-weights.dvc"
        }
        metadata_file = mock_project_dir / "metadata.json"
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        metadata.setdefault("metadata", {})["data_dependencies"] = [dep_entry]
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

        mock_remove_meta.return_value = dep_entry

        result = remove_data_import(mock_project_dir, "cms-pps-weights")

        assert result.success is True
        assert not import_dir.exists()
        assert not dvc_file.exists()

    def test_remove_not_found(self, mock_project_dir):
        """Test error when import doesn't exist in metadata."""
        result = remove_data_import(mock_project_dir, "nonexistent")

        assert result.success is False
        assert "not found" in result.error_message.lower()

    @patch('mintd.data_import.remove_dependency_from_metadata')
    @patch('mintd.data_import.check_dvc_yaml_references')
    def test_remove_with_warnings(self, mock_check, mock_remove_meta, mock_project_dir):
        """Test removal warns about dvc.yaml references."""
        import_dir = mock_project_dir / "data" / "imports" / "test"
        import_dir.mkdir(parents=True)

        dep_entry = {
            "source": "data_test",
            "local_path": "data/imports/test/",
            "dvc_file": "data/imports/test.dvc"
        }
        metadata_file = mock_project_dir / "metadata.json"
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        metadata.setdefault("metadata", {})["data_dependencies"] = [dep_entry]
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

        mock_remove_meta.return_value = dep_entry
        mock_check.return_value = ["Warning: dvc.yaml references data/imports/test/"]

        result = remove_data_import(mock_project_dir, "test", force=False)

        assert result.success is False
        assert len(result.warnings) == 1

    @patch('mintd.data_import.remove_dependency_from_metadata')
    @patch('mintd.data_import.check_dvc_yaml_references')
    def test_remove_force_ignores_warnings(self, mock_check, mock_remove_meta, mock_project_dir):
        """Test --force removes despite dvc.yaml references."""
        import_dir = mock_project_dir / "data" / "imports" / "test"
        import_dir.mkdir(parents=True)

        dep_entry = {
            "source": "data_test",
            "local_path": "data/imports/test/",
            "dvc_file": "data/imports/test.dvc"
        }
        metadata_file = mock_project_dir / "metadata.json"
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        metadata.setdefault("metadata", {})["data_dependencies"] = [dep_entry]
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

        mock_remove_meta.return_value = dep_entry
        mock_check.return_value = ["Warning: dvc.yaml references data/imports/test/"]

        result = remove_data_import(mock_project_dir, "test", force=True)

        assert result.success is True
        assert len(result.warnings) == 1


class TestRemoveCLI:
    """Tests for data remove CLI command."""

    def test_data_remove_help(self):
        """Test data remove command help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "remove", "--help"])
        assert result.exit_code == 0
        assert "Remove a data import" in result.output

    @patch('mintd.data_import.remove_data_import')
    def test_data_remove_success(self, mock_remove, mock_project_dir):
        """Test successful removal via CLI."""
        from mintd.cli import main
        mock_remove.return_value = RemoveResult(
            product_name="test",
            success=True,
            removed_path="data/imports/test/"
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "remove", "test", "-p", str(mock_project_dir)
        ])

        assert result.exit_code == 0
        mock_remove.assert_called_once()

    @patch('mintd.data_import.remove_data_import')
    def test_data_remove_not_found(self, mock_remove, mock_project_dir):
        """Test CLI error when import not found."""
        from mintd.cli import main
        mock_remove.return_value = RemoveResult(
            product_name="nonexistent",
            success=False,
            error_message="Dependency not found"
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "remove", "nonexistent", "-p", str(mock_project_dir)
        ])

        assert result.exit_code != 0


# =============================================================================
# Smart Import Default Tests (list_remote_data_paths, validate, prompt, --all)
# =============================================================================

class TestListRemoteDataPaths:
    """Tests for listing available data paths in a remote repo."""

    @patch('mintd.data_import.shutil.rmtree')
    @patch('mintd.data_import.tempfile.mkdtemp')
    @patch('mintd.data_import.git_command')
    def test_lists_data_directories(self, mock_git_cmd, mock_mkdtemp, mock_rmtree):
        """Test listing data directories from remote repo via shallow clone."""
        mock_mkdtemp.return_value = "/tmp/mintd-ls-test"
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        # First call: clone, second call: ls-tree
        mock_git.run.side_effect = [
            Mock(),  # clone
            Mock(stdout="raw\nintermediate\nfinal\n"),  # ls-tree
        ]

        paths = list_remote_data_paths(
            repo_url="git@github.com:test/data_test.git",
        )

        assert "data/raw" in paths
        assert "data/intermediate" in paths
        assert "data/final" in paths

    @patch('mintd.data_import.shutil.rmtree')
    @patch('mintd.data_import.tempfile.mkdtemp')
    @patch('mintd.data_import.git_command')
    def test_returns_empty_on_failure(self, mock_git_cmd, mock_mkdtemp, mock_rmtree):
        """Test returns empty list when git clone fails."""
        from mintd.exceptions import GitError
        mock_mkdtemp.return_value = "/tmp/mintd-ls-test"
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_git.run.side_effect = GitError(
            message="failed", command=["git"], returncode=1
        )

        paths = list_remote_data_paths(
            repo_url="git@github.com:test/data_test.git",
        )

        assert paths == []

    @patch('mintd.data_import.shutil.rmtree')
    @patch('mintd.data_import.tempfile.mkdtemp')
    @patch('mintd.data_import.git_command')
    def test_with_revision(self, mock_git_cmd, mock_mkdtemp, mock_rmtree):
        """Test listing paths at a specific revision."""
        mock_mkdtemp.return_value = "/tmp/mintd-ls-test"
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_git.run.side_effect = [
            Mock(),  # clone
            Mock(stdout="final\n"),  # ls-tree
        ]

        paths = list_remote_data_paths(
            repo_url="git@github.com:test/data_test.git",
            rev="v1.0",
        )

        # Clone call should include --branch v1.0
        clone_call = mock_git.run.call_args_list[0]
        assert "--branch" in str(clone_call)
        assert "v1.0" in str(clone_call)
        assert "data/final" in paths


class TestValidateSourcePath:
    """Tests for validating that a source path exists in the remote."""

    @patch('mintd.data_import.list_remote_data_paths')
    def test_path_exists(self, mock_list):
        """Test validation passes when path exists."""
        mock_list.return_value = ["data/raw", "data/intermediate", "data/final"]

        exists, available = validate_source_path(
            repo_url="git@github.com:test/data_test.git",
            source_path="data/final/",
        )

        assert exists is True
        assert "data/final" in available

    @patch('mintd.data_import.list_remote_data_paths')
    def test_path_not_exists(self, mock_list):
        """Test validation fails when path doesn't exist."""
        mock_list.return_value = ["data/raw", "data/intermediate"]

        exists, available = validate_source_path(
            repo_url="git@github.com:test/data_test.git",
            source_path="data/final/",
        )

        assert exists is False
        assert "data/raw" in available
        assert "data/intermediate" in available

    @patch('mintd.data_import.list_remote_data_paths')
    def test_empty_remote(self, mock_list):
        """Test when remote has no data directories."""
        mock_list.return_value = []

        exists, available = validate_source_path(
            repo_url="git@github.com:test/data_test.git",
            source_path="data/final/",
        )

        assert exists is False
        assert available == []


class TestPromptStageSelection:
    """Tests for interactive stage selection."""

    def test_prompt_with_choices(self):
        """Test prompting user to select from available paths."""
        from click.testing import CliRunner
        runner = CliRunner()

        import click
        @click.command()
        def _test_cmd():
            nonlocal selected
            selected = prompt_stage_selection(
                ["data/raw", "data/intermediate", "data/final"]
            )

        selected = None
        runner.invoke(_test_cmd, input="3\n")
        assert selected == "data/final"

    def test_prompt_single_choice(self):
        """Test when only one path is available, auto-selects it."""
        selected = prompt_stage_selection(["data/raw"])
        assert selected == "data/raw"

    def test_prompt_no_choices_raises(self):
        """Test raises error when no paths available."""
        with pytest.raises(DataImportError, match="No data directories"):
            prompt_stage_selection([])


class TestImportDataProductSmartDefault:
    """Tests for the updated import_data_product with validation and --all."""

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.validate_source_path')
    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    def test_default_imports_final(self, mock_validate, mock_query, mock_validate_src,
                                    mock_dvc_import, mock_update_meta, mock_project_dir,
                                    mock_data_product):
        """Test default import targets data/final/ and validates it exists."""
        mock_query.return_value = mock_data_product
        mock_validate_src.return_value = (True, ["data/raw", "data/intermediate", "data/final"])
        mock_dvc_import.return_value = "data/imports/cms-provider-data-service.dvc"

        result = import_data_product(
            product_name="cms-provider-data-service",
            project_path=mock_project_dir,
        )

        assert result.success is True
        mock_validate_src.assert_called_once()
        call_args = mock_validate_src.call_args
        assert "data/final/" in str(call_args)

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.validate_source_path')
    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    def test_import_all_flag(self, mock_validate, mock_query, mock_validate_src,
                              mock_dvc_import, mock_update_meta, mock_project_dir,
                              mock_data_product):
        """Test --all imports entire data/ directory."""
        mock_query.return_value = mock_data_product
        mock_validate_src.return_value = (True, ["data/raw", "data/intermediate", "data/final"])
        mock_dvc_import.return_value = "data/imports/cms-provider-data-service.dvc"

        result = import_data_product(
            product_name="cms-provider-data-service",
            project_path=mock_project_dir,
            import_all=True,
        )

        assert result.success is True
        dvc_call = mock_dvc_import.call_args
        # source_path arg should be "data/"
        assert dvc_call.kwargs.get("source_path") == "data/" or dvc_call[1].get("source_path") == "data/"

    @patch('mintd.data_import.prompt_stage_selection')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.validate_source_path')
    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    def test_fallback_prompts_when_final_missing(self, mock_validate, mock_query,
                                                   mock_validate_src, mock_dvc_import,
                                                   mock_prompt, mock_project_dir,
                                                   mock_data_product):
        """Test prompts user when data/final/ doesn't exist."""
        mock_query.return_value = mock_data_product
        mock_validate_src.return_value = (False, ["data/raw", "data/intermediate"])
        mock_prompt.return_value = "data/raw"
        mock_dvc_import.return_value = "data/imports/cms-provider-data-service.dvc"

        result = import_data_product(
            product_name="cms-provider-data-service",
            project_path=mock_project_dir,
        )

        mock_prompt.assert_called_once_with(["data/raw", "data/intermediate"])

    @patch('mintd.data_import.validate_source_path')
    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    def test_fails_when_no_data_dirs(self, mock_validate, mock_query,
                                      mock_validate_src, mock_project_dir,
                                      mock_data_product):
        """Test fails with clear error when no data dirs found."""
        mock_query.return_value = mock_data_product
        mock_validate_src.return_value = (False, [])

        result = import_data_product(
            product_name="cms-provider-data-service",
            project_path=mock_project_dir,
        )

        assert result.success is False

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.validate_project_directory')
    def test_source_path_skips_validation(self, mock_validate, mock_query,
                                           mock_dvc_import, mock_update_meta,
                                           mock_project_dir, mock_data_product):
        """Test --source-path bypasses smart default validation."""
        mock_query.return_value = mock_data_product
        mock_dvc_import.return_value = "custom/path/file.csv.dvc"

        result = import_data_product(
            product_name="cms-provider-data-service",
            project_path=mock_project_dir,
            path="custom/path/file.csv",
        )

        assert result.success is True


class TestImportCLIAllFlag:
    """Tests for the --all CLI flag."""

    def test_import_help_shows_all_flag(self):
        """Test --all flag appears in help."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["data", "import", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    @patch('mintd.data_import.import_data_product')
    def test_import_all_and_stage_mutually_exclusive(self, mock_import):
        """Test --all and --stage cannot be used together."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "import", "test-product", "--all", "--stage", "raw"
        ])
        assert result.exit_code != 0

    @patch('mintd.data_import.import_data_product')
    def test_import_all_and_source_path_mutually_exclusive(self, mock_import):
        """Test --all and --source-path cannot be used together."""
        from mintd.cli import main
        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "import", "test-product", "--all", "--source-path", "some/path"
        ])
        assert result.exit_code != 0


# =============================================================================
# Name Resolution Tests — full name (data_*) should be the primary key
# =============================================================================

class TestNameResolution:
    """Test that registry resolves both full and short product names."""

    @patch('mintd.data_import.get_registry_client')
    def test_full_name_resolves_directly(self, mock_get_client, mock_data_product):
        """Full name like data_mergerbuild should resolve via exact file match."""
        mock_client = Mock()
        mock_client.query_data_product.return_value = mock_data_product
        mock_get_client.return_value = mock_client

        result = query_data_product("data_cms-provider-data-service")
        assert result == mock_data_product
        mock_client.query_data_product.assert_called_once_with("data_cms-provider-data-service")

    @patch('mintd.data_import.get_registry_client')
    def test_short_name_resolves_with_prefix(self, mock_get_client, mock_data_product):
        """Short name like mergerbuild should try data_ prefix fallback."""
        mock_client = Mock()
        # First call with short name raises, second with full name succeeds
        mock_client.query_data_product.side_effect = [
            FileNotFoundError("Not found"),
            mock_data_product,
        ]
        mock_get_client.return_value = mock_client

        result = query_data_product("cms-provider-data-service")
        assert result == mock_data_product
        assert mock_client.query_data_product.call_count == 2
        mock_client.query_data_product.assert_any_call("cms-provider-data-service")
        mock_client.query_data_product.assert_any_call("data_cms-provider-data-service")

    @patch('mintd.data_import.get_registry_client')
    def test_short_name_no_match_raises(self, mock_get_client):
        """Short name with no matching full name should raise RegistryError."""
        mock_client = Mock()
        mock_client.query_data_product.side_effect = FileNotFoundError("Not found")
        mock_get_client.return_value = mock_client

        with pytest.raises(RegistryError, match="Failed to query registry"):
            query_data_product("nonexistent")


# =============================================================================
# CLI -s alias Tests
# =============================================================================

class TestSourcePathAlias:
    """Test -s short alias for --source-path."""

    @patch('mintd.data_import.import_data_product')
    def test_short_s_alias_works(self, mock_import):
        """Test -s works as alias for --source-path."""
        from mintd.cli import main
        mock_import.return_value = ImportResult(
            product_name="test-product", success=True,
            dvc_file="test.dvc", local_path="deriveddata/hosppanel"
        )
        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "import", "test-product", "-s", "deriveddata/hosppanel"
        ])
        assert result.exit_code == 0
        _, kwargs = mock_import.call_args
        assert kwargs.get("path") == "deriveddata/hosppanel"


# =============================================================================
# Recursive Import Tests — list_remote_dvc_files + recursive import_data_product
# =============================================================================

class TestListRemoteDvcFiles:
    """Test discovery of .dvc files in a remote repo path."""

    @patch('mintd.data_import.git_command')
    def test_discovers_dvc_files(self, mock_git_cmd):
        """Should find .dvc files and return corresponding data paths."""
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        # Simulate git ls-tree output listing files under the path
        mock_git.run.side_effect = [
            Mock(),  # clone
            Mock(stdout="file1.parquet.dvc\nfile2.dta.dvc\nREADME.md\n"),  # ls-tree
        ]

        paths = list_remote_dvc_files(
            "git@github.com:org/data_mergerbuild.git",
            "deriveddata/hosppanel",
        )

        assert "deriveddata/hosppanel/file1.parquet" in paths
        assert "deriveddata/hosppanel/file2.dta" in paths
        assert len(paths) == 2  # README.md excluded

    @patch('mintd.data_import.git_command')
    def test_no_dvc_files_returns_empty(self, mock_git_cmd):
        """Should return empty list when no .dvc files exist."""
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_git.run.side_effect = [
            Mock(),  # clone
            Mock(stdout="README.md\nscript.do\n"),  # ls-tree
        ]

        paths = list_remote_dvc_files(
            "git@github.com:org/data_mergerbuild.git",
            "deriveddata/hosppanel",
        )

        assert paths == []

    @patch('mintd.data_import.git_command')
    def test_clone_failure_returns_empty(self, mock_git_cmd):
        """Should return empty list on clone failure."""
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_git.run.side_effect = Exception("clone failed")

        paths = list_remote_dvc_files(
            "git@github.com:org/data_mergerbuild.git",
            "deriveddata/hosppanel",
        )

        assert paths == []


class TestRecursiveImport:
    """Test recursive import when --source-path targets a directory with .dvc files."""

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.list_remote_dvc_files')
    @patch('mintd.data_import.query_data_product')
    def test_recursive_imports_each_file(
        self, mock_query, mock_list_dvc, mock_dvc_import, mock_update_meta,
        mock_project_dir, mock_data_product
    ):
        """When source-path has .dvc files, should import each one."""
        mock_query.return_value = mock_data_product
        mock_list_dvc.return_value = [
            "deriveddata/hosppanel/file1.parquet",
            "deriveddata/hosppanel/file2.dta",
        ]
        mock_dvc_import.return_value = "file.dvc"

        result = import_data_product(
            product_name="data_mergerbuild",
            project_path=mock_project_dir,
            path="deriveddata/hosppanel",
        )

        assert result.success is True
        assert mock_dvc_import.call_count == 2

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.list_remote_dvc_files')
    @patch('mintd.data_import.query_data_product')
    def test_recursive_falls_back_to_single_import(
        self, mock_query, mock_list_dvc, mock_dvc_import, mock_update_meta,
        mock_project_dir, mock_data_product
    ):
        """When no .dvc files found, should fall back to single import."""
        mock_query.return_value = mock_data_product
        mock_list_dvc.return_value = []
        mock_dvc_import.return_value = "test.dvc"

        result = import_data_product(
            product_name="data_mergerbuild",
            project_path=mock_project_dir,
            path="deriveddata/hosppanel",
        )

        assert result.success is True
        assert mock_dvc_import.call_count == 1

    @patch('mintd.data_import.update_project_metadata')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.list_remote_dvc_files')
    @patch('mintd.data_import.query_data_product')
    def test_recursive_partial_failure(
        self, mock_query, mock_list_dvc, mock_dvc_import, mock_update_meta,
        mock_project_dir, mock_data_product
    ):
        """Partial failure: some files import, some fail. Overall should fail."""
        mock_query.return_value = mock_data_product
        mock_list_dvc.return_value = [
            "deriveddata/hosppanel/file1.parquet",
            "deriveddata/hosppanel/file2.dta",
        ]
        mock_dvc_import.side_effect = [
            "file1.dvc",
            DVCImportError("DVC import failed"),
        ]

        result = import_data_product(
            product_name="data_mergerbuild",
            project_path=mock_project_dir,
            path="deriveddata/hosppanel",
        )

        assert result.success is False
        assert mock_dvc_import.call_count == 2
