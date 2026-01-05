"""File manifest utilities for tracking file hashes and changes.

This module provides utilities to create and manage file manifests that track
file metadata including hashes, modification times, and sizes. This enables
data pipelines to skip processing unchanged files.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Union, Any
from datetime import datetime, timezone


def compute_file_hash(filepath: Union[str, Path], algorithm: str = "md5") -> str:
    """Compute hash of a file using specified algorithm.

    Args:
        filepath: Path to the file to hash
        algorithm: Hash algorithm ("md5" or "sha256")

    Returns:
        Hexadecimal string of the hash

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If unsupported algorithm
    """
    filepath = Path(filepath)

    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    if algorithm not in ["md5", "sha256"]:
        raise ValueError(f"Unsupported algorithm: {algorithm}. Use 'md5' or 'sha256'")

    hash_func = hashlib.md5() if algorithm == "md5" else hashlib.sha256()

    with open(filepath, "rb") as f:
        # Read in chunks to handle large files efficiently
        for chunk in iter(lambda: f.read(8192), b""):
            hash_func.update(chunk)

    return hash_func.hexdigest()


def get_file_metadata(filepath: Union[str, Path]) -> Dict[str, Any]:
    """Get comprehensive metadata for a file.

    Args:
        filepath: Path to the file

    Returns:
        Dictionary containing file metadata
    """
    filepath = Path(filepath)
    stat = filepath.stat()

    return {
        "hash_md5": compute_file_hash(filepath, "md5"),
        "hash_sha256": compute_file_hash(filepath, "sha256"),
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    }


def create_manifest(
    directory: Union[str, Path],
    pattern: str = "*",
    manifest_path: Optional[Union[str, Path]] = None,
    base_directory: Optional[Union[str, Path]] = None
) -> Dict[str, Any]:
    """Create or update a file manifest for a directory.

    Args:
        directory: Directory to scan for files
        pattern: Glob pattern to match files (default: "*")
        manifest_path: Path to save manifest (default: "manifest.json" in base_directory)
        base_directory: Base directory for relative paths (default: directory)

    Returns:
        Manifest dictionary
    """
    directory = Path(directory)
    base_directory = Path(base_directory) if base_directory else directory

    if manifest_path is None:
        manifest_path = base_directory / "manifest.json"

    # Load existing manifest if it exists
    manifest = load_manifest(manifest_path) if manifest_path.exists() else {
        "version": "1.0",
        "created": datetime.now(timezone.utc).isoformat(),
        "files": {}
    }

    # Update timestamp
    manifest["updated"] = datetime.now(timezone.utc).isoformat()

    # Find all matching files
    files = list(directory.glob(pattern))
    files.extend(list(directory.rglob(f"**/{pattern}")))  # Also match in subdirectories

    # Process each file
    for filepath in files:
        if filepath.is_file():
            # Get relative path from base directory
            try:
                relative_path = filepath.relative_to(base_directory)
            except ValueError:
                # File is not under base_directory, use absolute path
                relative_path = filepath

            manifest["files"][str(relative_path)] = get_file_metadata(filepath)

    # Save manifest
    save_manifest(manifest, manifest_path)

    return manifest


def load_manifest(manifest_path: Union[str, Path]) -> Dict[str, Any]:
    """Load a manifest from JSON file.

    Args:
        manifest_path: Path to the manifest file

    Returns:
        Manifest dictionary

    Raises:
        FileNotFoundError: If manifest file doesn't exist
        json.JSONDecodeError: If manifest is invalid JSON
    """
    manifest_path = Path(manifest_path)

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(manifest: Dict[str, Any], manifest_path: Union[str, Path]) -> None:
    """Save a manifest to JSON file.

    Args:
        manifest: Manifest dictionary to save
        manifest_path: Path where to save the manifest
    """
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def has_file_changed(
    filepath: Union[str, Path],
    manifest: Dict[str, Any],
    base_directory: Optional[Union[str, Path]] = None
) -> bool:
    """Check if a file has changed compared to the manifest.

    Args:
        filepath: Path to the file to check
        manifest: Manifest dictionary
        base_directory: Base directory for relative paths (default: parent of filepath)

    Returns:
        True if file has changed or doesn't exist in manifest, False if unchanged
    """
    filepath = Path(filepath)

    # Determine base directory
    if base_directory is None:
        base_directory = filepath.parent
    else:
        base_directory = Path(base_directory)

    # Get relative path
    try:
        relative_path = filepath.relative_to(base_directory)
    except ValueError:
        relative_path = filepath

    relative_path_str = str(relative_path)

    # Check if file exists in manifest
    if relative_path_str not in manifest.get("files", {}):
        return True  # File not in manifest = considered changed

    # Get current metadata
    if not filepath.exists():
        return True  # File doesn't exist = considered changed

    current_metadata = get_file_metadata(filepath)
    stored_metadata = manifest["files"][relative_path_str]

    # Compare hashes (prefer MD5 for speed, fall back to SHA256)
    if current_metadata.get("hash_md5") != stored_metadata.get("hash_md5"):
        return True

    # Also check SHA256 if available
    if ("hash_sha256" in current_metadata and "hash_sha256" in stored_metadata and
        current_metadata["hash_sha256"] != stored_metadata["hash_sha256"]):
        return True

    return False  # File unchanged


def get_files_to_update(
    directory: Union[str, Path],
    manifest: Dict[str, Any],
    pattern: str = "*",
    base_directory: Optional[Union[str, Path]] = None
) -> List[str]:
    """Get list of files in a directory that have changed according to the manifest.

    Args:
        directory: Directory to scan
        manifest: Manifest dictionary
        pattern: Glob pattern to match files
        base_directory: Base directory for relative paths

    Returns:
        List of file paths (relative to base_directory) that have changed
    """
    directory = Path(directory)
    base_directory = Path(base_directory) if base_directory else directory

    changed_files = []

    # Find all matching files
    files = list(directory.glob(pattern))
    files.extend(list(directory.rglob(f"**/{pattern}")))

    for filepath in files:
        if filepath.is_file():
            try:
                relative_path = filepath.relative_to(base_directory)
            except ValueError:
                relative_path = filepath

            if has_file_changed(filepath, manifest, base_directory):
                changed_files.append(str(relative_path))

    return changed_files


def get_unchanged_files(
    directory: Union[str, Path],
    manifest: Dict[str, Any],
    pattern: str = "*",
    base_directory: Optional[Union[str, Path]] = None
) -> List[str]:
    """Get list of files in a directory that have NOT changed according to the manifest.

    Args:
        directory: Directory to scan
        manifest: Manifest dictionary
        pattern: Glob pattern to match files
        base_directory: Base directory for relative paths

    Returns:
        List of file paths (relative to base_directory) that have not changed
    """
    directory = Path(directory)
    base_directory = Path(base_directory) if base_directory else directory

    unchanged_files = []

    # Find all matching files
    files = list(directory.glob(pattern))
    files.extend(list(directory.rglob(f"**/{pattern}")))

    for filepath in files:
        if filepath.is_file():
            try:
                relative_path = filepath.relative_to(base_directory)
            except ValueError:
                relative_path = filepath

            if not has_file_changed(filepath, manifest, base_directory):
                unchanged_files.append(str(relative_path))

    return unchanged_files
