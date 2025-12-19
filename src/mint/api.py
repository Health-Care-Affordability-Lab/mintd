"""Main API for creating projects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .templates import DataTemplate, ProjectTemplate, InfraTemplate
from .config import get_config
from .initializers.git import init_git, is_git_repo
from .initializers.storage import init_dvc, create_bucket, is_dvc_repo


@dataclass
class ProjectResult:
    """Result of project creation."""
    name: str
    full_name: str
    project_type: str
    path: Path


def create_project(
    project_type: str,
    name: str,
    path: str = ".",
    init_git: bool = True,
    init_dvc: bool = True,
    bucket_name: Optional[str] = None,
) -> ProjectResult:
    """Main API function called by both CLI and Stata.

    Args:
        project_type: Type of project ("data", "project", or "infra")
        name: Project name (without prefix)
        path: Directory to create project in
        init_git: Whether to initialize Git
        init_dvc: Whether to initialize DVC

    Returns:
        ProjectResult with creation details
    """
    # Get configuration for template context
    config = get_config()
    defaults = config.get("defaults", {})

    # Prepare template context
    context = {
        "author": defaults.get("author", ""),
        "organization": defaults.get("organization", ""),
        "storage_provider": config.get("storage", {}).get("provider", "s3"),
        "storage_endpoint": config.get("storage", {}).get("endpoint", ""),
        "storage_versioning": config.get("storage", {}).get("versioning", True),
        "bucket_name": "",  # Will be set later when DVC is implemented
        "project_type": project_type,
    }

    # Select and create template
    if project_type == "data":
        template = DataTemplate()
    elif project_type in ["project", "prj"]:
        template = ProjectTemplate()
        project_type = "project"  # Normalize
    elif project_type == "infra":
        template = InfraTemplate()
    else:
        raise ValueError(f"Unknown project type: {project_type}")

    # Create the project
    project_path = template.create(name, path, **context)

    # TODO: Initialize Git if requested
    if init_git:
        _init_git(project_path)

    # TODO: Initialize DVC if requested
    if init_dvc:
        _init_dvc(project_path, bucket_name)

    return ProjectResult(
        name=name,
        full_name=template.prefix + name,
        project_type=project_type,
        path=project_path,
    )


def _init_git(project_path: Path) -> None:
    """Initialize Git repository and create initial commit."""
    if not is_git_repo(project_path):
        init_git(project_path)


def _init_dvc(project_path: Path, bucket_name: Optional[str] = None) -> None:
    """Initialize DVC repository with S3 remote."""
    if not is_dvc_repo(project_path):
        # Determine bucket name
        if bucket_name is None:
            # Create bucket name based on project path
            project_name = project_path.name
            # Extract the actual project name (remove prefix)
            if project_name.startswith(("data_", "prj__", "infra_")):
                # Find the first underscore and take everything after it
                parts = project_name.split("_", 1)
                if len(parts) > 1:
                    project_name = parts[1]

            try:
                bucket_name = create_bucket(project_name)
            except Exception as e:
                # Log warning but don't fail the project creation
                # DVC initialization can be done later when credentials are properly configured
                print(f"Warning: Failed to create bucket: {e}")
                print("The project was created successfully, but DVC initialization was skipped.")
                return

        try:
            init_dvc(project_path, bucket_name)
        except Exception as e:
            # Log warning but don't fail the project creation
            print(f"Warning: Failed to initialize DVC: {e}")
            print("The project was created successfully, but DVC initialization was skipped.")