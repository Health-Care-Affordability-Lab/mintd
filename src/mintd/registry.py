"""Registry integration for mintd - Tokenless GitOps operations using git + gh CLI."""

import json
import shutil
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .exceptions import GHCLIError, GitError, RegistryError, ShellCommandError
from .shell import gh_command, git_command


class LocalRegistry:
    """Tokenless registry operations using git + gh CLI."""

    def __init__(self, registry_url: str):
        """
        Initialize local registry operations.

        Args:
            registry_url: URL of the registry repository (e.g., https://github.com/org/registry)
        """
        self.registry_url = registry_url
        self.registry_org, self.registry_name = self._parse_registry_url(registry_url)
        self.temp_dir = None
        self.repo_path = None

    def _parse_registry_url(self, url: str) -> Tuple[str, str]:
        """Parse registry URL to extract org and repo name."""
        parsed = urllib.parse.urlparse(url)
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) >= 2:
            return path_parts[0], path_parts[1]
        raise ValueError(f"Invalid registry URL: {url}")

    def _git(self, cwd: Optional[Path] = None):
        """Get a git command instance for the given directory."""
        return git_command(cwd=cwd or self.repo_path)

    def _gh(self, cwd: Optional[Path] = None):
        """Get a gh command instance for the given directory."""
        return gh_command(cwd=cwd or self.repo_path)

    def _clone_registry(self) -> Path:
        """Clone the registry repository using SSH."""
        self.temp_dir = Path(tempfile.mkdtemp(prefix="mintd-registry-"))
        ssh_url = f"git@github.com:{self.registry_org}/{self.registry_name}.git"

        print(f"📥 Cloning registry: {ssh_url}")
        self._git(cwd=self.temp_dir).run("clone", ssh_url)

        self.repo_path = self.temp_dir / self.registry_name
        print(f"✅ Cloned to: {self.repo_path}")
        return self.repo_path

    def _create_branch(self, branch_name: str) -> None:
        """Create and checkout a new branch."""
        print(f"🌿 Creating branch: {branch_name}")
        git = self._git()

        # Check if branch already exists
        try:
            git.run("checkout", "-b", branch_name)
        except GitError:
            # Branch might exist, try to checkout existing
            try:
                git.run("checkout", branch_name)
                print(f"📋 Switched to existing branch: {branch_name}")
            except GitError:
                raise RegistryError(f"Could not create or checkout branch: {branch_name}")

    def _write_catalog_entry(self, catalog_entry: Dict[str, Any], project_name: str) -> Path:
        """Write catalog entry to the appropriate file."""
        project_type = catalog_entry['project']['type']
        type_dir = {'data': 'data', 'project': 'projects', 'infra': 'infra'}[project_type]

        # Ensure catalog directory exists
        catalog_dir = self.repo_path / 'catalog' / type_dir
        catalog_dir.mkdir(parents=True, exist_ok=True)

        # Write catalog entry
        file_path = catalog_dir / f"{project_name}.yaml"
        with open(file_path, 'w') as f:
            yaml.dump(catalog_entry, f, default_flow_style=False, sort_keys=False)

        print(f"📝 Created catalog entry: {file_path}")
        return file_path

    def _commit_and_push(self, branch_name: str, commit_message: str) -> None:
        """Commit changes and push the branch."""
        git = self._git()

        # Add all changes
        git.run("add", ".")

        # Commit
        git.run("commit", "-m", commit_message)

        # Push branch
        git.run("push", "-u", "origin", branch_name)

        print(f"✅ Pushed branch: {branch_name}")

    def _create_pull_request(self, branch_name: str, title: str, body: str) -> str:
        """Create a pull request using GitHub CLI."""
        try:
            # Use GitHub CLI to create PR
            result = self._gh().run(
                "pr", "create",
                "--title", title,
                "--body", body,
                "--head", branch_name,
                "--base", "main"
            )

            pr_url = result.stdout.strip()
            print(f"✅ Created PR: {pr_url}")
            return pr_url

        except GHCLIError as e:
            print(f"❌ Failed to create PR: {e}")
            raise RegistryError(f"Failed to create pull request: {e.message}")

    def register_project(self, metadata: Dict[str, Any]) -> str:
        """
        Register a project by creating a pull request.

        Args:
            metadata: Project metadata dictionary from metadata.json

        Returns:
            URL of the created pull request
        """
        try:
            print("🚀 Starting project registration...")
            print(f"📋 Registry: https://github.com/{self.registry_org}/{self.registry_name}")

            # Clone registry
            self._clone_registry()

            # Generate catalog entry
            catalog_entry, project_name = self._generate_catalog_entry(metadata)

            print(f"📝 Generated catalog entry for: {project_name}")
            print(f"🏷️  Project type: {catalog_entry['project']['type']}")

            # Create branch
            branch_name = f"register-{project_name}"
            self._create_branch(branch_name)

            # Write catalog entry
            self._write_catalog_entry(catalog_entry, project_name)

            # Commit and push
            commit_message = f"Register new {catalog_entry['project']['type']} project: {project_name}"
            self._commit_and_push(branch_name, commit_message)

            # Create pull request
            pr_title = f"Register new {catalog_entry['project']['type']} project: {project_name}"
            pr_body = f"""## Project Registration

This PR registers a new {catalog_entry['project']['type']} project: **{project_name}**

### Details
- **Type**: {catalog_entry['project']['type']}
- **Full Name**: {catalog_entry['project']['full_name']}
- **Created by**: {catalog_entry['ownership']['created_by']}

### Checklist
- [ ] Catalog entry follows schema requirements
- [ ] Access control teams are appropriate
- [ ] Repository will be created at: `{catalog_entry['repository']['github_url']}`
- [ ] Storage configuration is correct

### Next Steps
After merging this PR:
1. Repository will be created automatically
2. Permissions will be synchronized
3. Project will be available in the registry
"""

            pr_url = self._create_pull_request(branch_name, pr_title, pr_body)

            print(f"✅ Project registration PR created: {pr_url}")
            return pr_url

        finally:
            # Cleanup
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def check_registration_status(self, project_name: str) -> Dict[str, Any]:
        """
        Check if a project is registered and get its status.

        Args:
            project_name: Short name of the project (without prefix)

        Returns:
            Dictionary with registration status information
        """
        try:
            # Clone registry to check current state
            self._clone_registry()

            # Try different project types
            for project_type in ["data", "project", "infra"]:
                catalog_path = self.repo_path / 'catalog' / {'data': 'data', 'project': 'projects', 'infra': 'infra'}[project_type] / f"{project_name}.yaml"

                if catalog_path.exists():
                    return {
                        "registered": True,
                        "type": project_type,
                        "full_name": f"{project_type}_{project_name}",
                        "url": f"{self.registry_url}/blob/main/{catalog_path.relative_to(self.repo_path)}"
                    }

            # Check for open PRs using gh CLI
            try:
                result = self._gh().run("pr", "list", "--state", "open", "--json", "title,url,headRefName")
                prs = json.loads(result.stdout)

                for pr in prs:
                    if f"register-{project_name}" in pr.get('headRefName', '') or f"Register.*{project_name}" in pr.get('title', ''):
                        return {
                            "registered": False,
                            "pending_pr": pr.get('url'),
                            "pr_title": pr.get('title'),
                            "status": "pending_review"
                        }
            except ShellCommandError:
                # gh CLI not available or no PRs found
                pass

            return {"registered": False, "status": "not_found"}

        finally:
            # Cleanup
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def query_data_product(self, product_name: str) -> Dict[str, Any]:
        """Query registry for data product information.

        Args:
            product_name: Name of the data product (e.g., "data_cms-provider-data-service")

        Returns:
            Dictionary with product catalog entry

        Raises:
            FileNotFoundError: If product not found
            RuntimeError: If registry access fails
        """
        try:
            # Clone registry to get current catalog
            self._clone_registry()

            # Look for the product in the catalog
            catalog_dir = self.repo_path / 'catalog' / 'data'
            catalog_file = catalog_dir / f"{product_name}.yaml"

            if not catalog_file.exists():
                # Try to find similar products for suggestions
                available_products = []
                if catalog_dir.exists():
                    for yaml_file in catalog_dir.glob("*.yaml"):
                        available_products.append(yaml_file.stem)

                error_msg = f"Data product '{product_name}' not found in registry"
                if available_products:
                    error_msg += f". Available products: {', '.join(available_products[:5])}"
                    if len(available_products) > 5:
                        error_msg += f" (and {len(available_products) - 5} more)"

                raise FileNotFoundError(error_msg)

            # Read and parse the catalog entry
            with open(catalog_file, 'r') as f:
                catalog_data = yaml.safe_load(f)

            return catalog_data

        finally:
            # Cleanup
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def list_data_products(self) -> List[Dict[str, Any]]:
        """List all available data products in the registry.

        Returns:
            List of data product summaries
        """
        try:
            # Clone registry to get current catalog
            self._clone_registry()

            products = []
            catalog_dir = self.repo_path / 'catalog' / 'data'

            if catalog_dir.exists():
                for yaml_file in catalog_dir.glob("*.yaml"):
                    try:
                        with open(yaml_file, 'r') as f:
                            catalog_data = yaml.safe_load(f)

                        product_info = {
                            "name": yaml_file.stem,
                            "type": catalog_data.get("project", {}).get("type", "data"),
                            "full_name": catalog_data.get("project", {}).get("full_name", ""),
                            "description": catalog_data.get("metadata", {}).get("description", ""),
                            "created_at": catalog_data.get("project", {}).get("created_at", ""),
                            "created_by": catalog_data.get("ownership", {}).get("created_by", "")
                        }
                        products.append(product_info)

                    except Exception:
                        # Skip malformed catalog entries but continue
                        continue

            return products

        finally:
            # Cleanup
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def _generate_catalog_entry(self, metadata: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Generate a catalog entry for the project using metadata.json values."""
        project_name = metadata["project"]["name"]
        project_type = metadata["project"]["type"]
        full_name = metadata["project"]["full_name"]

        # Start with the metadata.json as the base catalog entry
        entry = dict(metadata)

        # Update registry-specific fields that may need adjustment
        current_time = datetime.now().isoformat() + 'Z'
        entry['status']['last_updated'] = current_time

        # Add storage section for data and project types if not present
        if project_type in ['data', 'project'] and 'storage' not in entry:
            entry['storage'] = {
                'dvc': {
                    'remote_name': 'wasabi',
                    'bucket': "lab-data" if project_type == 'data' else "lab-projects",
                    'path': full_name,
                    'endpoint': 'https://s3.wasabisys.com',
                    'region': 'us-east-1'
                },
                'estimated_size': 'TBD',
                'sensitivity': 'restricted'
            }

        # Add data dependencies for projects if not present
        if project_type == 'project' and 'data_dependencies' not in entry['metadata']:
            entry['metadata']['data_dependencies'] = []

        return entry, project_name


def get_registry_client() -> LocalRegistry:
    """Create a registry client using the configured registry URL."""
    from .config import get_config
    config = get_config()
    registry_url = config.get("registry", {}).get("url", "")
    return LocalRegistry(registry_url)


def query_registry_for_product(product_name: str) -> Dict[str, Any]:
    """Helper to query registry for a data product.

    Args:
        product_name: Name of the data product (e.g., "data_cms-provider-data-service")

    Returns:
        Dictionary with existence and catalog data
    """
    try:
        client = get_registry_client()
        catalog_data = client.query_data_product(product_name)
        return {
            "exists": True,
            "catalog_data": catalog_data
        }
    except FileNotFoundError:
        return {"exists": False}
    except Exception as e:
        return {"exists": False, "error": str(e)}


def load_project_metadata(project_path: Path) -> Dict[str, Any]:
    """Load project metadata from metadata.json file.

    Args:
        project_path: Path to the project directory

    Returns:
        Project metadata dictionary

    Raises:
        FileNotFoundError: If metadata.json doesn't exist
        ValueError: If metadata is invalid
    """
    metadata_file = project_path / "metadata.json"

    if not metadata_file.exists():
        raise FileNotFoundError(f"metadata.json not found in {project_path}")

    with open(metadata_file, "r") as f:
        metadata = json.load(f)

    # Validate required fields (matching registry schema)
    required_fields = ["project", "metadata", "ownership", "access_control", "status"]
    for field in required_fields:
        if field not in metadata:
            raise ValueError(f"Missing required field '{field}' in metadata.json")

    # Validate project section
    project_fields = ["name", "type", "full_name"]
    if "project" in metadata:
        for field in project_fields:
            if field not in metadata["project"]:
                raise ValueError(f"Missing required project field '{field}' in metadata.json")

    # Validate access_control has teams
    if "access_control" in metadata and "teams" in metadata["access_control"]:
        teams = metadata["access_control"]["teams"]
        if not teams:
            raise ValueError("access_control.teams must contain at least one team")
        # Check for admin permission
        has_admin = any(team.get("permission") == "admin" for team in teams)
        if not has_admin:
            raise ValueError("At least one team must have 'admin' permission")

    return metadata


def save_pending_registration(project_path: Path, metadata: Dict[str, Any]) -> None:
    """Save registration request for later retry when registry is available.

    Args:
        project_path: Path to the project directory
        metadata: Project metadata to save for later registration
    """
    pending_dir = Path.home() / ".mintd" / "pending_registrations"
    pending_dir.mkdir(parents=True, exist_ok=True)

    pending_file = pending_dir / f"{metadata['project']['full_name']}.json"

    with open(pending_file, "w") as f:
        json.dump({
            "project_path": str(project_path),
            "metadata": metadata,
            "created_at": datetime.now().isoformat()
        }, f, indent=2)


def get_pending_registrations() -> list:
    """Get list of pending registrations.

    Returns:
        List of pending registration info dictionaries
    """
    pending_dir = Path.home() / ".mintd" / "pending_registrations"
    if not pending_dir.exists():
        return []

    pending = []
    for file in pending_dir.glob("*.json"):
        try:
            with open(file, "r") as f:
                pending.append(json.load(f))
        except (json.JSONDecodeError, IOError):
            continue

    return pending


def clear_pending_registration(project_name: str) -> None:
    """Remove a pending registration after successful registration.

    Args:
        project_name: Full project name (e.g., "data_hospital_project")
    """
    pending_dir = Path.home() / ".mintd" / "pending_registrations"
    pending_file = pending_dir / f"{project_name}.json"

    if pending_file.exists():
        pending_file.unlink()