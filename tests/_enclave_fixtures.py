"""Shared enclave-manifest test fixtures.

Promoted out of `tests/test_check.py` (slice-8 walker tests) so the check,
publish and CLI suites all stage the same manifest and seed the same catalog
entry. A second hand-rolled `InMemoryCatalogClient` holding `provider-xw` is
the thing this module exists to prevent.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mintd.catalog import InMemoryCatalogClient
from mintd.model import Metadata

FIXTURES = Path(__file__).parent / "fixtures"
ENCLAVE_FIXTURE = FIXTURES / "enclave_manifest_v2_minimal.yaml"
MINIMAL_METADATA = FIXTURES / "metadata_v2_minimal.json"


def stage_enclave_manifest(tmp_path: Path) -> Path:
    """Copy the minimal enclave manifest fixture into tmp_path."""
    dest = tmp_path / "enclave_manifest.yaml"
    shutil.copy(ENCLAVE_FIXTURE, dest)
    return dest


def client_with_provider_xw() -> InMemoryCatalogClient:
    """A catalog client holding the one repo the minimal manifest approves."""
    client = InMemoryCatalogClient()
    data = json.loads(MINIMAL_METADATA.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    client.register(Metadata.model_validate(data))
    return client
