"""mint - Lab Project Scaffolding Tool

A Python package that automates the creation of standardized project repositories
(data_, prj__, infra_) with pre-configured Git and DVC initialization.
"""

__version__ = "0.1.0"

from .api import create_project
from .manifest import (
    create_manifest,
    load_manifest,
    save_manifest,
    has_file_changed,
    get_files_to_update,
    get_unchanged_files,
    compute_file_hash,
)

__all__ = [
    "create_project",
    "create_manifest",
    "load_manifest",
    "save_manifest",
    "has_file_changed",
    "get_files_to_update",
    "get_unchanged_files",
    "compute_file_hash",
]