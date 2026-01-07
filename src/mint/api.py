"""Main API for creating projects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .templates import DataTemplate, ProjectTemplate, InfraTemplate, EnclaveTemplate
from .config import get_config, get_stata_executable, get_platform_info
from .initializers.git import init_git, is_git_repo
from .initializers.storage import init_dvc, is_dvc_repo


@dataclass
class ProjectResult:
    """Result of project creation."""
    name: str
    full_name: str
    project_type: str
    path: Path
    registration_url: Optional[str] = None


def create_project(
    project_type: str,
    name: str,
    language: str,
    path: str = ".",
    init_git: bool = True,
    init_dvc: bool = True,
    bucket_name: Optional[str] = None,
    register_project: bool = False,
    use_current_repo: bool = False,
    registry_url: Optional[str] = None,
    admin_team: Optional[str] = None,
    researcher_team: Optional[str] = None,
    classification: Optional[str] = None,
    team: Optional[str] = None,
    contract_info: Optional[str] = None,
    contract_slug: Optional[str] = None,
) -> ProjectResult:
    """Main API function called by both CLI and Stata.

    Args:
        project_type: Type of project ("data", "project", "infra", or "enclave")
        name: Project name (without prefix)
        path: Directory to create project in
        language: Primary programming language ("python", "r", or "stata")
        init_git: Whether to initialize Git
        init_dvc: Whether to initialize DVC
        bucket_name: Override bucket name for DVC
        register_project: Whether to register project with Data Commons Registry
        register_project: Whether to register project with Data Commons Registry
        use_current_repo: Whether to use current directory as project root (when in existing git repo)
        registry_url: Data Commons Registry GitHub URL (required for enclaves)
        classification: Data classification (public, private, contract)
        team: Owning team (GitHub slug)
        contract_info: Description or link to contract
        contract_slug: Short name for contract (used in S3 prefix)

    Returns:
        ProjectResult with creation details
    """
    # Get configuration for template context
    config = get_config()
    defaults = config.get("defaults", {})

    # Check if we're using current repo mode
    current_path = Path(path)
    is_in_git_repo = is_git_repo(current_path)

    if use_current_repo and not is_in_git_repo:
        raise ValueError("Cannot use --use-current-repo: not in a git repository")

    if use_current_repo:
        # Warn about potential file conflicts
        existing_files = ["README.md", "metadata.json", ".gitignore", ".dvcignore"]
        conflicting_files = [f for f in existing_files if (current_path / f).exists()]

        if conflicting_files:
            print(f"Warning: The following files already exist and may be overwritten: {', '.join(conflicting_files)}")
            print("Consider backing up these files before proceeding.")

    # Get platform information for cross-platform support
    platform_info = get_platform_info()
    stata_executable = get_stata_executable()
    
    # Prepare template context
    registry_config = config.get("registry", {})
    context = {
        "author": defaults.get("author", ""),
        "organization": defaults.get("organization", ""),
        "storage_provider": config.get("storage", {}).get("provider", "s3"),
        "storage_endpoint": config.get("storage", {}).get("endpoint", ""),
        "storage_versioning": config.get("storage", {}).get("versioning", True),
        "storage_sensitivity": "restricted",  # Default sensitivity level
        "bucket_name": "",  # Will be set later when DVC is implemented
        "project_type": project_type,
        "language": language,
        "use_current_repo": use_current_repo,
        # Registry context for enclave configuration
        "registry_url": registry_url or "",
        # Platform-specific context for cross-platform support
        "platform_os": platform_info["os"],  # 'windows', 'macos', or 'linux'
        "command_sep": platform_info["command_separator"],  # '&&' or '&'
        "stata_executable": stata_executable or "stata",  # Fallback to 'stata'
        # Registry context for metadata generation
        "registry_org": registry_config.get("org", "cooper-lab"),
        "admin_team": admin_team or registry_config.get("admin_team", "infrastructure-admins"),
        "researcher_team": researcher_team or registry_config.get("researcher_team", "all-researchers"),
    }
    
    # Governance and Storage Prefix Logic
    # Default values
    classification = classification or "private"
    target_team = team or context["admin_team"]
    
    # Calculate storage prefix
    if classification == "public":
        storage_prefix = f"public/{name}/"
    elif classification == "contract":
        if not contract_slug:
            # Fallback if slug missing (should be handled by CLI)
            contract_slug = "unknown-contract"
        storage_prefix = f"contract/{contract_slug}/{name}/"
    else:
        # Private/Lab
        storage_prefix = f"lab/{target_team}/{name}/"

    context.update({
        "classification": classification,
        "team": target_team,
        "contract_info": contract_info or "",
        "storage_prefix": storage_prefix,
        # Map classification to DVC sensitivity (for backward compatibility/ACLs)
        "storage_sensitivity": "public" if classification == "public" else "restricted",
    })

    # Select and create template
    if project_type == "data":
        template = DataTemplate()
    elif project_type in ["project", "prj"]:
        template = ProjectTemplate()
        project_type = "project"  # Normalize
    elif project_type == "infra":
        template = InfraTemplate()
    elif project_type == "enclave":
        template = EnclaveTemplate()
    else:
        raise ValueError(f"Unknown project type: {project_type}")

    # Create the project
    project_path = template.create(name, path, **context)

    # Initialize Git if requested
    if init_git:
        _init_git(project_path, use_current_repo)

    # Initialize DVC if requested
    if init_dvc:
        sensitivity = context.get("storage_sensitivity", "restricted")
        _init_dvc(project_path, bucket_name, sensitivity)

    # Register project with Data Commons Registry if requested
    registration_url = None
    if register_project:
        registration_url = _register_project(project_path)

    return ProjectResult(
        name=name,
        full_name=template.prefix + name,
        project_type=project_type,
        path=project_path,
        registration_url=registration_url,
    )


def _init_git(project_path: Path, use_current_repo: bool = False) -> None:
    """Initialize Git repository and create initial commit."""
    if use_current_repo:
        # When using current repo, assume git is already initialized
        # Just commit any new files that were added
        if is_git_repo(project_path):
            try:
                from .initializers.git import _run_git_command
                # Add all files (including new ones)
                _run_git_command(project_path, ["add", "."])
                # Try to commit, but don't fail if there are no changes
                try:
                    _run_git_command(project_path, ["commit", "-m", "Add mint project scaffolding"])
                except Exception:
                    # No changes to commit, that's fine
                    pass
            except Exception as e:
                print(f"Warning: Failed to commit scaffolded files: {e}")
    else:
        # Normal git initialization
        if not is_git_repo(project_path):
            init_git(project_path)


def _init_dvc(project_path: Path, bucket_prefix: Optional[str] = None, sensitivity: str = "restricted") -> None:
    """Initialize DVC repository with S3 remote."""
    if not is_dvc_repo(project_path):
        # Get bucket prefix from config if not provided
        if bucket_prefix is None:
            from .config import get_config
            config = get_config()
            bucket_prefix = config["storage"].get("bucket_prefix", "")
            if not bucket_prefix:
                print("Warning: Bucket prefix not configured. Run 'mint config setup' to configure storage.")
                print("The project was created successfully, but DVC initialization was skipped.")
                return

        # Extract project name from path
        project_name = project_path.name
        # Extract the actual project name (remove prefix)
        if project_name.startswith(("data_", "prj__", "infra_")):
            # Find the first underscore and take everything after it
            parts = project_name.split("_", 1)
            if len(parts) > 1:
                project_name = parts[1]

        try:
            init_dvc(project_path, bucket_prefix, sensitivity, project_name)
        except Exception as e:
            # Log warning but don't fail the project creation
            print(f"Warning: Failed to initialize DVC: {e}")
            print("The project was created successfully, but DVC initialization was skipped.")


def _register_project(project_path: Path) -> Optional[str]:
    """Register project with Data Commons Registry.

    Args:
        project_path: Path to the created project

    Returns:
        URL of the registration PR, or None if registration failed gracefully
    """
    try:
        from .registry import get_registry_client, load_project_metadata, save_pending_registration

        # Load project metadata
        metadata = load_project_metadata(project_path)

        # Create registry client and register
        client = get_registry_client()
        pr_url = client.register_project(metadata)

        return pr_url

    except Exception as e:
        # Registration is not critical - don't fail project creation
        # Save registration request for later retry
        try:
            from .registry import load_project_metadata, save_pending_registration
            metadata = load_project_metadata(project_path)
            save_pending_registration(project_path, metadata)
        except Exception:
            pass

        # Return None to indicate registration didn't happen
        # The CLI will show appropriate messaging
        return None