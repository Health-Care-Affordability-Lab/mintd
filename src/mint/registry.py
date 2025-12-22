"""Registry integration for mint - Tokenless GitOps operations using git + gh CLI."""

import os
import json
import yaml
import tempfile
import shutil
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any, Tuple


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

    def _run_git_command(self, *args, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command and return the result."""
        cmd = ['git'] + list(args)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or self.repo_path,
                capture_output=True,
                text=True,
                check=check
            )
            return result
        except subprocess.CalledProcessError as e:
            print(f"❌ Git command failed: {' '.join(cmd)}")
            print(f"stdout: {e.stdout}")
            print(f"stderr: {e.stderr}")
            raise

    def _run_gh_command(self, *args, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a gh CLI command and return the result."""
        cmd = ['gh'] + list(args)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd or self.repo_path,
                capture_output=True,
                text=True,
                check=check
            )
            return result
        except subprocess.CalledProcessError as e:
            print(f"❌ GitHub CLI command failed: {' '.join(cmd)}")
            print(f"stdout: {e.stdout}")
            print(f"stderr: {e.stderr}")
            raise

    def _clone_registry(self) -> Path:
        """Clone the registry repository using SSH."""
        self.temp_dir = Path(tempfile.mkdtemp(prefix="mint-registry-"))
        ssh_url = f"git@github.com:{self.registry_org}/{self.registry_name}.git"

        print(f"📥 Cloning registry: {ssh_url}")
        self._run_git_command('clone', ssh_url, self.temp_dir.name, cwd=self.temp_dir.parent)

        self.repo_path = self.temp_dir / self.registry_name
        print(f"✅ Cloned to: {self.repo_path}")
        return self.repo_path

    def _create_branch(self, branch_name: str) -> None:
        """Create and checkout a new branch."""
        print(f"🌿 Creating branch: {branch_name}")

        # Check if branch already exists
        try:
            self._run_git_command('checkout', '-b', branch_name)
        except subprocess.CalledProcessError:
            # Branch might exist, try to checkout existing
            try:
                self._run_git_command('checkout', branch_name)
                print(f"📋 Switched to existing branch: {branch_name}")
            except subprocess.CalledProcessError:
                raise RuntimeError(f"Could not create or checkout branch: {branch_name}")

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
        # Add all changes
        self._run_git_command('add', '.')

        # Commit
        self._run_git_command('commit', '-m', commit_message)

        # Push branch
        self._run_git_command('push', '-u', 'origin', branch_name)

        print(f"✅ Pushed branch: {branch_name}")

    def _create_pull_request(self, branch_name: str, title: str, body: str) -> str:
        """Create a pull request using GitHub CLI."""
        try:
            # Use GitHub CLI to create PR
            result = self._run_gh_command(
                'pr', 'create',
                '--title', title,
                '--body', body,
                '--head', branch_name,
                '--base', 'main'
            )

            pr_url = result.stdout.strip()
            print(f"✅ Created PR: {pr_url}")
            return pr_url

        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to create PR: {e}")
            print(f"stdout: {e.stdout}")
            print(f"stderr: {e.stderr}")
            raise

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
                result = self._run_gh_command('pr', 'list', '--state', 'open', '--json', 'title,url,headRefName')
                prs = json.loads(result.stdout)

                for pr in prs:
                    if f"register-{project_name}" in pr.get('headRefName', '') or f"Register.*{project_name}" in pr.get('title', ''):
                        return {
                            "registered": False,
                            "pending_pr": pr.get('url'),
                            "pr_title": pr.get('title'),
                            "status": "pending_review"
                        }
            except subprocess.CalledProcessError:
                # gh CLI not available or no PRs found
                pass

            return {"registered": False, "status": "not_found"}

        finally:
            # Cleanup
            if self.temp_dir and self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)

    def _generate_catalog_entry(self, metadata: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Generate a catalog entry for the project."""
        project_name = metadata["project"]["name"]
        project_type = metadata["project"]["type"]

        # Determine project type and full name
        if project_type == 'data':
            full_name = f"data_{project_name}"
        elif project_type == 'project':
            full_name = f"prj__{project_name}"
        elif project_type == 'infra':
            full_name = f"infra_{project_name}"

        # Get current user info
        creator = os.environ.get('GITHUB_ACTOR', os.environ.get('USER', 'unknown-user'))
        current_time = datetime.now().isoformat() + 'Z'

        # Generate catalog entry
        entry = {
            'schema_version': '1.0',
            'project': {
                'name': project_name,
                'type': project_type,
                'full_name': full_name,
                'display_name': project_name.replace('_', ' ').replace('-', ' ').title()
            },
            'metadata': {
                'description': metadata.get('description', f'{project_type.title()} project: {project_name}'),
                'tags': metadata.get('tags', []),
                'language': metadata.get('language', 'unknown')
            },
            'ownership': {
                'created_by': f"{creator}@{self.registry_org}.github.io",
                'created_at': current_time,
                'maintainers': [
                    {
                        'email': f"{creator}@{self.registry_org}.github.io",
                        'name': creator.replace('-', ' ').title(),
                        'role': 'lead'
                    }
                ]
            },
            'repository': {
                'github_url': f"https://github.com/{self.registry_org}/{full_name}",
                'default_branch': 'main',
                'visibility': 'private'  # Default to private
            },
            'access_control': {
                'teams': [
                    {
                        'team': 'infrastructure-admins',
                        'permission': 'admin'
                    },
                    {
                        'team': 'all-researchers',
                        'permission': 'read'
                    }
                ],
                'individuals': [],
                'requirements': [
                    'Must complete data access training',
                    'Must sign data use agreement'
                ]
            },
            'status': {
                'lifecycle': 'active',
                'last_updated': current_time
            }
        }

        # Add storage section for data and project types
        if project_type in ['data', 'project']:
            entry['storage'] = {
                'dvc': {
                    'remote_name': 'wasabi',
                    'bucket': "lab-data" if project_type == 'data' else "lab-projects",
                    'path': full_name,
                    'endpoint': 'https://s3.wasabisys.com',
                    'region': 'us-east-1'
                },
                'estimated_size': metadata.get('estimated_size', 'TBD'),
                'sensitivity': 'restricted'
            }

        # Add data dependencies for projects
        if project_type == 'project':
            entry['metadata']['data_dependencies'] = []

        return entry, project_name


def get_registry_client() -> LocalRegistry:
    """Create a registry client using the configured registry URL."""
    from .config import get_registry_url

    registry_url = get_registry_url()
    return LocalRegistry(registry_url)


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

    # Validate required fields
    required_fields = ["project", "ownership"]
    for field in required_fields:
        if field not in metadata:
            raise ValueError(f"Missing required field '{field}' in metadata.json")

    project_fields = ["name", "type", "full_name"]
    for field in project_fields:
        if field not in metadata["project"]:
            raise ValueError(f"Missing required project field '{field}' in metadata.json")

    return metadata


def save_pending_registration(project_path: Path, metadata: Dict[str, Any]) -> None:
    """Save registration request for later retry when registry is available.

    Args:
        project_path: Path to the project directory
        metadata: Project metadata to save for later registration
    """
    pending_dir = Path.home() / ".mint" / "pending_registrations"
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
    pending_dir = Path.home() / ".mint" / "pending_registrations"
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
        project_name: Full project name (e.g., "data_medicare_claims")
    """
    pending_dir = Path.home() / ".mint" / "pending_registrations"
    pending_file = pending_dir / f"{project_name}.json"

    if pending_file.exists():
        pending_file.unlink()