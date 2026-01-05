"""Enclave project template (enclave_*)."""

from pathlib import Path
from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class EnclaveTemplate(BaseTemplate):
    """Template for enclave projects (enclave_*)."""

    prefix = "enclave_"

    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for enclave projects."""
        structure = {
            "README.md": None,
            "metadata.json": None,
            "enclave_manifest.yaml": None,
            "requirements.txt": None,
            "data": {
                ".gitkeep": None,
            },
            "src": {
                "__init__.py": None,
                "registry.py": None,
                "download.py": None,
                "package.py": None,
                "verify.py": None,
            },
            "scripts": {
                "pull_data.sh": None,
                "package_transfer.sh": None,
                "unpack_transfer.sh": None,
                "verify_transfer.sh": None,
            },
            "transfers": {
                ".gitkeep": None,
            },
            ".gitignore": None,
            ".dvcignore": None,
        }

        return structure

    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return template files for enclave projects."""
        files = [
            ("README.md", "README_enclave.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            ("enclave_manifest.yaml", "enclave_manifest.yaml.j2"),
            ("requirements.txt", "requirements_enclave.txt.j2"),
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
            ("src/__init__.py", "__init__.py.j2"),
            ("src/registry.py", "registry.py.j2"),
            ("src/download.py", "download.py.j2"),
            ("src/package.py", "package.py.j2"),
            ("src/verify.py", "verify.py.j2"),
            ("scripts/pull_data.sh", "pull_data.sh.j2"),
            ("scripts/package_transfer.sh", "package_transfer.sh.j2"),
            ("scripts/unpack_transfer.sh", "unpack_transfer.sh.j2"),
            ("scripts/verify_transfer.sh", "verify_transfer.sh.j2"),
        ]

        return files

    def create(self, name: str, path: str = ".", **context) -> Path:
        """Create enclave project and make scripts executable."""
        project_path = super().create(name, path, **context)

        # Make shell scripts executable
        scripts_dir = project_path / "scripts"
        if scripts_dir.exists():
            for script_file in scripts_dir.glob("*.sh"):
                script_file.chmod(0o755)  # rwxr-xr-x

        return project_path
