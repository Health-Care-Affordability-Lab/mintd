"""DVC and storage initialization for S3-compatible buckets."""

from pathlib import Path

from ..config import get_config
from ..exceptions import DVCError
from ..shell import dvc_command

# Mapping from sensitivity levels to ACL folder names
SENSITIVITY_TO_ACL = {
    "public": "pub",
    "restricted": "lab",
    "confidential": "restricted"
}


def init_dvc(project_path: Path, bucket_prefix: str, sensitivity: str = "restricted", project_name: str = "", full_project_name: str = "") -> dict:
    """Initialize DVC and configure S3 remote.

    Args:
        project_path: Path to the project directory
        bucket_prefix: Name of the S3 bucket prefix to use as remote
        sensitivity: Data sensitivity level ("public", "restricted", "confidential")
        project_name: Name of the project (used in path construction)
        full_project_name: Full project name with prefix (e.g., data_cms-provider-data-service)

    Returns:
        Dict with remote_name and remote_url for storing in metadata
        
    Raises:
        RuntimeError: If DVC operations fail
    """
    config = get_config()
    storage = config["storage"]
    
    # Use full_project_name (with prefix) as the remote name for consistency
    remote_name = full_project_name if full_project_name else (project_name if project_name else "storage")
    
    # Compute ACL path and remote URL
    acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")
    if project_name:
        remote_url = f"s3://{bucket_prefix}/{acl_path}/{project_name}/"
    else:
        remote_url = f"s3://{bucket_prefix}/{acl_path}/"
    
    # Default return value in case DVC init fails
    dvc_info = {"remote_name": remote_name, "remote_url": remote_url}

    try:
        dvc = dvc_command(cwd=project_path)

        # Initialize DVC
        dvc.run("init")

        # Add as global remote so it's available across all projects
        dvc.run("remote", "add", "--global", "-d", remote_name, remote_url)

        # Configure remote settings (globally)
        if storage.get("endpoint"):
            dvc.run("remote", "modify", "--global", remote_name, "endpointurl", storage["endpoint"])

        if storage.get("region"):
            dvc.run("remote", "modify", "--global", remote_name, "region", storage["region"])

        # Enable cloud versioning support
        if storage.get("versioning", True):
            dvc.run("remote", "modify", "--global", remote_name, "version_aware", "true")

    except DVCError as e:
        # For any DVC-related error, just warn and continue
        # This allows the project creation to succeed even without DVC
        print(f"Warning: Failed to initialize DVC: {e}")
        print("The project was created successfully, but DVC initialization was skipped.")
    
    return dvc_info


def create_dvcignore(project_path: Path, project_type: str) -> None:
    """Write .dvcignore appropriate for project type.

    Note: The .dvcignore file is already created by the template system,
    so this function is mainly for future customization if needed.

    Args:
        project_path: Path to the project directory
        project_type: Type of project ("data", "project", "infra")
    """
    # The .dvcignore is already created by the template system
    # This function can be extended later for project-type-specific additions
    dvcignore_path = project_path / ".dvcignore"

    if not dvcignore_path.exists():
        raise FileNotFoundError(f".dvcignore not found at {dvcignore_path}")

    # For now, the base .dvcignore from templates is sufficient
    # Future enhancement: Add project-type-specific ignore patterns
    pass


def is_dvc_repo(project_path: Path) -> bool:
    """Check if a directory is already a DVC repository.

    Args:
        project_path: Path to check

    Returns:
        True if it's a DVC repository
    """
    dvc_dir = project_path / ".dvc"
    return dvc_dir.is_dir()


def add_dvc_remote(project_path: Path, bucket_prefix: str, sensitivity: str = "restricted", project_name: str = "", full_project_name: str = "") -> dict:
    """Add DVC remote to an existing DVC repository (without running dvc init).

    Use this when the repo is already a DVC repo (e.g., --use-current-repo flag).
    This adds the project-specific remote to the global DVC config.

    Args:
        project_path: Path to the project directory
        bucket_prefix: Name of the S3 bucket prefix to use as remote
        sensitivity: Data sensitivity level ("public", "restricted", "confidential")
        project_name: Name of the project (used in path construction)
        full_project_name: Full project name with prefix (e.g., data_cms-provider-data-service)

    Returns:
        Dict with remote_name and remote_url for storing in metadata
    """
    config = get_config()
    storage = config["storage"]

    # Use full_project_name (with prefix) as the remote name for consistency
    remote_name = full_project_name if full_project_name else (project_name if project_name else "storage")

    # Compute ACL path and remote URL
    acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")
    if project_name:
        remote_url = f"s3://{bucket_prefix}/{acl_path}/{project_name}/"
    else:
        remote_url = f"s3://{bucket_prefix}/{acl_path}/"

    # Default return value in case DVC commands fail
    dvc_info = {"remote_name": remote_name, "remote_url": remote_url}

    try:
        dvc = dvc_command(cwd=project_path)

        # Add as global remote so it's available across all projects
        # Use -f to force overwrite if remote already exists
        dvc.run("remote", "add", "--global", "-d", "-f", remote_name, remote_url)

        # Configure remote settings (globally)
        if storage.get("endpoint"):
            dvc.run("remote", "modify", "--global", remote_name, "endpointurl", storage["endpoint"])

        if storage.get("region"):
            dvc.run("remote", "modify", "--global", remote_name, "region", storage["region"])

        # Enable cloud versioning support
        if storage.get("versioning", True):
            dvc.run("remote", "modify", "--global", remote_name, "version_aware", "true")

    except DVCError as e:
        # For any DVC-related error, just warn and continue
        print(f"Warning: Failed to add DVC remote: {e}")
        print("The project was created successfully, but DVC remote configuration was skipped.")

    return dvc_info


