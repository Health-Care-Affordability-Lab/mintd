"""Unified shell command execution with consistent error handling.

This module provides a single abstraction for running external commands
(git, dvc, gh, etc.) with proper error handling and helpful error messages.
"""

from __future__ import annotations

import shutil
import subprocess
from enum import Enum
from pathlib import Path
from typing import Optional

from .exceptions import (
    CommandNotFoundError,
    DVCError,
    GHCLIError,
    GitError,
    ShellCommandError,
)


class CommandType(Enum):
    """Supported command types with associated error classes and installation hints."""

    GIT = ("git", GitError, "Install git from https://git-scm.com/")
    DVC = ("dvc", DVCError, "Install DVC with: pip install dvc or see https://dvc.org/doc/install")
    GH = ("gh", GHCLIError, "Install GitHub CLI from https://cli.github.com/ or run 'mintd config setup'")
    GENERIC = ("", ShellCommandError, "")

    def __init__(self, executable: str, error_class: type, install_hint: str):
        self.executable = executable
        self.error_class = error_class
        self.install_hint = install_hint


class ShellCommand:
    """Unified shell command execution with consistent error handling.

    This class consolidates the duplicate shell command implementations
    from registry.py, initializers/git.py, and initializers/storage.py.

    Example:
        git = ShellCommand(CommandType.GIT, cwd=project_path)
        result = git.run("status")
        output = git.run_output("rev-parse", "--abbrev-ref", "HEAD")
    """

    def __init__(
        self,
        command_type: CommandType,
        cwd: Optional[Path] = None,
    ):
        """Initialize ShellCommand.

        Args:
            command_type: Type of command (GIT, DVC, GH, or GENERIC)
            cwd: Working directory for command execution
        """
        self.command_type = command_type
        self.cwd = cwd
        self._executable: Optional[str] = None

    @property
    def executable(self) -> str:
        """Get the executable path, caching the result."""
        if self._executable is None:
            self._executable = self.command_type.executable
        return self._executable

    def _build_command(self, *args: str) -> list[str]:
        """Build the full command list."""
        if self.command_type == CommandType.GENERIC:
            return list(args)
        return [self.executable] + list(args)

    def _check_executable(self) -> None:
        """Check if the required executable is available."""
        if self.command_type == CommandType.GENERIC:
            return

        if not shutil.which(self.executable):
            raise CommandNotFoundError(
                message=f"{self.executable} command not found",
                command=[self.executable],
                suggestion=self.command_type.install_hint,
            )

    def _create_error(
        self,
        message: str,
        cmd: list[str],
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> ShellCommandError:
        """Create an appropriate error for the command type."""
        error_class = self.command_type.error_class

        # Add command-specific suggestions
        suggestion = None
        if self.command_type == CommandType.GH:
            if "gh auth login" in stderr or "GH_TOKEN" in stderr:
                suggestion = "GitHub CLI is not authenticated. Run: gh auth login"
            elif "not found" in stderr.lower():
                suggestion = self.command_type.install_hint

        return error_class(
            message=message,
            command=cmd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            suggestion=suggestion,
        )

    def run(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        env: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        """Run the command and return the result.

        Args:
            *args: Command arguments
            check: If True, raise an exception on non-zero exit code
            capture_output: If True, capture stdout and stderr
            text: If True, decode output as text
            env: Environment variables to set

        Returns:
            CompletedProcess with command results

        Raises:
            CommandNotFoundError: If the executable is not found
            GitError/DVCError/GHCLIError: On command failure (if check=True)
        """
        self._check_executable()
        cmd = self._build_command(*args)

        try:
            result = subprocess.run(
                cmd,
                cwd=self.cwd,
                capture_output=capture_output,
                text=text,
                check=check,
                env=env,
            )
            return result
        except subprocess.CalledProcessError as e:
            raise self._create_error(
                message=f"{self.command_type.executable} command failed",
                cmd=cmd,
                returncode=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
            ) from e
        except FileNotFoundError as e:
            raise CommandNotFoundError(
                message=f"{self.executable} command not found",
                command=cmd,
                suggestion=self.command_type.install_hint,
            ) from e

    def run_output(self, *args: str) -> str:
        """Run the command and return stdout only.

        Args:
            *args: Command arguments

        Returns:
            Command stdout with trailing whitespace stripped

        Raises:
            CommandNotFoundError: If the executable is not found
            GitError/DVCError/GHCLIError: On command failure
        """
        result = self.run(*args)
        return result.stdout.strip()

    def run_silent(self, *args: str, check: bool = True) -> bool:
        """Run the command silently, returning success status.

        Args:
            *args: Command arguments
            check: If True, raise on failure; if False, return False

        Returns:
            True if command succeeded, False if failed (when check=False)

        Raises:
            CommandNotFoundError: If the executable is not found
            GitError/DVCError/GHCLIError: On command failure (if check=True)
        """
        try:
            self.run(*args, check=check)
            return True
        except ShellCommandError:
            if check:
                raise
            return False


# Convenience functions for common use cases
def git_command(cwd: Optional[Path] = None) -> ShellCommand:
    """Create a ShellCommand for git operations."""
    return ShellCommand(CommandType.GIT, cwd=cwd)


def dvc_command(cwd: Optional[Path] = None) -> ShellCommand:
    """Create a ShellCommand for DVC operations."""
    return ShellCommand(CommandType.DVC, cwd=cwd)


def gh_command(cwd: Optional[Path] = None) -> ShellCommand:
    """Create a ShellCommand for GitHub CLI operations."""
    return ShellCommand(CommandType.GH, cwd=cwd)
