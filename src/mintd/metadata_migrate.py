"""v1 → v2 ``metadata.json`` schema migration.

Pure-function rules + an apply step that reads, transforms, validates
against the v2 :class:`Metadata` model, and atomically writes. Mirrors
the slice-21 config-migration pattern but for per-project metadata.

The migration rules:

- ``schema_version`` 1.x → ``"2.0"``
- Move ``project.description`` → ``metadata.description``
- Move ``project.tags`` → ``metadata.tags``
- Drop ``project.display_name`` (no v2 equivalent)
- Drop top-level ``language`` (per-scaffold concern handled by slice 19)
- Drop top-level ``metadata.version`` and ``metadata.mint_version``
  (legacy meta-meta tracking)
- Drop top-level ``storage`` (lives in ~/.config/mintd/config.yaml)
- Drop top-level ``schema`` (location is conventional, not stored)
- Drop top-level ``lifecycle``
- Default ``status.last_published_version`` to ``""`` when absent
- Default ``data_products`` to ``{"primary": None, "outputs": []}`` when absent
- Carry the rest through unchanged (``mint``, remaining ``project``,
  ``ownership``, ``access_control``, ``governance``, ``repository``)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .model import Metadata
from .publish import atomic_write_json


class MetadataMigrateError(Exception):
    """Migration produced a dict that fails v2 :class:`Metadata` validation.

    Carries the failing field path so users can hand-edit.
    """


class MetadataAlreadyV2(Exception):
    """``schema_version`` already starts with ``"2."``; no migration needed."""


@dataclass(frozen=True)
class MigrationReport:
    moved: list[tuple[str, str]] = field(default_factory=list)
    defaulted: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    schema_before: str = ""
    schema_after: str = ""


_DROP_TOPLEVEL = ("language", "schema", "lifecycle", "storage")
_DROP_PROJECT_SUBKEYS = ("description", "tags", "display_name")
_DROP_METADATA_SUBKEYS = ("version", "mint_version")


def migrate_v1_to_v2(v1_data: dict) -> tuple[dict, MigrationReport]:
    """Apply all migration rules and return (v2_dict, report).

    Pure function — does not touch disk.
    """
    moved: list[tuple[str, str]] = []
    defaulted: list[str] = []
    dropped: list[str] = []

    out: dict = {}
    schema_before = str(v1_data.get("schema_version", ""))

    # schema_version → 2.0
    out["schema_version"] = "2.0"
    if schema_before != "2.0":
        moved.append(("schema_version", "2.0"))

    # Carry mint as-is.
    if "mint" in v1_data:
        out["mint"] = v1_data["mint"]

    # Project: strip description/tags/display_name; keep the rest.
    project_in = dict(v1_data.get("project") or {})
    description_value = project_in.pop("description", None)
    tags_value = project_in.pop("tags", None)
    if "display_name" in project_in:
        project_in.pop("display_name", None)
        dropped.append("project.display_name")
    out["project"] = project_in

    # Metadata: build from moved description/tags; drop legacy meta-meta fields.
    metadata_in = dict(v1_data.get("metadata") or {})
    for legacy_key in _DROP_METADATA_SUBKEYS:
        if legacy_key in metadata_in:
            metadata_in.pop(legacy_key)
            dropped.append(f"metadata.{legacy_key}")
    if description_value is not None:
        metadata_in["description"] = description_value
        moved.append(("project.description", "metadata.description"))
    elif "description" not in metadata_in:
        metadata_in["description"] = ""
        defaulted.append("metadata.description")
    if tags_value is not None:
        metadata_in["tags"] = tags_value
        moved.append(("project.tags", "metadata.tags"))
    elif "tags" not in metadata_in:
        metadata_in["tags"] = []
        defaulted.append("metadata.tags")
    out["metadata"] = metadata_in

    # Carry-through fields.
    for key in ("ownership", "access_control", "governance", "repository"):
        if key in v1_data:
            out[key] = v1_data[key]

    # Status: default last_published_version.
    status_in = dict(v1_data.get("status") or {})
    if "last_published_version" not in status_in:
        status_in["last_published_version"] = ""
        defaulted.append("status.last_published_version")
    out["status"] = status_in

    # data_products: default to empty when absent. When present, normalize:
    # - `primary` may be a list in some v1 files (multiple "primary" entries);
    #   coerce to the first string element (rest are reachable via outputs).
    # - each `outputs[]` entry's `last_published` is required in v2 but absent
    #   in most v1 files; default to "".
    if v1_data.get("data_products") is None:
        # Handle both "absent" and "data_products: null" cases.
        out["data_products"] = {"primary": None, "outputs": []}
        defaulted.append("data_products")
    else:
        dp_in = dict(v1_data["data_products"])
        primary = dp_in.get("primary")
        if isinstance(primary, list):
            dp_in["primary"] = primary[0] if primary else None
            defaulted.append("data_products.primary (coerced from list)")
        outputs_in = list(dp_in.get("outputs") or [])
        normalized_outputs = []
        any_defaulted_last_pub = False
        any_defaulted_primary_flag = False
        for raw in outputs_in:
            entry = dict(raw)
            # Use `.get(...) is None` rather than `not in` so explicit-null
            # values from messy v1 files get the same default treatment as
            # absent keys (v2's DataProductOutput rejects `None` for both).
            if entry.get("last_published") is None:
                entry["last_published"] = ""
                any_defaulted_last_pub = True
            if entry.get("primary") is None:
                entry["primary"] = False
                any_defaulted_primary_flag = True
            normalized_outputs.append(entry)
        dp_in["outputs"] = normalized_outputs
        if any_defaulted_last_pub:
            defaulted.append("data_products.outputs[].last_published")
        if any_defaulted_primary_flag:
            defaulted.append("data_products.outputs[].primary")
        out["data_products"] = dp_in

    # Drop top-level v1-only keys.
    for legacy_key in _DROP_TOPLEVEL:
        if legacy_key in v1_data:
            dropped.append(legacy_key)

    report = MigrationReport(
        moved=moved,
        defaulted=defaulted,
        dropped=dropped,
        schema_before=schema_before,
        schema_after="2.0",
    )
    return out, report


def _find_dropped_keys(raw: Any, modeled: Any, prefix: str = "") -> list[str]:
    """Key paths present in ``raw`` but absent from ``modeled``.

    Compares key *existence* only — never values — so pydantic's value
    normalisation (``None`` defaults, datetime formatting) never registers as a
    drop. Recurses into dicts and zips lists by index, emitting paths like
    ``metadata.configurations`` and ``data_products.outputs[0].format``. Used to
    report which unmodeled v1 fields the canonical model dump strips on write.
    """
    dropped: list[str] = []
    if isinstance(raw, dict):
        modeled_dict = modeled if isinstance(modeled, dict) else {}
        for key, raw_val in raw.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in modeled_dict:
                dropped.append(path)
            else:
                dropped.extend(_find_dropped_keys(raw_val, modeled_dict[key], path))
    elif isinstance(raw, list):
        modeled_list = modeled if isinstance(modeled, list) else []
        for i, raw_item in enumerate(raw):
            if i < len(modeled_list):
                dropped.extend(
                    _find_dropped_keys(raw_item, modeled_list[i], f"{prefix}[{i}]")
                )
    return dropped


def apply_metadata_migration(
    path: Path, *, dry_run: bool = False
) -> MigrationReport:
    """Read ``path/metadata.json``, migrate, validate, atomically write.

    ``path`` is a project directory (the same shape as ``mintd publish [PATH]``).
    Raises :class:`MetadataAlreadyV2` if the file's ``schema_version`` already
    starts with ``"2."``. Raises :class:`MetadataMigrateError` if the migrated
    dict fails :class:`Metadata` validation. Returns the
    :class:`MigrationReport`. When ``dry_run`` is True, skips the write.
    """
    metadata_path = path / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"no metadata.json at {metadata_path}")

    v1_data = json.loads(metadata_path.read_text(encoding="utf-8"))
    schema = str(v1_data.get("schema_version", ""))
    if schema.startswith("2."):
        raise MetadataAlreadyV2(
            f"schema_version={schema!r}; metadata.json is already v2"
        )

    v2_data, report = migrate_v1_to_v2(v1_data)

    try:
        model = Metadata.model_validate(v2_data)
    except ValidationError as exc:
        errs = exc.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", ()))
            msg = first.get("msg", "")
            raise MetadataMigrateError(
                f"{loc}: {msg}"
            ) from exc
        raise MetadataMigrateError(str(exc)) from exc

    # Write the validated model dump, not the raw migrated dict. The dump drops
    # every field the v2 model doesn't declare — legacy meta-meta cruft like
    # ``metadata.configurations``/``methods``/``data_dependencies`` that pydantic
    # silently ignores on sub-models — so a migrated file is byte-identical to
    # what ``mintd init`` scaffolds. Record the stripped keys so ``--dry-run``
    # shows them leaving (key existence only, never value diffs).
    canonical = (
        model.model_dump_json(by_alias=True, exclude_none=False, indent=2) + "\n"
    )
    modeled = model.model_dump(by_alias=True, exclude_none=False, mode="json")
    report.dropped.extend(_find_dropped_keys(v2_data, modeled))

    if not dry_run:
        atomic_write_json(metadata_path, canonical)

    return report


__all__ = [
    "MetadataAlreadyV2",
    "MetadataMigrateError",
    "MigrationReport",
    "apply_metadata_migration",
    "migrate_v1_to_v2",
]
