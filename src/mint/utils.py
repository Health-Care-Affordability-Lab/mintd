"""Shared utilities for the mint package."""

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def get_platform() -> str:
    """Return 'windows', 'macos', or 'linux'.
    
    This function detects the current operating system and returns a simplified
    string identifier that can be used throughout the application for platform-
    specific logic.
    
    Returns:
        str: One of 'windows', 'macos', or 'linux'
    
    Examples:
        >>> get_platform()
        'macos'  # On a Mac
    """
    system = platform.system().lower()
    
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    else:
        # Assume Linux for everything else (Linux, FreeBSD, etc.)
        return "linux"


def detect_stata_executable() -> Optional[str]:
    """Auto-detect stata-mp or stata in PATH.
    
    This function attempts to find a Stata executable on the system by:
    1. Looking for stata-mp (the multiprocessor version) first
    2. Falling back to stata if stata-mp isn't found
    3. On Windows, also checking common installation paths
    
    Returns:
        Optional[str]: The name or path of the Stata executable if found, 
                      None if Stata is not detected
    
    Examples:
        >>> detect_stata_executable()
        'stata-mp'  # If stata-mp is in PATH
        >>> detect_stata_executable()
        'C:\\Program Files\\Stata18\\StataMP-64.exe'  # On Windows
    """
    current_platform = get_platform()
    
    # List of Stata executables to try, in order of preference
    stata_variants = ["stata-mp", "stata"]
    
    if current_platform == "windows":
        # On Windows, use where.exe to find executables
        for variant in stata_variants:
            try:
                result = subprocess.run(
                    ["where.exe", variant],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    # Return the first path found
                    return result.stdout.strip().split('\n')[0]
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
        
        # Check common Windows installation paths
        common_paths = [
            # Stata 18
            Path("C:/Program Files/Stata18/StataMP-64.exe"),
            Path("C:/Program Files/Stata18/StataSE-64.exe"),
            Path("C:/Program Files/Stata18/Stata-64.exe"),
            # Stata 17
            Path("C:/Program Files/Stata17/StataMP-64.exe"),
            Path("C:/Program Files/Stata17/StataSE-64.exe"),
            Path("C:/Program Files/Stata17/Stata-64.exe"),
            # Stata 16
            Path("C:/Program Files/Stata16/StataMP-64.exe"),
            Path("C:/Program Files/Stata16/StataSE-64.exe"),
            Path("C:/Program Files/Stata16/Stata-64.exe"),
        ]
        
        for path in common_paths:
            if path.exists():
                return str(path)
    else:
        # On macOS/Linux, use shutil.which (which uses 'which' under the hood)
        for variant in stata_variants:
            path = shutil.which(variant)
            if path:
                return variant  # Return just the command name, not full path
    
    return None


def get_command_separator() -> str:
    """Return '&&' for Unix or '&' for Windows CMD.
    
    Windows CMD uses '&' to chain commands, while Unix shells use '&&'.
    The '&&' operator only runs the second command if the first succeeds,
    while '&' on Windows runs both regardless.
    
    Returns:
        str: '&&' for macOS/Linux, '&' for Windows
    
    Examples:
        >>> get_command_separator()
        '&&'  # On Unix
        >>> get_command_separator()
        '&'   # On Windows
    """
    if get_platform() == "windows":
        return "&"
    else:
        return "&&"


def validate_project_name(name: str) -> bool:
    """Validate that a project name is valid.

    Args:
        name: Project name to validate

    Returns:
        True if valid, False otherwise
    """
    # Basic validation - can be expanded later
    if not name:
        return False
    if any(char in name for char in [" ", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]):
        return False
    return True


def format_project_name(project_type: str, name: str) -> str:
    """Format a full project name with the appropriate prefix.

    Args:
        project_type: Type of project ("data", "project", or "infra")
        name: Base project name

    Returns:
        Full project name with prefix
    """
    if project_type == "data":
        return f"data_{name}"
    elif project_type == "project":
        return f"prj__{name}"
    elif project_type == "infra":
        return f"infra_{name}"
    else:
        raise ValueError(f"Unknown project type: {project_type}")