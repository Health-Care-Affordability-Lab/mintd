"""Test to verify storage.prefix uses full_project_name (with type prefix).

This test specifically verifies the bug described where storage.prefix was using
project.name instead of project.full_name, causing inconsistency with DVC remote URL.

Bug description:
- storage.prefix was using project.name (without the type prefix)
- storage.dvc.remote_url was using project.full_name (with the type prefix, e.g. prj_)
- This caused mintd data push to upload to the wrong S3 path

Expected behavior:
- Both storage.prefix and storage.dvc.remote_url should use full_project_name
"""

import json
import tempfile
from pathlib import Path

import pytest

from mintd.api import create_project


class TestStoragePrefixBug:
    """Test that storage.prefix correctly uses full_project_name, not bare name."""

    def test_storage_prefix_uses_full_name_not_bare_name(self):
        """Verify storage.prefix includes the type prefix (data_, prj_, etc)."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a project with a specific name
            project_name = "my-project"
            result = create_project(
                project_type="project",
                name=project_name,
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="private",
                team="all-lab",
            )

            # Load the generated metadata.json
            metadata_path = result.path / "metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            # The bug: storage.prefix was "lab/my-project/" instead of "lab/prj_my-project/"
            storage_prefix = metadata["storage"]["prefix"]
            dvc_remote_url = metadata["storage"]["dvc"]["remote_url"]

            # Assert that storage.prefix contains the full name with prefix
            assert "prj_my-project" in storage_prefix, (
                f"storage.prefix should contain 'prj_my-project', got: {storage_prefix}"
            )

            # Ensure it doesn't use the bare name
            assert storage_prefix != "lab/my-project/", (
                f"storage.prefix should NOT be 'lab/my-project/', got: {storage_prefix}"
            )

            # Expected correct value
            assert storage_prefix == "lab/prj_my-project/", (
                f"storage.prefix should be 'lab/prj_my-project/', got: {storage_prefix}"
            )

            # Verify consistency between prefix and DVC URL
            bucket = metadata["storage"]["bucket"]
            expected_dvc_url = f"s3://{bucket}/{storage_prefix}"
            assert dvc_remote_url == expected_dvc_url, (
                f"DVC URL should match storage.prefix: {dvc_remote_url} != {expected_dvc_url}"
            )

    def test_all_project_types_use_full_name_in_storage_prefix(self):
        """Test that all project types use full_name in storage.prefix."""
        test_cases = [
            ("data", "test-data", "data_test-data"),
            ("project", "test-proj", "prj_test-proj"),
            ("enclave", "test-enc", "enclave_test-enc"),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            for project_type, name, expected_full_name in test_cases:
                result = create_project(
                    project_type=project_type,
                    name=name,
                    language="python",
                    path=tmp,
                    init_git=False,
                    init_dvc=False,
                    classification="private",
                    team="all-lab",
                )

                metadata_path = result.path / "metadata.json"
                with open(metadata_path) as f:
                    metadata = json.load(f)

                storage_prefix = metadata["storage"]["prefix"]

                # Verify the full name is used
                assert expected_full_name in storage_prefix, (
                    f"For {project_type} project, storage.prefix should contain "
                    f"'{expected_full_name}', got: {storage_prefix}"
                )

    def test_example_from_bug_report(self):
        """Test the exact example from the bug report."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a project matching the bug report example
            result = create_project(
                project_type="project",
                name="my-project",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="private",
                team="all-lab",
            )

            metadata_path = result.path / "metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            # From bug report - WRONG behavior was:
            # "prefix": "lab/my-project/",
            # "remote_url": "s3://bucket-name/lab/prj_my-project/"

            # CORRECT behavior should be:
            # "prefix": "lab/prj_my-project/",
            # "remote_url": "s3://bucket-name/lab/prj_my-project/"

            storage_prefix = metadata["storage"]["prefix"]
            dvc_remote_url = metadata["storage"]["dvc"]["remote_url"]

            # Assert correct behavior
            assert storage_prefix == "lab/prj_my-project/", (
                f"Bug reproduced! storage.prefix is '{storage_prefix}' "
                f"but should be 'lab/prj_my-project/'"
            )

            # Both should use the same path after the bucket
            bucket = metadata["storage"]["bucket"]
            assert dvc_remote_url == f"s3://{bucket}/lab/prj_my-project/", (
                f"DVC remote URL should be 's3://{bucket}/lab/prj_my-project/', "
                f"got: {dvc_remote_url}"
            )


class TestStoragePrefixAllClassifications:
    """Test storage.prefix uses full_name across all classifications."""

    def test_public_classification_uses_full_name(self):
        """Public projects should use pub/{full_name}/."""
        with tempfile.TemporaryDirectory() as tmp:
            result = create_project(
                project_type="data",
                name="public-dataset",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="public",
                team="all-lab",
            )

            metadata_path = result.path / "metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            storage_prefix = metadata["storage"]["prefix"]
            assert storage_prefix == "pub/data_public-dataset/", (
                f"Public project storage.prefix should be 'pub/data_public-dataset/', "
                f"got: {storage_prefix}"
            )

    def test_contract_classification_uses_full_name(self):
        """Contract projects should use contract/{slug}/{full_name}/."""
        with tempfile.TemporaryDirectory() as tmp:
            result = create_project(
                project_type="data",
                name="contract-data",
                language="python",
                path=tmp,
                init_git=False,
                init_dvc=False,
                classification="contract",
                team="all-lab",
                contract_slug="cms-2024",
                contract_info="Test contract",
            )

            metadata_path = result.path / "metadata.json"
            with open(metadata_path) as f:
                metadata = json.load(f)

            storage_prefix = metadata["storage"]["prefix"]
            assert storage_prefix == "contract/cms-2024/data_contract-data/", (
                f"Contract project storage.prefix should be "
                f"'contract/cms-2024/data_contract-data/', got: {storage_prefix}"
            )