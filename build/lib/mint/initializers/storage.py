"""DVC and storage initialization for S3-compatible buckets."""

import subprocess
import shutil
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from ..config import get_config, get_storage_credentials

# Mapping from sensitivity levels to ACL folder names
SENSITIVITY_TO_ACL = {
    "public": "pub",
    "restricted": "lab",
    "confidential": "restricted"
}


def _is_command_available(command: str) -> bool:
    """Check if a command is available on the system.

    Args:
        command: Command name to check

    Returns:
        True if command is available
    """
    return shutil.which(command) is not None


def init_dvc(project_path: Path, bucket_prefix: str, sensitivity: str = "restricted", project_name: str = "") -> None:
    """Initialize DVC and configure S3 remote.

    Args:
        project_path: Path to the project directory
        bucket_prefix: Name of the S3 bucket prefix to use as remote
        sensitivity: Data sensitivity level ("public", "restricted", "confidential")
        project_name: Name of the project (used in path construction)

    Raises:
        RuntimeError: If DVC operations fail
    """
    config = get_config()
    storage = config["storage"]

    try:
        # Initialize DVC
        _run_dvc_command(project_path, ["init"])

        # Add remote with ACL-based path prefix
        # Use project_name as the remote name for clarity
        acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")  # Default to "lab" if invalid sensitivity
        remote_name = project_name if project_name else "storage"
        if project_name:
            remote_url = f"s3://{bucket_prefix}/{acl_path}/{project_name}/"
        else:
            remote_url = f"s3://{bucket_prefix}/{acl_path}/"
        
        # Add as global remote so it's available across all projects
        _run_dvc_command(project_path, ["remote", "add", "--global", "-d", remote_name, remote_url])

        # Configure remote settings (globally)
        if storage.get("endpoint"):
            _run_dvc_command(project_path, [
                "remote", "modify", "--global", remote_name, "endpointurl", storage["endpoint"]
            ])

        if storage.get("region"):
            _run_dvc_command(project_path, [
                "remote", "modify", "--global", remote_name, "region", storage["region"]
            ])

        # Enable cloud versioning support
        if storage.get("versioning", True):
            _run_dvc_command(project_path, [
                "remote", "modify", "--global", remote_name, "version_aware", "true"
            ])

    except Exception as e:
        # For any DVC-related error, just warn and continue
        # This allows the project creation to succeed even without DVC
        print(f"Warning: Failed to initialize DVC: {e}")
        print("The project was created successfully, but DVC initialization was skipped.")


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


def _run_dvc_command(project_path: Path, args: list[str]) -> str:
    """Run a DVC command in the project directory.

    Args:
        project_path: Path to the project directory
        args: DVC command arguments

    Returns:
        Command output

    Raises:
        subprocess.CalledProcessError: If the command fails
        FileNotFoundError: If dvc is not available
    """
    try:
        result = subprocess.run(
            ["dvc"] + args,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except FileNotFoundError:
        raise FileNotFoundError("dvc command not found")
    except subprocess.CalledProcessError:
        # Re-raise for caller to handle
        raise