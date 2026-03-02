"""Data command group for managing data products."""

from pathlib import Path

import click

from .main import main
from .utils import console


@main.group()
def data():
    """Manage data products and dependencies."""
    pass


@data.command(name="pull")
@click.argument("product_name")
@click.option("--destination", "-d", help="Local destination directory")
@click.option("--stage", help="Pipeline stage to pull")
@click.option("--path", help="Specific path to pull from the product")
def data_pull(product_name, destination, stage, path):
    """Pull/download data from a registered data product."""
    from ..data_import import pull_data_product

    try:
        if stage and path:
            console.print("❌ Cannot specify both --stage and --path", style="red")
            raise click.Abort()

        success = pull_data_product(
            product_name=product_name, destination=destination, stage=stage, path=path
        )
        if not success:
            raise click.Abort()

    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command(name="import")
@click.argument("product_name")
@click.option("--stage", help="Pipeline stage to import")
@click.option("--source-path", help="Specific path to import")
@click.option("--dest", help="Local destination path")
@click.option("--rev", help="Specific git revision")
@click.option("--project-path", "-p", type=click.Path(exists=True, path_type=Path))
def import_(product_name, stage, source_path, dest, rev, project_path):
    """Import data product as DVC dependency into current project."""
    from ..data_import import import_data_product, query_data_product, update_project_metadata

    project_path = Path(project_path) if project_path else Path.cwd()

    try:
        if stage and source_path:
            console.print("❌ Cannot specify both --stage and --source-path", style="red")
            raise click.Abort()

        result = import_data_product(
            product_name=product_name, project_path=project_path,
            stage=stage, path=source_path, dest=dest, repo_rev=rev
        )

        if result.success:
            try:
                product_info = query_data_product(product_name)
                update_project_metadata(project_path, result, product_info)
                console.print("✅ Metadata updated with dependency information", style="green")
            except Exception as e:
                console.print(f"⚠️ Import succeeded but metadata update failed: {e}", style="yellow")
        else:
            console.print(f"❌ Import failed: {result.error_message}", style="red")
            raise click.Abort()

    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command(name="remove")
@click.argument("import_name")
@click.option("--force", "-f", is_flag=True, help="Remove even if dvc.yaml has references")
@click.option("--project-path", "-p", type=click.Path(exists=True, path_type=Path))
def data_remove(import_name, force, project_path):
    """Remove a data import from the project.

    Removes the import directory, .dvc file, and metadata entry.
    """
    from ..data_import import remove_data_import

    project_path = Path(project_path) if project_path else Path.cwd()

    try:
        result = remove_data_import(
            project_path=project_path,
            import_name=import_name,
            force=force
        )

        if not result.success:
            if result.warnings:
                for warning in result.warnings:
                    console.print(f"⚠️  {warning}", style="yellow")
            console.print(f"❌ {result.error_message}", style="red")
            raise click.Abort()

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command(name="list")
@click.option("--imported", "-i", is_flag=True, help="Show imported dependencies")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def data_list(imported, path):
    """List available data products or imported dependencies."""
    from ..data_import import list_data_products

    project_path = Path(path) if path else Path.cwd()

    try:
        list_data_products(show_imported=imported, project_path=project_path)
    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()


@data.command(name="update")
@click.argument("path", required=False)
@click.option("--rev", help="Specific git revision to update to")
@click.option("--dry-run", is_flag=True, help="Show what would be updated")
@click.option("--project-path", "-p", type=click.Path(exists=True, path_type=Path))
def data_update(path, rev, dry_run, project_path):
    """Update DVC data imports to latest version.

    If PATH is provided, update only that specific .dvc file.
    Otherwise, update all imports in the project.
    """
    from ..data_import import update_all_imports, update_single_import

    project_path = Path(project_path) if project_path else Path.cwd()

    try:
        if path:
            # Update single import
            result = update_single_import(
                project_path=project_path,
                dvc_file_path=path,
                rev=rev
            )
            if result.success:
                if result.skipped:
                    console.print(f"⏭️  {path} is already up-to-date", style="yellow")
                else:
                    console.print(f"✅ Updated {path}", style="green")
            else:
                console.print(f"❌ Failed to update {path}: {result.error_message}", style="red")
                raise click.Abort()
        else:
            # Update all imports
            if dry_run:
                console.print("🔍 Dry run - showing what would be updated:", style="blue")

            results = update_all_imports(
                project_path=project_path,
                rev=rev,
                dry_run=dry_run
            )

            if not results:
                console.print("No data imports found to update.", style="yellow")
                return

            success_count = sum(1 for r in results if r.success)
            fail_count = sum(1 for r in results if not r.success)

            for r in results:
                if r.success:
                    if dry_run or r.skipped:
                        console.print(f"  📦 {r.dvc_file}", style="dim")
                    else:
                        console.print(f"  ✅ {r.dvc_file}", style="green")
                else:
                    console.print(f"  ❌ {r.dvc_file}: {r.error_message}", style="red")

            if not dry_run:
                console.print(f"\n📊 Updated {success_count}/{len(results)} imports")
                if fail_count > 0:
                    raise click.Abort()

    except click.Abort:
        raise
    except Exception as e:
        console.print(f"❌ Error: {e}", style="red")
        raise click.Abort()
