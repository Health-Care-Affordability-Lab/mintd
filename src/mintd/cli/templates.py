"""Templates command group."""

import click

from .main import main
from .utils import console


@main.group()
def templates():
    """Manage project templates."""
    pass


@templates.command(name="list")
def list_templates():
    """List available project templates."""
    from rich.table import Table

    from ..utils.loader import get_custom_template_dir, load_custom_templates

    table = Table(title="Available Templates")
    table.add_column("Type", style="cyan")
    table.add_column("Prefix", style="green")
    table.add_column("Source", style="dim")
    table.add_column("Description")

    table.add_row("project", "prj_", "Built-in", "Standard research project")
    table.add_row("data", "data_", "Built-in", "Data product")
    table.add_row("infra", "infra_", "Built-in", "Infrastructure library")
    table.add_row("enclave", "enclave_", "Built-in", "Secure data enclave")

    custom_templates = load_custom_templates()
    custom_dir = get_custom_template_dir()

    for prefix, cls in custom_templates.items():
        type_name = prefix.rstrip("_")
        description = cls.__doc__.strip() if cls.__doc__ else "Custom template"
        description = description.split("\n")[0]
        table.add_row(type_name, prefix, "Custom", description)

    console.print(table)
    console.print(f"\nCustom templates directory: [dim]{custom_dir}[/dim]")
