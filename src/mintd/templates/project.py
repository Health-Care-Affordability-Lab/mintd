"""Project template (prj__*)."""

from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class ProjectTemplate(BaseTemplate):
    """Template for research projects (prj__*)."""

    prefix = "prj_"
    template_type = "project"

    def define_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for research projects."""
        return {
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
            self.source_dir: {
                ".gitkeep": None, # Base code directory
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
            "logs": {
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
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
        ]