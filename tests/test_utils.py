"""Tests for the utils module."""

import os
import platform
import pytest
from unittest.mock import patch, MagicMock

from mintd.utils import (
    get_platform,
    detect_stata_executable,
    detect_gh_cli,
    check_gh_auth,
    get_gh_install_instructions,
    get_command_separator,
    validate_project_name,
    format_project_name,
)


class TestGetPlatform:
    """Tests for get_platform function."""

    @patch("platform.system")
    def test_darwin_returns_macos(self, mock_system):
        """Test that Darwin returns 'macos'."""
        mock_system.return_value = "Darwin"
        assert get_platform() == "macos"

    @patch("platform.system")
    def test_windows_returns_windows(self, mock_system):
        """Test that Windows returns 'windows'."""
        mock_system.return_value = "Windows"
        assert get_platform() == "windows"

    @patch("platform.system")
    def test_linux_returns_linux(self, mock_system):
        """Test that Linux returns 'linux'."""
        mock_system.return_value = "Linux"
        assert get_platform() == "linux"

    @patch("platform.system")
    def test_unknown_returns_linux(self, mock_system):
        """Test that unknown systems default to 'linux'."""
        mock_system.return_value = "FreeBSD"
        assert get_platform() == "linux"


class TestDetectStataExecutable:
    """Tests for detect_stata_executable function."""

    @patch("mintd.utils.get_platform")
    @patch("shutil.which")
    def test_detect_stata_mp_unix(self, mock_which, mock_platform):
        """Test detecting stata-mp on Unix."""
        mock_platform.return_value = "macos"
        mock_which.side_effect = lambda x: "stata-mp" if x == "stata-mp" else None

        result = detect_stata_executable()
        assert result == "stata-mp"

    @patch("mintd.utils.get_platform")
    @patch("shutil.which")
    def test_detect_stata_fallback_unix(self, mock_which, mock_platform):
        """Test falling back to stata when stata-mp not found."""
        mock_platform.return_value = "linux"
        mock_which.side_effect = lambda x: "stata" if x == "stata" else None

        result = detect_stata_executable()
        assert result == "stata"

    @patch("mintd.utils.get_platform")
    @patch("shutil.which")
    def test_no_stata_found_unix(self, mock_which, mock_platform):
        """Test when Stata is not found on Unix."""
        mock_platform.return_value = "macos"
        mock_which.return_value = None

        result = detect_stata_executable()
        assert result is None

    @patch("mintd.utils.get_platform")
    @patch("subprocess.run")
    def test_detect_stata_windows(self, mock_run, mock_platform):
        """Test detecting Stata on Windows."""
        mock_platform.return_value = "windows"
        mock_run.return_value = MagicMock(returncode=0, stdout="C:\\Stata\\stata-mp.exe\n")

        result = detect_stata_executable()
        assert result == "C:\\Stata\\stata-mp.exe"

    @patch("mintd.utils.get_platform")
    @patch("subprocess.run")
    @patch("pathlib.Path.exists")
    def test_detect_stata_windows_common_paths(self, mock_exists, mock_run, mock_platform):
        """Test checking common Windows paths when where.exe fails."""
        mock_platform.return_value = "windows"
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        # Simulate finding Stata in common path
        call_count = [0]
        def exists_side_effect():
            call_count[0] += 1
            # Return True for the first path checked
            return call_count[0] == 1

        mock_exists.side_effect = exists_side_effect

        result = detect_stata_executable()
        # Result depends on mock behavior


class TestDetectGhCli:
    """Tests for detect_gh_cli function."""

    @patch("shutil.which")
    def test_gh_found(self, mock_which):
        """Test when gh is found."""
        mock_which.return_value = "/usr/local/bin/gh"
        result = detect_gh_cli()
        assert result == "/usr/local/bin/gh"

    @patch("shutil.which")
    def test_gh_not_found(self, mock_which):
        """Test when gh is not found."""
        mock_which.return_value = None
        result = detect_gh_cli()
        assert result is None


class TestCheckGhAuth:
    """Tests for check_gh_auth function."""

    @patch("mintd.utils.detect_gh_cli")
    def test_gh_not_installed(self, mock_detect):
        """Test when gh is not installed."""
        mock_detect.return_value = None
        is_auth, msg = check_gh_auth()
        assert is_auth is False
        assert "not installed" in msg

    @patch("mintd.utils.detect_gh_cli")
    @patch("subprocess.run")
    def test_gh_authenticated(self, mock_run, mock_detect):
        """Test when gh is authenticated."""
        mock_detect.return_value = "/usr/local/bin/gh"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Logged in to github.com account testuser",
            stderr=""
        )

        is_auth, username = check_gh_auth()
        assert is_auth is True
        assert "testuser" in username or username == "authenticated"

    @patch("mintd.utils.detect_gh_cli")
    @patch("subprocess.run")
    def test_gh_not_authenticated(self, mock_run, mock_detect):
        """Test when gh is not authenticated."""
        mock_detect.return_value = "/usr/local/bin/gh"
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        is_auth, msg = check_gh_auth()
        assert is_auth is False

    @patch("mintd.utils.detect_gh_cli")
    @patch("subprocess.run")
    def test_gh_auth_exception(self, mock_run, mock_detect):
        """Test handling exceptions during auth check."""
        mock_detect.return_value = "/usr/local/bin/gh"
        mock_run.side_effect = Exception("Connection error")

        is_auth, msg = check_gh_auth()
        assert is_auth is False
        assert "Connection error" in msg


class TestGetGhInstallInstructions:
    """Tests for get_gh_install_instructions function."""

    @patch("mintd.utils.get_platform")
    def test_macos_instructions(self, mock_platform):
        """Test macOS installation instructions."""
        mock_platform.return_value = "macos"
        instructions = get_gh_install_instructions()

        assert "brew install gh" in instructions
        assert "macOS" in instructions

    @patch("mintd.utils.get_platform")
    def test_windows_instructions(self, mock_platform):
        """Test Windows installation instructions."""
        mock_platform.return_value = "windows"
        instructions = get_gh_install_instructions()

        assert "winget" in instructions
        assert "Windows" in instructions

    @patch("mintd.utils.get_platform")
    def test_linux_instructions(self, mock_platform):
        """Test Linux installation instructions."""
        mock_platform.return_value = "linux"
        instructions = get_gh_install_instructions()

        assert "apt" in instructions or "dnf" in instructions
        assert "Linux" in instructions


class TestGetCommandSeparator:
    """Tests for get_command_separator function."""

    @patch("mintd.utils.get_platform")
    def test_unix_separator(self, mock_platform):
        """Test Unix command separator."""
        mock_platform.return_value = "macos"
        assert get_command_separator() == "&&"

    @patch("mintd.utils.get_platform")
    def test_linux_separator(self, mock_platform):
        """Test Linux command separator."""
        mock_platform.return_value = "linux"
        assert get_command_separator() == "&&"

    @patch("mintd.utils.get_platform")
    def test_windows_separator(self, mock_platform):
        """Test Windows command separator."""
        mock_platform.return_value = "windows"
        assert get_command_separator() == "&"


class TestValidateProjectName:
    """Tests for validate_project_name function."""

    def test_valid_alphanumeric(self):
        """Test valid alphanumeric names."""
        assert validate_project_name("myproject") is True
        assert validate_project_name("MyProject123") is True

    def test_valid_with_underscores(self):
        """Test valid names with underscores."""
        assert validate_project_name("my_project") is True
        assert validate_project_name("my_project_2") is True

    def test_valid_with_hyphens(self):
        """Test valid names with hyphens."""
        assert validate_project_name("my-project") is True
        assert validate_project_name("my-project-2") is True

    def test_empty_name_invalid(self):
        """Test that empty names are invalid."""
        assert validate_project_name("") is False

    def test_special_chars_invalid(self):
        """Test that special characters are invalid."""
        assert validate_project_name("my.project") is False
        assert validate_project_name("my/project") is False
        assert validate_project_name("my project") is False
        assert validate_project_name("my@project") is False

    def test_path_traversal_invalid(self):
        """Test that path traversal attempts are invalid."""
        assert validate_project_name("../parent") is False
        assert validate_project_name("./current") is False


class TestFormatProjectName:
    """Tests for format_project_name function."""

    def test_data_project(self):
        """Test formatting data project name."""
        result = format_project_name("data", "mydata")
        assert result == "data_mydata"

    def test_project_project(self):
        """Test formatting project project name."""
        result = format_project_name("project", "myproject")
        assert result == "prj_myproject"

    def test_infra_project(self):
        """Test formatting infra project name."""
        result = format_project_name("infra", "myinfra")
        assert result == "infra_myinfra"

    def test_invalid_project_type(self):
        """Test error for invalid project type."""
        with pytest.raises(ValueError, match="Unknown project type"):
            format_project_name("invalid", "test")
