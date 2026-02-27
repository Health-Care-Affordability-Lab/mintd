"""Tests for template functionality."""

import tempfile

from mintd.templates import DataTemplate, ProjectTemplate, InfraTemplate


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


def test_infra_template():
    """Test infra template creation."""
    template = InfraTemplate()

    # Test directory structure
    structure = template.get_directory_structure()
    assert "code" in structure
    assert "tests" in structure
    assert "data" in structure
    assert structure["data"]["analysis"] == {".gitkeep": None}
    assert structure["data"]["raw"] == {".gitkeep": None}

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



