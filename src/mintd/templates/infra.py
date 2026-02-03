"""Infrastructure template (infra_*)."""

from pathlib import Path
from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class InfraTemplate(BaseTemplate):
    """Template for infrastructure projects (infra_*)."""

    prefix = "infra_"

    template_type = "infra"
    package_name: str = "package_name"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return base directory structure for infrastructure projects.

        Follows AEA Data Editor guidelines for reproducible research packages.
        See: https://aeadataeditor.github.io/aea-de-guidance/preparing-for-data-deposit
        """
        return {
            "README.md": None,
            "metadata.json": None,
            "data": {
                "raw": {
                    ".gitkeep": None,
                },
                "analysis": {
                    ".gitkeep": None,
                },
            },
            "docs": {
                ".gitkeep": None,
            },
            self.source_dir: {
                ".gitkeep": None,
            },
            "tests": {
                ".gitkeep": None,
            },
            ".gitignore": None,
        }

    def define_files(self) -> List[Tuple[str, str]]:
        """Return base template files for infrastructure projects."""
        return [
            ("README.md", "README_infra.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            (".gitignore", "gitignore.txt"),
            ("pyproject.toml", "pyproject_infra.toml.j2"),
        ]

    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure with strategy updates."""
        structure = self.define_structure(use_current_repo)
        
        if self.strategy:
            updates = self.strategy.get_infra_structure(self.package_name, self.source_dir)
            self._merge_structure(structure, updates)
            
        return structure

    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return template files with strategy updates."""
        files = self.define_files()
        
        if self.strategy:
            files.extend(self.strategy.get_infra_files(self.package_name, self.source_dir))
            
        return files

    def create(self, name: str, path: str = ".", **context) -> Path:
        """Create infrastructure project with dynamic package name."""
        # Set language first to initialize strategy (via super or manual?)
        # BaseTemplate.create does this, but we need package_name BEFORE create calls structure.
        # So we override create, set up state, then call super().create?
        # super().create() does everything.
        
        # 1. Validate name
        if not self._validate_name(name):
            raise ValueError(f"Invalid project name: {name}")

        # 2. Determine package name
        self.package_name = name.lower().replace("-", "_").replace(" ", "_")
        context["package_name"] = self.package_name
        
        # 3. Call parent create
        return super().create(name, path, **context)

    # _create_directories_dynamic and _create_files_dynamic are no longer needed
    # as BaseTemplate._create_directories/files will use our get_* methods 
    # which now handle the dynamic logic via the strategy.


    def _validate_name(self, name: str) -> bool:
        """Validate that a project name is valid."""
        # Basic validation - can be expanded later
        if not name:
            return False
        if any(char in name for char in [" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]):
            return False
        return True