"""Data import functionality for mintd - pull and import DVC-tracked data.

Handles pulling data from registered data products and importing DVC dependencies
into project/infra repositories with robust error handling and rollback support.
"""

import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, asdict

import git
from rich.console import Console

from .registry import get_registry_client, load_project_metadata

console = Console()


@dataclass
class ImportResult:
    """Result of a single import operation."""
    product_name: str
    success: bool
    error_message: Optional[str] = None
    dvc_file: Optional[str] = None
    local_path: Optional[str] = None
    source_commit: Optional[str] = None


class ImportTransaction:
    """Track import operations for rollback on failure.

    Provides transaction-like semantics for multi-import operations,
    allowing partial failure recovery and cleanup.
    """

    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.completed: List[Dict[str, Any]] = []
        self.failed: List[Dict[str, Any]] = []
        self.rollback_actions: List[Callable[[], None]] = []
        self.state_file = project_path / ".mintd" / "import_state.json"

    def save_state(self) -> None:
        """Save current transaction state for recovery."""
        state_dir = self.state_file.parent
        state_dir.mkdir(exist_ok=True)

        state = {
            "started_at": datetime.now().isoformat(),
            "completed": self.completed,
            "failed": self.failed,
            "rollback_actions_count": len(self.rollback_actions)
        }

        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def load_state(self) -> bool:
        """Load previous transaction state. Returns True if state was loaded."""
        if not self.state_file.exists():
            return False

        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)

            self.completed = state.get("completed", [])
            self.failed = state.get("failed", [])
            return True
        except (json.JSONDecodeError, IOError):
            return False

    def cleanup_state(self) -> None:
        """Remove state file after successful completion."""
        if self.state_file.exists():
            self.state_file.unlink()

    def add_success(self, result: ImportResult) -> None:
        """Record a successful import."""
        self.completed.append(asdict(result))

    def add_failure(self, result: ImportResult) -> None:
        """Record a failed import."""
        self.failed.append(asdict(result))

    def add_rollback_action(self, action: Callable[[], None]) -> None:
        """Add a cleanup action to be executed on rollback."""
        self.rollback_actions.append(action)

    def rollback(self) -> None:
        """Execute all rollback actions in reverse order."""
        console.print("🔄 Rolling back import operations...", style="yellow")

        for action in reversed(self.rollback_actions):
            try:
                action()
            except Exception as e:
                console.print(f"⚠️  Rollback action failed: {e}", style="yellow")

        console.print("✅ Rollback completed", style="green")

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of transaction results."""
        return {
            "total": len(self.completed) + len(self.failed),
            "successful": len(self.completed),
            "failed": len(self.failed),
            "completed": self.completed,
            "failed": self.failed
        }


class DataImportError(Exception):
    """Base exception for data import operations."""
    pass


class RegistryError(DataImportError):
    """Error accessing the registry."""
    pass


class DVCImportError(DataImportError):
    """Error during DVC import operation."""
    pass


class MetadataUpdateError(DataImportError):
    """Error updating project metadata."""
    pass


def query_data_product(product_name: str) -> Dict[str, Any]:
    """Query registry for data product information.

    Args:
        product_name: Name of the data product (e.g., "data_cms-provider-data-service")

    Returns:
        Dictionary with product information including repository URL, storage config, etc.

    Raises:
        RegistryError: If product not found or registry inaccessible
    """
    try:
        registry_client = get_registry_client()
        return registry_client.query_data_product(product_name)
    except Exception as e:
        raise RegistryError(f"Failed to query registry for '{product_name}': {e}")


def validate_project_directory(project_path: Path) -> None:
    """Validate that the current directory is a valid mint project.

    Args:
        project_path: Path to check

    Raises:
        DataImportError: If not a valid project directory
    """
    metadata_file = project_path / "metadata.json"

    if not metadata_file.exists():
        raise DataImportError(
            f"Not a mintd project directory. Missing metadata.json in {project_path}"
        )

    try:
        metadata = load_project_metadata(project_path)
        project_type = metadata["project"]["type"]

        if project_type not in ["project", "infra"]:
            raise DataImportError(
                f"Data import only supported for project/infra repositories, "
                f"not '{project_type}' repositories"
            )

    except Exception as e:
        raise DataImportError(f"Invalid project metadata: {e}")


def run_dvc_import(
    project_path: Path,
    repo_url: str,
    source_path: str,
    dest_path: str,
    repo_rev: Optional[str] = None
) -> str:
    """Run dvc import command with error handling.

    Args:
        project_path: Project directory
        repo_url: Source repository URL
        source_path: Path in source repo to import
        dest_path: Local destination path
        repo_rev: Specific revision to import from

    Returns:
        Path to created .dvc file

    Raises:
        DVCImportError: If import fails
    """
    # Convert HTTPS URL to SSH for authentication
    if repo_url.startswith('https://github.com/'):
        ssh_url = repo_url.replace('https://github.com/', 'git@github.com:')
    else:
        ssh_url = repo_url

    cmd = ["dvc", "import", ssh_url, source_path, dest_path]

    if repo_rev:
        cmd.extend(["--rev", repo_rev])

    try:
        subprocess.run(
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True
        )

        # Extract .dvc file path from output or construct it
        # DVC typically creates file.dvc for destination file
        if dest_path.endswith('/'):
            # Directory import - dvc creates .dir file
            dvc_file = f"{dest_path.rstrip('/')}.dvc"
        else:
            # File import
            dvc_file = f"{dest_path}.dvc"

        return dvc_file

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        raise DVCImportError(f"DVC import failed: {error_msg}")


def update_project_metadata(
    project_path: Path,
    import_result: ImportResult,
    product_info: Dict[str, Any]
) -> None:
    """Update project metadata.json with data dependency information.

    Args:
        project_path: Project directory
        import_result: Result of the import operation
        product_info: Information about the imported product

    Raises:
        MetadataUpdateError: If metadata update fails
    """
    metadata_file = project_path / "metadata.json"

    try:
        # Read existing metadata
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)

        # Ensure data_dependencies section exists
        if "data_dependencies" not in metadata.get("metadata", {}):
            if "metadata" not in metadata:
                metadata["metadata"] = {}
            metadata["metadata"]["data_dependencies"] = []

        # Create dependency entry
        dependency = {
            "source": import_result.product_name,
            "source_url": product_info.get("repository", {}).get("github_url", ""),
            "stage": product_info.get("stage", ""),
            "path": product_info.get("path", ""),
            "local_path": import_result.local_path,
            "dvc_file": import_result.dvc_file,
            "imported_at": datetime.now().isoformat(),
            "source_commit": import_result.source_commit
        }

        # Check if this dependency already exists
        dependencies = metadata["metadata"]["data_dependencies"]
        existing_index = None
        for i, dep in enumerate(dependencies):
            if (dep["source"] == import_result.product_name and
                dep["local_path"] == import_result.local_path):
                existing_index = i
                break

        if existing_index is not None:
            # Update existing dependency
            dependencies[existing_index] = dependency
        else:
            # Add new dependency
            dependencies.append(dependency)

        # Write updated metadata
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

    except Exception as e:
        raise MetadataUpdateError(f"Failed to update metadata.json: {e}")


def import_data_product(
    product_name: str,
    project_path: Path,
    stage: Optional[str] = None,
    path: Optional[str] = None,
    dest: Optional[str] = None,
    repo_rev: Optional[str] = None
) -> ImportResult:
    """Import a data product as a DVC dependency.

    Args:
        product_name: Name of the data product to import
        project_path: Path to the project directory
        stage: Pipeline stage to import (e.g., "final", "clean")
        path: Specific path to import from the product
        dest: Local destination path (default: data/imports/{product_name}/)
        repo_rev: Specific revision to import from

    Returns:
        ImportResult with operation details

    Raises:
        DataImportError: For various import failures
    """
    result = ImportResult(product_name=product_name, success=False)

    try:
        # Validate project directory
        validate_project_directory(project_path)

        # Query product information from registry
        console.print(f"🔍 Querying registry for '{product_name}'...")
        product_info = query_data_product(product_name)

        # Determine what to import
        if stage and path:
            raise DataImportError("Cannot specify both --stage and --path")
        elif not stage and not path:
            # Default to final stage
            stage = "final"

        if stage:
            # Import pipeline stage output
            source_path = f"data/{stage}/"
            if not dest:
                dest = f"data/imports/{product_name.replace('data_', '')}/"
        else:
            # Import specific path
            source_path = path
            if not dest:
                # Use same relative path structure
                dest = path

        # Ensure destination directory exists
        dest_path = project_path / dest
        if dest_path.suffix:  # It's a file
            dest_path.parent.mkdir(parents=True, exist_ok=True)
        else:  # It's a directory
            dest_path.mkdir(parents=True, exist_ok=True)

        # Run DVC import
        console.print(f"📥 Importing {source_path} from {product_name}...")
        dvc_file = run_dvc_import(
            project_path=project_path,
            repo_url=product_info["repository"]["github_url"],
            source_path=source_path,
            dest_path=dest,
            repo_rev=repo_rev
        )

        # Get source commit information
        # This would need to be extracted from the DVC file or git operations
        source_commit = repo_rev or "HEAD"

        # Success
        result.success = True
        result.dvc_file = dvc_file
        result.local_path = dest
        result.source_commit = source_commit

        console.print(f"✅ Successfully imported {product_name}", style="green")
        console.print(f"   DVC file: {dvc_file}")
        console.print(f"   Local path: {dest}")

        # Update project metadata
        try:
            update_project_metadata(project_path, result, product_info)
            console.print("📝 Updated project metadata dependencies")
        except MetadataUpdateError as e:
            console.print(f"⚠️  Failed to update metadata: {e}", style="yellow")
            # We don't fail the import for metadata update failure, but we log it

        return result

    except Exception as e:
        result.error_message = str(e)
        console.print(f"❌ Failed to import {product_name}: {e}", style="red")
        return result


def pull_data_product(
    product_name: str,
    destination: Optional[str] = None,
    stage: Optional[str] = None,
    path: Optional[str] = None
) -> bool:
    """Pull/download data from a registered data product.

    This is similar to enclave data pulling but for general use.

    Args:
        product_name: Name of the data product
        destination: Local destination directory
        stage: Pipeline stage to pull
        path: Specific path to pull

    Returns:
        True if successful
    """
    try:
        # Query product information
        console.print(f"🔍 Querying registry for '{product_name}'...")
        product_info = query_data_product(product_name)

        # Determine destination
        if not destination:
            destination = f"./{product_name}_data"

        dest_path = Path(destination)
        dest_path.mkdir(parents=True, exist_ok=True)

        # This would implement the actual data pulling logic
        # For now, just clone and pull the data
        repo_url = product_info["repository"]["github_url"]

        console.print(f"📥 Pulling data from {product_name}...")

        # Convert to SSH URL
        if repo_url.startswith('https://github.com/'):
            ssh_url = repo_url.replace('https://github.com/', 'git@github.com:')

        # Clone repository
        temp_dir = Path(tempfile.mkdtemp())
        try:
            git.Repo.clone_from(ssh_url, temp_dir)

            # Determine what to copy
            if stage:
                source_path = temp_dir / "data" / stage
            elif path:
                source_path = temp_dir / path
            else:
                source_path = temp_dir / "data" / "final"

            if source_path.exists():
                if source_path.is_file():
                    shutil.copy2(source_path, dest_path)
                else:
                    shutil.copytree(source_path, dest_path, dirs_exist_ok=True)

                console.print(f"✅ Data pulled to {dest_path}", style="green")
                return True
            else:
                console.print(f"❌ Source path {source_path} not found", style="red")
                return False

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        console.print(f"❌ Failed to pull data: {e}", style="red")
        return False


def list_data_products(show_imported: bool = False, project_path: Optional[Path] = None) -> None:
    """List available data products or imported dependencies.

    Args:
        show_imported: If True, show imported dependencies instead of available products
        project_path: Project directory (required for --imported)
    """
    if show_imported:
        if not project_path:
            project_path = Path.cwd()

        try:
            metadata = load_project_metadata(project_path)
            dependencies = metadata.get("metadata", {}).get("data_dependencies", [])

            if not dependencies:
                console.print("No data dependencies found in this project.")
                return

            console.print("📋 Imported Data Dependencies:")
            console.print("-" * 50)

            for dep in dependencies:
                console.print(f"• {dep['source']}")
                console.print(f"  Stage: {dep.get('stage', 'N/A')}")
                console.print(f"  Local path: {dep['local_path']}")
                console.print(f"  Imported: {dep['imported_at']}")
                console.print()

        except Exception as e:
            console.print(f"❌ Error reading project metadata: {e}", style="red")

    else:
        # List available data products from registry
        try:
            registry_client = get_registry_client()
            products = registry_client.list_data_products()

            console.print("📋 Available Data Products:")
            console.print("-" * 30)

            for product in products:
                console.print(f"• {product['name']}")
                console.print(f"  Description: {product.get('description', 'N/A')}")
                console.print()

        except Exception as e:
            console.print(f"❌ Error accessing registry: {e}", style="red")
