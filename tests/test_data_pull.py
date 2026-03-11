"""Tests for mintd data pull functionality."""

import json
from pathlib import Path
from unittest.mock import Mock, patch, call

import pytest
from click.testing import CliRunner

from mintd.data_import import GetResult, clone_and_pull_product, pull_local, _resolve_primary_path, _resolve_dvc_target


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


@pytest.fixture
def mock_data_product_with_products():
    """Mock catalog entry with data_products section."""
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
        "data_products": {
            "primary": "data/analysis/",
            "outputs": [
                {"path": "data/analysis/", "description": "Main analysis", "primary": True},
                {"path": "data/raw/", "description": "Raw data"},
            ],
        },
    }


class TestResolvePrimaryPath:
    def test_returns_primary_when_present(self, mock_data_product_with_products):
        assert _resolve_primary_path(mock_data_product_with_products) == "data/analysis/"

    def test_falls_back_to_data_final(self, mock_data_product):
        assert _resolve_primary_path(mock_data_product) == "data/final/"

    def test_empty_data_products(self):
        assert _resolve_primary_path({"data_products": {}}) == "data/final/"


class TestCloneAndPullValidation:
    def test_rejects_slash_in_product_name(self):
        result = clone_and_pull_product("../evil")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_backslash_in_product_name(self):
        result = clone_and_pull_product("..\\evil")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_dotdot_product_name(self):
        result = clone_and_pull_product("..")
        assert result.success is False
        assert "Invalid product name" in result.error_message

    def test_rejects_traversal_in_dest(self):
        result = clone_and_pull_product("good-name", dest="../../etc/passwd")
        assert result.success is False
        assert "Destination path must not contain '..'" in result.error_message


class TestCloneAndPullProduct:
    @patch("mintd.data_import._resolve_dvc_target", return_value=["data/final.dvc"])
    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_clones_and_pulls_primary(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_resolve, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        result = clone_and_pull_product("aha-annual-survey", dest=dest)

        assert result.success is True
        # Verify git clone called with SSH URL and --depth 1
        clone_args = mock_git.run.call_args[0]
        assert "clone" in clone_args
        assert "--depth" in clone_args
        assert "1" in clone_args
        assert "git@github.com:" in clone_args[-2]

        # Verify dvc pull called with primary .dvc file
        dvc_calls = mock_dvc.run_live.call_args[0]
        assert "pull" in dvc_calls
        assert "data/final.dvc" in dvc_calls

    @patch("mintd.data_import._resolve_dvc_target", return_value=["data/analysis.dvc"])
    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_uses_data_products_primary(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_resolve, mock_data_product_with_products, tmp_path):
        mock_query.return_value = mock_data_product_with_products
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        result = clone_and_pull_product("aha-annual-survey", dest=dest)

        assert result.success is True
        assert result.source_path == "data/analysis/"
        dvc_calls = mock_dvc.run_live.call_args[0]
        assert "data/analysis.dvc" in dvc_calls

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_pull_all_skips_primary_path(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        result = clone_and_pull_product("aha-annual-survey", dest=dest, pull_all=True)

        assert result.success is True
        assert result.source_path == "all"
        dvc_calls = mock_dvc.run_live.call_args[0]
        assert "pull" in dvc_calls
        # Should NOT contain a specific .dvc target — just "pull" + remote flags
        assert "data/final.dvc" not in dvc_calls

    @patch("mintd.data_import._resolve_dvc_target", return_value=["data/final.dvc"])
    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_rev_passed_to_git_clone(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_resolve, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        clone_and_pull_product("aha-annual-survey", dest=dest, rev="v2.0")

        clone_args = mock_git.run.call_args[0]
        assert "--branch" in clone_args
        assert "v2.0" in clone_args

    @patch("mintd.data_import._resolve_dvc_target", return_value=["data/final.dvc"])
    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_jobs_passed_to_dvc(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_resolve, mock_data_product, tmp_path):
        mock_query.return_value = mock_data_product
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        dest = str(tmp_path / "aha")
        clone_and_pull_product("aha-annual-survey", dest=dest, jobs=4)

        dvc_calls = mock_dvc.run_live.call_args[0]
        assert "-j" in dvc_calls
        assert "4" in dvc_calls

    @patch("mintd.data_import.query_data_product")
    def test_registry_failure(self, mock_query):
        from mintd.exceptions import RegistryError
        mock_query.side_effect = RegistryError("not found")

        result = clone_and_pull_product("nonexistent")

        assert result.success is False
        assert "not found" in result.error_message

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.git_command")
    @patch("mintd.data_import.query_data_product")
    def test_default_dest_uses_product_name(self, mock_query, mock_git_cmd, mock_dvc_cmd, mock_data_product):
        mock_query.return_value = mock_data_product
        mock_git = Mock()
        mock_git_cmd.return_value = mock_git
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = clone_and_pull_product("aha-annual-survey")

        assert result.dest_path is not None
        assert "aha-annual-survey" in result.dest_path


class TestPullLocal:
    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.get_project_remote")
    def test_pulls_with_remote(self, mock_remote, mock_dvc_cmd, tmp_path):
        mock_remote.return_value = "data_my-project"
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        result = pull_local(project_path=tmp_path)

        assert result is True
        dvc_args = mock_dvc.run_live.call_args[0]
        assert "pull" in dvc_args
        assert "-r" in dvc_args
        assert "data_my-project" in dvc_args

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.get_project_remote")
    def test_passes_jobs(self, mock_remote, mock_dvc_cmd, tmp_path):
        mock_remote.return_value = "data_my-project"
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        pull_local(project_path=tmp_path, jobs=8)

        dvc_args = mock_dvc.run_live.call_args[0]
        assert "-j" in dvc_args
        assert "8" in dvc_args

    @patch("mintd.data_import.dvc_command")
    @patch("mintd.data_import.get_project_remote")
    def test_passes_targets(self, mock_remote, mock_dvc_cmd, tmp_path):
        mock_remote.return_value = "data_my-project"
        mock_dvc = Mock()
        mock_dvc_cmd.return_value = mock_dvc

        pull_local(project_path=tmp_path, targets=["data/final.dvc"])

        dvc_args = mock_dvc.run_live.call_args[0]
        assert "data/final.dvc" in dvc_args


class TestDataPullCLI:
    """Tests for the CLI command wiring."""

    @patch("mintd.data_import.clone_and_pull_product")
    def test_remote_pull_with_product_name(self, mock_clone):
        from mintd.cli.main import main

        mock_clone.return_value = GetResult(
            product_name="aha-annual-survey", success=True,
            dest_path="./aha-annual-survey", source_path="data/final/",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["data", "pull", "aha-annual-survey"])

        assert result.exit_code == 0
        mock_clone.assert_called_once()
        call_kwargs = mock_clone.call_args[1]
        assert call_kwargs["product_name"] == "aha-annual-survey"

    @patch("mintd.data_import.clone_and_pull_product")
    def test_remote_pull_with_options(self, mock_clone):
        from mintd.cli.main import main

        mock_clone.return_value = GetResult(
            product_name="aha-annual-survey", success=True,
            dest_path="/tmp/out", source_path="all",
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "data", "pull", "aha-annual-survey",
            "--dest", "/tmp/out",
            "--rev", "v2.0",
            "--all",
            "-j", "4",
        ])

        assert result.exit_code == 0
        call_kwargs = mock_clone.call_args[1]
        assert call_kwargs["dest"] == "/tmp/out"
        assert call_kwargs["rev"] == "v2.0"
        assert call_kwargs["pull_all"] is True
        assert call_kwargs["jobs"] == 4

    @patch("mintd.data_import.clone_and_pull_product")
    def test_remote_pull_failure_aborts(self, mock_clone):
        from mintd.cli.main import main

        mock_clone.return_value = GetResult(
            product_name="bad", success=False, error_message="not found",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["data", "pull", "bad"])

        assert result.exit_code != 0

    @patch("mintd.data_import.pull_local")
    def test_local_pull_inside_project(self, mock_pull, tmp_path):
        from mintd.cli.main import main

        # Create metadata.json so it detects a mintd project
        metadata = {"project": {"name": "test", "type": "data", "full_name": "data_test"}}
        (tmp_path / "metadata.json").write_text(json.dumps(metadata))
        mock_pull.return_value = True

        runner = CliRunner()
        result = runner.invoke(main, ["data", "pull", "-p", str(tmp_path)])

        assert result.exit_code == 0
        mock_pull.assert_called_once()

    def test_no_args_outside_project_shows_help(self, tmp_path):
        from mintd.cli.main import main

        runner = CliRunner()
        result = runner.invoke(main, ["data", "pull", "-p", str(tmp_path)])

        assert result.exit_code != 0
        assert "mintd data pull <product_name>" in result.output


# =============================================================================
# _resolve_dvc_target Tests
# =============================================================================

class TestResolveDvcTarget:
    """Test DVC target resolution for different repo layouts."""

    def test_standalone_dvc_file(self, tmp_path):
        """When data/final.dvc exists, return it as the target."""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "final.dvc").write_text("outs:\n- md5: abc\n")
        assert _resolve_dvc_target(tmp_path, "data/final/") == ["data/final.dvc"]

    def test_nested_dvc_files(self, tmp_path):
        """When .dvc files exist inside data/final/, return all of them."""
        final_dir = tmp_path / "data" / "final"
        final_dir.mkdir(parents=True)
        (final_dir / "deprivation.dvc").write_text("outs:\n- md5: abc\n")
        (final_dir / "scores.dvc").write_text("outs:\n- md5: def\n")
        result = _resolve_dvc_target(tmp_path, "data/final/")
        assert sorted(result) == ["data/final/deprivation.dvc", "data/final/scores.dvc"]

    def test_nested_dvc_files_in_subdirs(self, tmp_path):
        """When .dvc files exist in subdirectories under primary path, find them."""
        sub_dir = tmp_path / "data" / "final" / "sub"
        sub_dir.mkdir(parents=True)
        (sub_dir / "deep.dvc").write_text("outs:\n- md5: abc\n")
        result = _resolve_dvc_target(tmp_path, "data/final/")
        assert result == ["data/final/sub/deep.dvc"]

    def test_pipeline_output(self, tmp_path):
        """When data/final/ is a pipeline output in dvc.yaml, return the path."""
        dvc_yaml = {
            "stages": {
                "build": {
                    "cmd": "python build.py",
                    "outs": ["data/final/"],
                }
            }
        }
        import yaml
        (tmp_path / "dvc.yaml").write_text(yaml.dump(dvc_yaml))
        assert _resolve_dvc_target(tmp_path, "data/final/") == ["data/final/"]

    def test_pipeline_output_without_trailing_slash(self, tmp_path):
        """Trailing slash mismatch should still match."""
        dvc_yaml = {
            "stages": {
                "build": {
                    "cmd": "python build.py",
                    "outs": ["data/final"],
                }
            }
        }
        import yaml
        (tmp_path / "dvc.yaml").write_text(yaml.dump(dvc_yaml))
        assert _resolve_dvc_target(tmp_path, "data/final/") == ["data/final"]

    def test_pipeline_nested_outputs(self, tmp_path):
        """Pipeline outputs nested under primary path should be collected."""
        dvc_yaml = {
            "stages": {
                "build_a": {
                    "cmd": "python build_a.py",
                    "outs": ["data/final/table_a.parquet"],
                },
                "build_b": {
                    "cmd": "python build_b.py",
                    "outs": ["data/final/table_b.parquet"],
                },
                "other": {
                    "cmd": "python other.py",
                    "outs": ["data/other/"],
                },
            }
        }
        import yaml
        (tmp_path / "dvc.yaml").write_text(yaml.dump(dvc_yaml))
        result = _resolve_dvc_target(tmp_path, "data/final/")
        assert sorted(result) == ["data/final/table_a.parquet", "data/final/table_b.parquet"]

    def test_no_match_returns_empty(self, tmp_path):
        """When neither .dvc file nor pipeline output exists, return empty list."""
        assert _resolve_dvc_target(tmp_path, "data/final/") == []

    def test_no_match_with_dvc_yaml(self, tmp_path):
        """dvc.yaml exists but doesn't contain the primary path."""
        dvc_yaml = {
            "stages": {
                "build": {
                    "cmd": "python build.py",
                    "outs": ["data/other/"],
                }
            }
        }
        import yaml
        (tmp_path / "dvc.yaml").write_text(yaml.dump(dvc_yaml))
        assert _resolve_dvc_target(tmp_path, "data/final/") == []

    def test_fallback_warning_in_clone_and_pull(self, tmp_path):
        """When target can't be resolved, clone_and_pull falls back to pulling all."""
        from unittest.mock import patch, Mock

        mock_data_product = {
            "repository": {"github_url": "https://github.com/test/repo"},
            "storage": {"dvc": {"remote_name": "myremote"}},
        }

        with patch("mintd.data_import.query_data_product", return_value=mock_data_product), \
             patch("mintd.data_import.git_command") as mock_git_cmd, \
             patch("mintd.data_import.dvc_command") as mock_dvc_cmd, \
             patch("mintd.data_import._resolve_dvc_target", return_value=[]):
            mock_git_cmd.return_value = Mock()
            mock_dvc = Mock()
            mock_dvc_cmd.return_value = mock_dvc

            dest = str(tmp_path / "repo")
            result = clone_and_pull_product("test-product", dest=dest)

            assert result.success is True
            # dvc pull should have no specific target — just pull + remote
            dvc_calls = mock_dvc.run_live.call_args[0]
            assert "pull" in dvc_calls
            assert "data/final/" not in dvc_calls
            assert "data/final.dvc" not in dvc_calls
