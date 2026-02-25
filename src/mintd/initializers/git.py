"""Git repository initialization and management."""

import shutil
from pathlib import Path

from ..exceptions import GitError
from ..shell import git_command


def init_git(project_path: Path) -> None:
    """Initialize git repository and create initial commit.

    Args:
        project_path: Path to the project directory

    Raises:
        RuntimeError: If git operations fail
    """
    try:
        git = git_command(cwd=project_path)

        # Initialize git repository
        git.run("init")

        # Add all files
        git.run("add", ".")

        # Create initial commit
        git.run("commit", "-m", "Initial commit: Project scaffolded with mintd")

    except GitError as e:
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


