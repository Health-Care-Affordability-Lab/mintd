"""Update command group."""

import json
from pathlib import Path

import click

from .main import main
from .utils import console


@main.group()
def update():
    """Update project components."""
    pass


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
@click.option("--sensitivity", type=click.Choice(["public", "restricted", "confidential"]))
@click.option("--mirror-url", help="Mirror repository URL")
def metadata(path, sensitivity, mirror_url):
    """Update metadata.json to latest schema with new fields."""
    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"

    if not metadata_path.exists():
        console.print("❌ metadata.json not found.", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, 'r') as f:
            metadata_data = json.load(f)
    except Exception as e:
        console.print(f"❌ Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    current_sensitivity = metadata_data.get("storage", {}).get("sensitivity", "restricted")
    current_mirror_url = metadata_data.get("repository", {}).get("mirror", {}).get("url", "")

    if sensitivity is None:
        sensitivity = click.prompt("Storage sensitivity level", default=current_sensitivity,
                                   type=click.Choice(["public", "restricted", "confidential"]))

    if mirror_url is None:
        mirror_url = click.prompt("Mirror repository URL (leave empty for none)", default=current_mirror_url)

    with console.status("Updating metadata.json..."):
        try:
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

            with open(metadata_path, 'w') as f:
                json.dump(metadata_data, f, indent=2)

        except Exception as e:
            console.print(f"❌ Failed to update metadata: {e}", style="red")
            raise click.Abort()

    console.print("✅ Updated metadata.json with new schema fields")


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def storage(path, yes):
    """Update DVC storage configuration."""
    from ..config import get_config
    from ..initializers.storage import SENSITIVITY_TO_ACL, is_dvc_repo
    from ..shell import dvc_command

    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"

    if not metadata_path.exists():
        console.print("❌ metadata.json not found.", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
    except Exception as e:
        console.print(f"❌ Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    if not is_dvc_repo(project_path):
        console.print("❌ DVC is not initialized in this project.", style="red")
        raise click.Abort()

    project_name = metadata["project"]["name"]
    full_name = metadata["project"]["full_name"]
    sensitivity = metadata.get("storage", {}).get("sensitivity", "restricted")

    cfg = get_config()
    bucket_prefix = cfg.get("storage", {}).get("bucket_prefix")
    if not bucket_prefix:
        console.print("❌ Storage bucket_prefix not configured.", style="red")
        raise click.Abort()

    acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")
    remote_name = project_name
    remote_url = f"s3://{bucket_prefix}/{acl_path}/{project_name}/"

    console.print("📋 Current configuration will be updated:")
    console.print(f"   - Project: {full_name}")
    console.print(f"   - Remote name: {remote_name}")
    console.print(f"   - Sensitivity: {sensitivity} → ACL: {acl_path}")
    console.print(f"   - New remote URL: {remote_url}")

    if not yes:
        if not click.confirm("⚠️  Continue with DVC remote reconfiguration?"):
            raise click.Abort()

    with console.status("Updating DVC storage configuration..."):
        try:
            dvc = dvc_command(cwd=project_path)
            storage_cfg = cfg["storage"]

            try:
                dvc.run("remote", "modify", "--global", remote_name, "url", remote_url)
            except Exception:
                dvc.run("remote", "add", "--global", "-d", remote_name, remote_url)

            if storage_cfg.get("endpoint"):
                dvc.run("remote", "modify", "--global", remote_name, "endpointurl", storage_cfg["endpoint"])

            if storage_cfg.get("region"):
                dvc.run("remote", "modify", "--global", remote_name, "region", storage_cfg["region"])

            if storage_cfg.get("versioning", True):
                dvc.run("remote", "modify", "--global", remote_name, "version_aware", "true")

        except Exception as e:
            console.print(f"❌ Failed to update storage configuration: {e}", style="red")
            raise click.Abort()

    console.print("✅ Updated DVC storage configuration")


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def utils(path):
    """Update mintd utility scripts to the latest version."""
    project_path = Path(path) if path else Path.cwd()

    with console.status("Updating utility scripts..."):
        try:
            metadata_path = project_path / "metadata.json"
            if not metadata_path.exists():
                console.print("❌ metadata.json not found.", style="red")
                raise click.Abort()

            with open(metadata_path, 'r') as f:
                metadata = json.load(f)

            project_name = metadata["project"]["name"]
            project_type = metadata["project"]["type"]
            language = metadata.get("language", "python")

            from ..templates.base import BaseTemplate
            mint_info = BaseTemplate._get_mint_info()

            metadata["mint"] = {"version": mint_info["mint_version"], "commit_hash": mint_info["mint_hash"]}

            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)

            console.print(f"✅ Updated mintd version in metadata.json to {mint_info['mint_version']}")

            from ..templates import DataTemplate, EnclaveTemplate, InfraTemplate, ProjectTemplate

            template_map = {
                "data": DataTemplate, "project": ProjectTemplate, "prj": ProjectTemplate,
                "infra": InfraTemplate, "enclave": EnclaveTemplate
            }
            template_class = template_map.get(project_type)
            if not template_class:
                console.print(f"❌ Unknown project type: {project_type}", style="red")
                raise click.Abort()

            template = template_class()
            template.language = language

            utils_files = [(rp, tn) for rp, tn in template.get_template_files() if "_mintd_utils" in rp]

            if not utils_files:
                console.print(f"⚠️ No utility files found for {language} projects", style="yellow")
                return

            context = {
                "author": metadata["project"].get("created_by", ""),
                "organization": "",
                "storage_provider": metadata["storage"].get("provider", "s3"),
                "storage_endpoint": metadata["storage"].get("endpoint", ""),
                "storage_versioning": metadata["storage"].get("versioning", True),
                "bucket_name": metadata["storage"].get("bucket", ""),
                "project_type": project_type, "language": language, "use_current_repo": False,
            }
            context.update(mint_info)

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
