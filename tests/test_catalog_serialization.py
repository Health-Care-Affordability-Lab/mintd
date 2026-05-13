"""Tests for Metadata.to_catalog_entry() and the audience filter.

These tests are the "does slice-1's Owner x Audience design earn its weight?"
suite. They pin that the projection uses the field annotations (not a parallel
hand-maintained list) and that the right fields make it through.
"""

from __future__ import annotations

import json
from pathlib import Path

from mintd.catalog import CatalogEntry  # noqa: F401  (imported for slice-2 surface check)
from mintd.model import Audience, Metadata, field_metadata


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


# ---------------------------------------------------------------------------
# Inclusion: every field on a CatalogEntry has Audience.CATALOG
# ---------------------------------------------------------------------------

def _leaves(obj, prefix=""):
    """Yield (dotted_path, value) for every scalar leaf in a dict/list tree.

    List indices are NOT included in the path — field_metadata looks up field
    declarations, not values, so teams[0].name and teams[1].name both
    correspond to the single declaration teams.name.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            yield from _leaves(v, path)
    elif isinstance(obj, list):
        for item in obj:
            yield from _leaves(item, prefix)
    else:
        yield prefix, obj


def test_catalog_entry_contains_only_catalog_audience_fields():
    """For every leaf field present on the projected CatalogEntry, the
    corresponding field on Metadata must be annotated Audience.CATALOG.

    This is the core acceptance criterion of slice 2: the projection is driven
    by annotations, no hand-list of catalog fields.
    """
    m = Metadata.from_json_file(MINIMAL)
    dumped = m.to_catalog_entry().model_dump()
    for path, _ in _leaves(dumped):
        _, audience = field_metadata(Metadata, path)
        assert audience == Audience.CATALOG, (
            f"{path} has audience {audience}, expected CATALOG"
        )


# ---------------------------------------------------------------------------
# Exclusion: non-CATALOG fields are absent
# ---------------------------------------------------------------------------

def test_local_audience_fields_excluded():
    """mint.version is Audience.LOCAL — must not appear in to_catalog_entry()."""
    m = Metadata.from_json_file(MINIMAL)
    entry = m.to_catalog_entry()
    dumped = entry.model_dump()
    assert "version" not in dumped.get("mint", {})
    


def test_producer_contract_fields_excluded():
    """storage.* fields are Audience.PRODUCER_CONTRACT — must not appear.

    The minimal fixture has storage=null, which would make this test pass for
    the wrong reason. Populate storage with real values, then assert the whole
    storage block is dropped on projection.
    """
    data = json.loads(MINIMAL.read_text())
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
    assert "storage" not in dumped, f"storage (PRODUCER_CONTRACT) leaked: {dumped.get('storage')}"


def test_consumer_audience_fields_excluded():
    """Any field annotated Audience.CONSUMER (slice 4+) must not appear in the
    catalog projection. Slice 2 has no CONSUMER fields surviving the filter,
    so this passes vacuously — kept as a forward guard for when slice 4 adds
    CONSUMER-audience fields to Metadata.
    """
    m = Metadata.from_json_file(MINIMAL)
    dumped = m.to_catalog_entry().model_dump()
    for path, _ in _leaves(dumped):
        _, audience = field_metadata(Metadata, path)
        assert audience != Audience.CONSUMER, f"{path} has CONSUMER audience, must not appear"


# ---------------------------------------------------------------------------
# Idempotence and round-trip
# ---------------------------------------------------------------------------

def test_to_catalog_entry_idempotent():
    """Calling to_catalog_entry() twice on the same Metadata returns equal CatalogEntries."""
    m = Metadata.from_json_file(MINIMAL)
    assert m.to_catalog_entry() == m.to_catalog_entry()



# ---------------------------------------------------------------------------
# Optional: the slice-1 retro tripwire
# ---------------------------------------------------------------------------
#
# If the implementation of to_catalog_entry() ends up requiring a hardcoded list
# of CATALOG paths, capture that pain in notes/SLICE-2-user_input.md and
# revisit the Owner x Audience design before slice 3 doubles down on it.
