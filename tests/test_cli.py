"""Tests for CLI functionality."""

from click.testing import CliRunner
from mintd.cli import main


def test_cli_main():
    """Test that the main CLI command works."""
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "mintd - Lab Project Scaffolding Tool" in result.output


def test_create_data_help():
    """Test the create data command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "data", "--help"])
    assert result.exit_code == 0
    assert "data_{name}" in result.output


def test_create_project_help():
    """Test the create project command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "project", "--help"])
    assert result.exit_code == 0
    assert "prj__{name}" in result.output


def test_create_infra_help():
    """Test the create infra command help."""
    runner = CliRunner()
    result = runner.invoke(main, ["create", "infra", "--help"])
    assert result.exit_code == 0
    assert "infra_{name}" in result.output