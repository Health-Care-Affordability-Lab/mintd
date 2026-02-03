"""Manifest command group for file change detection."""

from pathlib import Path

import click

from .main import main
from .utils import console


@main.group()
def manifest():
    """Manage file manifests for change detection."""
    pass


@manifest.command()
@click.option("--directory", "-d", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--pattern", "-p", default="*", help="File pattern to match")
@click.option("--output", "-o", type=click.Path(path_type=Path))
def create(directory, pattern, output):
    """Create or update a file manifest for change detection."""
    from ..manifest import create_manifest

    try:
        if output is None:
            output = Path.cwd() / "manifest.json"

        with console.status("Creating manifest..."):
            manifest_data = create_manifest(directory, pattern, output, base_directory=Path.cwd())

        file_count = len(manifest_data.get("files", {}))
        console.print(f"✅ Created manifest with {file_count} files", style="green")
        console.print(f"   Saved to: {output}")

    except Exception as e:
        console.print(f"❌ Error creating manifest: {e}", style="red")
        raise click.Abort()


@manifest.command()
@click.argument("filepath", type=click.Path(path_type=Path))
@click.option("--manifest", "-m", type=click.Path(exists=True, path_type=Path))
def check(filepath, manifest):
    """Check if a file has changed compared to the manifest."""
    from ..manifest import has_file_changed, load_manifest

    try:
        manifest_path = manifest if manifest else Path.cwd() / "manifest.json"

        if not manifest_path.exists():
            console.print("❌ Manifest file not found", style="red")
            raise click.Abort()

        manifest_data = load_manifest(manifest_path)

        if has_file_changed(filepath, manifest_data, base_directory=Path.cwd()):
            console.print(f"📝 File has changed: {filepath}", style="yellow")
            return 1
        else:
            console.print(f"✅ File unchanged: {filepath}", style="green")
            return 0

    except Exception as e:
        console.print(f"❌ Error checking file: {e}", style="red")
        raise click.Abort()


@manifest.command(name="status")
@click.option("--directory", "-d", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--pattern", "-p", default="*", help="File pattern to match")
@click.option("--manifest", "-m", type=click.Path(exists=True, path_type=Path))
def manifest_status(directory, pattern, manifest):
    """Show status of files in a directory compared to manifest."""
    from ..manifest import get_files_to_update, get_unchanged_files, load_manifest

    try:
        manifest_path = manifest if manifest else Path.cwd() / "manifest.json"

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
            for f in changed_files[:10]:
                console.print(f"   • {f}")
            if len(changed_files) > 10:
                console.print(f"   ... and {len(changed_files) - 10} more")
        else:
            console.print("✅ No changed files found", style="green")

        if unchanged_files:
            console.print(f"✅ Unchanged files ({len(unchanged_files)}):", style="green")
            for f in unchanged_files[:10]:
                console.print(f"   • {f}")
            if len(unchanged_files) > 10:
                console.print(f"   ... and {len(unchanged_files) - 10} more")

    except Exception as e:
        console.print(f"❌ Error getting status: {e}", style="red")
        raise click.Abort()
