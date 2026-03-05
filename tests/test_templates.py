"""Tests for template functionality."""

import json
import tempfile

from jinja2 import Environment, FileSystemLoader

from mintd.templates import CodeTemplate, DataTemplate, ProjectTemplate
from mintd.templates.base import BaseTemplate


def test_data_template():
    """Test data template creation."""
    template = DataTemplate()

    # Test directory structure
    structure = template.get_directory_structure()
    assert "README.md" in structure
    assert "code" in structure
    assert "data" in structure
    assert structure["data"]["raw"] == {".gitkeep": None}
    assert structure["data"]["intermediate"] == {".gitkeep": None}
    assert structure["data"]["final"] == {".gitkeep": None}

    # Test template files
    template_files = template.get_template_files()
    file_names = [name for _, name in template_files]
    assert "README_data.md.j2" in file_names
    assert "metadata.json.j2" in file_names


def test_project_template():
    """Test project template creation."""
    template = ProjectTemplate()

    # Test directory structure
    structure = template.get_directory_structure()
    assert "code" in structure
    # Check numbered code subdirectories (config/utils at code/ level)
    assert "01_data_prep" in structure["code"]
    assert "02_analysis" in structure["code"]
    assert "03_tables" in structure["code"]
    assert "04_figures" in structure["code"]
    # Check data directories (AEA-compliant)
    assert "data" in structure
    assert "notebooks" in structure
    assert "references" in structure
    assert "tests" in structure
    assert structure["data"]["raw"] == {".gitkeep": None}
    assert structure["data"]["analysis"] == {".gitkeep": None}
    # Check results directories include estimates
    assert "results" in structure
    assert "estimates" in structure["results"]
    assert "citations.md" in structure

    # Test template files
    template_files = template.get_template_files()
    file_names = [name for _, name in template_files]
    assert "README_project.md.j2" in file_names
    assert "citations.md.j2" in file_names


def test_template_creation():
    """Test actual template creation."""
    template = DataTemplate()

    with tempfile.TemporaryDirectory() as temp_dir:
        result_path = template.create("test_template", temp_dir)

        assert result_path.exists()
        assert (result_path / "README.md").exists()
        assert (result_path / "metadata.json").exists()
        assert (result_path / "code" / "_mintd_utils.py").exists()
        assert (result_path / "data" / "raw" / ".gitkeep").exists()
        assert (result_path / "data" / "intermediate" / ".gitkeep").exists()
        assert (result_path / "data" / "final" / ".gitkeep").exists()


def test_template_with_context():
    """Test template creation with custom context."""
    template = ProjectTemplate()

    context = {
        "author": "Test Author",
        "organization": "Test Lab"
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        result_path = template.create("test_context", temp_dir, **context)

        # Check that context was used in README
        readme_content = (result_path / "README.md").read_text()
        assert "Test Author" in readme_content
        assert "Test Lab" in readme_content


# --- Code template tests ---


class TestCodeTemplate:
    """Tests for code-only project template."""

    def test_code_template_structure_is_minimal(self):
        """Code template should only create metadata.json — no directories."""
        template = CodeTemplate()
        structure = template.get_directory_structure()

        # Only metadata.json, nothing else
        assert "metadata.json" in structure
        assert len(structure) == 1

        # Must NOT have data/project scaffold dirs
        assert "data" not in structure
        assert "code" not in structure
        assert "results" not in structure
        assert "notebooks" not in structure
        assert "README.md" not in structure

    def test_code_template_has_no_prefix(self):
        """Code repos keep their own name — no data_/prj_ prefix."""
        template = CodeTemplate()
        assert template.prefix == ""

    def test_code_template_type(self):
        """Template type should be 'code'."""
        template = CodeTemplate()
        assert template.template_type == "code"

    def test_code_template_files_only_metadata(self):
        """Only template file should be metadata_code.json.j2."""
        template = CodeTemplate()
        template_files = template.get_template_files()

        assert len(template_files) == 1
        path, tmpl_name = template_files[0]
        assert path == "metadata.json"
        assert tmpl_name == "metadata_code.json.j2"

    def test_code_template_creation(self):
        """Test actual creation — should produce only metadata.json."""
        template = CodeTemplate()
        context = {
            "author": "mad265",
            "organization": "health-care-affordability-lab",
            "team": "health-econ",
            "admin_team": "infrastructure-admins",
            "researcher_team": "all-researchers",
            "classification": "private",
            "contract_info": "",
            "registry_org": "health-care-affordability-lab",
            "language": "python",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = template.create("my-stata-utils", temp_dir, **context)

            # Path should be just the name (no prefix)
            assert result_path.name == "my-stata-utils"
            assert result_path.exists()

            # Only metadata.json should exist
            assert (result_path / "metadata.json").exists()

            # No scaffold directories
            assert not (result_path / "data").exists()
            assert not (result_path / "code").exists()
            assert not (result_path / "results").exists()
            assert not (result_path / "README.md").exists()

    def test_code_template_metadata_content(self):
        """Metadata should have governance, mirror, and ownership — no storage/DVC."""
        template = CodeTemplate()
        context = {
            "author": "mad265",
            "organization": "health-care-affordability-lab",
            "team": "health-econ",
            "admin_team": "infrastructure-admins",
            "researcher_team": "all-researchers",
            "classification": "private",
            "contract_info": "",
            "registry_org": "health-care-affordability-lab",
            "language": "stata",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = template.create("my-stata-utils", temp_dir, **context)
            metadata = json.loads((result_path / "metadata.json").read_text())

            # Project section
            assert metadata["project"]["type"] == "code"
            assert metadata["project"]["name"] == "my-stata-utils"
            assert metadata["project"]["full_name"] == "my-stata-utils"

            # Governance
            assert metadata["governance"]["classification"] == "private"

            # Ownership
            assert metadata["ownership"]["team"] == "health-econ"

            # Access control
            teams = metadata["access_control"]["teams"]
            assert any(t["permission"] == "admin" for t in teams)

            # Repository with mirror section
            assert "repository" in metadata
            assert "mirror" in metadata["repository"]

            # Must NOT have storage/DVC/schema sections
            assert "storage" not in metadata
            assert "schema" not in metadata
            assert "lifecycle" not in metadata

    def test_code_template_use_current_repo(self):
        """With use_current_repo, metadata.json drops into current dir."""
        template = CodeTemplate()
        context = {
            "author": "mad265",
            "organization": "health-care-affordability-lab",
            "team": "health-econ",
            "admin_team": "infrastructure-admins",
            "researcher_team": "all-researchers",
            "classification": "public",
            "contract_info": "",
            "registry_org": "health-care-affordability-lab",
            "language": "python",
            "use_current_repo": True,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = template.create("my-lib", temp_dir, **context)

            # Should use temp_dir directly, not create a subdirectory
            assert str(result_path) == temp_dir
            assert (result_path / "metadata.json").exists()


class TestCodeRegistry:
    """Tests for code type in registry."""

    def test_registry_type_dir_includes_code(self):
        """Registry should map 'code' type to 'code' catalog directory."""
        from mintd.registry import LocalRegistry

        registry = LocalRegistry("https://github.com/health-care-affordability-lab/test-registry")

        # _write_catalog_entry uses a type_dir mapping — code should be included
        # We test indirectly by checking it doesn't KeyError
        import tempfile
        from pathlib import Path
        registry.temp_dir = Path(tempfile.mkdtemp())
        registry.repo_path = registry.temp_dir

        catalog_entry = {"project": {"type": "code"}}
        catalog_path = registry._write_catalog_entry(catalog_entry, "test-lib")

        assert "catalog/code/test-lib.yaml" in str(catalog_path)

        # Cleanup
        import shutil
        shutil.rmtree(registry.temp_dir)


class TestStataScaffoldBugs:
    """Regression tests for Stata scaffold bugs."""

    def _render_template(self, template_name, **context):
        """Render a Jinja2 template with given context."""
        template = DataTemplate()
        jinja_env = template.jinja_env
        tmpl = jinja_env.get_template(template_name)
        return tmpl.render(**context)

    def test_dvc_yaml_stata_uses_code_wdir(self):
        """Bug 3: dvc.yaml should use wdir: code for Stata projects."""
        content = self._render_template(
            "dvc_data.yaml.j2",
            language="stata",
            source_dir="code",
        )
        assert "wdir: code" in content
        assert "wdir: src" not in content

    def test_dvc_yaml_python_uses_src_wdir(self):
        """dvc.yaml should still use wdir: src for Python projects."""
        content = self._render_template(
            "dvc_data.yaml.j2",
            language="python",
            source_dir="src",
        )
        assert "wdir: src" in content
        assert "wdir: code" not in content

    def test_dvc_yaml_r_uses_src_wdir(self):
        """dvc.yaml should still use wdir: src for R projects."""
        content = self._render_template(
            "dvc_data.yaml.j2",
            language="r",
            source_dir="src",
        )
        assert "wdir: src" in content
        assert "wdir: code" not in content

    def test_mintd_utils_do_no_strrpos(self):
        """Bug 1: _mintd_utils.do must not use non-existent strrpos function."""
        content = self._render_template(
            "_mintd_utils.do.j2",
            project_name="test_project",
        )
        assert "strrpos" not in content
        assert "ustrregexm" in content

    def test_generate_schema_py_uses_file_path(self):
        """Bug 2: generate_schema.py should resolve paths via __file__, not CWD."""
        content = self._render_template(
            "generate_schema.py.j2",
            project_name="test_project",
        )
        assert '__file__' in content
        assert 'Path("v1")' not in content
        assert 'Path("../data/final")' not in content


class TestCodeCLI:
    """Tests for `mintd create code` CLI command."""

    def test_create_code_help(self):
        """The create code command should exist and show help."""
        from click.testing import CliRunner
        from mintd.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["create", "code", "--help"])

        assert result.exit_code == 0
        assert "--name" in result.output
        assert "--lang" in result.output
        assert "--team" in result.output

    def test_create_code_produces_metadata_only(self):
        """create code should produce only metadata.json, no scaffold."""
        from click.testing import CliRunner
        from mintd.cli import main
        from pathlib import Path

        runner = CliRunner()

        with tempfile.TemporaryDirectory() as temp_dir:
            result = runner.invoke(main, [
                "create", "code",
                "--name", "my-test-lib",
                "--lang", "python",
                "--private",
                "--team", "health-econ",
                "--path", temp_dir,
                "--no-git",
            ])

            assert result.exit_code == 0, f"Failed: {result.output}"

            project_path = Path(temp_dir) / "my-test-lib"
            assert (project_path / "metadata.json").exists()

            # No scaffold
            assert not (project_path / "data").exists()
            assert not (project_path / "code").exists()
            assert not (project_path / "README.md").exists()
            assert not (project_path / ".dvcignore").exists()

