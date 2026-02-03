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
