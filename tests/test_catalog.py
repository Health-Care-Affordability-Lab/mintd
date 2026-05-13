"""Tests for InMemoryCatalogClient — the four-method interface.

These tests pin the register / update / fetch / list behavior. They use the
in-memory client; the git-backed GitCatalogClient lands in slice 3 and reuses
this same test shape via the CatalogClient Protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mintd.catalog import (
    CatalogAlreadyExists,
    CatalogFilter,
    CatalogNotFound,
    FieldChange,
    InMemoryCatalogClient,
    RegisterResult,
    UpdateResult,
)
from mintd.model import Metadata


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _load_metadata(
    name: str = "test_project",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> Metadata:
    """Load the minimal fixture as a Metadata instance, optionally renaming
    project.name and mutating the dict before validation.
    """
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = name
    if mutate is not None:
        mutate(data)
    return Metadata.model_validate(data)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

def test_register_stores_entry():
    """register(metadata) on a fresh client stores the entry; fetch by name
    returns the projected CatalogEntry."""
    client = InMemoryCatalogClient()
    m = _load_metadata(name="data_alpha")
    result = client.register(m)
    assert isinstance(result, RegisterResult)
    assert result.name == "data_alpha"
    assert result.dry_run is False
    assert client.fetch("data_alpha") == m.to_catalog_entry()


def test_register_duplicate_raises():
    """A second register() with the same project.name raises CatalogAlreadyExists."""
    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="dup"))
    with pytest.raises(CatalogAlreadyExists):
        client.register(_load_metadata(name="dup"))


def test_register_dry_run_does_not_mutate():
    """register(metadata, dry_run=True) returns a RegisterResult with dry_run=True
    but a subsequent fetch raises CatalogNotFound."""
    client = InMemoryCatalogClient()
    result = client.register(_load_metadata(name="ghost"), dry_run=True)
    assert result.dry_run is True
    assert result.name == "ghost"
    with pytest.raises(CatalogNotFound):
        client.fetch("ghost")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def test_fetch_missing_raises_not_found():
    """fetch on a name that was never registered raises CatalogNotFound."""
    client = InMemoryCatalogClient()
    with pytest.raises(CatalogNotFound):
        client.fetch("nonexistent")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def test_update_returns_field_changes():
    """update() with a mutated CATALOG field returns an UpdateResult with one
    FieldChange describing the diff (field_path / before / after).

    Uses metadata.description (USER-owned, Audience.CATALOG).
    """
    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="proj"))

    def change_desc(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = "updated description"

    result = client.update(_load_metadata(name="proj", mutate=change_desc))
    assert isinstance(result, UpdateResult)
    assert result.name == "proj"
    assert result.dry_run is False
    assert len(result.changes) == 1
    change = result.changes[0]
    assert isinstance(change, FieldChange)
    assert change.field_path == "metadata.description"
    assert change.before == ""
    assert change.after == "updated description"


def test_update_missing_raises_not_found():
    """update() on a name that was never registered raises CatalogNotFound."""
    client = InMemoryCatalogClient()
    with pytest.raises(CatalogNotFound):
        client.update(_load_metadata(name="never_registered"))


def test_update_dry_run_returns_changes_without_mutating():
    """update(metadata, dry_run=True) returns the would-be UpdateResult but a
    subsequent fetch shows the OLD entry."""
    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="proj"))
    original = client.fetch("proj")

    def change_desc(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = "would-be"

    result = client.update(_load_metadata(name="proj", mutate=change_desc), dry_run=True)
    assert result.dry_run is True
    assert len(result.changes) == 1
    assert client.fetch("proj") == original


def test_update_data_products_roundtrips():
    """Adding an output to data_products and calling update() persists the change.

    This is the structural fix for today's registry-update data_products
    writeback bug: register and update both go through the same to_catalog_entry()
    projection, so the field can't be silently dropped on update.
    """
    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="proj"))

    def add_output(data: dict[str, Any]) -> None:
        data["data_products"]["outputs"].append({
            "path": "out.parquet",
            "description": "primary output",
            "primary": True,
            "last_published": "2026-05-01",
        })

    updated = _load_metadata(name="proj", mutate=add_output)
    client.update(updated)
    fetched = client.fetch("proj")
    assert fetched == updated.to_catalog_entry()
    dumped = fetched.model_dump()
    assert dumped["data_products"]["outputs"][0]["path"] == "out.parquet"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_list_returns_all_entries():
    """list() with no filter returns every registered entry."""
    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="a"))
    client.register(_load_metadata(name="b"))
    client.register(_load_metadata(name="c"))
    assert len(client.list()) == 3


def test_list_filter_by_project_type():
    """list(filter=CatalogFilter(project_type='data')) returns only data projects."""
    def set_type(t: str) -> Callable[[dict[str, Any]], None]:
        def _m(data: dict[str, Any]) -> None:
            data["project"]["type"] = t
        return _m

    client = InMemoryCatalogClient()
    client.register(_load_metadata(name="d1", mutate=set_type("data")))
    client.register(_load_metadata(name="d2", mutate=set_type("data")))
    client.register(_load_metadata(name="c1", mutate=set_type("code")))

    entries = client.list(filter=CatalogFilter(project_type="data"))
    assert len(entries) == 2
    for e in entries:
        assert e.model_dump()["project"]["type"] == "data"


def test_list_empty_client_returns_empty():
    """list() on a fresh client returns []."""
    assert InMemoryCatalogClient().list() == []
