"""Base template system for project scaffolding."""

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Tuple, Any
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, Template
try:
    # Python 3.9+
    from importlib.resources import files
except ImportError:
    # Python < 3.9
    from importlib_resources import files

from ..utils import validate_project_name, format_project_name
from .. import __version__


class BaseTemplate(ABC):
    """Base class for all project templates."""

    prefix: str  # e.g., "data_", "prj__", "infra_"

    def __init__(self):
        """Initialize the template."""
        # Try to find templates in the installed package first, fall back to development path
        try:
            # Use importlib.resources to get the path to the mint package files
            mint_files = files('mint')
            self.template_dir = Path(mint_files / 'files')
        except (ImportError, AttributeError):
            # Fall back to development path if importlib.resources fails
            self.template_dir = Path(__file__).parent.parent / "files"

        # Ensure template directory exists
        if not self.template_dir.exists():
            raise RuntimeError(f"Template directory not found: {self.template_dir}")

        self.jinja_env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            trim_blocks=True,
            lstrip_blocks=True
        )

        self.language = "python"  # Default language

    def _get_mint_info(self) -> Dict[str, str]:
        """Get mint version and commit hash information."""
        version = __version__

        # Try to get git commit hash
        try:
            result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True,
                text=True,
                cwd=Path(__file__).parent.parent.parent  # mint package root
            )
            if result.returncode == 0:
                commit_hash = result.stdout.strip()
            else:
                commit_hash = "unknown"
        except (subprocess.SubprocessError, FileNotFoundError):
            commit_hash = "unknown"

        return {
            "mint_version": version,
            "mint_hash": commit_hash
        }

    @abstractmethod
    def get_directory_structure(self, use_current_repo: bool = False) -> Dict[str, Any]:
        """Return nested dict representing directory structure to create.

        Example:
        {
            "data": {
                "raw": {},
                "intermediate": {},
                "final": {}
            },
            "src": {
                "ingest.py": None,  # File to create
                "clean.py": None,
            }
        }
        """
        pass

    @abstractmethod
    def get_template_files(self) -> List[Tuple[str, str]]:
        """Return list of (relative_path, template_name) tuples for Jinja2 templates."""
        pass

    def create(self, name: str, path: str = ".", **context) -> Path:
        """Create the complete project structure.

        Args:
            name: Project name (without prefix)
            path: Directory to create project in
            **context: Additional context for template rendering

        Returns:
            Path to created project directory
        """
        # Set language from context
        self.language = context.get("language", "python")
        use_current_repo = context.get("use_current_repo", False)

        # Validate project name
        if not validate_project_name(name):
            raise ValueError(f"Invalid project name: {name}")

        if use_current_repo:
            # Use current directory as project root
            project_path = Path(path)
            full_name = f"{self.prefix}{name}"  # Still use full name for metadata, but don't create subdirectory
        else:
            # Create full project path (normal behavior)
            full_name = f"{self.prefix}{name}"
            project_path = Path(path) / full_name

        # Create directory structure
        self._create_directories(project_path, use_current_repo)

        # Create template files
        self._create_files(project_path, name, **context)

        return project_path

    def _create_directories(self, project_path: Path, use_current_repo: bool = False) -> None:
        """Create the directory structure."""
        structure = self.get_directory_structure(use_current_repo)

        def create_from_dict(base_path: Path, structure_dict: Dict[str, Any]) -> None:
            for name, content in structure_dict.items():
                current_path = base_path / name

                if isinstance(content, dict):
                    # It's a directory
                    current_path.mkdir(parents=True, exist_ok=True)
                    create_from_dict(current_path, content)
                elif content is None:
                    # It's a file - create parent directory and empty file
                    current_path.parent.mkdir(parents=True, exist_ok=True)
                    current_path.touch(exist_ok=True)
                else:
                    # Unexpected content
                    raise ValueError(f"Unexpected structure content for {name}: {content}")

        create_from_dict(project_path, structure)

    def _create_files(self, project_path: Path, name: str, **context) -> None:
        """Create files from Jinja2 templates.
        
        The context passed to templates includes:
        - project_name: Base project name
        - full_project_name: Name with prefix (e.g., data_myproject)
        - created_at: ISO timestamp
        - author, organization: From config
        - mint_version, mint_hash: Version info
        - platform_os: 'windows', 'macos', or 'linux'
        - command_sep: '&&' for Unix, '&' for Windows
        - stata_executable: Path or name of Stata executable
        - language: 'python', 'r', or 'stata'
        """
        # Prepare common context
        common_context = {
            "project_name": name,
            "full_project_name": f"{self.prefix}{name}",
            "created_at": datetime.now().isoformat(),
            "author": context.get("author", ""),
            "organization": context.get("organization", ""),
            # Platform-specific defaults (will be overridden by context if provided)
            "platform_os": context.get("platform_os", "linux"),
            "command_sep": context.get("command_sep", "&&"),
            "stata_executable": context.get("stata_executable", "stata"),
        }

        # Add mint version information
        mint_info = self._get_mint_info()
        common_context.update(mint_info)

        # Update with all context (this allows api.py to override defaults)
        common_context.update(context)

        # Create each template file
        for relative_path, template_name in self.get_template_files():
            file_path = project_path / relative_path

            try:
                template = self.jinja_env.get_template(template_name)
                content = template.render(**common_context)

                # Ensure parent directory exists
                file_path.parent.mkdir(parents=True, exist_ok=True)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

            except Exception as e:
                raise RuntimeError(f"Failed to create file {relative_path} from template {template_name}: {e}")