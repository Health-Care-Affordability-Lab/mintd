"""Command Line Interface for mint."""

import click
from pathlib import Path
from rich.console import Console

console = Console()


@click.group()
@click.version_option(version="1.0.0")
def main():
    """mint - Lab Project Scaffolding Tool"""
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
def data(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool):
    """Create a data product repository (data_{name})."""
    from .api import create_project

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
def project(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool):
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
def infra(name: str, path: str, lang: str, no_git: bool, no_dvc: bool, bucket: str, register: bool, use_current_repo: bool):
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
            )
            console.print(f"✅ Created: {result.full_name}", style="green")
            console.print(f"   Location: {result.path}", style="dim")

            if register and result.registration_url:
                console.print(f"   Registration PR: {result.registration_url}", style="dim")
        except Exception as e:
            console.print(f"❌ Error: {e}", style="red")
            raise click.Abort()


@main.group()
def update():
    """Update project components."""


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory (defaults to current directory)")
def utils(path):
    """Update mint utility scripts to the latest version."""
    project_path = Path(path) if path else Path.cwd()

    with console.status("Updating utility scripts..."):
        try:
            from .registry import load_project_metadata
            from pathlib import Path
            import json

            # Load existing metadata to get project info
            metadata_path = project_path / "metadata.json"
            if not metadata_path.exists():
                console.print("❌ metadata.json not found. Are you in a mint project directory?", style="red")
                raise click.Abort()

            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            # Extract project info
            project_name = metadata["project"]["name"]
            project_type = metadata["project"]["type"]
            language = metadata.get("language", "python")  # Try to get from metadata, fallback to python

            # Get mint version info for updating metadata
            from ..templates.base import BaseTemplate
            template = BaseTemplate()
            mint_info = template._get_mint_info()

            # Update metadata with new mint version
            metadata["mint"] = {
                "version": mint_info["mint_version"],
                "commit_hash": mint_info["mint_hash"]
            }

            # Write updated metadata
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)

            console.print(f"✅ Updated mint version in metadata.json to {mint_info['mint_version']}")

            # Regenerate utility scripts
            from .templates import DataTemplate, ProjectTemplate, InfraTemplate

            if project_type == "data":
                template_class = DataTemplate
            elif project_type in ["project", "prj"]:
                template_class = ProjectTemplate
            elif project_type == "infra":
                template_class = InfraTemplate
            else:
                console.print(f"❌ Unknown project type: {project_type}", style="red")
                raise click.Abort()

            # Create template instance and set language
            template = template_class()
            template.language = language

            # Only regenerate utility files
            utils_files = []
            for relative_path, template_name in template.get_template_files():
                if "_mint_utils" in relative_path:
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
    """Configure mint settings."""


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
                console.print("❌ Registry URL not configured. Set MINT_REGISTRY_URL environment variable")
                console.print("   or configure registry.url in ~/.mint/config.yaml")
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
                    console.print("💾 Registration request saved. Run 'mint register' when prerequisites are met.")
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
                console.print("   Run 'mint register --path /path/to/project' to register it.")

        except Exception as e:
            error_msg = str(e)
            if "Registry URL not configured" in error_msg:
                console.print("❌ Registry URL not configured. Set MINT_REGISTRY_URL environment variable")
                console.print("   or configure registry.url in ~/.mint/config.yaml")
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


if __name__ == "__main__":
    main()