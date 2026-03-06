"""Main API for creating projects."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .templates import CodeTemplate, DataTemplate, ProjectTemplate, EnclaveTemplate
from .config import get_config, get_stata_executable, get_platform_info
from .initializers.git import init_git, is_git_repo
from .initializers.storage import init_dvc, is_dvc_repo, add_dvc_remote


@dataclass
class ProjectResult:
    """Result of project creation."""
    name: str
    full_name: str
    project_type: str
    path: Path
    registration_url: Optional[str] = None


class ProjectBuilder:
    """Fluent builder for project creation.

    This class provides a cleaner API for creating projects compared to the
    many-parameter create_project() function. It groups related options together.

    Example:
        result = (ProjectBuilder("data", "my-project", "python")
            .at_path("/path/to/create")
            .with_git(enabled=True)
            .with_dvc(bucket="my-bucket")
            .with_governance(classification="private", team="my-team")
            .build())
    """

    def __init__(self, project_type: str, name: str, language: str):
        """Initialize the builder with required parameters.

        Args:
            project_type: Type of project ("data", "project", "code", "enclave")
            name: Project name (without prefix)
            language: Primary programming language ("python", "r", "stata")
        """
        self._project_type = project_type
        self._name = name
        self._language = language
        self._path = "."
        self._init_git = True
        self._init_dvc = True
        self._bucket_name: Optional[str] = None
        self._register_project = False
        self._use_current_repo = False
        self._registry_url: Optional[str] = None
        self._admin_team: Optional[str] = None
        self._researcher_team: Optional[str] = None
        self._classification: Optional[str] = None
        self._team: Optional[str] = None
        self._contract_info: Optional[str] = None
        self._contract_slug: Optional[str] = None

    def at_path(self, path: str) -> "ProjectBuilder":
        """Set the path where the project will be created.

        Args:
            path: Directory to create project in

        Returns:
            Self for method chaining
        """
        self._path = path
        return self

    def with_git(self, enabled: bool = True, use_current_repo: bool = False) -> "ProjectBuilder":
        """Configure Git initialization.

        Args:
            enabled: Whether to initialize Git
            use_current_repo: Use current directory as project root (when in existing git repo)

        Returns:
            Self for method chaining
        """
        self._init_git = enabled
        self._use_current_repo = use_current_repo
        return self

    def with_dvc(self, enabled: bool = True, bucket: Optional[str] = None) -> "ProjectBuilder":
        """Configure DVC initialization.

        Args:
            enabled: Whether to initialize DVC
            bucket: Override bucket name for DVC remote

        Returns:
            Self for method chaining
        """
        self._init_dvc = enabled
        self._bucket_name = bucket
        return self

    def with_governance(
        self,
        classification: str = "private",
        team: Optional[str] = None,
        contract_slug: Optional[str] = None,
        contract_info: Optional[str] = None,
    ) -> "ProjectBuilder":
        """Configure data governance settings.

        Args:
            classification: Data classification ("public", "private", "contract")
            team: Owning team (GitHub slug)
            contract_slug: Short name for contract (used in S3 prefix)
            contract_info: Description or link to contract

        Returns:
            Self for method chaining
        """
        self._classification = classification
        self._team = team
        self._contract_slug = contract_slug
        self._contract_info = contract_info
        return self

    def with_registry(
        self,
        register: bool = False,
        url: Optional[str] = None,
        admin_team: Optional[str] = None,
        researcher_team: Optional[str] = None,
    ) -> "ProjectBuilder":
        """Configure registry settings.

        Args:
            register: Whether to register project with Data Commons Registry
            url: Data Commons Registry GitHub URL (required for enclaves)
            admin_team: Override default admin team
            researcher_team: Override default researcher team

        Returns:
            Self for method chaining
        """
        self._register_project = register
        self._registry_url = url
        self._admin_team = admin_team
        self._researcher_team = researcher_team
        return self

    def build(self) -> "ProjectResult":
        """Create the project with the configured settings.

        Returns:
            ProjectResult with creation details
        """
        return create_project(
            project_type=self._project_type,
            name=self._name,
            language=self._language,
            path=self._path,
            init_git=self._init_git,
            init_dvc=self._init_dvc,
            bucket_name=self._bucket_name,
            register_project=self._register_project,
            use_current_repo=self._use_current_repo,
            registry_url=self._registry_url,
            admin_team=self._admin_team,
            researcher_team=self._researcher_team,
            classification=self._classification,
            team=self._team,
            contract_info=self._contract_info,
            contract_slug=self._contract_slug,
        )


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
        project_type: Type of project ("data", "project", "code", or "enclave")
        name: Project name (without prefix)
        path: Directory to create project in
        language: Primary programming language ("python", "r", or "stata")
        init_git: Whether to initialize Git
        init_dvc: Whether to initialize DVC
        bucket_name: Override bucket name for DVC
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

    # Get bucket from config if not provided
    storage_config = config.get("storage", {})
    if not bucket_name:
        bucket_name = storage_config.get("bucket_prefix", "")

    # Prepare template context
    registry_config = config.get("registry", {})
    if not registry_config.get("org"):
        raise ValueError(
            "GitHub organization not configured. Run 'mintd config setup' to set registry.org."
        )

    # Governance and Storage Prefix Logic
    # Default values
    classification = classification or "private"
    target_team = team or "all-lab"

    # Determine the type prefix to build the full project name
    # This must match the template's prefix attribute
    _prefix_map = {
        "data": "data_",
        "project": "prj_",
        "prj": "prj_",
        "code": "",
        "enclave": "enclave_",
    }
    type_prefix = _prefix_map.get(project_type, "")
    full_project_name = f"{type_prefix}{name}"

    # Calculate storage prefix using full_project_name
    # Path patterns match initializers/storage.py SENSITIVITY_TO_ACL mapping
    if classification == "public":
        storage_prefix = f"pub/{full_project_name}/"
    elif classification == "contract":
        if not contract_slug:
            # Fallback if slug missing (should be handled by CLI)
            contract_slug = "unknown-contract"
        storage_prefix = f"contract/{contract_slug}/{full_project_name}/"
    else:
        # Private/Lab
        storage_prefix = f"lab/{full_project_name}/"

    # Always populate DVC remote info for metadata (even if DVC init is deferred)
    dvc_remote_name = full_project_name
    dvc_remote_url = ""
    if bucket_name:
        dvc_remote_url = f"s3://{bucket_name}/{storage_prefix}"

    context = {
        "author": defaults.get("author", ""),
        "organization": defaults.get("organization", ""),
        "storage_provider": storage_config.get("provider", "s3"),
        "storage_endpoint": storage_config.get("endpoint", ""),
        "storage_versioning": storage_config.get("versioning", True),
        "storage_sensitivity": "public" if classification == "public" else "restricted",
        "bucket_name": bucket_name,  # Now properly set from config or parameter
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
        "registry_org": registry_config.get("org", ""),
        "admin_team": admin_team or registry_config.get("admin_team", "infrastructure-admins"),
        "researcher_team": researcher_team or registry_config.get("researcher_team", "all-researchers"),
        # Governance
        "classification": classification,
        "team": target_team,
        "contract_info": contract_info or "",
        "storage_prefix": storage_prefix,
        # DVC remote info for metadata
        "dvc_remote_name": dvc_remote_name,
        "dvc_remote_url": dvc_remote_url,
    }

    # Select and create template
    if project_type == "data":
        template = DataTemplate()
    elif project_type in ["project", "prj"]:
        template = ProjectTemplate()
        project_type = "project"  # Normalize
    elif project_type == "code":
        template = CodeTemplate()
    elif project_type == "enclave":
        template = EnclaveTemplate()
    else:
        # Check for custom templates
        from .utils.loader import load_custom_templates
        custom_templates = load_custom_templates()
        
        # Check against prefixes (e.g. project_type "foo" matches prefix "foo_")
        # Or should we expect project_type to MATCH the prefix?
        # CLI commands correspond to cleaned names ("foo" from "foo_")
        # Let's map clean names to templates
        
        custom_map = {prefix.rstrip("_"): cls for prefix, cls in custom_templates.items()}
        
        if project_type in custom_map:
            template_cls = custom_map[project_type]
            template = template_cls()
        elif project_type in custom_templates: # In case full prefix was passed
            template_cls = custom_templates[project_type]
            template = template_cls()
        else:
            raise ValueError(f"Unknown project type: {project_type}")

    # Create the project
    project_path = template.create(name, path, **context)

    # Validate metadata if it was created
    metadata_path = project_path / "metadata.json"
    if metadata_path.exists():
        import json
        from .utils.validation import validate_metadata
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            is_valid, errors = validate_metadata(metadata)
            if not is_valid:
                print("Warning: Metadata validation issues found:")
                for error in errors:
                    print(f"  - {error}")
        except Exception as e:
            print(f"Warning: Could not validate metadata: {e}")

    # Initialize Git if requested
    if init_git:
        _init_git(project_path, use_current_repo)

    # Initialize DVC if requested
    if init_dvc:
        sensitivity = context.get("storage_sensitivity", "restricted")
        full_project_name = template.prefix + name
        _init_dvc(project_path, bucket_name, sensitivity, name, full_project_name)

    # Note: We no longer need to update metadata.json with DVC info since it's in the template context

    # Install pre-commit hooks
    _install_precommit_hooks(project_path)

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
                from .shell import git_command
                git = git_command(cwd=project_path)
                # Add all files (including new ones)
                git.run("add", ".")
                # Try to commit, but don't fail if there are no changes
                try:
                    git.run("commit", "-m", "Add mint project scaffolding")
                except Exception:
                    # No changes to commit, that's fine
                    pass
            except Exception as e:
                print(f"Warning: Failed to commit scaffolded files: {e}")
    else:
        # Normal git initialization
        if not is_git_repo(project_path):
            init_git(project_path)


def _init_dvc(project_path: Path, bucket_prefix: Optional[str] = None, sensitivity: str = "restricted", project_name: str = "", full_project_name: str = "") -> None:
    """Initialize DVC repository with S3 remote.

    For new repos: runs dvc init and adds remote.
    For existing DVC repos: only adds remote (supports --use-current-repo).
    """
    # Get bucket prefix from config if not provided
    if bucket_prefix is None:
        from .config import get_config
        config = get_config()
        bucket_prefix = config["storage"].get("bucket_prefix", "")

    # Require bucket for DVC initialization
    if not bucket_prefix:
        raise ValueError(
            "Storage bucket not configured. "
            "Run 'mintd config setup' to configure storage before using DVC."
        )

    # Use provided project_name or extract from path
    if not project_name:
        project_name = project_path.name
        # Extract the actual project name (remove prefix)
        if project_name.startswith(("data_", "prj__")):
            parts = project_name.split("_", 1)
            if len(parts) > 1:
                project_name = parts[1]

    try:
        if is_dvc_repo(project_path):
            # Existing DVC repo: just add the remote (for --use-current-repo)
            add_dvc_remote(project_path, bucket_prefix, sensitivity, project_name, full_project_name)
        else:
            # New repo: full DVC initialization
            init_dvc(project_path, bucket_prefix, sensitivity, project_name, full_project_name)
    except Exception as e:
        # Log warning but don't fail the project creation
        print(f"Warning: Failed to initialize DVC: {e}")
        print("The project was created successfully, but DVC initialization was skipped.")


# Function removed - DVC info is now passed directly to template context


def _install_precommit_hooks(project_path: Path) -> None:
    """Install pre-commit hooks if pre-commit config exists.

    Also makes hook scripts executable.

    Args:
        project_path: Path to the project directory
    """
    import os
    import subprocess

    # Make hook scripts executable
    scripts_to_chmod = [
        "check-dvc-sync.sh",
        "check-env-lockfiles.sh",
    ]
    for script_name in scripts_to_chmod:
        script_path = project_path / "scripts" / script_name
        if script_path.exists():
            try:
                os.chmod(script_path, 0o755)
            except Exception as e:
                print(f"Warning: Could not make {script_path} executable: {e}")

    # Check if pre-commit config exists
    precommit_config = project_path / ".pre-commit-config.yaml"
    if not precommit_config.exists():
        return

    # Try to install pre-commit hooks
    try:
        # First check if pre-commit is available
        result = subprocess.run(
            ["pre-commit", "--version"],
            capture_output=True,
            text=True,
            cwd=project_path
        )
        if result.returncode != 0:
            print("Note: pre-commit not found. Install with: pip install pre-commit")
            print("      Then run: pre-commit install")
            return

        # Install hooks
        result = subprocess.run(
            ["pre-commit", "install"],
            capture_output=True,
            text=True,
            cwd=project_path
        )
        if result.returncode != 0:
            print(f"Warning: Failed to install pre-commit hooks: {result.stderr}")

    except FileNotFoundError:
        print("Note: pre-commit not found. Install with: pip install pre-commit")
        print("      Then run: pre-commit install")
    except Exception as e:
        print(f"Warning: Could not install pre-commit hooks: {e}")


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

    except Exception:
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