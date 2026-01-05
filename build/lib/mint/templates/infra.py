"""Infrastructure template (infra_*)."""

from pathlib import Path
from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class InfraTemplate(BaseTemplate):
    """Template for infrastructure projects (infra_*)."""

    prefix = "infra_"

    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for infrastructure projects."""
        # This will be overridden in create() for dynamic package names
        return {
            "README.md": None,
            "metadata.json": None,
            "pyproject.toml": None,
            "src": {
                "package_placeholder": {  # Will be replaced dynamically
                    "__init__.py": None,
                },
            },
            "tests": {
                "__init__.py": None,
            },
            "docs": {
                ".gitkeep": None,
            },
            ".gitignore": None,
        }

    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return template files for infrastructure projects."""
        # Package name will be dynamically determined
        return [
            ("README.md", "README_infra.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            ("pyproject.toml", "pyproject_infra.toml.j2"),
            ("src/package_name/__init__.py", "__init__.py.j2"),
            ("tests/__init__.py", "__init__.py.j2"),
            (".gitignore", "gitignore.txt"),
        ]

    def create(self, name: str, path: str = ".", **context) -> Path:
        """Create infrastructure project with dynamic package name."""

        # Validate project name
        if not self._validate_name(name):
            raise ValueError(f"Invalid project name: {name}")

        # Add package name to context
        package_name = name.lower().replace("-", "_").replace(" ", "_")
        context["package_name"] = package_name
        use_current_repo = context.get("use_current_repo", False)

        if use_current_repo:
            # Use current directory as project root
            project_path = Path(path)
            full_name = f"{self.prefix}{name}"  # For metadata, but don't create subdirectory
        else:
            # Create full project path (normal behavior)
            full_name = f"{self.prefix}{name}"
            project_path = Path(path) / full_name

        # Create directory structure with dynamic package name
        self._create_directories_dynamic(project_path, package_name, use_current_repo)

        # Create template files
        self._create_files_dynamic(project_path, name, package_name, context)

        return project_path

    def _create_directories_dynamic(self, project_path: Path, package_name: str, use_current_repo: bool = False) -> None:
        """Create directory structure with dynamic package name."""
        structure = {
            "README.md": None,
            "metadata.json": None,
            "pyproject.toml": None,
            "src": {
                package_name: {
                    "__init__.py": None,
                },
            },
            "tests": {
                "__init__.py": None,
            },
            "docs": {
                ".gitkeep": None,
            },
            ".gitignore": None,
        }

        def create_from_dict(base_path: Path, structure_dict: dict) -> None:
            for name, content in structure_dict.items():
                current_path = base_path / name

                if isinstance(content, dict):
                    # It's a directory
                    current_path.mkdir(parents=True, exist_ok=True)
                    create_from_dict(current_path, content)
                elif content is None:
                    # It's a file (create parent directory)
                    current_path.parent.mkdir(parents=True, exist_ok=True)

        create_from_dict(project_path, structure)

    def _create_files_dynamic(self, project_path: Path, name: str, package_name: str, context: dict) -> None:
        """Create files with dynamic package name replacement."""
        from datetime import datetime

        # Prepare common context
        common_context = {
            "project_name": name,
            "full_project_name": f"{self.prefix}{name}",
            "package_name": package_name,
            "created_at": datetime.now().isoformat(),
            "author": context.get("author", ""),
            "organization": context.get("organization", ""),
        }
        common_context.update(context)

        # Create files, replacing "package_name" with actual package name in paths
        for relative_path, template_name in self.get_template_files():
            actual_path = relative_path.replace("package_name", package_name)
            file_path = project_path / actual_path

            try:
                template = self.jinja_env.get_template(template_name)
                content = template.render(**common_context)

                # Ensure parent directory exists
                file_path.parent.mkdir(parents=True, exist_ok=True)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            except Exception as e:
                raise RuntimeError(f"Failed to create file {actual_path} from template {template_name}: {e}")

    def _validate_name(self, name: str) -> bool:
        """Validate that a project name is valid."""
        # Basic validation - can be expanded later
        if not name:
            return False
        if any(char in name for char in [" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]):
            return False
        return True