"""Tests for template functionality."""

import tempfile
from pathlib import Path

from mint.templates import DataTemplate, ProjectTemplate, InfraTemplate


def test_data_template():
    """Test data template creation."""
    template = DataTemplate()

    # Test directory structure
    structure = template.get_directory_structure()
    assert "README.md" in structure
    assert "src" in structure
    assert "data" in structure
    assert structure["data"]["raw"] == {".gitkeep": None}

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
    assert "src" in structure
    assert "analysis" in structure["src"]
    assert "stata" in structure["src"]
    assert "r" in structure["src"]

    # Test template files
    template_files = template.get_template_files()
    file_names = [name for _, name in template_files]
    assert "README_project.md.j2" in file_names
    assert "analysis.R.j2" in file_names
    assert ".Rprofile.j2" in file_names


def test_infra_template():
    """Test infra template creation."""
    template = InfraTemplate()

    # Test directory structure
    structure = template.get_directory_structure()
    assert "src" in structure
    assert "tests" in structure

    # Test template files
    template_files = template.get_template_files()
    file_names = [name for _, name in template_files]
    assert "README_infra.md.j2" in file_names
    assert "pyproject_infra.toml.j2" in file_names


def test_template_creation():
    """Test actual template creation."""
    template = DataTemplate()

    with tempfile.TemporaryDirectory() as temp_dir:
        result_path = template.create("test_template", temp_dir)

        assert result_path.exists()
        assert (result_path / "README.md").exists()
        assert (result_path / "metadata.json").exists()
        assert (result_path / "src" / "__init__.py").exists()
        assert (result_path / "data" / "raw" / ".gitkeep").exists()


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
