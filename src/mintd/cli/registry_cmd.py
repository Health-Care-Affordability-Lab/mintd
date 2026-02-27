"""Registry command group."""

from pathlib import Path

import click

from .main import main
from .utils import console


@main.group()
def registry():
    """Manage project registration in Data Commons Registry."""
    pass


@registry.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
def register(path):
    """Register a project with the Data Commons Registry."""
    project_path = Path(path) if path else Path.cwd()

    with console.status("Registering project with Data Commons Registry..."):
        try:
            from ..registry import get_registry_client, load_project_metadata, save_pending_registration

            metadata = load_project_metadata(project_path)
            client = get_registry_client()
            pr_url = client.register_project(metadata)

            console.print(f"✅ Registration PR created: {pr_url}")
            console.print("   The PR will be reviewed and merged by registry administrators.")

        except Exception as e:
            error_msg = str(e)
            if "Registry URL not configured" in error_msg:
                console.print("❌ Registry URL not configured.")
                console.print("   Configure registry.url in ~/.mintd/config.yaml")
            else:
                console.print(f"❌ Registration failed: {e}")
                try:
                    from ..registry import load_project_metadata, save_pending_registration
                    metadata = load_project_metadata(project_path)
                    save_pending_registration(project_path, metadata)
                    console.print("💾 Registration request saved for later.")
                except Exception:
                    pass


@registry.command(name="status")
@click.argument("project_name")
def registry_status(project_name):
    """Check registration status of a project."""
    with console.status(f"Checking registration status for '{project_name}'..."):
        try:
            from ..registry import get_registry_client

            client = get_registry_client()
            status_info = client.check_registration_status(project_name)

            if status_info.get("registered"):
                console.print(f"✅ Project '{project_name}' is registered")
                console.print(f"   Type: {status_info['type']}")
                console.print(f"   Full Name: {status_info['full_name']}")
            elif status_info.get("pending_pr"):
                console.print(f"⏳ Project '{project_name}' has a pending registration PR")
                console.print(f"   PR: {status_info['pending_pr']}")
            else:
                console.print(f"❌ Project '{project_name}' is not registered")

        except Exception as e:
            console.print(f"❌ Status check failed: {e}")


@registry.command()
@click.argument("project_name", required=False)
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
@click.option("--dry-run", is_flag=True, help="Show changes without creating PR")
def update(project_name, path, dry_run):
    """Update project metadata in the registry.

    Syncs local metadata.json changes to the Data Commons Registry by creating
    a pull request with the updates.

    Two modes of operation:

    \b
    1. From project directory (recommended):
       cd /path/to/my-project
       mintd registry update

    \b
    2. By project name:
       mintd registry update PROJECT_NAME --path /path/to/project
    """
    from ..exceptions import RegistryNotFoundError
    from ..registry import get_registry_client, load_project_metadata

    # Determine project path
    project_path = Path(path) if path else Path.cwd()

    # Load local metadata
    try:
        local_metadata = load_project_metadata(project_path)
    except FileNotFoundError:
        console.print("❌ metadata.json not found")
        console.print("   Run from a project directory or specify --path")
        return

    # Get project name from metadata if not provided
    if not project_name:
        project_name = local_metadata.get("project", {}).get("name")
        if not project_name:
            console.print("❌ Could not determine project name from metadata.json")
            return

    # Get registry client
    try:
        client = get_registry_client()
    except Exception as e:
        error_msg = str(e)
        if "Registry URL not configured" in error_msg or not error_msg:
            console.print("❌ Registry URL not configured")
            console.print("   Configure registry.url in ~/.mintd/config.yaml")
        else:
            console.print(f"❌ Failed to initialize registry client: {e}")
        return

    # Perform update
    action = "Checking for changes" if dry_run else "Updating project in registry"
    with console.status(f"{action}..."):
        try:
            pr_url = client.update_project(project_name, local_metadata, dry_run=dry_run)

            if pr_url:
                console.print(f"✅ Update PR created: {pr_url}")
                console.print("   The PR will be reviewed and merged by registry administrators.")
            elif not dry_run:
                console.print("✅ No changes to update")

        except RegistryNotFoundError as e:
            console.print(f"❌ {e.message}")
            if e.suggestion:
                console.print(f"   💡 {e.suggestion}")

        except Exception as e:
            console.print(f"❌ Update failed: {e}")


@registry.command()
def sync():
    """Process pending registrations."""
    from ..registry import clear_pending_registration, get_pending_registrations

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
            from ..registry import get_registry_client
            client = get_registry_client()
            pr_url = client.register_project(item["metadata"])
            console.print(f"✅ Registered: {pr_url}")
            clear_pending_registration(project_name)
            successful += 1
        except Exception as e:
            console.print(f"❌ Failed to register {project_name}: {e}")
            failed += 1

    console.print(f"\n📊 Summary: {successful} successful, {failed} failed")
