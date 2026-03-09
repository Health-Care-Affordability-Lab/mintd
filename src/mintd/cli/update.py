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
                f.write("\n")

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

    try:
        project_name = metadata["project"]["name"]
        full_name = metadata["project"]["full_name"]
    except KeyError as e:
        console.print(f"❌ metadata.json missing required field: {e}", style="red")
        console.print("   Ensure metadata.json has project.name and project.full_name")
        raise click.Abort()
    sensitivity = metadata.get("storage", {}).get("sensitivity", "restricted")

    cfg = get_config()
    bucket_prefix = cfg.get("storage", {}).get("bucket_prefix")
    if not bucket_prefix:
        console.print("❌ Storage bucket_prefix not configured.", style="red")
        raise click.Abort()

    acl_path = SENSITIVITY_TO_ACL.get(sensitivity, "lab")
    remote_name = full_name
    remote_url = f"s3://{bucket_prefix}/{acl_path}/{full_name}/"

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

            # Update LOCAL config (committed to git, shareable with collaborators)
            try:
                dvc.run("remote", "modify", remote_name, "url", remote_url)
            except Exception:
                dvc.run("remote", "add", "-d", "-f", remote_name, remote_url)

            if storage_cfg.get("endpoint"):
                dvc.run("remote", "modify", remote_name, "endpointurl", storage_cfg["endpoint"])

            if storage_cfg.get("region"):
                dvc.run("remote", "modify", remote_name, "region", storage_cfg["region"])

            if storage_cfg.get("versioning", True):
                dvc.run("remote", "modify", remote_name, "version_aware", "true")

            # Also update GLOBAL config for cross-project convenience (opt-in)
            if storage_cfg.get("sync_global", True):
                try:
                    dvc.run("remote", "modify", "--global", remote_name, "url", remote_url)
                except Exception:
                    dvc.run("remote", "add", "--global", "-d", "-f", remote_name, remote_url)

                if storage_cfg.get("endpoint"):
                    dvc.run("remote", "modify", "--global", remote_name, "endpointurl", storage_cfg["endpoint"])

                if storage_cfg.get("region"):
                    dvc.run("remote", "modify", "--global", remote_name, "region", storage_cfg["region"])

                if storage_cfg.get("versioning", True):
                    dvc.run("remote", "modify", "--global", remote_name, "version_aware", "true")

        except Exception as e:
            console.print(f"❌ Failed to update storage configuration: {e}", style="red")
            raise click.Abort()

    # Sync metadata.json with updated DVC remote info
    try:
        with open(metadata_path, 'r') as f:
            meta = json.load(f)
        if "storage" not in meta:
            meta["storage"] = {}
        if "dvc" not in meta["storage"]:
            meta["storage"]["dvc"] = {}
        meta["storage"]["dvc"]["remote_name"] = remote_name
        meta["storage"]["dvc"]["remote_url"] = remote_url
        with open(metadata_path, 'w') as f:
            json.dump(meta, f, indent=2)
            f.write("\n")
    except Exception as e:
        console.print(f"⚠️  Could not sync metadata.json: {e}", style="yellow")

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

            try:
                project_name = metadata["project"]["name"]
                project_type = metadata["project"]["type"]
            except KeyError as e:
                console.print(f"❌ metadata.json missing required field: {e}", style="red")
                console.print("   Ensure metadata.json has project.name and project.type")
                raise click.Abort()
            language = metadata.get("language", "python")

            from ..templates.base import BaseTemplate
            mint_info = BaseTemplate._get_mint_info()

            metadata["mint"] = {"version": mint_info["mint_version"], "commit_hash": mint_info["mint_hash"]}

            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
                f.write("\n")

            console.print(f"✅ Updated mintd version in metadata.json to {mint_info['mint_version']}")

            from ..templates import DataTemplate, EnclaveTemplate, ProjectTemplate

            template_map = {
                "data": DataTemplate, "project": ProjectTemplate, "prj": ProjectTemplate,
                "enclave": EnclaveTemplate
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


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
@click.option("--generate", "-g", is_flag=True,
              help="Auto-generate schema from data files")
@click.option("--force", "-f", is_flag=True,
              help="Overwrite existing schema.json")
def schema(path, generate, force):
    """Add Frictionless Table Schema support to existing projects."""
    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"
    schemas_dir = project_path / "schemas" / "v1"
    schema_file = schemas_dir / "schema.json"

    if not metadata_path.exists():
        console.print("❌ metadata.json not found.", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, 'r') as f:
            metadata_data = json.load(f)
    except Exception as e:
        console.print(f"❌ Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    with console.status("Updating schema configuration..."):
        # Add schema section to metadata.json if missing
        if "schema" not in metadata_data:
            metadata_data["schema"] = {
                "version": "1.0.0",
                "standard": "frictionless-table-schema",
                "location": "schemas/v1/schema.json"
            }
            with open(metadata_path, 'w') as f:
                json.dump(metadata_data, f, indent=2)
                f.write("\n")
            console.print("✅ Added schema section to metadata.json")
        else:
            console.print("ℹ️  Schema section already exists in metadata.json")

        # Create schemas/v1 directory
        schemas_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"✅ Created {schemas_dir.relative_to(project_path)}/")

        # Generate or copy starter schema
        if generate:
            data_dir = project_path / "data"
            if not data_dir.exists():
                console.print("⚠️  No data/ directory found, skipping auto-generation", style="yellow")
            else:
                if schema_file.exists() and not force:
                    console.print("⚠️  schema.json exists, use --force to overwrite", style="yellow")
                else:
                    try:
                        from ..utils.schema import generate_schema_file
                        generate_schema_file(data_dir, schema_file)
                        console.print(f"✅ Generated schema from data files: {schema_file.relative_to(project_path)}")
                    except Exception as e:
                        console.print(f"⚠️  Could not auto-generate schema: {e}", style="yellow")
                        console.print("   Creating starter template instead...")
                        _write_starter_schema(schema_file, force)
        elif not schema_file.exists() or force:
            _write_starter_schema(schema_file, force)
        else:
            console.print(f"ℹ️  {schema_file.relative_to(project_path)} already exists")

    console.print("✅ Schema configuration complete")


@update.command()
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path),
              help="Path to project directory")
@click.option("--force", "-f", is_flag=True,
              help="Overwrite existing pre-commit config")
def hooks(path, force):
    """Add or update pre-commit hooks for DVC sync checking."""
    project_path = Path(path) if path else Path.cwd()
    metadata_path = project_path / "metadata.json"
    precommit_config = project_path / ".pre-commit-config.yaml"
    scripts_dir = project_path / "scripts"
    dvc_script_path = scripts_dir / "check-dvc-sync.sh"
    env_script_path = scripts_dir / "check-env-lockfiles.sh"

    if not metadata_path.exists():
        console.print("Error: metadata.json not found.", style="red")
        raise click.Abort()

    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
    except Exception as e:
        console.print(f"Error: Failed to read metadata.json: {e}", style="red")
        raise click.Abort()

    # Check if files already exist
    if precommit_config.exists() and not force:
        console.print("Pre-commit config already exists. Use --force to overwrite.", style="yellow")
        raise click.Abort()

    with console.status("Adding pre-commit hooks..."):
        try:
            # Get project info from metadata
            language = metadata.get("language", "python")
            project_type = metadata.get("project", {}).get("type", "data")
            project_name = metadata.get("project", {}).get("name", "project")

            # Determine source directory based on language
            source_dir = "code" if language == "stata" else "src"

            from ..templates.base import BaseTemplate
            mint_info = BaseTemplate._get_mint_info()

            # Create scripts directory
            scripts_dir.mkdir(parents=True, exist_ok=True)

            # Get Jinja environment from base template
            from ..templates import DataTemplate
            template = DataTemplate()

            # Render pre-commit config
            jinja_template = template.jinja_env.get_template("pre-commit-config.yaml.j2")
            content = jinja_template.render(
                project_name=project_name,
                language=language,
                source_dir=source_dir,
                **mint_info
            )
            with open(precommit_config, "w", encoding="utf-8") as f:
                f.write(content)
            console.print(f"Created: .pre-commit-config.yaml")

            # Render check-dvc-sync.sh
            jinja_template = template.jinja_env.get_template("check-dvc-sync.sh.j2")
            content = jinja_template.render(
                project_name=project_name,
                language=language,
                source_dir=source_dir,
                **mint_info
            )
            with open(dvc_script_path, "w", encoding="utf-8") as f:
                f.write(content)

            # Render check-env-lockfiles.sh
            jinja_template = template.jinja_env.get_template("check-env-lockfiles.sh.j2")
            content = jinja_template.render(
                project_name=project_name,
                language=language,
                source_dir=source_dir,
                **mint_info
            )
            with open(env_script_path, "w", encoding="utf-8") as f:
                f.write(content)

            # Make scripts executable
            import os
            os.chmod(dvc_script_path, 0o755)
            os.chmod(env_script_path, 0o755)
            console.print(f"Created: scripts/check-dvc-sync.sh")
            console.print(f"Created: scripts/check-env-lockfiles.sh")

            # Try to install pre-commit hooks
            import subprocess
            try:
                result = subprocess.run(
                    ["pre-commit", "--version"],
                    capture_output=True,
                    text=True,
                    cwd=project_path
                )
                if result.returncode == 0:
                    result = subprocess.run(
                        ["pre-commit", "install"],
                        capture_output=True,
                        text=True,
                        cwd=project_path
                    )
                    if result.returncode == 0:
                        console.print("Installed pre-commit hooks")
                    else:
                        console.print(f"Warning: Failed to install hooks: {result.stderr}", style="yellow")
                else:
                    console.print("Note: Install pre-commit with: pip install pre-commit", style="dim")
                    console.print("      Then run: pre-commit install", style="dim")
            except FileNotFoundError:
                console.print("Note: Install pre-commit with: pip install pre-commit", style="dim")
                console.print("      Then run: pre-commit install", style="dim")

        except Exception as e:
            console.print(f"Error adding hooks: {e}", style="red")
            raise click.Abort()

    console.print("Pre-commit hooks configured successfully", style="green")


def _write_starter_schema(schema_file: Path, force: bool):
    """Write the starter Frictionless Table Schema template."""
    if schema_file.exists() and not force:
        return

    starter_schema = {
        "$schema": "https://specs.frictionlessdata.io/schemas/table-schema.json",
        "fields": [
            {
                "name": "id",
                "type": "integer",
                "title": "Record ID",
                "description": "Unique identifier for each record",
                "constraints": {"required": True}
            }
        ],
        "missingValues": ["", "NA", "."],
        "_comment": "This is a starter Frictionless Table Schema. Run 'python generate_schema.py' from the schemas/ directory to auto-generate from your data files."
    }

    with open(schema_file, 'w') as f:
        json.dump(starter_schema, f, indent=2)
        f.write("\n")

    console.print(f"✅ Created starter schema: {schema_file.name}")
