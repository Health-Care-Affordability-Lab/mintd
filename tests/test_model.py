"""Tests for the Pydantic Metadata model.

These tests pin the validation behavior — they're the spec for what
metadata.json is allowed to look like, what gets rejected, and how
the `Owner` annotation on each field is introspected.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
import json
from mintd.model import Metadata, Owner, field_metadata


FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Happy-path: minimal valid fixture parses
# ---------------------------------------------------------------------------

def test_minimal_fixture_parses():
    """The minimal-valid metadata.json under tests/fixtures parses cleanly.

    Acceptance:
      - Metadata.from_json_file returns a Metadata instance.
      - schema_version is "2.0".
      - project.type is one of the four literals.
      - data_products.primary + outputs are publish-valid (slice 32).
      - storage is None (optional).
    """
    m = Metadata.from_json_file(FIXTURES / "metadata_v2_minimal.json")

    assert isinstance(m, Metadata)
    assert m.schema_version == "2.0"
    assert m.project.type in {"data", "code", "project", "enclave"}
    # Slice 32: fixture now ships with a publish-valid data_products
    # block so test_check_clean_file_returns_empty keeps passing.
    assert m.data_products.primary == "data/final/"
    assert len(m.data_products.outputs) == 1
    assert m.data_products.outputs[0].path == "data/final/"
    assert m.storage is None


# ---------------------------------------------------------------------------
# Schema version rejection
# ---------------------------------------------------------------------------

def test_wrong_schema_version_rejected():
    """schema_version != '2.0' fails validation.

    Construct a dict with schema_version="1.1" (or anything else); call
    Metadata.model_validate(); assert ValidationError raised.
    """
    
    data=json.loads((FIXTURES / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    data["schema_version"] = "1.1"    
    with pytest.raises(ValidationError) as exc:
        Metadata.model_validate(data)
    
    errors = exc.value.errors()
    assert any(err["loc"] == ("schema_version",) for err in errors)

# ---------------------------------------------------------------------------
# Field annotation introspection
# ---------------------------------------------------------------------------

def test_field_metadata_returns_owner():
    """field_metadata(Metadata, 'project.name') returns Owner.MINTD —
    mintd sets project.name at scaffold time."""
    assert field_metadata(Metadata, "project.name") == Owner.MINTD


def test_field_metadata_traverses_nested():
    """field_metadata follows dotted paths through sub-models.

    e.g., field_metadata(Metadata, 'storage.dvc.remote_name') returns
    Owner.MINTD.
    """
    assert field_metadata(Metadata, "storage.dvc.remote_name") == Owner.MINTD


def test_field_metadata_user_owned_fields():
    """metadata.description and ownership.team are USER-owned — human edits."""
    assert field_metadata(Metadata, "metadata.description") == Owner.USER
    assert field_metadata(Metadata, "ownership.team") == Owner.USER


def test_field_metadata_pipeline_owned_fields():
    """data_products.* and status.last_updated are PIPELINE-owned — auto-stamped
    by the publish flow / DVC tracking."""
    assert field_metadata(Metadata, "data_products.primary") == Owner.PIPELINE
    assert field_metadata(Metadata, "status.last_updated") == Owner.PIPELINE



# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_data_products_outputs_default_empty():
    """A Metadata constructed without data_products has outputs=[].

    Code projects often have no published outputs; the model must accept
    this as the natural default, not require an explicit empty list.
    """
    data = json.loads((FIXTURES / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    del data["data_products"]

    m = Metadata.model_validate(data)

    assert m.data_products.outputs == []
    assert m.data_products.primary is None


# ---------------------------------------------------------------------------
# Extra fields, literal enforcement, datetime coercion, all project types
# ---------------------------------------------------------------------------


def test_extra_fields_allowed_during_transition():
    """Metadata sets ConfigDict(extra='allow') — unknown top-level keys parse
    cleanly during the 1.1→2.0 transition.

    Slice 6 will tighten this to extra='forbid'; this test will need to flip
    then. Keeping it now pins the transition contract.
    """
    data = json.loads((FIXTURES / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    data["something_old"] = "from 1.1, not in the model yet"

    m = Metadata.model_validate(data)

    assert isinstance(m, Metadata)


def test_project_type_literal_rejects_unknown():
    """project.type is Literal['data','code','project','enclave'] — anything
    else fails validation with the error pinned to project.type."""
    data = json.loads((FIXTURES / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    data["project"]["type"] = "invalid"

    with pytest.raises(ValidationError) as exc:
        Metadata.model_validate(data)

    assert any(err["loc"] == ("project", "type") for err in exc.value.errors())


def test_datetime_parses_iso8601():
    """Pydantic auto-coerces ISO-8601 strings into datetime objects on fields
    typed as datetime. No custom validator needed."""
    m = Metadata.from_json_file(FIXTURES / "metadata_v2_minimal.json")

    assert isinstance(m.project.created_at, datetime)
    assert m.project.created_at.year == 2026
    assert m.project.created_at.month == 5
    assert m.project.created_at.day == 11


@pytest.mark.parametrize("project_type", ["data", "code", "project", "enclave"])
def test_all_project_types_have_same_shape(project_type: str):
    """The same Metadata model validates for every project.type literal.

    Slice 1's choice (single class, not a discriminated union) only holds if
    every type parses through the same fields. This test would fail the moment
    someone tried to gate a field behind a specific project.type.
    """
    data = json.loads((FIXTURES / "metadata_v2_minimal.json").read_text(encoding="utf-8"))
    data["project"]["type"] = project_type

    m = Metadata.model_validate(data)

    assert m.project.type == project_type
