"""Tests for data import functionality."""

import pytest
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from click.testing import CliRunner

from mintd.data_import import (
    ImportTransaction, ImportResult, DataImportError,
    RegistryError, DVCImportError, query_data_product, validate_project_directory,
    run_dvc_import, update_project_metadata,
    import_data_product, pull_data_product, list_data_products
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

    def test_validate_project_directory_invalid_type(self, temp_dir):
        """Test validation fails for invalid project type."""
        # Create metadata with invalid type
        metadata = {
            "project": {
                "name": "test_project",
                "type": "invalid",
                "full_name": "prj_test_project"
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
    def test_import_data_product_success(
        self, mock_update_metadata, mock_run_dvc, mock_validate, mock_query,
        mock_project_dir, mock_data_product
    ):
        """Test successful data product import."""
        mock_query.return_value = mock_data_product
        mock_run_dvc.return_value = "test.dvc"

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


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:

    @patch('mintd.data_import.query_data_product')
    @patch('mintd.data_import.run_dvc_import')
    @patch('mintd.data_import.update_project_metadata')
    def test_import_e2e_workflow(
        self, mock_update, mock_dvc_import, mock_query,
        mock_project_dir, mock_data_product
    ):
        """Test full import workflow end-to-end."""
        mock_query.return_value = mock_data_product
        mock_dvc_import.return_value = "test.dvc"

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
