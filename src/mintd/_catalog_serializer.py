"""Metadata ↔ catalog YAML.

The catalog stores `metadata.json` verbatim except for
`Metadata.CATALOG_EXCLUDED_PATHS` (mint-internal provenance fields). No
audience-based filtering, no advisory tier — see `notes/decisions.md`
2026-05-14 for the rationale.

Two thin wrappers around `yaml.safe_dump` / `yaml.safe_load`:
  - `serialize(metadata)` calls `metadata.to_catalog_entry()` and dumps.
  - `deserialize(yaml_text)` parses into a `CatalogEntry`.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from .catalog import CatalogEntry, CatalogEntryInvalid
from .model import Metadata


def serialize(metadata: Metadata) -> str:
    """Emit `metadata` as catalog yaml text."""
    entry = metadata.to_catalog_entry()
    return yaml.safe_dump(entry.model_dump(mode="json"), sort_keys=False, default_flow_style=False)


def deserialize(yaml_text: str, *, source: object = None) -> CatalogEntry:
    """Parse a catalog yaml string into a CatalogEntry.

    A registry entry is a file in someone else's repo — unparseable YAML (a
    merge conflict marker) and a shape pydantic rejects are both routine, and
    both used to escape as a raw traceback through every verb that reads the
    catalog. One guard here because every reader routes through this function.
    """
    try:
        raw = yaml.safe_load(yaml_text) or {}
        return CatalogEntry.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        where = f" ({source})" if source is not None else ""
        raise CatalogEntryInvalid(f"catalog entry is unreadable{where}: {e}") from e
