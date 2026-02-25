"""Config command group."""

import click

from .main import main
from .utils import console


@main.group()
def config():
    """Configure mintd settings."""
    pass


@config.command()
def show():
    """Show current configuration."""
    from ..config import get_config

    cfg = get_config()

    console.print("[bold]Current Configuration:[/bold]")
    console.print()

    console.print("[bold blue]Storage:[/bold blue]")
    storage = cfg.get("storage", {})
    console.print(f"  Provider: {storage.get('provider', 'Not set')}")
    console.print(f"  Endpoint: {storage.get('endpoint', 'Not set')}")
    console.print(f"  Region: {storage.get('region', 'Not set')}")
    console.print(f"  Bucket Prefix: {storage.get('bucket_prefix', 'Not set')}")
    console.print(f"  Versioning: {storage.get('versioning', 'Not set')}")

    console.print()
    console.print("[bold blue]Registry:[/bold blue]")
    registry = cfg.get("registry", {})
    console.print(f"  URL: {registry.get('url', 'Not set')}")
    console.print(f"  Organization: {registry.get('org', 'Not set')}")
    console.print(f"  Default Branch: {registry.get('default_branch', 'Not set')}")

    console.print()
    console.print("[bold blue]Defaults:[/bold blue]")
    defaults = cfg.get("defaults", {})
    console.print(f"  Author: {defaults.get('author', 'Not set')}")
    console.print(f"  Organization: {defaults.get('organization', 'Not set')}")


@config.command()
@click.option("--set", "set_value", nargs=2, metavar="KEY VALUE",
              help="Set a configuration value")
@click.option("--set-credentials", is_flag=True, help="Set storage credentials interactively")
def setup(set_value, set_credentials):
    """Set up or modify configuration."""
    from ..config import get_config, init_config, save_config, set_storage_credentials

    if set_value:
        key, value = set_value
        cfg = get_config()
        keys = key.split(".")
        current = cfg
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        current[keys[-1]] = value
        save_config(cfg)
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
        init_config()
