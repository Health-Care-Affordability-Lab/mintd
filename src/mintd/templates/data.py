"""Data project template (data_*)."""

from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class DataTemplate(BaseTemplate):
    """Template for data projects (data_*)."""

    prefix = "data_"

    template_type = "data"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for data projects.

        Follows AEA Data Editor guidelines for reproducible research packages.
        See: https://aeadataeditor.github.io/aea-de-guidance/preparing-for-data-deposit
        """
        # Base structure
        structure = {
            "README.md": None,
            "metadata.json": None,
            "data": {
                "raw": {
                    ".gitkeep": None,
                },
                "intermediate": {
                    ".gitkeep": None,
                },
                "final": {
                    ".gitkeep": None,
                },
            },
            "schemas": {
                "v1": {
                    "schema.json": None,
                },
            },
            self.source_dir: {},  # Renamed from src
            ".gitignore": None,
            ".dvcignore": None,
            "dvc_vars.yaml": None,
            "dvc.yaml": None,
        }

        return structure

    def define_files(self) -> List[Tuple[str, str]]:
        """Return template files for data projects."""
        return [
            ("README.md", "README_data.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            ("schemas/v1/schema.json", "schema.json.j2"),
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
            ("dvc_vars.yaml", "dvc_vars.yaml.j2"),
            ("dvc.yaml", "dvc_data.yaml.j2"),
        ]