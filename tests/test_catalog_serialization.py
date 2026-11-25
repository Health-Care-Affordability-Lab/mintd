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

import pytest
from pydantic import ValidationError

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
    """Every top-level field on Metadata except CATALOG_EXCLUDED_PATHS shows up,
    under the key the file uses (its alias, where it has one)."""
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["schema"] = {"location": "schemas/v1/schema.json"}
    m = Metadata.model_validate(data)
    dumped = m.to_catalog_entry().model_dump()
    expected = {f.alias or n for n, f in Metadata.model_fields.items()} - CATALOG_EXCLUDED_PATHS
    for field in expected:
        assert field in dumped, f"{field} should be in CatalogEntry but missing"


def test_catalog_entry_names_where_the_schema_is_committed():
    """A product's `schema` key reaches its catalog entry and the file publish
    writes, under that name, with the keys the catalog site reads.

    Mutations: drop `serialize_by_alias`, or SchemaRef's `extra="allow"` -> red.
    """
    ref = {"location": "schemas/v1/schema.json", "version": "1.0.0"}
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["schema"] = ref
    m = Metadata.model_validate(data)

    assert m.to_catalog_entry().model_dump()["schema"] == ref
    written = json.loads(m.model_dump_json())  # what publish writes back to metadata.json
    assert written["schema"] == ref
    assert "schema_" not in written


def test_metadata_without_a_schema_gains_no_schema_key():
    """A product with no `schema` dumps exactly as before: no key, never
    `"schema": null`. A `schema` that names no location is invalid.

    Mutations: drop `exclude_if`, or make `location` optional -> red.
    """
    m = Metadata.from_json_file(MINIMAL)
    assert "schema" not in json.loads(m.model_dump_json())
    assert "schema" not in json.loads(m.model_dump_json(by_alias=True, exclude_none=False))
    assert "schema" not in m.to_catalog_entry().model_dump()

    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["schema"] = {}
    with pytest.raises(ValidationError) as ei:
        Metadata.model_validate(data)
    assert ("schema", "location") in [e["loc"] for e in ei.value.errors()]


@pytest.mark.parametrize("location", ["--output=/tmp/x", "/etc/passwd", "../outside.json", "a/../../b", "a\\b", "", "C:/x.json"])
def test_schema_location_must_be_a_path_inside_the_repo(location: str) -> None:
    """Catalog builders hand `schema.location` to git, so it must be a path
    inside the repo, never an option, an absolute path or one that climbs out.

    Mutation: drop the location validator -> red.
    """
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["schema"] = {"location": location}
    with pytest.raises(ValidationError) as ei:
        Metadata.model_validate(data)
    assert ("schema", "location") in [e["loc"] for e in ei.value.errors()]



def _with_output(path: str) -> dict:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"] = {"primary": None, "outputs": [{"path": path, "description": "d", "primary": True}]}
    return data


@pytest.mark.parametrize(
    "path", ["/data/final/", "../outside/", "data/../../b", "data\\final", "", "-rf", "--output=x", "C:/data/", "a\x00b"]
)
def test_an_output_path_must_be_a_path_inside_the_repo(path: str) -> None:
    """A catalog rebuild hands each declared output path to git and dvc, so
    it must be a relative posix path inside the repo: never absolute, on a
    drive, climbing out, written with a backslash, empty or read as an option.

    Mutations: drop the validator from `DataProductOutput.path`, or drop any
    one refused shape from `_a_path_inside_the_repo` -> red.
    """
    with pytest.raises(ValidationError) as ei:
        Metadata.model_validate(_with_output(path))
    assert ("data_products", "outputs", 0, "path") in [e["loc"] for e in ei.value.errors()]


@pytest.mark.parametrize("path", ["data/final/", "./data/final", "data/final/a.csv", "data/a:b.csv"])
def test_an_output_path_inside_the_repo_loads_unchanged(path: str) -> None:
    """The refusal is narrow: a folder, a `./` path and a colon inside a name
    all load as written."""
    assert Metadata.model_validate(_with_output(path)).data_products.outputs[0].path == path

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
