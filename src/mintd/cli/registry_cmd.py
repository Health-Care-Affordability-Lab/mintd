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
@click.argument("project_name")
@click.option("--description", help="Update project description")
@click.option("--add-tag", multiple=True, help="Add a tag")
@click.option("--remove-tag", multiple=True, help="Remove a tag")
def update(project_name, description, add_tag, remove_tag):
    """Update project metadata in the registry."""
    console.print("❌ Update functionality is not yet implemented.")


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
