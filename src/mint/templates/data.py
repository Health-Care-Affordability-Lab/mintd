"""Data project template (data_*)."""

from typing import Dict, List, Tuple, Any

from .base import BaseTemplate


class DataTemplate(BaseTemplate):
    """Template for data projects (data_*)."""

    prefix = "data_"

    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return directory structure for data projects."""
        # Base structure
        structure = {
            "README.md": None,
            "metadata.json": None,
            "data": {
                "raw": {
                    ".gitkeep": None,
                },
                "clean": {
                    ".gitkeep": None,
                },
                "intermediate": {
                    ".gitkeep": None,
                },
            },
            "src": {},
            ".gitignore": None,
            ".dvcignore": None,
            "dvc.yaml": None,
        }

        # Language-specific configurations
        if self.language == "python":
            structure["requirements.txt"] = None
            structure["src"] = {
                "__init__.py": None,
                "ingest.py": None,
                "clean.py": None,
                "validate.py": None,
            }
        elif self.language == "r":
            structure["DESCRIPTION"] = None
            structure["renv.lock"] = None
            structure["src"] = {
                "ingest.R": None,
                "clean.R": None,
                "validate.R": None,
            }
        elif self.language == "stata":
            structure["requirements.txt"] = None  # For any Python dependencies
            structure["src"] = {
                "ingest.do": None,
                "clean.do": None,
                "validate.do": None,
            }

        return structure

    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return template files for data projects."""
        files = [
            ("README.md", "README_data.md.j2"),
            ("metadata.json", "metadata.json.j2"),
            (".gitignore", "gitignore.txt"),
            (".dvcignore", "dvcignore.txt"),
            ("dvc.yaml", "dvc_data.yaml.j2"),
        ]

        # Language-specific files
        if self.language == "python":
            files.extend([
                ("requirements.txt", "requirements_data.txt.j2"),
                ("src/__init__.py", "__init__.py.j2"),
                ("src/ingest.py", "ingest.py.j2"),
                ("src/clean.py", "clean.py.j2"),
                ("src/validate.py", "validate.py.j2"),
            ])
        elif self.language == "r":
            files.extend([
                ("DESCRIPTION", "DESCRIPTION.j2"),
                ("renv.lock", "renv.lock.j2"),
                ("src/ingest.R", "ingest.R.j2"),
                ("src/clean.R", "clean.R.j2"),
                ("src/validate.R", "validate.R.j2"),
            ])
        elif self.language == "stata":
            files.extend([
                ("requirements.txt", "requirements_data.txt.j2"),
                ("src/ingest.do", "ingest.do.j2"),
                ("src/clean.do", "clean.do.j2"),
                ("src/validate.do", "validate.do.j2"),
            ])

        return files