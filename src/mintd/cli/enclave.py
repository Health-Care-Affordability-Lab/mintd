"""Enclave command group for managing secure data transfers."""

from pathlib import Path

import click
import yaml

from .main import main
from .utils import console


@main.group()
def enclave():
    """Manage enclave data transfers and workspace."""
    pass


@enclave.command()
@click.argument("repo_name")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
@click.option("--no-pull", is_flag=True, help="Add to approved list without pulling data")
def add(repo_name, path, no_pull):
    """Add a data product to the enclave's approved list."""
    enclave_path = Path(path) if path else Path.cwd()
    manifest_path = enclave_path / "enclave_manifest.yaml"

    if not manifest_path.exists():
        console.print(f"❌ Enclave manifest not found: {manifest_path}", style="red")
        raise click.Abort()

    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    approved = manifest.setdefault('approved_products', [])
    existing = any(item['repo'] == repo_name for item in approved)

    if existing:
        console.print(f"⚠ Repository '{repo_name}' is already approved.")
        if no_pull:
            return

    if not existing:
        approved.append({
            'repo': repo_name,
            'registry_entry': f"catalog/data/{repo_name}.yaml",
            'stage': 'final'
        })
        with open(manifest_path, 'w') as f:
            yaml.dump(manifest, f, default_flow_style=False, sort_keys=False)
        console.print(f"✅ Added '{repo_name}' to approved products.")

    if not no_pull:
        from ..enclave_commands import pull_enclave_data
        try:
            pull_enclave_data(enclave_path, repo_name=repo_name)
            console.print("✅ Data pull completed successfully.")
        except Exception as e:
            console.print(f"❌ Data pull failed: {e}", style="red")


@enclave.command(name="pull")
@click.argument("repo_name", required=False)
@click.option("--all", "-a", "pull_all", is_flag=True, help="Pull all approved products")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def enclave_pull(repo_name, pull_all, path):
    """Pull data products from registry."""
    from ..enclave_commands import pull_enclave_data

    enclave_path = Path(path) if path else Path.cwd()
    try:
        pull_enclave_data(enclave_path, repo_name=repo_name, pull_all=pull_all)
    except Exception as e:
        console.print(f"❌ Pull failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.option("--name", "-n", help="Transfer package name")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
@click.option("--force", is_flag=True, help="Force packaging")
def package(name, path, force):
    """Package downloaded data for transfer to enclave."""
    from ..enclave_commands import package_transfer

    enclave_path = Path(path) if path else Path.cwd()
    try:
        package_transfer(enclave_path, name=name, force=force)
    except Exception as e:
        console.print(f"❌ Packaging failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.argument("transfer_file", type=click.Path(exists=True, path_type=Path))
@click.option("--dest", "-d", type=click.Path(path_type=Path))
def unpack(transfer_file, dest):
    """Unpack a transfer archive."""
    from ..enclave_commands import unpack_transfer
    try:
        unpack_transfer(transfer_file, dest_dir=dest)
    except Exception as e:
        console.print(f"❌ Unpack failed: {e}", style="red")
        raise click.Abort()


@enclave.command()
@click.argument("transfer_path", type=click.Path(exists=True, path_type=Path))
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def verify(transfer_path, path):
    """Verify a transfer and update enclave manifest."""
    from ..enclave_commands import verify_transfer

    enclave_path = Path(path) if path else Path.cwd()
    try:
        success = verify_transfer(transfer_path, enclave_path=enclave_path)
        if not success:
            raise click.Abort()
    except Exception as e:
        console.print(f"❌ Verification failed: {e}", style="red")
        raise click.Abort()


@enclave.command(name="list")
@click.argument("repo_name", required=False)
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def enclave_list(repo_name, path):
    """List approved and transferred data products."""
    enclave_path = Path(path) if path else Path.cwd()
    manifest_path = enclave_path / "enclave_manifest.yaml"

    if not manifest_path.exists():
        console.print(f"❌ Enclave manifest not found: {manifest_path}", style="red")
        raise click.Abort()

    with open(manifest_path, 'r') as f:
        manifest = yaml.safe_load(f)

    approved = manifest.get('approved_products', [])
    transferred = manifest.get('transferred', [])

    if repo_name:
        approved = [item for item in approved if item['repo'] == repo_name]
        transferred = [item for item in transferred if item['repo'] == repo_name]

    console.print(f"Enclave Status{' for ' + repo_name if repo_name else ''}:")
    console.print("-" * 40)
    console.print(f"Approved Products: {len(approved)}")

    for item in approved:
        repo = item['repo']
        transferred_count = len([t for t in transferred if t['repo'] == repo])
        console.print(f"  • {repo} ({transferred_count} versions transferred)")

    if transferred:
        console.print(f"\nTransferred Data: {len(transferred)}")
        for item in transferred:
            version = item['dvc_hash'][:7]
            date = item.get('transfer_date', 'unknown')
            console.print(f"  • {item['repo']}: {version} ({date})")


@enclave.command()
@click.option("--keep", "-k", default=1, type=int, help="Versions to keep")
@click.option("--staging-only", is_flag=True, help="Only clean staging")
@click.option("--path", "-p", type=click.Path(exists=True, path_type=Path))
def clean(keep, staging_only, path):
    """Prune old data versions and clean staging area."""
    from ..enclave_commands import clean_enclave

    enclave_path = Path(path) if path else Path.cwd()
    try:
        clean_enclave(enclave_path, keep_recent=keep, staging_only=staging_only)
    except Exception as e:
        console.print(f"❌ Cleanup failed: {e}", style="red")
        raise click.Abort()
