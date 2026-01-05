"""Project template (prj__*)."""

from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class ProjectTemplate(BaseTemplate):
    """Template for research projects (prj__*)."""

    prefix = "prj__"

    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for research projects."""
        return {
            "README.md": None,
            "metadata.json": None,
            "requirements.txt": None,
            "renv.lock": None,  # R environment lockfile
            "data": {
                ".gitkeep": None,
            },
            "src": {
                "analysis": {
                    "__init__.py": None,
                },
                "stata": {
                    ".gitkeep": None,
                },
                "r": {
                    ".gitkeep": None,
                },
            },
            "output": {
                "figures": {
                    ".gitkeep": None,
                },
                "tables": {
                    ".gitkeep": None,
                },
            },
            "docs": {
                ".gitkeep": None,
            },
            ".gitignore": None,
            ".dvcignore": None,
        }

    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return template files for research projects."""
        return [
            ("README.md", "README_project.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            ("requirements.txt", "requirements_project.txt.j2"),
            ("src/analysis/__init__.py", "__init__.py.j2"),
            ("src/r/analysis.R", "analysis.R.j2"),
            (".Rprofile", ".Rprofile.j2"),
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
        ]