"""Command Line Interface for mintd."""

import subprocess
import click
from pathlib import Path
from rich.console import Console

console = Console()


@click.group()
@click.version_option(version="1.0.0")
def main():
    """mintd - Lab Project Scaffolding Tool"""
    pass


@main.group()
def create():
    """Create a new project."""
    pass


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root (when in existing git repo)")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
@click.option("--public", is_flag=True, help="Mark as public data")
@click.option("--contract", help="Mark as contract data (provide contract slug)")
@click.option("--private", is_flag=True, help="Mark as private/lab data (default)")
@click.option("--contract-info", help="Description or link to contract")
@click.option("--team", help="Owning team slug")
def data(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool, admin_team: str, researcher_team: str, public: bool, contract: str, private: bool, contract_info: str, team: str):
    """Create a data product repository (data_{name})."""
    from .api import create_project

    # Interactive Prompts for Governance
    classification = "private"
    contract_slug = None
    
    # Determine classification (if not silenced by flags)
    if public:
        classification = "public"
    elif contract:
        classification = "contract"
        contract_slug = contract
    elif private:
        classification = "private"
    else:
        # No flag provided, prompt user
        console.print()
        console.print("[bold]Governance Configuration[/bold]")
        classification = click.prompt(
            "Data Classification",
            type=click.Choice(["public", "private", "contract"]),
            default="private"
        )
        
        if classification == "contract":
            contract_slug = click.prompt("Contract Slug (short-name for URL)")

    # Get Contract Info if needed
    if classification == "contract" and not contract_info:
        contract_info = click.prompt("Contract Info (URL or description)", default="")

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="data",
                name=name,
                path=path,
                language=lang,
                init_git=not no_git,
                init_dvc=not no_dvc,
                bucket_name=bucket,
                register_project=register,
                use_current_repo=use_current_repo,
                admin_team=admin_team,
                researcher_team=researcher_team,
                classification=classification,
                team=team,
                contract_slug=contract_slug,
                contract_info=contract_info,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")

            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root (when in existing git repo)")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
def project(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool, admin_team: str, researcher_team: str):
    """Create a project repository (prj__{name})."""
    from .api import create_project

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="project",
                name=name,
                path=path,
                language=lang,
                init_git=not no_git,
                init_dvc=not no_dvc,
                bucket_name=bucket,
                register_project=register,
                use_current_repo=use_current_repo,
                admin_team=admin_team,
                researcher_team=researcher_team,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")

            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--bucket", help="Override bucket name for DVC remote")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root (when in existing git repo)")
@click.option("--admin-team", help="Override default admin team")
@click.option("--researcher-team", help="Override default researcher team")
def infra(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool, admin_team: str, researcher_team: str):
    """Create an infrastructure repository (infra_{name})."""
    from .api import create_project

    with console.status("Scaffolding project..."):
        try:
            result = create_project(
                project_type="infra",
                name=name,
                path=path,
                language=lang,
                init_git=not no_git,
                init_dvc=not no_dvc,
                bucket_name=bucket,
                register_project=register,
                use_current_repo=use_current_repo,
                admin_team=admin_team,
                researcher_team=researcher_team,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")

            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@create.command()
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--registry-url", required=False, help="Data Commons Registry GitHub URL (e.g., https://github.com/org/data-registry). Uses config default if not provided.")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
def enclave(name: str, path: str, registry_url: str, no_git: bool):
    """Create a secure data enclave workspace (enclave_{name})."""
    from .config import CONFIG_FILE, get_config, save_config
    import re
    
    # Get registry URL - check if explicitly configured
    if not registry_url:
        # Check if config file exists and has registry URL set
        config = get_config()
        config_registry_url = config.get("registry", {}).get("url", "")
        
        # Check if this is actually from user config (file exists) or just defaults
        config_exists = CONFIG_FILE.exists()
        
        if config_exists and config_registry_url:
            # Use configured registry URL
            registry_url = config_registry_url
        else:
            # No config set - prompt the user
            console.print()
            console.print("[bold yellow]Registry URL not configured.[/bold yellow]")
            console.print("The enclave needs a Data Commons Registry URL to pull data from.")
            console.print()
            
            registry_url = click.prompt(
                "Enter the registry URL (e.g., https://github.com/org/data-registry)",
                type=str
            )
            
            # Ask if they want to save this for future use
            if click.confirm("Save this registry URL to your mintd config for future use?", default=True):
                config["registry"]["url"] = registry_url
                save_config(config)
                console.print(f"✅ Registry URL saved to {CONFIG_FILE}", style="green")
    
    # Validate registry URL format
    if not re.match(r'^https://github\.com/[^/]+/[^/]+/?$', registry_url):
        console.print("❌ Invalid registry URL format. Expected: https://github.com/org/repo", style="red")
        raise click.Abort()

    from .api import create_project

    with console.status("Scaffolding enclave..."):
        try:
            result = create_project(
                project_type="enclave",
                name=name,
                path=path,
                language="python",  # Enclaves use Python for scripts
                init_git=not no_git,
                init_dvc=False,  # Enclaves don't need DVC
                bucket_name=None,
                register_project=False,  # Enclaves aren't registered in the registry
                use_current_repo=False,
                registry_url=registry_url,  # Pass registry URL
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")
            console.print(f"   Registry: {registry_url}", style="dim")
            console.print()
            console.print("Next steps:")
            console.print("  1. cd " + str(result.path))
            console.print("  2. Run 'mintd enclave add <repo-name>' to add approved data products")
            console.print("  3. Run './scripts/pull_data.sh --all' to download data")
            console.print("  4. Run './scripts/package_transfer.sh' to create transfer packages")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()

@create.command()
@click.argument("template_name")
@click.option("--name", "-n", required=True, help="Project name")
@click.option("--path", "-p", default=".", help="Output directory")
@click.option("--lang", "--language", default="python", help="Primary programming language")
@click.option("--no-git", is_flag=True, help="Skip Git initialization")
@click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
@click.option("--register", is_flag=True, help="Register project with Data Commons Registry")
@click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
def custom(template_name: str, name: str, path: str, lang: str, no_git: bool, no_dvc: bool, register: bool, use_current_repo: bool):
    """Create a project from a custom template."""
    from .api import create_project

    with console.status(f"Scaffolding {template_name} project..."):
        try:
            result = create_project(
                project_type=template_name,
                name=name,
                path=path,
                language=lang,
                init_git=not no_git,
                init_dvc=not no_dvc,
                register_project=register,
                use_current_repo=use_current_repo,
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")

            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@main.group()
def templates():
    """Manage project templates."""
    pass


@templates.command(name="list")
def list_templates():
    """List available project templates."""
    from .utils.loader import load_custom_templates, get_custom_template_dir
    from rich.table import Table

    table = Table(title="Available Templates")
    table.add_column("Type", style="cyan")
    table.add_column("Prefix", style="green")
    table.add_column("Source", style="dim")
    table.add_column("Description")

    # Built-in templates
    table.add_row("project", "prj_", "Built-in", "Standard research project")
    table.add_row("data", "data_", "Built-in", "Data product")
    table.add_row("infra", "infra_", "Built-in", "Infrastructure library")
    table.add_row("enclave", "enclave_", "Built-in", "Secure data enclave")

    # Custom templates
    custom_templates = load_custom_templates()
    custom_dir = get_custom_template_dir()

    for prefix, cls in custom_templates.items():
        # Derive type name from prefix (remove trailing underscore)
        type_name = prefix.rstrip("_")
        description = cls.__doc__.strip() if cls.__doc__ else "Custom template"
        # First line of docstring
        description = description.split("\n")[0]
        
        table.add_row(type_name, prefix, "Custom", description)

    console.print(table)
    console.print(f"\nCustom templates directory: [dim]{custom_dir}[/dim]")


@main.group()
def update():
    """Update project components."""


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
@click.option("--sensitivity", type=click.Choice(["public", "restricted", "confidential"]),
              help="Data sensitivity level (defaults to prompting)")
@click.option("--mirror-url", help="Mirror repository URL for external collaboration")
def metadata(path, sensitivity, mirror_url):
    """Update metadata.json to latest schema with new fields."""
    import json
    
    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"
    
    # Check if metadata.json exists before prompting
    if not metadata_path.exists():
        console.print("❌ metadata.json not found. Are you in a mintd project directory?", style="red")
        raise click.Abort()
    
    # Load existing metadata to get current values for prompts
    try:
        with open(metadata_path, 'r') as f:
            metadata_data = json.load(f)
    except Exception as e:
        console.print(f"❌ Failed to read metadata.json: {e}", style="red")
        raise click.Abort()
    
    # Get current values
    current_sensitivity = metadata_data.get("storage", {}).get("sensitivity", "restricted")
    current_mirror_url = metadata_data.get("repository", {}).get("mirror", {}).get("url", "")
    
    # Prompt for sensitivity if not provided (OUTSIDE the status spinner)
    if sensitivity is None:
        sensitivity = click.prompt(
            "Storage sensitivity level",
            default=current_sensitivity,
            type=click.Choice(["public", "restricted", "confidential"])
        )
    
    # Prompt for mirror URL if not provided (OUTSIDE the status spinner)
    if mirror_url is None:
        mirror_url = click.prompt(
            "Mirror repository URL (leave empty for none)",
            default=current_mirror_url
        )
    
    # Now do the update inside the status spinner
    with console.status("Updating metadata.json..."):
        try:
            # Update metadata with new fields
            if "storage" not in metadata_data:
                metadata_data["storage"] = {}
            metadata_data["storage"]["sensitivity"] = sensitivity

            if mirror_url.strip():
                if "repository" not in metadata_data:
                    metadata_data["repository"] = {}
                if "mirror" not in metadata_data["repository"]:
                    metadata_data["repository"]["mirror"] = {}
                metadata_data["repository"]["mirror"]["url"] = mirror_url.strip()
                metadata_data["repository"]["mirror"]["purpose"] = "external_collaboration"

            # Write updated metadata
            with open(metadata_path, 'w') as f:
                json.dump(metadata_data, f, indent=2)

        except Exception as e:
            console.print(f"❌ Failed to update metadata: {e}", style="red")
            raise click.Abort()

    console.print("✅ Updated metadata.json with new schema fields")
    console.print(f"   - Storage sensitivity: {sensitivity}")
    if mirror_url.strip():
        console.print(f"   - Mirror URL: {mirror_url}")
    else:
        console.print("   - No mirror URL configured")


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def storage(path, yes):
    """Update DVC storage configuration to use new bucket naming (opt-in)."""
    import json
    from .config import get_config
    from .initializers.storage import init_dvc, is_dvc_repo

    project_path = Path(path) if path else Path.cwd()

    # Phase 1: Load and validate (no spinner needed, it's fast)
    metadata_path = project_path / "metadata.json"
    if not metadata_path.exists():
        console.print("❌ metadata.json not found. Are you in a mintd project directory?", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
    except Exception as e:
        console.print(f"❌ Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    if not is_dvc_repo(project_path):
        console.print("❌ DVC is not initialized in this project.", style="red")
        console.print("Run 'mintd create' with --init-dvc or initialize DVC manually first.")
        raise click.Abort()

    # Get project info
    project_name = metadata["project"]["name"]
    full_name = metadata["project"]["full_name"]
    sensitivity = metadata.get("storage", {}).get("sensitivity", "restricted")

    config = get_config()
    bucket_prefix = config.get("storage", {}).get("bucket_prefix")
    if not bucket_prefix:
        console.print("❌ Storage bucket_prefix not configured.", style="red")
        console.print("Run 'mintd config' to set up storage configuration.")
        raise click.Abort()

    new_bucket_name = bucket_prefix
    remote_name = project_name  # Use project name as remote name

    # Phase 2: Show info and confirm OUTSIDE any spinner
    from .initializers.storage import SENSITIVITY_TO_ACL
    acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")

    console.print(f"📋 Current configuration will be updated:")
    console.print(f"   - Project: {full_name}")
    console.print(f"   - Remote name: {remote_name}")
    console.print(f"   - Sensitivity: {sensitivity} → ACL: {acl_path}")
    console.print(f"   - New bucket: {new_bucket_name}")
    console.print(f"   - New remote URL: s3://{new_bucket_name}/{acl_path}/{project_name}/")

    if not yes:
        if not click.confirm("⚠️  This will reconfigure your DVC remote. Data migration is not included. Continue?"):
            raise click.Abort()

    # Phase 3: Perform update with spinner
    with console.status("Updating DVC storage configuration..."):
        try:
            # Import the ACL mapping
            from .initializers.storage import SENSITIVITY_TO_ACL

            # Map sensitivity to ACL path
            acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")
            remote_url = f"s3://{new_bucket_name}/{acl_path}/{project_name}/"

            # If DVC is already initialized, add/modify the remote
            from .initializers.storage import _run_dvc_command

            # Try to modify existing global remote first, then add if it doesn't exist
            try:
                _run_dvc_command(project_path, ["remote", "modify", "--global", remote_name, "url", remote_url])
            except subprocess.CalledProcessError:
                # Remote doesn't exist, add it as global
                _run_dvc_command(project_path, ["remote", "add", "--global", "-d", remote_name, remote_url])

            # Configure remote settings (globally)
            config = get_config()
            storage = config["storage"]
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
            console.print(f"❌ Failed to update storage configuration: {e}", style="red")
            raise click.Abort()

    console.print("✅ Updated DVC storage configuration")
    console.print(f"   - Remote '{remote_name}' now points to: s3://{new_bucket_name}/{acl_path}/{project_name}/")
    console.print(f"   - Cloud versioning is enabled (version_aware: true)")

    console.print("\n📝 Next steps for data migration:")
    console.print(f"   1. Create the bucket '{new_bucket_name}' if it doesn't exist")
    console.print("   2. Copy data from old bucket to new bucket structure")
    console.print(f"   3. Run 'dvc push -r {remote_name}' to upload data to the new location")
    console.print("   4. Update any existing DVC files to reference the new remote")


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
def utils(path):
    """Update mintd utility scripts to the latest version."""
    project_path = Path(path) if path else Path.cwd()

    with console.status("Updating utility scripts..."):
        try:
            from .registry import load_project_metadata
            import json

            # Load existing metadata to get project info
            metadata_path = project_path / "metadata.json"
            if not metadata_path.exists():
                console.print("❌ metadata.json not found. Are you in a mintd project directory?", style="red")
                raise click.Abort()

            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            # Extract project info
            project_name = metadata["project"]["name"]
            project_type = metadata["project"]["type"]
            language = metadata.get("language", "python")  # Try to get from metadata, fallback to python

            # Get mint version info for updating metadata
            from .templates.base import BaseTemplate
            mint_info = BaseTemplate._get_mint_info()

            # Update metadata with new mint version
            metadata["mint"] = {
                "version": mint_info["mint_version"],
                "commit_hash": mint_info["mint_hash"]
            }

            # Write updated metadata
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)

            console.print(f"✅ Updated mintd version in metadata.json to {mint_info['mint_version']}")

            # Regenerate utility scripts
            from .templates import DataTemplate, ProjectTemplate, InfraTemplate, EnclaveTemplate

            if project_type == "data":
                template_class = DataTemplate
            elif project_type in ["project", "prj"]:
                template_class = ProjectTemplate
            elif project_type == "infra":
                template_class = InfraTemplate
            elif project_type == "enclave":
                template_class = EnclaveTemplate
            else:
                console.print(f"❌ Unknown project type: {project_type}", style="red")
                raise click.Abort()

            # Create template instance and set language
            template = template_class()
            template.language = language

            # Only regenerate utility files
            utils_files = []
            for relative_path, template_name in template.get_template_files():
                if "_mintd_utils" in relative_path:
                    utils_files.append((relative_path, template_name))

            if not utils_files:
                console.print(f"⚠️ No utility files found for {language} projects", style="yellow")
                return

            # Prepare context
            context = {
                "author": metadata["project"].get("created_by", ""),
                "organization": "",  # Could be extracted from metadata if available
                "storage_provider": metadata["storage"].get("provider", "s3"),
                "storage_endpoint": metadata["storage"].get("endpoint", ""),
                "storage_versioning": metadata["storage"].get("versioning", True),
                "bucket_name": metadata["storage"].get("bucket", ""),
                "project_type": project_type,
                "language": language,
                "use_current_repo": False,  # Assume normal project structure
            }
            context.update(mint_info)

            # Regenerate utility files
            for relative_path, template_name in utils_files:
                file_path = project_path / relative_path

                try:
                    jinja_template = template.jinja_env.get_template(template_name)
                    content = jinja_template.render(
                        project_name=project_name,
                        full_project_name=metadata["project"]["full_name"],
                        created_at=metadata["project"]["created_at"],
                        **context
                    )

                    # Ensure parent directory exists
                    file_path.parent.mkdir(parents=True, exist_ok=True)

                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)

                    console.print(f"✅ Updated: {relative_path}")

                except Exception as e:
                    console.print(f"❌ Failed to update {relative_path}: {e}", style="red")
                    raise click.Abort()

            console.print(f"✅ Successfully updated all utility scripts for {project_name}")

        except Exception as e:
            console.print(f"❌ Error updating utilities: {e}", style="red")
            raise click.Abort()


@main.group()
def config():
    """Configure mintd settings."""


@main.group()
def registry():
    """Manage project registration in Data Commons Registry."""


@config.command()
def show():
    """Show current configuration."""
    from .config import get_config

    config = get_config()

    console.print("[bold]Current Configuration:[/bold]")
    console.print()

    console.print("[bold blue]Storage:[/bold blue]")
    storage = config.get("storage", {})
    console.print(f"  Provider: {storage.get('provider', 'Not set')}")
    console.print(f"  Endpoint: {storage.get('endpoint', 'Not set')}")
    console.print(f"  Region: {storage.get('region', 'Not set')}")
    console.print(f"  Bucket Prefix: {storage.get('bucket_prefix', 'Not set')}")
    console.print(f"  Versioning: {storage.get('versioning', 'Not set')}")

    console.print()
    console.print("[bold blue]Registry:[/bold blue]")
    registry = config.get("registry", {})
    console.print(f"  URL: {registry.get('url', 'Not set')}")
    console.print(f"  Organization: {registry.get('org', 'Not set')}")
    console.print(f"  Default Branch: {registry.get('default_branch', 'Not set')}")

    console.print()
    console.print("[bold blue]Defaults:[/bold blue]")
    defaults = config.get("defaults", {})
    console.print(f"  Author: {defaults.get('author', 'Not set')}")
    console.print(f"  Organization: {defaults.get('organization', 'Not set')}")


@registry.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
def register(path):
    """Register a project with the Data Commons Registry."""
    project_path = Path(path) if path else Path.cwd()

    with console.status("Registering project with Data Commons Registry..."):
        try:
            from .registry import get_registry_client, load_project_metadata, save_pending_registration

            # Load project metadata
            metadata = load_project_metadata(project_path)

            # Create registry client and register
            client = get_registry_client()
            pr_url = client.register_project(metadata)

            console.print(f"✅ Registration PR created: {pr_url}")
            console.print("   The PR will be reviewed and merged by registry administrators.")
            console.print("   Registry workflows will validate and synchronize permissions.")

        except Exception as e:
            error_msg = str(e)
            if "Registry URL not configured" in error_msg:
                console.print("❌ Registry URL not configured. Set MINTD_REGISTRY_URL environment variable")
                console.print("   or configure registry.url in ~/.mintd/config.yaml")
            elif "subprocess.CalledProcessError" in str(type(e)):
                console.print("❌ Git or GitHub CLI error. Make sure you have:")
                console.print("   - SSH key configured for GitHub")
                console.print("   - GitHub CLI (gh) installed and authenticated")
                console.print("   - Push access to the registry repository")

                # Save for later retry
                try:
                    metadata = load_project_metadata(project_path)
                    save_pending_registration(project_path, metadata)
                    console.print("💾 Registration request saved. Run 'mint register' when prerequisites are met.")
                except Exception:
                    pass
            else:
                console.print(f"❌ Registration failed: {e}")

                # Try to save for later retry
                try:
                    from .registry import load_project_metadata, save_pending_registration
                    metadata = load_project_metadata(project_path)
                    save_pending_registration(project_path, metadata)
                    console.print("💾 Registration request saved. Run 'mintd register' when prerequisites are met.")
                except Exception:
                    pass


@registry.command()
@click.argument("project_name")
def status(project_name):
    """Check registration status of a project."""
    with console.status(f"Checking registration status for '{project_name}'..."):
        try:
            from .registry import get_registry_client

            client = get_registry_client()
            status_info = client.check_registration_status(project_name)

            if status_info.get("registered"):
                console.print(f"✅ Project '{project_name}' is registered")
                console.print(f"   Type: {status_info['type']}")
                console.print(f"   Full Name: {status_info['full_name']}")
                console.print(f"   Registry URL: {status_info['url']}")
            elif status_info.get("pending_pr"):
                console.print(f"⏳ Project '{project_name}' has a pending registration PR")
                console.print(f"   PR: {status_info['pending_pr']}")
                console.print(f"   Title: {status_info['pr_title']}")
            else:
                console.print(f"❌ Project '{project_name}' is not registered")
                console.print("   Run 'mintd register --path /path/to/project' to register it.")

        except Exception as e:
            error_msg = str(e)
            if "Registry URL not configured" in error_msg:
                console.print("❌ Registry URL not configured. Set MINTD_REGISTRY_URL environment variable")
                console.print("   or configure registry.url in ~/.mintd/config.yaml")
            else:
                console.print(f"❌ Status check failed: {e}")


@registry.command()
@click.argument("project_name")
@click.option("--description", help="Update project description")
@click.option("--add-tag", multiple=True, help="Add a tag to the project")
@click.option("--remove-tag", multiple=True, help="Remove a tag from the project")
def update(project_name, description, add_tag, remove_tag):
    """Update project metadata in the registry."""
    console.print("❌ Update functionality is not yet implemented.")
    console.print("   This feature will be added in a future version.")
    console.print("   For now, updates must be made directly in the registry repository.")


@registry.command()
def sync():
    """Process pending registrations that were saved for offline mode."""
    from .registry import get_pending_registrations, RegistryClient, clear_pending_registration

    pending = get_pending_registrations()

    if not pending:
        console.print("✅ No pending registrations to process.")
        return

    console.print(f"Found {len(pending)} pending registration(s). Processing...")

    successful = 0
    failed = 0

    for item in pending:
        project_name = item["metadata"]["project"]["full_name"]
        console.print(f"Processing: {project_name}")

        try:
            from .registry import get_registry_client
            client = get_registry_client()
            pr_url = client.register_project(item["metadata"])

            console.print(f"✅ Registered: {pr_url}")
            clear_pending_registration(project_name)
            successful += 1

        except Exception as e:
            console.print(f"❌ Failed to register {project_name}: {e}")
            failed += 1

    console.print(f"\n📊 Summary: {successful} successful, {failed} failed")
    if failed > 0:
        console.print("Failed registrations remain in queue. Try again later.")


@config.command()
@click.option("--set", "set_value", nargs=2, metavar="KEY VALUE",
              help="Set a configuration value (e.g., --set storage.bucket_prefix mylab)")
@click.option("--set-credentials", is_flag=True,
              help="Set storage credentials interactively")
def setup(set_value, set_credentials):
    """Set up or modify configuration."""
    from .config import init_config, save_config, get_config, set_storage_credentials

    if set_value:
        key, value = set_value
        config = get_config()

        # Parse nested keys like "storage.bucket_prefix"
        keys = key.split(".")
        current = config
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        current[keys[-1]] = value

        save_config(config)
        console.print(f"✅ Set {key} = {value}")

    elif set_credentials:
        from rich.prompt import Prompt

        access_key = Prompt.ask("AWS Access Key ID")
        secret_key = Prompt.ask("AWS Secret Access Key", password=True)

        try:
            set_storage_credentials(access_key, secret_key)
            console.print("✅ Credentials stored securely")
        except RuntimeError as e:
            console.print(f"❌ Error storing credentials: {e}")

    else:
        # Interactive setup
        init_config()


@main.group()
def data():
    """Manage data products and dependencies."""
    pass


@data.command()
@click.argument("product_name")
@click.option("--destination", "-d", help="Local destination directory")
@click.option("--stage", help="Pipeline stage to pull (e.g., final, clean)")
@click.option("--path", help="Specific path to pull from the product")
def pull(product_name, destination, stage, path):
    """Pull/download data from a registered data product."""
    from pathlib import Path
    from .data_import import pull_data_product

    try:
        if stage and path:
            console.print("❌ Cannot specify both --stage and --path", style="red")
            raise click.Abort()

        success = pull_data_product(
            product_name=product_name,
            destination=destination,
            stage=stage,
            path=path
        )

        if not success:
            raise click.Abort()

    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command(name="import")
@click.argument("product_name")
@click.option("--stage", help="Pipeline stage to import (e.g., final, clean)")
@click.option("--source-path", help="Specific path to import from the product")
@click.option("--dest", help="Local destination path")
@click.option("--rev", help="Specific git revision to import from")
@click.option("--project-path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
def import_(product_name, stage, source_path, dest, rev, project_path):
    """Import data product as DVC dependency into current project."""
    from pathlib import Path
    from .data_import import import_data_product, update_project_metadata, query_data_product

    project_path = Path(project_path) if project_path else Path.cwd()

    try:
        if stage and source_path:
            console.print("❌ Cannot specify both --stage and --source-path", style="red")
            raise click.Abort()

        # Import the data
        result = import_data_product(
            product_name=product_name,
            project_path=project_path,
            stage=stage,
            path=source_path,
            dest=dest,
            repo_rev=rev
        )

        if result.success:
            # Update metadata with dependency info
            try:
                product_info = query_data_product(product_name)
                update_project_metadata(project_path, result, product_info)
                console.print("✅ Metadata updated with dependency information", style="green")
            except Exception as e:
                console.print(f"⚠️ Import succeeded but metadata update failed: {e}", style="yellow")
                console.print("You may need to manually update metadata.json", style="yellow")
        else:
            console.print(f"❌ Import failed: {result.error_message}", style="red")
            raise click.Abort()

    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command()
@click.option("--imported", "-i", is_flag=True, help="Show imported dependencies instead of available products")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
def list(imported, project_path):
    """List available data products or imported dependencies."""
    from pathlib import Path
    from .data_import import list_data_products

    project_path = Path(project_path) if project_path else Path.cwd()

    try:
        list_data_products(show_imported=imported, project_path=project_path)
    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@main.group()
def enclave():
    """Manage enclave data transfers and workspace."""
    pass


@enclave.command()
@click.argument("repo_name")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
@click.option("--no-pull", is_flag=True, help="Add to approved list without pulling data")
def add(repo_name, path, no_pull):
    """Add a data product to the enclave's approved list and automatically pull the data.

    By default, this command will add the product to the approved list and immediately
    attempt to pull/download the data. Use --no-pull to add without downloading."""
    from pathlib import Path
    import yaml
    import subprocess

    enclave_path = Path(path) if path else Path.cwd()
    manifest_path = enclave_path / "enclave_manifest.yaml"

    if not manifest_path.exists():
        console.print(f"❌ Enclave manifest not found: {manifest_path}", style="red")
        raise click.Abort()

    # Load manifest
    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    # Check if already approved
    approved = manifest.setdefault('approved_products', [])
    existing = any(item['repo'] == repo_name for item in approved)

    if existing:
        console.print(f"⚠ Repository '{repo_name}' is already approved in this enclave.")
        if not no_pull:
            console.print("Attempting to pull latest data anyway...")
        else:
            return

    if not existing:
        # Add to approved list (basic structure - user can edit details)
        approved.append({
            'repo': repo_name,
            'registry_entry': f"catalog/data/{repo_name}.yaml",
            'stage': 'final'
        })

        # Save manifest
        with open(manifest_path, 'w') as f:
            yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)

        console.print(f"✅ Added '{repo_name}' to approved products.")

    # Pull the data unless --no-pull is specified
    if not no_pull:
        from .enclave_commands import pull_enclave_data
        try:
            pull_enclave_data(enclave_path, repo_name=repo_name)
            console.print("✅ Data pull completed successfully.")
        except Exception as e:
            console.print(f"❌ Data pull failed: {e}", style="red")
            console.print("You can try pulling manually later with:")
            console.print(f"  ./scripts/pull_data.sh {repo_name}")
    else:
        console.print("Edit enclave_manifest.yaml to customize registry entry and stage if needed.")


@enclave.command()
@click.argument("repo_name", required=False)
@click.option("--all", "-a", "pull_all", is_flag=True, help="Pull latest for all approved products")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
def pull(repo_name, pull_all, path):
    """Pull data products from registry (networked machine only)."""
    from .enclave_commands import pull_enclave_data

    enclave_path = Path(path) if path else Path.cwd()
    
    try:
        pull_enclave_data(enclave_path, repo_name=repo_name, pull_all=pull_all)
    except Exception as e:
        console.print(f"❌ Pull failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.option("--name", "-n", help="Transfer package name")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
def package(name, path):
    """Package downloaded data for transfer to enclave."""
    from .enclave_commands import package_transfer

    enclave_path = Path(path) if path else Path.cwd()
    
    try:
        package_transfer(enclave_path, name=name)
    except Exception as e:
        console.print(f"❌ Packaging failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.argument("transfer_file", type=click.Path(exists=True, path_type=Path))
@click.option("--dest", "-d", type=click.Path(path_type=Path), help="Destination directory")
def unpack(transfer_file, dest):
    """Unpack a transfer archive."""
    from .enclave_commands import unpack_transfer
    try:
        unpack_transfer(transfer_file, dest_dir=dest)
    except Exception as e:
        console.print(f"❌ Unpack failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.argument("transfer_path", type=click.Path(exists=True, path_type=Path))
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
def verify(transfer_path, path):
    """Verify a transfer (archive or unpacked directory) and update enclave manifest."""
    from .enclave_commands import verify_transfer
    enclave_path = Path(path) if path else Path.cwd()
    try:
        success = verify_transfer(transfer_path, enclave_path=enclave_path)
        if not success:
            raise click.Abort()
    except Exception as e:
        console.print(f"❌ Verification failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.argument("repo_name", required=False)
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
def list(repo_name, path):
    """List approved and transferred data products."""
    from pathlib import Path
    import yaml

    enclave_path = Path(path) if path else Path.cwd()
    manifest_path = enclave_path / "enclave_manifest.yaml"

    if not manifest_path.exists():
        console.print(f"❌ Enclave manifest not found: {manifest_path}", style="red")
        raise click.Abort()

    # Load manifest
    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    # Filter by repo if specified
    approved = manifest.get('approved_products', [])
    transferred = manifest.get('transferred', [])

    if repo_name:
        approved = [item for item in approved if item['repo'] == repo_name]
        transferred = [item for item in transferred if item['repo'] == repo_name]

    console.print(f"Enclave Status{' for ' + repo_name if repo_name else ''}:")
    console.print("-" * 40)

    console.print(f"Approved Products: {len(approved)}")
    for item in approved:
        repo = item['repo']
        transferred_count = len([t for t in transferred if t['repo'] == repo])
        console.print(f"  • {repo} ({transferred_count} versions transferred)")

    if transferred:
        console.print(f"\nTransferred Data: {len(transferred)}")
        for item in transferred:
            repo = item['repo']
            version = item['dvc_hash'][:7]
            date = item.get('transfer_date', 'unknown')
            console.print(f"  • {repo}: {version} ({date})")


@enclave.command()
@click.option("--keep", "-k", default=1, type=int, help="Number of recent versions to keep (default: 1)")
@click.option("--staging-only", is_flag=True, help="Only clean the staging area, keep all downloads")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to enclave directory (defaults to current directory)")
def clean(keep, staging_only, path):
    """Prune old data versions and clean staging area."""
    from .enclave_commands import clean_enclave
    enclave_path = Path(path) if path else Path.cwd()
    
    try:
        clean_enclave(enclave_path, keep_recent=keep, staging_only=staging_only)
    except Exception as e:
        console.print(f"❌ Cleanup failed: {e}", style="red")
        raise click.Abort()


@main.group()
def manifest():
    """Manage file manifests for change detection."""


@manifest.command()
@click.option("--directory", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Directory to scan for files")
@click.option("--pattern", "-p", default="*", help="File pattern to match (default: *)")
@click.option("--output", "-o", type=click.Path(path_type=Path),
              help="Manifest output path (default: manifest.json in current directory)")
def create(directory: Path, pattern: str, output: Path):
    """Create or update a file manifest for change detection."""
    from .manifest import create_manifest

    try:
        if output is None:
            output = Path.cwd() / "manifest.json"

        with console.status("Creating manifest..."):
            manifest = create_manifest(directory, pattern, output, base_directory=Path.cwd())

        file_count = len(manifest.get("files", {}))
        console.print(f"✅ Created manifest with {file_count} files", style="green")
        console.print(f"   Saved to: {output}")

    except Exception as e:
        console.print(f"❌ Error creating manifest: {e}", style="red")
        raise click.Abort()


@manifest.command()
@click.argument("filepath", type=click.Path(path_type=Path))
@click.option("--manifest", "-m", type=click.Path(exists=True, path_type=Path),
              help="Path to manifest file (default: manifest.json in current directory)")
def check(filepath: Path, manifest: Path):
    """Check if a file has changed compared to the manifest."""
    from .manifest import load_manifest, has_file_changed

    try:
        if manifest is None:
            manifest_path = Path.cwd() / "manifest.json"
        else:
            manifest_path = manifest

        if not manifest_path.exists():
            console.print("❌ Manifest file not found", style="red")
            raise click.Abort()

        manifest_data = load_manifest(manifest_path)

        if has_file_changed(filepath, manifest_data, base_directory=Path.cwd()):
            console.print(f"📝 File has changed: {filepath}", style="yellow")
            return 1  # Exit code for changed
        else:
            console.print(f"✅ File unchanged: {filepath}", style="green")
            return 0  # Exit code for unchanged

    except Exception as e:
        console.print(f"❌ Error checking file: {e}", style="red")
        raise click.Abort()


@manifest.command()
@click.option("--directory", "-d", required=True, type=click.Path(exists=True, path_type=Path),
              help="Directory to scan for files")
@click.option("--pattern", "-p", default="*", help="File pattern to match (default: *)")
@click.option("--manifest", "-m", type=click.Path(exists=True, path_type=Path),
              help="Path to manifest file (default: manifest.json in current directory)")
def status(directory: Path, pattern: str, manifest: Path):
    """Show status of files in a directory compared to manifest."""
    from .manifest import load_manifest, get_files_to_update, get_unchanged_files

    try:
        if manifest is None:
            manifest_path = Path.cwd() / "manifest.json"
        else:
            manifest_path = manifest

        if not manifest_path.exists():
            console.print("❌ Manifest file not found", style="red")
            raise click.Abort()

        manifest_data = load_manifest(manifest_path)

        changed_files = get_files_to_update(directory, manifest_data, pattern, base_directory=Path.cwd())
        unchanged_files = get_unchanged_files(directory, manifest_data, pattern, base_directory=Path.cwd())

        console.print(f"📊 Manifest status for {directory}")
        console.print(f"   Pattern: {pattern}")
        console.print(f"   Manifest: {manifest_path}")
        console.print()

        if changed_files:
            console.print(f"📝 Changed files ({len(changed_files)}):", style="yellow")
            for f in changed_files[:10]:  # Show first 10
                console.print(f"   • {f}")
            if len(changed_files) > 10:
                console.print(f"   ... and {len(changed_files) - 10} more")
        else:
            console.print("✅ No changed files found", style="green")

        if unchanged_files:
            console.print(f"✅ Unchanged files ({len(unchanged_files)}):", style="green")
            for f in unchanged_files[:10]:  # Show first 10
                console.print(f"   • {f}")
            if len(unchanged_files) > 10:
                console.print(f"   ... and {len(unchanged_files) - 10} more")

    except Exception as e:
        console.print(f"❌ Error getting status: {e}", style="red")
        raise click.Abort()



def register_custom_commands():
    """Register CLI commands for custom templates."""
    try:
        from .utils.loader import load_custom_templates
        custom_templates = load_custom_templates()
        
        for prefix, _ in custom_templates.items():
            cmd_name = prefix.rstrip("_")
            
            # Skip if command already exists
            if cmd_name in create.commands:
                continue
                
            # Create a closure to capture cmd_name
            def create_command_func(cmd_name_val):
                @create.command(name=cmd_name_val, help=f"Create a {cmd_name_val} project ({prefix}*).")
                @click.option("--name", "-n", required=True, help="Project name")
                @click.option("--path", "-p", default=".", help="Output directory")
                @click.option("--lang", "--language", type=click.Choice(["python", "r", "stata"], case_sensitive=False), required=True, help="Primary programming language")
                @click.option("--no-git", is_flag=True, help="Skip Git initialization")
                @click.option("--no-dvc", is_flag=True, help="Skip DVC initialization")
                @click.option("--bucket", help="Override bucket name for DVC remote")
                @click.option("--register", is_flag=True, help="Register project with Registry")
                @click.option("--use-current-repo", is_flag=True, help="Use current directory as project root")
                @click.option("--admin-team", help="Override default admin team")
                @click.option("--researcher-team", help="Override default researcher team")
                def custom_cmd(name, path, lang, no_git, no_dvc, bucket, register, use_current_repo, admin_team, researcher_team):
                    from .api import create_project
                    with console.status("Scaffolding project..."):
                        try:
                            result = create_project(
                                project_type=cmd_name_val,
                                name=name,
                                path=path,
                                language=lang,
                                init_git=not no_git,
                                init_dvc=not no_dvc,
                                bucket_name=bucket,
                                register_project=register,
                                use_current_repo=use_current_repo,
                                admin_team=admin_team,
                                researcher_team=researcher_team,
                            )
                            console.print(f"✅ Created: {result.full_name}", style="green")
                            console.print(f"   Location: {result.path}", style="dim")

                            if register and result.registration_url:
                                console.print(f"   Registration PR: {result.registration_url}", style="dim")
                        except Exception as e:
                            console.print(f"❌ Error: {e}", style="red")
                            raise click.Abort()
                return custom_cmd
            
            # Register the command
            create_command_func(cmd_name)
                        
    except Exception as e:
        # Don't crash CLI if custom template loading fails
        pass

# Register custom commands
register_custom_commands()


if __name__ == "__main__":
    main()