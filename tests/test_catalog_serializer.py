"""Tests for the catalog yaml serializer.

After the 2026-05-14 audience-filter drop, this is a thin wrapper around
`yaml.safe_dump` / `yaml.safe_load` plus `Metadata.to_catalog_entry()` (which
strips CATALOG_EXCLUDED_PATHS). The two-tier emission and `last_synced_at`
machinery are gone — these tests pin the simpler shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mintd._catalog_serializer import deserialize, serialize
from mintd.model import CATALOG_EXCLUDED_PATHS, Metadata

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _full_metadata() -> Metadata:
    """Load the minimal fixture and populate storage + data_products so the
    round-trip assertions have content for the formerly-filtered fields.
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
    data["data_products"] = {
        "primary": "out.parquet",
        "outputs": [
            {"path": "out.parquet", "description": "primary output",
             "primary": True, "last_published": "2026-05-01"},
        ],
    }
    return Metadata.model_validate(data)


def test_round_trip_preserves_full_catalog_subset():
    """serialize → deserialize yields a CatalogEntry whose dump equals
    `metadata.to_catalog_entry().model_dump()`. Datetimes round-trip through
    iso-strings; normalize via json."""
    m = _full_metadata()
    entry = deserialize(serialize(m))
    assert _round(entry.model_dump()) == _round(m.to_catalog_entry().model_dump())


def test_round_trip_preserves_storage():
    """storage.* is now in the catalog yaml."""
    m = _full_metadata()
    entry = deserialize(serialize(m))
    dumped = entry.model_dump()
    assert dumped["storage"]["bucket"] == "my-bucket"
    assert dumped["storage"]["dvc"]["remote_name"] == "myremote"


def test_round_trip_preserves_data_products():
    """data_products.outputs is now in the catalog yaml."""
    m = _full_metadata()
    entry = deserialize(serialize(m))
    dumped = entry.model_dump()
    assert dumped["data_products"]["primary"] == "out.parquet"
    assert dumped["data_products"]["outputs"][0]["path"] == "out.parquet"


def test_excluded_paths_absent_from_yaml():
    """CATALOG_EXCLUDED_PATHS (mint-internal provenance) are stripped before
    serialization."""
    m = _full_metadata()
    raw = yaml.safe_load(serialize(m))
    for path in CATALOG_EXCLUDED_PATHS:
        assert path not in raw, f"{path} should be excluded but appears: {raw.get(path)}"


def test_yaml_is_flat_no_advisory_block():
    """The advisory: wrapper from slice 3 is gone — yaml is flat."""
    raw = yaml.safe_load(serialize(_full_metadata()))
    assert "advisory" not in raw
    assert "last_synced_at" not in raw


def _round(obj):
    """Normalize through json so datetime vs iso-string doesn't matter."""
    return json.loads(json.dumps(obj, default=str))
