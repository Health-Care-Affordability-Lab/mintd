"""Tests for the manifest module."""

import json
import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from mintd.manifest import (
    compute_file_hash,
    get_file_metadata,
    create_manifest,
    load_manifest,
    save_manifest,
    has_file_changed,
    get_files_to_update,
    get_unchanged_files,
)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_file(temp_dir):
    """Create a sample file for testing."""
    filepath = temp_dir / "test_file.txt"
    filepath.write_text("Hello, World!")
    return filepath


@pytest.fixture
def sample_directory(temp_dir):
    """Create a directory with multiple files."""
    # Create files
    (temp_dir / "file1.txt").write_text("Content 1")
    (temp_dir / "file2.txt").write_text("Content 2")
    (temp_dir / "data.csv").write_text("col1,col2\n1,2")

    # Create subdirectory
    subdir = temp_dir / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("Nested content")

    return temp_dir


class TestComputeFileHash:
    """Tests for compute_file_hash function."""

    def test_compute_md5_hash(self, sample_file):
        """Test computing MD5 hash."""
        result = compute_file_hash(sample_file, "md5")
        assert isinstance(result, str)
        assert len(result) == 32  # MD5 hex length

    def test_compute_sha256_hash(self, sample_file):
        """Test computing SHA256 hash."""
        result = compute_file_hash(sample_file, "sha256")
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 hex length

    def test_hash_consistency(self, sample_file):
        """Test that hashing the same file produces consistent results."""
        hash1 = compute_file_hash(sample_file, "md5")
        hash2 = compute_file_hash(sample_file, "md5")
        assert hash1 == hash2

    def test_different_algorithms_different_hashes(self, sample_file):
        """Test that different algorithms produce different hashes."""
        md5_hash = compute_file_hash(sample_file, "md5")
        sha256_hash = compute_file_hash(sample_file, "sha256")
        assert md5_hash != sha256_hash

    def test_file_not_found(self, temp_dir):
        """Test error when file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            compute_file_hash(temp_dir / "nonexistent.txt")

    def test_unsupported_algorithm(self, sample_file):
        """Test error for unsupported algorithm."""
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            compute_file_hash(sample_file, "sha512")

    def test_hash_string_path(self, sample_file):
        """Test hashing with string path."""
        result = compute_file_hash(str(sample_file), "md5")
        assert isinstance(result, str)


class TestGetFileMetadata:
    """Tests for get_file_metadata function."""

    def test_metadata_includes_hashes(self, sample_file):
        """Test that metadata includes both hashes."""
        metadata = get_file_metadata(sample_file)
        assert "hash_md5" in metadata
        assert "hash_sha256" in metadata

    def test_metadata_includes_size(self, sample_file):
        """Test that metadata includes file size."""
        metadata = get_file_metadata(sample_file)
        assert "size_bytes" in metadata
        assert metadata["size_bytes"] == len("Hello, World!")

    def test_metadata_includes_modified(self, sample_file):
        """Test that metadata includes modification time."""
        metadata = get_file_metadata(sample_file)
        assert "modified" in metadata
        # Check ISO format
        assert "T" in metadata["modified"]


class TestCreateManifest:
    """Tests for create_manifest function."""

    def test_create_basic_manifest(self, sample_directory):
        """Test creating a basic manifest."""
        manifest = create_manifest(sample_directory, "*.txt")

        assert "version" in manifest
        assert "created" in manifest
        assert "updated" in manifest
        assert "files" in manifest

    def test_manifest_includes_files(self, sample_directory):
        """Test that manifest includes matching files."""
        manifest = create_manifest(sample_directory, "*.txt")

        # Should find txt files
        file_names = list(manifest["files"].keys())
        assert any("file1.txt" in f for f in file_names)
        assert any("file2.txt" in f for f in file_names)

    def test_manifest_saves_to_file(self, sample_directory):
        """Test that manifest is saved to file."""
        manifest_path = sample_directory / "test_manifest.json"
        create_manifest(sample_directory, "*.txt", manifest_path=manifest_path)

        assert manifest_path.exists()

        with open(manifest_path) as f:
            saved = json.load(f)
        assert saved["version"] == "1.0"

    def test_manifest_with_custom_base_directory(self, sample_directory):
        """Test manifest with custom base directory."""
        subdir = sample_directory / "subdir"
        manifest = create_manifest(subdir, "*.txt", base_directory=sample_directory)

        # Should have relative paths from sample_directory
        assert "files" in manifest

    def test_manifest_updates_existing(self, sample_directory):
        """Test that creating manifest updates existing one."""
        manifest_path = sample_directory / "manifest.json"

        # Create initial manifest
        manifest1 = create_manifest(sample_directory, "*.txt", manifest_path=manifest_path)
        created = manifest1["created"]

        # Update manifest
        manifest2 = create_manifest(sample_directory, "*.txt", manifest_path=manifest_path)

        assert manifest2["created"] == created  # Created time preserved
        assert "updated" in manifest2


class TestLoadManifest:
    """Tests for load_manifest function."""

    def test_load_existing_manifest(self, temp_dir):
        """Test loading an existing manifest."""
        manifest_path = temp_dir / "manifest.json"
        original = {"version": "1.0", "files": {}}

        with open(manifest_path, "w") as f:
            json.dump(original, f)

        loaded = load_manifest(manifest_path)
        assert loaded == original

    def test_load_nonexistent_manifest(self, temp_dir):
        """Test error when loading nonexistent manifest."""
        with pytest.raises(FileNotFoundError):
            load_manifest(temp_dir / "nonexistent.json")


class TestSaveManifest:
    """Tests for save_manifest function."""

    def test_save_manifest_creates_file(self, temp_dir):
        """Test that save_manifest creates the file."""
        manifest_path = temp_dir / "new_manifest.json"
        manifest = {"version": "1.0", "files": {}}

        save_manifest(manifest, manifest_path)

        assert manifest_path.exists()

    def test_save_manifest_creates_parent_dirs(self, temp_dir):
        """Test that save_manifest creates parent directories."""
        manifest_path = temp_dir / "nested" / "dir" / "manifest.json"
        manifest = {"version": "1.0", "files": {}}

        save_manifest(manifest, manifest_path)

        assert manifest_path.exists()

    def test_save_manifest_preserves_content(self, temp_dir):
        """Test that saved manifest matches original."""
        manifest_path = temp_dir / "manifest.json"
        manifest = {"version": "1.0", "files": {"test.txt": {"hash_md5": "abc123"}}}

        save_manifest(manifest, manifest_path)

        with open(manifest_path) as f:
            loaded = json.load(f)
        assert loaded == manifest


class TestHasFileChanged:
    """Tests for has_file_changed function."""

    def test_file_not_in_manifest(self, sample_file, temp_dir):
        """Test that file not in manifest is considered changed."""
        manifest = {"version": "1.0", "files": {}}

        result = has_file_changed(sample_file, manifest)
        assert result is True

    def test_file_unchanged(self, sample_file, temp_dir):
        """Test detecting unchanged file."""
        # Create manifest with current file state
        metadata = get_file_metadata(sample_file)
        relative_path = sample_file.relative_to(temp_dir)
        manifest = {
            "version": "1.0",
            "files": {str(relative_path): metadata}
        }

        result = has_file_changed(sample_file, manifest, base_directory=temp_dir)
        assert result is False

    def test_file_changed(self, sample_file, temp_dir):
        """Test detecting changed file."""
        # Create manifest with old state
        relative_path = sample_file.relative_to(temp_dir)
        manifest = {
            "version": "1.0",
            "files": {str(relative_path): {"hash_md5": "old_hash", "hash_sha256": "old_sha"}}
        }

        result = has_file_changed(sample_file, manifest, base_directory=temp_dir)
        assert result is True

    def test_file_deleted(self, temp_dir):
        """Test that deleted file is considered changed."""
        manifest = {
            "version": "1.0",
            "files": {"deleted.txt": {"hash_md5": "abc123"}}
        }

        result = has_file_changed(temp_dir / "deleted.txt", manifest, base_directory=temp_dir)
        assert result is True


class TestGetFilesToUpdate:
    """Tests for get_files_to_update function."""

    def test_returns_changed_files(self, sample_directory):
        """Test that only changed files are returned."""
        # Create manifest with all files
        manifest = create_manifest(sample_directory, "*")

        # Modify one file
        (sample_directory / "file1.txt").write_text("Modified content")

        changed = get_files_to_update(sample_directory, manifest, "*")

        assert any("file1.txt" in f for f in changed)

    def test_new_files_detected(self, sample_directory):
        """Test that new files are detected as needing update."""
        # Create manifest
        manifest = create_manifest(sample_directory, "*.txt")

        # Add new file
        (sample_directory / "new_file.txt").write_text("New content")

        changed = get_files_to_update(sample_directory, manifest, "*.txt")

        assert any("new_file.txt" in f for f in changed)


class TestGetUnchangedFiles:
    """Tests for get_unchanged_files function."""

    def test_returns_unchanged_files(self, sample_directory):
        """Test that unchanged files are returned."""
        # Create manifest
        manifest = create_manifest(sample_directory, "*.txt")

        # Modify one file
        (sample_directory / "file1.txt").write_text("Modified content")

        unchanged = get_unchanged_files(sample_directory, manifest, "*.txt")

        # file2.txt should be unchanged
        assert any("file2.txt" in f for f in unchanged)
        # file1.txt should not be in unchanged list
        assert not any("file1.txt" in f for f in unchanged)
