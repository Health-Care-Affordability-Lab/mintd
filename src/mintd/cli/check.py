"""Check command - validate consistency between metadata.json and .dvc/config."""

import configparser
import json
from pathlib import Path

import click

from .main import main
from .utils import console


def _parse_dvc_config(dvc_config_path: Path) -> dict:
    """Parse .dvc/config to extract default remote name and URL.

    Returns:
        Dict with 'remote_name' and 'remote_url' (empty strings if not found)
    """
    result = {"remote_name": "", "remote_url": ""}

    if not dvc_config_path.exists():
        return result

    parser = configparser.ConfigParser()
    parser.read(dvc_config_path)

    # Extract default remote name from [core] section
    result["remote_name"] = parser.get("core", "remote", fallback="")

    # Extract remote URL from ['remote "NAME"'] section
    remote_name = result["remote_name"]
    if remote_name:
        section = f'remote "{remote_name}"'
        result["remote_url"] = parser.get(section, "url", fallback="")

    return result


@main.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
def check(path):
    """Validate consistency between metadata.json and DVC configuration."""
    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"

    if not metadata_path.exists():
        console.print("metadata.json not found.", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
    except Exception as e:
        console.print(f"Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    # Extract metadata DVC info
    dvc_meta = metadata.get("storage", {}).get("dvc", {})
    meta_remote_name = dvc_meta.get("remote_name", "")
    meta_remote_url = dvc_meta.get("remote_url", "")

    issues = []

    # Check if DVC is initialized
    dvc_config_path = project_path / ".dvc" / "config"
    if not (project_path / ".dvc").is_dir():
        console.print("DVC is not initialized in this project.", style="yellow")
        console.print("Run 'dvc init' to initialize DVC.")
        return

    # Parse .dvc/config
    dvc_config = _parse_dvc_config(dvc_config_path)

    # Compare remote names
    if meta_remote_name and dvc_config["remote_name"]:
        if meta_remote_name != dvc_config["remote_name"]:
            issues.append(
                f"Remote name mismatch: metadata.json has '{meta_remote_name}', "
                f".dvc/config has '{dvc_config['remote_name']}'"
            )

    # Compare remote URLs
    if meta_remote_url and dvc_config["remote_url"]:
        if meta_remote_url != dvc_config["remote_url"]:
            issues.append(
                f"Remote URL mismatch: metadata.json has '{meta_remote_url}', "
                f".dvc/config has '{dvc_config['remote_url']}'"
            )

    # Report results
    if issues:
        console.print(f"Found {len(issues)} issue(s):", style="yellow")
        for issue in issues:
            console.print(f"  - {issue}", style="yellow")
        console.print(
            "\nRun 'mintd update storage -y' to sync configuration.",
            style="dim",
        )
    else:
        console.print("All checks passed. Configuration is consistent.", style="green")
