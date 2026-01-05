"""Git repository initialization and management."""

import subprocess
import shutil
from pathlib import Path
from typing import Optional

from ..utils import format_project_name


def _is_command_available(command: str) -> bool:
    """Check if a command is available on the system.

    Args:
        command: Command name to check

    Returns:
        True if command is available
    """
    return shutil.which(command) is not None


def init_git(project_path: Path) -> None:
    """Initialize git repository and create initial commit.

    Args:
        project_path: Path to the project directory

    Raises:
        RuntimeError: If git operations fail
    """
    try:
        # Initialize git repository
        _run_git_command(project_path, ["init"])

        # Add all files
        _run_git_command(project_path, ["add", "."])

        # Create initial commit
        _run_git_command(project_path, ["commit", "-m", "Initial commit: Project scaffolded with mint"])

    except Exception as e:
        # For any git-related error, just warn and continue
        # This allows the project creation to succeed even without git
        print(f"Warning: Failed to initialize git repository: {e}")
        print("The project was created successfully, but git initialization was skipped.")


def create_gitignore(project_path: Path, project_type: str) -> None:
    """Write .gitignore appropriate for project type.

    Note: The .gitignore file is already created by the template system,
    so this function is mainly for future customization if needed.

    Args:
        project_path: Path to the project directory
        project_type: Type of project ("data", "project", "infra")
    """
    # The .gitignore is already created by the template system
    # This function can be extended later for project-type-specific additions
    gitignore_path = project_path / ".gitignore"

    if not gitignore_path.exists():
        raise FileNotFoundError(f".gitignore not found at {gitignore_path}")

    # For now, the base .gitignore from templates is sufficient
    # Future enhancement: Add project-type-specific ignore patterns
    pass


def is_git_repo(project_path: Path) -> bool:
    """Check if a directory is already a git repository.

    Args:
        project_path: Path to check

    Returns:
        True if it's a git repository
    """
    git_dir = project_path / ".git"
    return git_dir.is_dir()


def _run_git_command(project_path: Path, args: list[str]) -> str:
    """Run a git command in the project directory.

    Args:
        project_path: Path to the project directory
        args: Git command arguments

    Returns:
        Command output

    Raises:
        subprocess.CalledProcessError: If the command fails
        FileNotFoundError: If git is not available
    """
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except FileNotFoundError:
        raise FileNotFoundError("git command not found")
    except subprocess.CalledProcessError as e:
        # Check if it's a "command not found" type error
        if "returned non-zero exit status" in str(e) and b"git: command not found" in e.stderr.encode():
            raise FileNotFoundError("git command not found")
        raise