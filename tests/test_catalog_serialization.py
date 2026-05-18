"""Tests for Metadata.to_catalog_entry() — the model-level projection.

After the 2026-05-14 audience-filter drop, this projection is just
"metadata.model_dump() minus CATALOG_EXCLUDED_PATHS." The exhaustive
audience-tier suite from slice 2 has been replaced by:

  - an exclude-list test (mint-internal fields are stripped)
  - an inclusion test (everything else round-trips)
  - an idempotence test

The two-tier serializer tests live in test_catalog_serializer.py; the
parameterized client tests live in test_catalog.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from mintd.model import CATALOG_EXCLUDED_PATHS, Metadata

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def test_catalog_entry_excludes_mint_internal_paths():
    """Every path in CATALOG_EXCLUDED_PATHS is absent from to_catalog_entry()."""
    m = Metadata.from_json_file(MINIMAL)
    dumped = m.to_catalog_entry().model_dump()
    for path in CATALOG_EXCLUDED_PATHS:
        assert path not in dumped, f"{path} should be excluded from CatalogEntry but appears: {dumped[path]}"


def test_catalog_entry_includes_everything_else():
    """Every top-level field on Metadata except CATALOG_EXCLUDED_PATHS shows up."""
    m = Metadata.from_json_file(MINIMAL)
    dumped = m.to_catalog_entry().model_dump()
    expected = set(Metadata.model_fields.keys()) - CATALOG_EXCLUDED_PATHS
    for field in expected:
        assert field in dumped, f"{field} should be in CatalogEntry but missing"


def test_catalog_entry_includes_storage_when_populated():
    """storage is now in the catalog (no longer filtered out as PRODUCER_CONTRACT).

    Pin this explicitly — the slice-2 design excluded storage; the 2026-05-14
    audience-filter drop puts it back. This test would have failed under the
    pre-drop design.
    """
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["storage"] = {
        "provider": "s3",
        "bucket": "my-bucket",
        "prefix": "raw/",
        "endpoint": "https://s3.example.com",
        "versioning": True,
        "dvc": {"remote_name": "myremote"},
    }
    m = Metadata.model_validate(data)
    dumped = m.to_catalog_entry().model_dump()
    assert dumped["storage"]["bucket"] == "my-bucket"


def test_catalog_entry_includes_data_products_when_populated():
    """data_products is in the catalog. Consumers re-read producer at pin
    for authoritative values, but the catalog carries the latest publish
    state for browse-time display."""
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"] = {
        "primary": "out.parquet",
        "outputs": [
            {"path": "out.parquet", "description": "primary output",
             "primary": True, "last_published": "2026-05-01"},
        ],
    }
    m = Metadata.model_validate(data)
    dumped = m.to_catalog_entry().model_dump()
    assert dumped["data_products"]["outputs"][0]["path"] == "out.parquet"


def test_to_catalog_entry_idempotent():
    """Calling to_catalog_entry() twice on the same Metadata returns equal entries."""
    m = Metadata.from_json_file(MINIMAL)
    assert m.to_catalog_entry() == m.to_catalog_entry()
