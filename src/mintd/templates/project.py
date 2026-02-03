"""Project template (prj__*)."""

from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class ProjectTemplate(BaseTemplate):
    """Template for research projects (prj__*)."""

    prefix = "prj_"
    template_type = "project"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for research projects.

        Follows AEA Data Editor guidelines for reproducible research packages.
        See: https://aeadataeditor.github.io/aea-de-guidance/preparing-for-data-deposit
        """
        return {
            "README.md": None,
            "metadata.json": None,
            "citations.md": None,
            "data": {
                "raw": {
                    ".gitkeep": None,
                },
                "analysis": {
                    ".gitkeep": None,
                },
            },
            self.source_dir: {
                "01_data_prep": {
                    ".gitkeep": None,
                },
                "02_analysis": {
                    ".gitkeep": None,
                },
                "03_tables": {
                    ".gitkeep": None,
                },
                "04_figures": {
                    ".gitkeep": None,
                },
            },
            "notebooks": {
                ".gitkeep": None,
            },
            "results": {
                "figures": {
                    ".gitkeep": None,
                },
                "tables": {
                    ".gitkeep": None,
                },
                "estimates": {
                    ".gitkeep": None,
                },
                "presentations": {
                    ".gitkeep": None,
                },
            },
            "docs": {
                ".gitkeep": None,
            },
            "references": {
                ".gitkeep": None,
            },
            "tests": {
                ".gitkeep": None,
            },
            ".gitignore": None,
            ".dvcignore": None,
        }

    def define_files(self) -> List[Tuple[str, str]]:
        """Return template files for research projects."""
        return [
            ("README.md", "README_project.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            ("citations.md", "citations.md.j2"),
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
        ]