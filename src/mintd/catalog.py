"""Catalog client — the registry-side seam.

Slice 2 introduces the `CatalogClient` interface and an in-memory implementation.
Slice 3 will add `GitCatalogClient` (writes via git + gh PR) alongside the
existing in-memory one.

The four methods (register / update / fetch / list) are the only public surface
for catalog access. Other modules (publish flow, registry update preflight, the
CLI) MUST go through this client — no direct catalog file reads or writes.

Audience filter: `Metadata.to_catalog_entry()` projects a full Metadata down to
the CATALOG-audience subset. This is the first place the slice-1 Owner x Audience
annotations earn their weight — if you find yourself maintaining a parallel
list of "which fields go to the catalog," stop and grill the design first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path  # noqa: F401  (likely used in slice 3 GitCatalogClient)
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .model import Metadata


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CatalogNotFound(Exception):
    """Raised by fetch()/update() when the named entry is not in the catalog."""


class CatalogAlreadyExists(Exception):
    """Raised by register() when the name is already in the catalog."""


# ---------------------------------------------------------------------------
# Result types (frozen dataclasses — same shape choice as CheckFinding)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldChange:
    field_path: str
    before: Any
    after: Any


@dataclass(frozen=True)
class RegisterResult:
    name: str
    dry_run: bool


@dataclass(frozen=True)
class UpdateResult:
    name: str
    changes: list[FieldChange]
    dry_run: bool


@dataclass(frozen=True)
class CatalogFilter:
    project_type: str | None = None


# ---------------------------------------------------------------------------
# CatalogEntry — projected subset of Metadata
# ---------------------------------------------------------------------------
#
# Decision (see SLICE-2.md):
#   - α (dynamic projection): `class CatalogEntry(BaseModel)` with extra='allow';
#     `to_catalog_entry()` builds the dict via the audience filter, then validates
#     it into a CatalogEntry. The shape of CatalogEntry is *derived* from Metadata.
#   - β (hand-defined): a parallel Pydantic class with the catalog fields spelled
#     out. Two sources of truth → drift risk; β makes the slice-1 annotations
#     decorative.
#
# Strong rec: α. Reach for β only if α turns out to break Pydantic type-checking.
class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# CatalogClient interface (Protocol — structural typing)
# ---------------------------------------------------------------------------

class CatalogClient(Protocol):
    """Structural interface for catalog access. InMemoryCatalogClient implements
    it today; slice 3's GitCatalogClient will too — no inheritance, just shape.
    """

    def register(self, metadata: Metadata, *, dry_run: bool = False) -> RegisterResult: ...
    def update(self, metadata: Metadata, *, dry_run: bool = False) -> UpdateResult: ...
    def fetch(self, name: str) -> CatalogEntry: ...
    def list(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]: ...


# ---------------------------------------------------------------------------
# InMemoryCatalogClient — slice 2's only concrete implementation
# ---------------------------------------------------------------------------

class InMemoryCatalogClient:
    """Catalog backed by an in-process dict. Used in tests and as the in-memory
    store before flush in slice-3's GitCatalogClient.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}

    def register(self, metadata: Metadata, *, dry_run: bool = False) -> RegisterResult:
        name = metadata.project.name
        if name in self._entries:
            raise CatalogAlreadyExists(name)
        entry = metadata.to_catalog_entry()
        if not dry_run:
            self._entries[name] = entry
        return RegisterResult(name=name, dry_run=dry_run)

    def update(self, metadata: Metadata, *, dry_run: bool = False) -> UpdateResult:
        name = metadata.project.name
        if name not in self._entries:
            raise CatalogNotFound(name)
        new_entry = metadata.to_catalog_entry()
        old_entry = self._entries[name]
        changes = _diff_entries(old_entry, new_entry)
        if not dry_run:
            self._entries[name] = new_entry
        return UpdateResult(name=name, changes=changes, dry_run=dry_run)

    def fetch(self, name: str) -> CatalogEntry:
        if name not in self._entries:
            raise CatalogNotFound(name)
        return self._entries[name]

    def list(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]:
        entries = list(self._entries.values())
        if filter is None or filter.project_type is None:
            return entries
        return [e for e in entries if e.model_dump().get("project", {}).get("type") == filter.project_type]


def _diff_entries(old: CatalogEntry, new: CatalogEntry) -> list[FieldChange]:
    """Compute leaf-level FieldChanges between two CatalogEntry dumps.
    Dicts recurse; lists compare as wholes.
    """
    return _dict_diff(old.model_dump(), new.model_dump())


def _dict_diff(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[FieldChange]:
    changes: list[FieldChange] = []
    for key in old.keys() | new.keys():
        path = f"{prefix}.{key}" if prefix else key
        old_v = old.get(key)
        new_v = new.get(key)
        if isinstance(old_v, dict) and isinstance(new_v, dict):
            changes.extend(_dict_diff(old_v, new_v, path))
        elif old_v != new_v:
            changes.append(FieldChange(field_path=path, before=old_v, after=new_v))
    return changes


# ---------------------------------------------------------------------------
# (Optional) walk_fields helper — see SLICE-2.md decision 2
# ---------------------------------------------------------------------------
#
# `field_metadata` handles dotted paths but not recursion over list[Submodel].
# Slice 2's audience filter needs to recurse. Two options:
#   - extend field_metadata to walk containers, or
#   - add a sibling walk_fields(model, callback) that does the traversal.
#
# Slight rec: sibling function — keeps field_metadata simple, separates "lookup"
# from "walk."

# TODO: def walk_fields(model_class, callback) -> None:
#           """Walk every leaf field on `model_class` (recursing into sub-models
#           and list[Submodel] containers). Invoke `callback(dotted_path, owner,
#           audience, leaf_type)` for each leaf."""
#           ...
