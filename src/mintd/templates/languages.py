"""Language strategies for project templates."""

from abc import ABC, abstractmethod
from typing import Dict, List, Tuple, Any


class LanguageStrategy(ABC):
    """Base class for language-specific template strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Language name (e.g., 'python', 'r', 'stata')."""
        pass

    @property
    @abstractmethod
    def file_extension(self) -> str:
        """File extension (e.g., 'py', 'R', 'do')."""
        pass

    def get_system_requirements(self) -> Dict[str, Any]:
        """Return system requirement files (requirements.txt, etc)."""
        return {}

    def get_project_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure for standard Project template."""
        return {}

    def get_project_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        """Return files for standard Project template."""
        return []

    def get_data_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure for Data template."""
        return {}

    def get_data_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        """Return files for Data template."""
        return []

    def get_infra_structure(self, package_name: str, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure for Infra (Library) template."""
        return {}

    def get_infra_files(self, package_name: str, source_dir: str = "code") -> List[Tuple[str, str]]:
        """Return files for Infra (Library) template."""
        return []


class PythonStrategy(LanguageStrategy):
    """Python language strategy."""

    name = "python"
    file_extension = "py"

    def get_system_requirements(self) -> Dict[str, Any]:
        return {"requirements.txt": None}

    def get_project_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure with numbered subdirectories per AEA guidelines."""
        return {
            source_dir: {
                "_mintd_utils.py": None,
                "config.py": None,
                "02_analysis": {
                    "__init__.py": None,
                },
            },
            "run_all.py": None,
        }

    def get_project_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("requirements.txt", "requirements_project.txt.j2"),
            (f"{source_dir}/_mintd_utils.py", "_mintd_utils.py.j2"),
            (f"{source_dir}/config.py", "config.py.j2"),
            (f"{source_dir}/02_analysis/__init__.py", "__init__.py.j2"),
            ("run_all.py", "run_all.py.j2"),
        ]

    def get_data_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        return {
            "requirements.txt": None,
            source_dir: {
                "_mintd_utils.py": None,
                "ingest.py": None,
                "clean.py": None,
                "validate.py": None,
            }
        }

    def get_data_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("requirements.txt", "requirements_data.txt.j2"),
            (f"{source_dir}/_mintd_utils.py", "_mintd_utils.py.j2"),
            (f"{source_dir}/ingest.py", "ingest.py.j2"),
            (f"{source_dir}/clean.py", "clean.py.j2"),
            (f"{source_dir}/validate.py", "validate.py.j2"),
        ]

    def get_infra_structure(self, package_name: str, source_dir: str = "code") -> Dict[str, Any]:
        return {
            "pyproject.toml": None,
            source_dir: {
                package_name: {
                    "__init__.py": None,
                },
            },
            "tests": {
                "__init__.py": None,
            },
        }

    def get_infra_files(self, package_name: str, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("pyproject.toml", "pyproject_infra.toml.j2"),
            (f"{source_dir}/{package_name}/__init__.py", "__init__.py.j2"),
            ("tests/__init__.py", "__init__.py.j2"),
        ]


class RStrategy(LanguageStrategy):
    """R language strategy."""

    name = "r"
    file_extension = "R"

    def get_system_requirements(self) -> Dict[str, Any]:
        return {
            "DESCRIPTION": None,
            "renv.lock": None,
        }

    def get_project_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure with numbered subdirectories per AEA guidelines."""
        return {
            source_dir: {
                "_mintd_utils.R": None,
                "config.R": None,
                "02_analysis": {
                    "analysis.R": None,
                },
            },
            "run_all.R": None,
        }

    def get_project_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("DESCRIPTION", "DESCRIPTION.j2"),
            ("renv.lock", "renv.lock.j2"),
            (f"{source_dir}/_mintd_utils.R", "_mintd_utils.R.j2"),
            (f"{source_dir}/config.R", "config.R.j2"),
            (f"{source_dir}/02_analysis/analysis.R", "analysis.R.j2"),
            ("run_all.R", "run_all.R.j2"),
            (".Rprofile", ".Rprofile.j2"),
        ]

    def get_data_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        return {
            "DESCRIPTION": None,
            "renv.lock": None,
            source_dir: {
                "_mintd_utils.R": None,
                "ingest.R": None,
                "clean.R": None,
                "validate.R": None,
            }
        }

    def get_data_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("DESCRIPTION", "DESCRIPTION.j2"),
            ("renv.lock", "renv.lock.j2"),
            (f"{source_dir}/_mintd_utils.R", "_mintd_utils.R.j2"),
            (f"{source_dir}/ingest.R", "ingest.R.j2"),
            (f"{source_dir}/clean.R", "clean.R.j2"),
            (f"{source_dir}/validate.R", "validate.R.j2"),
        ]

    def get_infra_structure(self, package_name: str, source_dir: str = "code") -> Dict[str, Any]:
        # R package structure using code/ directory for consistency with other mintd projects
        # Note: Standard R packages use R/ directory, but we use code/ for mintd consistency.
        # Users can configure .Rbuildignore or symlink if needed for CRAN submission.
        return {
            "DESCRIPTION": None,
            "NAMESPACE": None,
            source_dir: {
                f"{package_name}.R": None,
            }
        }

    def get_infra_files(self, package_name: str, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            ("DESCRIPTION", "DESCRIPTION_infra.j2"),
            ("NAMESPACE", "NAMESPACE.j2"),
            (f"{source_dir}/{package_name}.R", "package.R.j2"),
        ]


class StataStrategy(LanguageStrategy):
    """Stata language strategy."""

    name = "stata"
    file_extension = "do"

    def get_system_requirements(self) -> Dict[str, Any]:
        return {}

    def get_project_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        """Return structure with numbered subdirectories per AEA guidelines."""
        return {
            source_dir: {
                "_mintd_utils.do": None,
                "config.do": None,
                "02_analysis": {
                    ".gitkeep": None,
                },
            },
            "run_all.do": None,
        }

    def get_project_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            (f"{source_dir}/_mintd_utils.do", "_mintd_utils.do.j2"),
            (f"{source_dir}/config.do", "config.do.j2"),
            ("run_all.do", "run_all.do.j2"),
        ]

    def get_data_structure(self, source_dir: str = "code") -> Dict[str, Any]:
        return {
            source_dir: {
                "_mintd_utils.do": None,
                "ingest.do": None,
                "clean.do": None,
                "validate.do": None,
            },
            "schemas": {
                "generate_schema.py": None,
            }
        }

    def get_data_files(self, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            (f"{source_dir}/_mintd_utils.do", "_mintd_utils.do.j2"),
            (f"{source_dir}/ingest.do", "ingest.do.j2"),
            (f"{source_dir}/clean.do", "clean.do.j2"),
            (f"{source_dir}/validate.do", "validate.do.j2"),
            ("schemas/generate_schema.py", "generate_schema.py.j2"),
        ]
        
    def get_infra_structure(self, package_name: str, source_dir: str = "code") -> Dict[str, Any]:
        return {
            source_dir: {
                f"{package_name}.ado": None,
                f"{package_name}.sthlp": None,
            }
        }

    def get_infra_files(self, package_name: str, source_dir: str = "code") -> List[Tuple[str, str]]:
        return [
            (f"{source_dir}/{package_name}.ado", "package.ado.j2"),
            (f"{source_dir}/{package_name}.sthlp", "package.sthlp.j2"),
        ]
