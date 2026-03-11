"""Data import functionality for mintd - pull, push, and import DVC-tracked data.

Handles pulling data from registered data products, pushing data to the correct
DVC remote, and importing DVC dependencies into project repositories with robust
error handling and rollback support.
"""

import json
import re
import shutil
import tempfile

import yaml
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import click
from rich.console import Console

from .exceptions import DVCImportError, DVCUpdateError, DependencyNotFoundError, DependencyRemovalError
from .registry import get_registry_client, load_project_metadata
from .shell import dvc_command, git_command

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


@dataclass
class UpdateResult:
    """Result of a DVC update operation."""
    dvc_file: str
    success: bool
    error_message: Optional[str] = None
    previous_commit: Optional[str] = None
    new_commit: Optional[str] = None
    skipped: bool = False  # True if already up-to-date


@dataclass
class RemoveResult:
    """Result of a dependency removal operation."""
    product_name: str
    success: bool
    error_message: Optional[str] = None
    removed_path: Optional[str] = None
    removed_dvc_file: Optional[str] = None
    warnings: List[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


@dataclass
class GetResult:
    """Result of a data get operation."""
    product_name: str
    success: bool
    error_message: Optional[str] = None
    dest_path: Optional[str] = None
    source_path: Optional[str] = None
    files_downloaded: int = 0


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
            "failed_count": len(self.failed),
            "completed": self.completed,
            "failed": self.failed
        }


# Re-export exceptions for backwards compatibility
from .exceptions import DataImportError, MetadataUpdateError, RegistryError, DVCUpdateError, DependencyNotFoundError, DependencyRemovalError


def _https_to_ssh(url: str) -> str:
    """Convert a GitHub HTTPS URL to SSH format for authentication."""
    if url.startswith('https://github.com/'):
        return url.replace('https://github.com/', 'git@github.com:')
    return url


def query_data_product(product_name: str) -> Dict[str, Any]:
    """Query registry for data product information.

    Accepts either the full name (e.g., "data_mergerbuild") or the short name
    (e.g., "mergerbuild"). If the exact name is not found and it doesn't already
    have a ``data_`` prefix, a second lookup with the prefix is attempted.

    Args:
        product_name: Name of the data product

    Returns:
        Dictionary with product information including repository URL, storage config, etc.

    Raises:
        RegistryError: If product not found or registry inaccessible
    """
    try:
        registry_client = get_registry_client()

        # Determine the alternate name (with or without data_ prefix)
        if product_name.startswith("data_"):
            alt_name = product_name.removeprefix("data_")
        else:
            alt_name = f"data_{product_name}"

        # Try exact name first, fall back to alternate
        try:
            return registry_client.query_data_product(product_name)
        except FileNotFoundError:
            pass

        try:
            return registry_client.query_data_product(alt_name)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Data product '{product_name}' not found in registry"
            )
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

        if project_type not in ("project", "data"):
            raise DataImportError(
                f"Data import only supported for project and data repositories, "
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
    ssh_url = _https_to_ssh(repo_url)

    dvc = dvc_command(cwd=project_path)
    args = ["import", ssh_url, source_path, "-o", dest_path]

    if repo_rev:
        args.extend(["--rev", repo_rev])

    try:
        dvc.run_live(*args)

        # Extract .dvc file path from output or construct it
        # DVC typically creates file.dvc for destination file
        if dest_path.endswith('/'):
            # Directory import - dvc creates .dir file
            dvc_file = f"{dest_path.rstrip('/')}.dvc"
        else:
            # File import
            dvc_file = f"{dest_path}.dvc"

        return dvc_file

    except Exception as e:
        error_msg = str(e)
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


def _parse_dvc_yaml_data_paths(repo_url: str, rev: Optional[str] = None) -> List[str]:
    """Extract data/ output paths from a remote repo's dvc.yaml.

    DVC pipeline outputs (like data/final/) are not git-tracked directories —
    they only appear as ``outs`` in ``dvc.yaml``.  This function reads the
    pipeline definition and resolves output paths relative to each stage's
    ``wdir`` so that paths like ``../data/final/`` (with ``wdir: code``) are
    normalised to ``data/final``.

    Returns:
        Sorted, deduplicated list of ``data/*`` paths found in pipeline outputs.
    """
    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="mintd-dvc-yaml-"))
        clone_args = [
            "clone", "--depth", "1", "--no-checkout",
            repo_url, str(temp_dir / "repo"),
        ]
        if rev:
            clone_args.extend(["--branch", rev])
        git_cmd = git_command()
        git_cmd.run(*clone_args)

        cloned_git = git_command(cwd=temp_dir / "repo")
        ref = rev or "HEAD"
        try:
            result = cloned_git.run("show", f"{ref}:dvc.yaml")
        except Exception:
            return []

        pipeline = yaml.safe_load(result.stdout) or {}
        data_paths: set[str] = set()

        for stage_info in (pipeline.get("stages") or {}).values():
            wdir = stage_info.get("wdir", ".")
            for out in stage_info.get("outs", []):
                out_path = out if isinstance(out, str) else list(out.keys())[0]
                # Resolve relative to wdir (e.g. wdir=code, out=../data/final/)
                resolved = (Path(wdir) / out_path).as_posix()
                # Normalise (collapses ../)
                parts = resolved.rstrip("/").split("/")
                normalized: list[str] = []
                for p in parts:
                    if p == "..":
                        if normalized:
                            normalized.pop()
                    elif p != ".":
                        normalized.append(p)
                norm = "/".join(normalized)
                if norm.startswith("data/") and norm != "data":
                    # Keep only the first level under data/
                    top_level = "data/" + norm.split("/")[1]
                    data_paths.add(top_level)

        return sorted(data_paths)
    except Exception:
        return []
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def list_remote_data_paths(
    repo_url: str,
    rev: Optional[str] = None,
) -> List[str]:
    """List available data directories in a remote git repository.

    Discovers directories both from the git tree (e.g. data/raw/) and from
    DVC pipeline outputs in dvc.yaml (e.g. data/final/).

    Args:
        repo_url: SSH or HTTPS URL of the source repository
        rev: Git revision to inspect (default: HEAD)

    Returns:
        List of directory paths under data/ (e.g., ["data/raw", "data/final"])
    """
    temp_dir = None
    git_paths: List[str] = []
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="mintd-ls-"))
        clone_args = ["clone", "--depth", "1", "--no-checkout", repo_url, str(temp_dir / "repo")]
        if rev:
            clone_args.extend(["--branch", rev])
        git_cmd = git_command()
        git_cmd.run(*clone_args)

        cloned_git = git_command(cwd=temp_dir / "repo")
        ref = rev or "HEAD"
        result = cloned_git.run("ls-tree", "--name-only", "-d", f"{ref}:data")
        git_paths = [f"data/{line}" for line in result.stdout.strip().splitlines() if line]
    except Exception:
        pass
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # Also discover DVC pipeline output paths
    dvc_paths = _parse_dvc_yaml_data_paths(repo_url, rev=rev)

    # Merge and deduplicate
    all_paths = sorted(set(git_paths + dvc_paths))
    return all_paths


def list_remote_dvc_files(
    repo_url: str,
    source_path: str,
    rev: Optional[str] = None,
) -> List[str]:
    """Discover .dvc files under a path in a remote git repository.

    Performs a shallow clone and uses ``git ls-tree`` to list files.
    For each ``.dvc`` file found, returns the corresponding data path
    (i.e. the filename with the ``.dvc`` suffix stripped).

    Args:
        repo_url: SSH or HTTPS URL of the source repository
        source_path: Directory path to inspect (e.g. "deriveddata/hosppanel")
        rev: Git revision to inspect (default: HEAD)

    Returns:
        List of data paths tracked by .dvc files under *source_path*.
        Returns empty list on any error.
    """
    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp(prefix="mintd-ls-dvc-"))
        clone_args = ["clone", "--depth", "1", "--no-checkout", repo_url, str(temp_dir / "repo")]
        if rev:
            clone_args.extend(["--branch", rev])
        git_cmd = git_command()
        git_cmd.run(*clone_args)

        cloned_git = git_command(cwd=temp_dir / "repo")
        ref = rev or "HEAD"
        normalized = source_path.rstrip("/")
        result = cloned_git.run("ls-tree", "--name-only", "-r", f"{ref}:{normalized}")

        data_paths = []
        for line in result.stdout.strip().splitlines():
            if line.endswith(".dvc"):
                # Strip .dvc suffix to get the actual data path
                data_file = line[: -len(".dvc")]
                data_paths.append(f"{normalized}/{data_file}")
        return data_paths
    except Exception:
        return []
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def validate_source_path(
    repo_url: str,
    source_path: str,
    rev: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """Validate that a source path exists in the remote repository.

    Args:
        repo_url: SSH or HTTPS URL of the source repository
        source_path: Path to validate (e.g., "data/final/")
        rev: Git revision to check

    Returns:
        Tuple of (exists: bool, available_paths: List[str])
    """
    available = list_remote_data_paths(repo_url, rev=rev)
    normalized = source_path.rstrip("/")
    exists = normalized in available
    return exists, available


def prompt_stage_selection(available_paths: List[str]) -> str:
    """Prompt user to select from available data directories.

    Args:
        available_paths: List of available data paths

    Returns:
        Selected path

    Raises:
        DataImportError: If no paths available
    """
    if not available_paths:
        raise DataImportError(
            "No data directories found in the source repository"
        )

    if len(available_paths) == 1:
        console.print(f"Only one data directory available: {available_paths[0]}")
        return available_paths[0]

    console.print("\nThe default 'data/final/' was not found. Available directories:")
    for i, path in enumerate(available_paths, 1):
        console.print(f"  {i}. {path}")

    choice = click.prompt(
        "Select a directory to import",
        type=click.IntRange(1, len(available_paths)),
    )
    return available_paths[choice - 1]


def _warn_outdated_gitignore(project_path: Path) -> None:
    """Warn if the project .gitignore uses the old ``data/`` rule.

    The old pattern (``data/``) ignores the entire directory so git refuses to
    create ``.dvc`` tracking files inside it.  The fix is to replace it with
    ``/data/**`` plus negation rules for directories and ``.dvc`` files.
    """
    gitignore = project_path / ".gitignore"
    if not gitignore.exists():
        return

    text = gitignore.read_text()
    # Match a bare "data/" rule that is NOT part of the "/data/**" pattern
    has_old = any(
        re.fullmatch(r"data/", line.strip())
        for line in text.splitlines()
        if not line.strip().startswith("#")
    )
    has_new = "/data/**" in text

    if has_old and not has_new:
        console.print(
            "⚠️  Your .gitignore uses the outdated 'data/' rule which blocks "
            "DVC import tracking files.\n"
            "   Run [bold]mintd init --update-gitignore[/bold] or replace the "
            "'data/' line with:\n\n"
            "     /data/**\n"
            "     !/data/.gitkeep\n"
            "     !/data/**/\n"
            "     !/data/**/*.dvc\n",
            style="yellow",
        )
        if not click.confirm("Continue anyway?", default=False):
            raise DataImportError(
                "Import aborted. Please update your .gitignore first."
            )


def import_data_product(
    product_name: str,
    project_path: Path,
    stage: Optional[str] = None,
    path: Optional[str] = None,
    dest: Optional[str] = None,
    repo_rev: Optional[str] = None,
    import_all: bool = False,
) -> ImportResult:
    """Import a data product as a DVC dependency.

    By default imports the catalog's ``data_products.primary`` path (falling
    back to ``data/final/``). If the path is not found, prompts the user to
    choose from available directories.

    Args:
        product_name: Name of the data product to import
        project_path: Path to the project directory
        stage: Pipeline stage to import (e.g., "final", "clean")
        path: Specific path to import from the product
        dest: Local destination path (default: data/imports/{product_name}/)
        repo_rev: Specific revision to import from
        import_all: If True, import the entire data/ directory

    Returns:
        ImportResult with operation details

    Raises:
        DataImportError: For various import failures
    """
    result = ImportResult(product_name=product_name, success=False)

    try:
        # Validate project directory
        validate_project_directory(project_path)

        # Check for outdated gitignore that blocks DVC imports
        _warn_outdated_gitignore(project_path)

        # Query product information from registry
        console.print(f"🔍 Querying registry for '{product_name}'...")
        product_info = query_data_product(product_name)

        repo_url = product_info["repository"]["github_url"]
        ssh_url = _https_to_ssh(repo_url)

        # Determine what to import — stage, path, and import_all are mutually exclusive
        exclusive_count = sum(bool(x) for x in [stage, path, import_all])
        if exclusive_count > 1:
            raise DataImportError(
                "Cannot combine --stage, --source-path, and --all. Use only one."
            )

        if import_all:
            # Import entire data/ directory
            source_path = "data/"
            if not dest:
                dest = f"data/imports/{product_name.replace('data_', '')}/"
        elif path:
            # Import specific path — try recursive .dvc discovery first
            source_path = path
            if not dest:
                dest = path
            dvc_data_paths = list_remote_dvc_files(ssh_url, path, rev=repo_rev)
            if dvc_data_paths:
                # Recursive import: import each discovered data file individually
                dest_path = project_path / dest
                dest_path.mkdir(parents=True, exist_ok=True)

                source_commit = repo_rev or "HEAD"
                success_count = 0
                fail_count = 0

                for i, data_path in enumerate(dvc_data_paths, 1):
                    # Preserve relative structure under dest
                    rel = data_path[len(path.rstrip("/")) + 1:]  # e.g. "file1.parquet"
                    file_dest = f"{dest.rstrip('/')}/{rel}"
                    file_dest_dir = (project_path / file_dest).parent
                    file_dest_dir.mkdir(parents=True, exist_ok=True)

                    console.print(
                        f"📥 Importing file {i}/{len(dvc_data_paths)}: {data_path}..."
                    )
                    try:
                        run_dvc_import(
                            project_path=project_path,
                            repo_url=repo_url,
                            source_path=data_path,
                            dest_path=file_dest,
                            repo_rev=repo_rev,
                        )
                        success_count += 1
                    except DVCImportError as e:
                        console.print(f"  ⚠️  Failed: {e}", style="yellow")
                        fail_count += 1

                console.print(
                    f"\n📊 Imported {success_count}/{len(dvc_data_paths)} files"
                    + (f" ({fail_count} failed)" if fail_count else "")
                )

                result.local_path = dest
                result.source_commit = source_commit
                if fail_count == 0:
                    result.success = True
                    console.print(
                        f"✅ Successfully imported {product_name}", style="green"
                    )
                else:
                    result.success = False
                    result.error_message = (
                        f"{fail_count}/{len(dvc_data_paths)} files failed to import"
                    )

                try:
                    update_project_metadata(project_path, result, product_info)
                except MetadataUpdateError as e:
                    console.print(
                        f"⚠️  Failed to update metadata: {e}", style="yellow"
                    )

                return result
            # No .dvc files found — fall through to single import
        else:
            # Smart default: use catalog primary or data/{stage}/
            if stage:
                source_path = f"data/{stage}/"
            else:
                source_path = _resolve_primary_path(product_info)
                console.print(
                    f"📦 Using default data product: {source_path}"
                )
                console.print(
                    "   Use --source-path to import a specific path, "
                    "or --all for the entire data/ directory"
                )

            exists, available_paths = validate_source_path(
                repo_url=ssh_url,
                source_path=source_path,
                rev=repo_rev,
            )

            if not exists:
                if not available_paths:
                    raise DataImportError(
                        f"No data directories found in '{product_name}'. "
                        f"Use --source-path to specify a custom path."
                    )
                # Prompt user to select from available paths
                source_path = prompt_stage_selection(available_paths) + "/"

            if not dest:
                dest = f"data/imports/{product_name.replace('data_', '')}/"

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


def pull_local(
    project_path: Path,
    targets: Optional[List[str]] = None,
    jobs: Optional[int] = None,
) -> bool:
    """Pull DVC-tracked data in the current mintd project.

    Mirrors ``push_data`` — reads the remote name from metadata.json and
    runs ``dvc pull -r <remote>``.

    Args:
        project_path: Path to the project directory
        targets: Optional list of specific .dvc files or stages to pull
        jobs: Number of parallel download jobs

    Returns:
        True if pull succeeded
    """
    try:
        remote_name = get_project_remote(project_path)

        dvc = dvc_command(cwd=project_path)
        args = ["pull", "-r", remote_name]

        if jobs:
            args.extend(["-j", str(jobs)])

        if targets:
            args.extend(targets)

        console.print(f"Pulling from remote '{remote_name}'...")
        dvc.run_live(*args)
        console.print("Pull complete.", style="green")
        return True
    except Exception as e:
        console.print(f"❌ Pull failed: {e}", style="red")
        return False


def _resolve_primary_path(product_info: Dict[str, Any]) -> str:
    """Extract the primary data product path from catalog info.

    Falls back to ``data/final/`` when the ``data_products`` section is
    absent or the value is invalid (backwards compatible with older catalog
    entries).
    """
    fallback = "data/final/"
    primary = (
        product_info
        .get("data_products", {})
        .get("primary", fallback)
    )
    if not primary or not isinstance(primary, str) or ".." in primary or primary.startswith("/"):
        return fallback
    return primary


def _resolve_dvc_target(repo_path: Path, primary_path: str) -> List[str]:
    """Determine the correct DVC pull target(s) for a primary data path.

    Checks for:
    1. A standalone ``.dvc`` file (e.g. ``data/final.dvc``)
    2. Individual ``.dvc`` files inside the directory (e.g. ``data/final/*.dvc``)
    3. An exact pipeline output match in ``dvc.yaml``
    4. Pipeline outputs nested under the primary path in ``dvc.yaml``

    Returns a list of target strings for ``dvc pull``, or an empty list if
    nothing is found (caller should fall back to pulling everything).
    """
    normalized = primary_path.rstrip("/")

    # 1. Check for a standalone .dvc file (e.g. data/final.dvc)
    dvc_file = repo_path / (normalized + ".dvc")
    if dvc_file.exists():
        return [normalized + ".dvc"]

    # 2. Check for .dvc files inside the directory (e.g. data/final/foo.dvc)
    primary_dir = repo_path / normalized
    if primary_dir.is_dir():
        nested_dvc = sorted(primary_dir.rglob("*.dvc"))
        if nested_dvc:
            return [str(p.relative_to(repo_path)) for p in nested_dvc]

    # 3. Check if it's a pipeline output in dvc.yaml (exact or nested)
    dvc_yaml = repo_path / "dvc.yaml"
    if dvc_yaml.exists():
        try:
            with open(dvc_yaml, "r") as f:
                pipeline = yaml.safe_load(f) or {}

            def _collect_outs(outs_list):
                targets = []
                for out in outs_list:
                    out_path = out if isinstance(out, str) else list(out.keys())[0]
                    out_norm = out_path.rstrip("/")
                    if out_norm == normalized or out_norm.startswith(normalized + "/"):
                        targets.append(out_path)
                return targets

            targets = []
            for stage in pipeline.get("stages", {}).values():
                targets.extend(_collect_outs(stage.get("outs", [])))
            # Also check top-level outs (less common but valid)
            targets.extend(_collect_outs(pipeline.get("outs", [])))
            if targets:
                return targets
        except Exception:
            pass  # Fall through to empty list

    return []


def clone_and_pull_product(
    product_name: str,
    dest: Optional[str] = None,
    rev: Optional[str] = None,
    pull_all: bool = False,
    jobs: Optional[int] = None,
) -> GetResult:
    """Clone a data product repo and ``dvc pull`` its data from S3.

    Args:
        product_name: Registered data product name
        dest: Local directory to clone into (default: ``./<product_name>/``)
        rev: Git tag or ref to checkout
        pull_all: If True, pull all DVC data; otherwise only the primary product
        jobs: Number of parallel DVC download jobs

    Returns:
        GetResult with operation details
    """
    # Input validation
    if "/" in product_name or "\\" in product_name or product_name in ("..", "."):
        return GetResult(
            product_name=product_name, success=False,
            dest_path=dest or "", source_path="",
            error_message=f"Invalid product name: {product_name}",
        )
    if dest and ".." in Path(dest).parts:
        return GetResult(
            product_name=product_name, success=False,
            dest_path=dest, source_path="",
            error_message="Destination path must not contain '..' components",
        )

    dest = dest or f"./{product_name}"
    dest_path = Path(dest)

    result = GetResult(
        product_name=product_name, success=False,
        dest_path=dest, source_path="",
    )

    try:
        # Resolve product from registry
        console.print(f"Looking up '{product_name}' in registry...")
        product_info = query_data_product(product_name)
        repo_url = product_info["repository"]["github_url"]
        ssh_url = _https_to_ssh(repo_url)
        primary_path = _resolve_primary_path(product_info)
        result.source_path = primary_path if not pull_all else "all"

        # Clone
        clone_args = ["clone", "--depth", "1"]
        if rev:
            clone_args.extend(["--branch", rev])
        clone_args.extend([ssh_url, str(dest_path)])

        console.print(f"Cloning {product_name}...")
        git_cmd = git_command()
        git_cmd.run(*clone_args)

        # DVC pull
        dvc = dvc_command(cwd=dest_path)
        pull_args = ["pull"]

        remote_name = (
            product_info.get("storage", {})
            .get("dvc", {})
            .get("remote_name", "")
        )
        if remote_name:
            pull_args.extend(["-r", remote_name])

        if jobs:
            pull_args.extend(["-j", str(jobs)])

        if not pull_all:
            dvc_targets = _resolve_dvc_target(dest_path, primary_path)
            if dvc_targets:
                pull_args.extend(dvc_targets)
            else:
                console.print(
                    f"[yellow]Warning: Could not find a DVC target for "
                    f"'{primary_path}'. Pulling all tracked data instead.[/yellow]"
                )

        console.print(
            f"Pulling {'all data' if pull_all else primary_path} from DVC remote..."
        )
        dvc.run_live(*pull_args)

        result.success = True
        console.print(f"Data available at {dest_path}", style="green")
        return result

    except Exception as e:
        result.error_message = str(e)
        if dest_path.exists():
            console.print(
                f"[yellow]Warning: partial clone left at {dest_path}. "
                f"Remove it before retrying.[/yellow]"
            )
        return result


def get_project_remote(project_path: Path) -> str:
    """Get the DVC remote name for a mintd project from its metadata.

    Args:
        project_path: Path to the project directory

    Returns:
        Remote name string

    Raises:
        DataImportError: If metadata is missing or has no remote configured
    """
    metadata_file = project_path / "metadata.json"
    if not metadata_file.exists():
        raise DataImportError(
            f"Not a mintd project directory. Missing metadata.json in {project_path}"
        )

    with open(metadata_file, 'r') as f:
        metadata = json.load(f)

    remote_name = metadata.get("storage", {}).get("dvc", {}).get("remote_name", "")
    if not remote_name:
        raise DataImportError(
            "No DVC remote configured in metadata.json. "
            "Was this project created with DVC enabled?"
        )

    return remote_name


def push_data(
    project_path: Path,
    targets: Optional[List[str]] = None,
    jobs: Optional[int] = None,
) -> bool:
    """Push DVC-tracked data to the project's configured remote.

    Reads the remote name from metadata.json and runs dvc push with the
    correct -r flag to ensure data goes to the right remote.

    Args:
        project_path: Path to the project directory
        targets: Optional list of specific .dvc files or stages to push
        jobs: Number of parallel upload jobs

    Returns:
        True if push succeeded
    """
    remote_name = get_project_remote(project_path)

    dvc = dvc_command(cwd=project_path)
    args = ["push", "-r", remote_name]

    if jobs:
        args.extend(["-j", str(jobs)])

    if targets:
        args.extend(targets)

    console.print(f"Pushing to remote '{remote_name}'...")
    dvc.run_live(*args)
    console.print("Push complete.", style="green")
    return True


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


def run_dvc_update(
    project_path: Path,
    dvc_file: str,
    rev: Optional[str] = None
) -> UpdateResult:
    """Run dvc update command on an existing .dvc file.

    Args:
        project_path: Project directory
        dvc_file: Path to the .dvc file to update (relative to project_path)
        rev: Specific revision to update to (optional)

    Returns:
        UpdateResult with update information

    Raises:
        DVCUpdateError: If update fails
    """
    dvc_path = project_path / dvc_file
    if not dvc_path.exists():
        raise DVCUpdateError(f"DVC file not found: {dvc_file}")

    dvc = dvc_command(cwd=project_path)
    args = ["update", dvc_file]

    if rev:
        args.extend(["--rev", rev])

    try:
        dvc.run_live(*args)

        return UpdateResult(
            dvc_file=dvc_file,
            success=True,
            previous_commit=None,  # Could parse from .dvc file before update
            new_commit=None  # Could parse from .dvc file after update
        )

    except Exception as e:
        raise DVCUpdateError(f"DVC update failed: {e}")


def update_single_import(
    project_path: Path,
    dvc_file_path: str,
    rev: Optional[str] = None
) -> UpdateResult:
    """Update a single DVC import by path.

    Args:
        project_path: Path to the project
        dvc_file_path: Path to the .dvc file to update
        rev: Specific revision to update to

    Returns:
        UpdateResult with update status

    Raises:
        DependencyNotFoundError: If .dvc file not found
    """
    dvc_path = project_path / dvc_file_path
    if not dvc_path.exists():
        raise DependencyNotFoundError(f"DVC file not found: {dvc_file_path}")

    return run_dvc_update(project_path, dvc_file_path, rev)


def update_all_imports(
    project_path: Path,
    rev: Optional[str] = None,
    dry_run: bool = False
) -> List[UpdateResult]:
    """Update all data imports in a project.

    Args:
        project_path: Path to the project
        rev: Specific revision for all updates (optional)
        dry_run: If True, show what would be updated without making changes

    Returns:
        List of UpdateResult for each dependency
    """
    results: List[UpdateResult] = []

    try:
        metadata = load_project_metadata(project_path)
    except Exception:
        return results

    dependencies = metadata.get("metadata", {}).get("data_dependencies", [])

    if not dependencies:
        return results

    for dep in dependencies:
        dvc_file = dep.get("dvc_file")
        if not dvc_file:
            continue

        dvc_path = project_path / dvc_file
        if not dvc_path.exists():
            results.append(UpdateResult(
                dvc_file=dvc_file,
                success=False,
                error_message=f"DVC file not found: {dvc_file}"
            ))
            continue

        if dry_run:
            results.append(UpdateResult(
                dvc_file=dvc_file,
                success=True,
                skipped=True
            ))
        else:
            try:
                result = run_dvc_update(project_path, dvc_file, rev)
                results.append(result)
            except DVCUpdateError as e:
                results.append(UpdateResult(
                    dvc_file=dvc_file,
                    success=False,
                    error_message=str(e)
                ))

    return results


def update_dependency_metadata(
    project_path: Path,
    update_result: UpdateResult
) -> None:
    """Update metadata.json after a successful dependency update.

    Args:
        project_path: Project directory
        update_result: Result of the update operation
    """
    metadata_file = project_path / "metadata.json"

    with open(metadata_file, 'r') as f:
        metadata = json.load(f)

    dependencies = metadata.get("metadata", {}).get("data_dependencies", [])

    for dep in dependencies:
        if dep.get("dvc_file") == update_result.dvc_file:
            if update_result.previous_commit:
                dep["previous_commit"] = update_result.previous_commit
            if update_result.new_commit:
                dep["source_commit"] = update_result.new_commit
            dep["updated_at"] = datetime.now().isoformat()
            break

    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)


def remove_dependency_from_metadata(
    project_path: Path,
    dependency_name: str
) -> Dict[str, Any]:
    """Remove a dependency entry from metadata.json.

    Args:
        project_path: Path to the project
        dependency_name: Name of the dependency (with or without data_ prefix)

    Returns:
        The removed dependency info for confirmation

    Raises:
        DependencyNotFoundError: If dependency not found in metadata
    """
    metadata_file = project_path / "metadata.json"

    with open(metadata_file, 'r') as f:
        metadata = json.load(f)

    dependencies = metadata.get("metadata", {}).get("data_dependencies", [])

    # Find the dependency (match with or without data_ prefix)
    removed = None
    remaining = []
    for dep in dependencies:
        source = dep.get("source", "")
        # Match either exact name or with/without data_ prefix
        if (source == dependency_name or
            source == f"data_{dependency_name}" or
            source.replace("data_", "") == dependency_name):
            removed = dep
        else:
            remaining.append(dep)

    if removed is None:
        raise DependencyNotFoundError(
            f"Dependency '{dependency_name}' not found in project metadata"
        )

    # Update metadata
    metadata["metadata"]["data_dependencies"] = remaining

    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)

    return removed


def check_dvc_yaml_references(
    project_path: Path,
    removed_paths: List[str]
) -> List[str]:
    """Check if dvc.yaml still references any of the removed paths.

    Args:
        project_path: Path to the project
        removed_paths: List of paths that were removed

    Returns:
        List of warning messages for each reference found
    """
    dvc_yaml = project_path / "dvc.yaml"
    warnings = []

    if not dvc_yaml.exists():
        return warnings

    content = dvc_yaml.read_text()

    for path in removed_paths:
        # Normalize path for matching
        normalized = path.rstrip("/")
        if normalized in content or path in content:
            warnings.append(
                f"Warning: dvc.yaml still references '{path}'. "
                f"You may need to update your pipeline."
            )

    return warnings


def remove_data_import(
    project_path: Path,
    import_name: str,
    force: bool = False
) -> RemoveResult:
    """Remove a data import from the project.

    Args:
        project_path: Path to the project
        import_name: Name of the import to remove (e.g., "cms-pps-weights")
        force: If True, remove even if dvc.yaml still references paths

    Returns:
        RemoveResult with operation details
    """
    result = RemoveResult(product_name=import_name, success=False)

    try:
        # Look up the dependency in metadata (validates it exists) without removing yet
        metadata_file = project_path / "metadata.json"
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
        dependencies = metadata.get("metadata", {}).get("data_dependencies", [])
        matched = None
        for dep in dependencies:
            source = dep.get("source", "")
            if (source == import_name or
                source == f"data_{import_name}" or
                source.replace("data_", "") == import_name):
                matched = dep
                break
        if matched is None:
            raise DependencyNotFoundError(
                f"Dependency '{import_name}' not found in project metadata"
            )

        local_path = matched.get("local_path", "")
        dvc_file = matched.get("dvc_file", "")

        # Check for dvc.yaml references BEFORE modifying metadata
        paths_to_check = [p for p in [local_path] if p]
        warnings = check_dvc_yaml_references(project_path, paths_to_check)
        result.warnings = warnings

        if warnings and not force:
            result.error_message = (
                "Cannot remove: dvc.yaml still references this import. "
                "Use --force to remove anyway."
            )
            return result

        # Now safe to remove from metadata
        removed_info = remove_dependency_from_metadata(project_path, import_name)

        # Remove the import directory (with path validation)
        if local_path:
            import_dir = (project_path / local_path).resolve()
            # Security: ensure path is within project directory
            if not str(import_dir).startswith(str(project_path.resolve())):
                raise DependencyRemovalError(
                    f"Invalid path in metadata: {local_path}"
                )
            if import_dir.exists():
                shutil.rmtree(import_dir)
                result.removed_path = local_path

        # Remove the .dvc file (with path validation)
        if dvc_file:
            dvc_path = (project_path / dvc_file).resolve()
            # Security: ensure path is within project directory
            if not str(dvc_path).startswith(str(project_path.resolve())):
                raise DependencyRemovalError(
                    f"Invalid dvc file path in metadata: {dvc_file}"
                )
            if dvc_path.exists():
                dvc_path.unlink()
                result.removed_dvc_file = dvc_file

        result.success = True
        console.print(f"✅ Removed data import '{import_name}'", style="green")
        if result.removed_path:
            console.print(f"   Removed directory: {result.removed_path}")
        if result.removed_dvc_file:
            console.print(f"   Removed DVC file: {result.removed_dvc_file}")

        for warning in warnings:
            console.print(f"⚠️  {warning}", style="yellow")

        return result

    except (DependencyNotFoundError, DependencyRemovalError) as e:
        result.error_message = str(e)
        return result
    except Exception as e:
        result.error_message = f"Failed to remove import: {e}"
        return result
