"""Tests for mintd data get functionality."""

import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, call

import pytest
from click.testing import CliRunner

from mintd.data_import import GetResult, get_data_product


@pytest.fixture
def mock_data_product():
    """Mock data product catalog entry."""
    return {
        "project": {
            "name": "aha-annual-survey",
            "type": "data",
            "full_name": "data_aha-annual-survey",
        },
        "repository": {
            "github_url": "https://github.com/test-org/data_aha-annual-survey"
        },
        "storage": {"dvc": {"remote_name": "data_aha-annual-survey"}},
    }


class TestGetResult:
    def test_defaults(self):
        result = GetResult(product_name="test", success=False)
        assert result.product_name == "test"
        assert result.success is False
        assert result.error_message is None
        assert result.dest_path is None
        assert result.source_path is None
        assert result.files_downloaded == 0

    def test_success_result(self):
        result = GetResult(
            product_name="aha",
            success=True,
            dest_path="/tmp/aha",
            source_path="data/final/",
            files_downloaded=3,
        )
        assert result.success is True
        assert result.files_downloaded == 3


class TestGetDataProductValidation:
    """Tests for input validation and path traversal prevention."""

    def test_rejects_slash_in_product_name(self):
        result = get_data_product("../evil")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_backslash_in_product_name(self):
        result = get_data_product("..\\evil")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_dotdot_product_name(self):
        result = get_data_product("..")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_dot_product_name(self):
        result = get_data_product(".")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_allows_dots_in_product_name(self):
        """Legitimate product names with dots should not be rejected."""
        # This should NOT be rejected (dots are fine, only ".." as path component is bad)
        # It will fail at registry lookup, not at validation
        result = get_data_product("my-project.v2")
        # Should get past validation (fail at registry lookup instead)
        assert result.error_message is None or "Invalid product name" not in result.error_message

    def test_rejects_traversal_in_dest(self):
        result = get_data_product("good-name", dest="../../etc/passwd")
        assert result.success is False
        assert "Destination path must not contain '..'" in result.error_message

    def test_rejects_traversal_in_path(self):
        result = get_data_product("good-name", path="../../etc/passwd")
        assert result.success is False
        assert "Source path must not contain '..'" in result.error_message


class TestGetDataProduct:
    """Tests for the get_data_product business logic."""

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_default_downloads_data_final(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = get_data_product("aha-annual-survey", dest=str(tmp_path / "aha-annual-survey"), with_schema=False)

        mock_query.assert_called_once_with("aha-annual-survey")
        # Should call dvc get with data/final/
        dvc_args = mock_dvc.run_live.call_args[0]
        assert "get" in dvc_args
        assert "data/final/" in dvc_args
        assert result.success is True
        assert result.source_path == "data/final/"

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_custom_path(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = get_data_product(
            "aha-annual-survey",
            path="data/raw",
            dest=str(tmp_path / "aha-raw"),
        )

        dvc_args = mock_dvc.run_live.call_args[0]
        assert "data/raw" in dvc_args
        assert result.source_path == "data/raw"

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_with_schema(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        result = get_data_product("aha-annual-survey", dest=dest, with_schema=True)

        # Should make two dvc get calls: data/final/ and schemas/v1/schema.json
        assert mock_dvc.run_live.call_count == 2
        first_call = mock_dvc.run_live.call_args_list[0][0]
        second_call = mock_dvc.run_live.call_args_list[1][0]
        assert "data/final/" in first_call
        assert "schemas/v1/schema.json" in second_call

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_with_schema_failure_still_succeeds(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        """Schema download failure should warn but not fail the operation."""
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc
        # First call succeeds (data), second fails (schema)
        mock_dvc.run_live.side_effect = [None, Exception("schema not found")]

        dest = str(tmp_path / "aha")
        result = get_data_product("aha-annual-survey", dest=dest, with_schema=True)

        assert result.success is True

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_rev_passed_to_dvc(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        get_data_product("aha-annual-survey", dest=str(tmp_path / "aha"), rev="v2.0")

        dvc_args = mock_dvc.run_live.call_args[0]
        assert "--rev" in dvc_args
        assert "v2.0" in dvc_args

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_default_dest_uses_product_name(self, mock_query, mock_dvc_cmd, mock_data_product):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = get_data_product("aha-annual-survey")

        assert result.dest_path is not None
        assert "aha-annual-survey" in result.dest_path

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_ssh_url_used(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        get_data_product("aha-annual-survey", dest=str(tmp_path / "aha"))

        dvc_args = mock_dvc.run_live.call_args[0]
        assert "git@github.com:" in dvc_args[1]

    @patch("mintd.data_import.query_data_product")
    def test_registry_failure(self, mock_query, tmp_path):
        from mintd.exceptions import RegistryError
        mock_query.side_effect = RegistryError("not found")

        result = get_data_product("nonexistent", dest=str(tmp_path / "out"))

        assert result.success is False
        assert "not found" in result.error_message

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_dvc_failure(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc
        mock_dvc.run_live.side_effect = Exception("dvc get failed")

        result = get_data_product("aha-annual-survey", dest=str(tmp_path / "aha"))

        assert result.success is False
        assert "dvc get failed" in result.error_message

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.query_data_product")
    def test_dry_run_no_dvc_call(self, mock_query, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = get_data_product("aha-annual-survey", dest=str(tmp_path / "aha"), dry_run=True)

        mock_dvc.run_live.assert_not_called()
        assert result.success is True


class TestDataGetCLI:
    """Tests for the CLI command wiring."""

    @patch("mintd.data_import.get_data_product")
    def test_basic_invocation(self, mock_get):
        from mintd.cli.main import main

        mock_get.return_value = GetResult(
            product_name="aha-annual-survey", success=True,
            dest_path="./aha-annual-survey", source_path="data/final/",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["data", "get", "aha-annual-survey"])

        assert result.exit_code == 0
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs["product_name"] == "aha-annual-survey"

    @patch("mintd.data_import.get_data_product")
    def test_all_options(self, mock_get):
        from mintd.cli.main import main

        mock_get.return_value = GetResult(
            product_name="aha-annual-survey", success=True,
            dest_path="/tmp/out", source_path="data/raw",
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "get", "aha-annual-survey",
            "--dest", "/tmp/out",
            "--rev", "v2.0",
            "--path", "data/raw",
            "--no-schema",
            "--dry-run",
        ])

        assert result.exit_code == 0
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs["dest"] == "/tmp/out"
        assert call_kwargs["rev"] == "v2.0"
        assert call_kwargs["path"] == "data/raw"
        assert call_kwargs["with_schema"] is False
        assert call_kwargs["dry_run"] is True

    @patch("mintd.data_import.get_data_product")
    def test_failure_aborts(self, mock_get):
        from mintd.cli.main import main

        mock_get.return_value = GetResult(
            product_name="bad", success=False, error_message="not found",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["data", "get", "bad"])

        assert result.exit_code != 0
