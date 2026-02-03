"""Custom exception hierarchy for mintd.

This module provides a structured exception hierarchy that enables:
- Precise error handling for different failure modes
- Consistent error messages and guidance
- Better debugging through specific exception types
"""

from __future__ import annotations

from typing import Optional


class MintdError(Exception):
    """Base exception for all mintd errors.

    All custom exceptions in mintd should inherit from this class
    to enable catching all mintd-specific errors with a single except clause.
    """

    def __init__(self, message: str, suggestion: Optional[str] = None):
        """Initialize MintdError.

        Args:
            message: Error message describing what went wrong
            suggestion: Optional suggestion for how to resolve the error
        """
        self.message = message
        self.suggestion = suggestion
        super().__init__(message)

    def __str__(self) -> str:
        if self.suggestion:
            return f"{self.message}\n\n💡 {self.suggestion}"
        return self.message


# Project-related exceptions
class ProjectError(MintdError):
    """Base exception for project operations."""
    pass


class ProjectCreationError(ProjectError):
    """Error during project creation."""
    pass


class ProjectValidationError(ProjectError):
    """Error during project validation."""
    pass


class ProjectNotFoundError(ProjectError):
    """Project not found at expected location."""
    pass


# Registry-related exceptions
class RegistryError(MintdError):
    """Base exception for registry operations."""
    pass


class RegistryConnectionError(RegistryError):
    """Error connecting to or accessing the registry."""
    pass


class RegistryValidationError(RegistryError):
    """Error validating registry data."""
    pass


# Configuration-related exceptions
class ConfigError(MintdError):
    """Base exception for configuration operations."""
    pass


class ConfigValidationError(ConfigError):
    """Error validating configuration."""
    pass


class ConfigNotFoundError(ConfigError):
    """Configuration file not found."""
    pass


class CredentialError(ConfigError):
    """Error with credentials (missing, invalid, expired)."""
    pass


# Shell command exceptions
class ShellCommandError(MintdError):
    """Base exception for shell command execution errors.

    Attributes:
        command: The command that was executed
        returncode: Exit code from the command
        stdout: Standard output from the command
        stderr: Standard error from the command
    """

    def __init__(
        self,
        message: str,
        command: Optional[list[str]] = None,
        returncode: Optional[int] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        suggestion: Optional[str] = None,
    ):
        super().__init__(message, suggestion)
        self.command = command or []
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""

    def __str__(self) -> str:
        parts = [self.message]
        if self.command:
            parts.append(f"Command: {' '.join(self.command)}")
        if self.returncode is not None:
            parts.append(f"Exit code: {self.returncode}")
        if self.stderr:
            parts.append(f"Error output: {self.stderr.strip()}")
        if self.suggestion:
            parts.append(f"\n💡 {self.suggestion}")
        return "\n".join(parts)


class CommandNotFoundError(ShellCommandError):
    """Required command-line tool not found."""
    pass


class GitError(ShellCommandError):
    """Error executing git command."""
    pass


class DVCError(ShellCommandError):
    """Error executing DVC command."""
    pass


class GHCLIError(ShellCommandError):
    """Error executing GitHub CLI (gh) command."""
    pass


# Template-related exceptions
class TemplateError(MintdError):
    """Base exception for template operations."""
    pass


class TemplateNotFoundError(TemplateError):
    """Template not found."""
    pass


class TemplateRenderError(TemplateError):
    """Error rendering a template."""
    pass


class TemplateValidationError(TemplateError):
    """Error validating template structure or content."""
    pass


# Data import exceptions (maintain compatibility with existing data_import.py)
class DataImportError(MintdError):
    """Base exception for data import operations."""
    pass


class DVCImportError(DataImportError):
    """Error during DVC import operation."""
    pass


class MetadataUpdateError(DataImportError):
    """Error updating project metadata."""
    pass
