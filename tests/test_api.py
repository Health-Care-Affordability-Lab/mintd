"""Tests for API functionality."""

import tempfile
from pathlib import Path

from mint.api import create_project


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
        assert result.full_name == "prj__test_api"
        assert result.project_type == "project"
        assert result.path.exists()
        assert (result.path / "src" / "r" / "analysis.R").exists()


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
        assert (result.path / "src" / "test_api" / "__init__.py").exists()


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


