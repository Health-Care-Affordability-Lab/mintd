"""Metadata validation utilities."""

from typing import Dict, List, Tuple


def validate_metadata(metadata: dict) -> Tuple[bool, List[str]]:
    """Validate metadata completeness and correctness.

    Args:
        metadata: The metadata dictionary to validate

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []

    # Check required top-level sections
    required_sections = ["project", "storage", "governance"]
    for section in required_sections:
        if section not in metadata:
            errors.append(f"Missing required section: {section}")

    # Check storage configuration
    storage = metadata.get("storage", {})
    if not storage.get("bucket"):
        errors.append("storage.bucket is empty - bucket configuration required")
    if not storage.get("endpoint"):
        # Only warn for endpoint, as it might be standard AWS S3
        pass  # endpoint can be empty for standard AWS S3
    if not storage.get("prefix"):
        errors.append("storage.prefix is empty")

    # Check DVC configuration
    dvc = storage.get("dvc", {})
    if not dvc.get("remote_url") and not dvc.get("remote_name"):
        # Both empty suggests DVC not configured
        pass  # This is okay if DVC is not being used
    elif dvc.get("remote_name") and not dvc.get("remote_url"):
        errors.append("storage.dvc.remote_name is set but remote_url is empty")

    # Check governance
    governance = metadata.get("governance", {})
    classification = governance.get("classification", "")
    valid_classifications = ["public", "private", "contract"]

    if not classification:
        errors.append("governance.classification is empty")
    elif classification not in valid_classifications:
        errors.append(f"Invalid classification: '{classification}'. Must be one of: {valid_classifications}")

    # Check for contract_slug if classification is contract
    if classification == "contract":
        storage_prefix = storage.get("prefix", "")
        if "unknown-contract" in storage_prefix:
            errors.append("Contract classification has invalid contract_slug (using 'unknown-contract')")
        elif not storage_prefix or not storage_prefix.startswith("contract/"):
            errors.append("Contract classification but storage prefix doesn't follow contract pattern")

    # Check project info
    project = metadata.get("project", {})
    if not project.get("name"):
        errors.append("project.name is empty")
    if not project.get("type"):
        errors.append("project.type is empty")

    # Check team configuration
    ownership = metadata.get("ownership", {})
    if not ownership.get("team"):
        errors.append("ownership.team is empty")

    return len(errors) == 0, errors


def check_metadata_completeness(metadata: dict, config: dict) -> List[str]:
    """Check for missing required fields based on configuration.

    Args:
        metadata: The metadata dictionary to check
        config: The mintd configuration dictionary

    Returns:
        List of error messages for missing/incomplete fields
    """
    errors = []

    # Check storage section against config requirements
    storage = metadata.get("storage", {})
    storage_config = config.get("storage", {})

    # Check if bucket is required and present
    if not storage.get("bucket") and storage_config.get("require_bucket", True):
        errors.append("storage.bucket is required but empty")

    # Check if endpoint matches config
    if storage_config.get("endpoint") and storage.get("endpoint") != storage_config.get("endpoint"):
        errors.append(f"storage.endpoint doesn't match config: expected '{storage_config.get('endpoint')}'")

    # Check registry org
    registry_config = config.get("registry", {})
    if registry_config.get("org"):
        repo_section = metadata.get("repository", {})
        github_url = repo_section.get("github_url", "")
        expected_org = registry_config.get("org")
        if expected_org not in github_url:
            errors.append(f"repository.github_url doesn't contain configured org '{expected_org}'")

    # Check team configuration
    ownership = metadata.get("ownership", {})
    defaults = config.get("defaults", {})
    if not ownership.get("team") and not defaults.get("team"):
        errors.append("ownership.team is empty and no default configured")

    return errors


def validate_classification(classification: str) -> bool:
    """Validate that classification is one of the allowed values.

    Args:
        classification: The classification value to validate

    Returns:
        True if valid, False otherwise
    """
    return classification in ["public", "private", "contract"]


def validate_storage_prefix(classification: str, prefix: str, team: str = None,
                           contract_slug: str = None) -> Tuple[bool, str]:
    """Validate that storage prefix matches the expected pattern for the classification.

    Args:
        classification: The data classification
        prefix: The storage prefix to validate
        team: The team name (for private classification)
        contract_slug: The contract slug (for contract classification)

    Returns:
        Tuple of (is_valid, error_message)
    """
    if classification == "public":
        # Expected: public/{name}/
        if not prefix.startswith("public/"):
            return False, f"Public classification should have prefix starting with 'public/', got '{prefix}'"
    elif classification == "contract":
        # Expected: contract/{contract_slug}/{name}/
        if not prefix.startswith("contract/"):
            return False, f"Contract classification should have prefix starting with 'contract/', got '{prefix}'"
        if contract_slug and f"contract/{contract_slug}/" not in prefix:
            return False, f"Contract prefix should contain slug '{contract_slug}', got '{prefix}'"
        if "unknown-contract" in prefix:
            return False, "Contract classification using invalid 'unknown-contract' slug"
    elif classification == "private":
        # Expected: lab/{team}/{name}/
        if not prefix.startswith("lab/"):
            return False, f"Private classification should have prefix starting with 'lab/', got '{prefix}'"
        if team and f"lab/{team}/" not in prefix:
            return False, f"Private prefix should contain team '{team}', got '{prefix}'"
    else:
        return False, f"Unknown classification: {classification}"

    return True, ""
