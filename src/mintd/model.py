"""Typed Pydantic model for metadata.json.

This is the canonical in-memory representation of a mintd project's metadata.
Every read site goes through Metadata.from_json_file(); every write site goes
through model.model_dump_json().

Each field carries an `Owner` annotation describing who is allowed to write
it. The audience taxonomy (LOCAL / CATALOG / PRODUCER_CONTRACT / CONSUMER)
that earlier drafts encoded has been dropped — the catalog is "metadata
minus a small exclude list" (see CATALOG_EXCLUDED_PATHS below). See
`notes/decisions.md` 2026-05-14 for the rationale.
"""

import types
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from .catalog import CatalogEntry


# ---------------------------------------------------------------------------
# Owner annotations
# ---------------------------------------------------------------------------


class Owner(StrEnum):
    """Who is allowed to write a field.

    Drives `mintd check`'s warnings ("USER field looks tool-generated") and
    informs reviewers reading the model what to expect a field's lifecycle
    to be.

    - USER: human edits in metadata.json (description, tags, ownership, ...)
    - MINTD: tool writes (schema_version, mint.*, project.created_at, ...)
    - PIPELINE: derived from project state (data_products.outputs[].path
      from DVC tracking; status.last_updated from publish flow)
    - REGISTRY: catalog regenerates (none today; reserved for the
      registry-side rewriting flow)
    """
    USER = "user"
    MINTD = "mintd"
    PIPELINE = "pipeline"
    REGISTRY = "registry"


# Paths excluded from the catalog projection (`Metadata.to_catalog_entry`).
# Everything else on `Metadata` ships to the registry catalog yaml as-is.
# Kept minimal — only fields that describe the metadata.json file itself
# rather than the project.
CATALOG_EXCLUDED_PATHS: frozenset[str] = frozenset({
    "schema_version",     # provenance of the file format, not the project
    "mint",               # mint tool version + commit_hash (file build info)
})


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Mint(BaseModel):
    version: Annotated[str, Owner.MINTD]
    commit_hash: Annotated[str, Owner.MINTD]


class Project(BaseModel):
    name: Annotated[str, Owner.MINTD]
    type: Annotated[Literal["data", "code", "project", "enclave"], Owner.MINTD]
    full_name: Annotated[str, Owner.MINTD]
    created_at: Annotated[datetime, Owner.MINTD]
    created_by: Annotated[str, Owner.MINTD]


class ProjectMetadataBlock(BaseModel):
    description: Annotated[str, Owner.USER]
    tags: Annotated[list[str], Owner.USER]


class Ownership(BaseModel):
    team: Annotated[str, Owner.USER]
    maintainers: Annotated[list[str], Owner.USER]


class AccessTeam(BaseModel):
    name: Annotated[str, Owner.USER]
    permission: Annotated[str, Owner.USER]


class AccessControl(BaseModel):
    teams: Annotated[list[AccessTeam], Owner.USER]


class Governance(BaseModel):
    classification: Annotated[str, Owner.USER]
    contract_info: Annotated[str, Owner.USER]


class DvcStorage(BaseModel):
    remote_name: Annotated[str, Owner.MINTD]


class Storage(BaseModel):
    provider: Annotated[str, Owner.MINTD]
    bucket: Annotated[str, Owner.MINTD]
    prefix: Annotated[str, Owner.MINTD]
    endpoint: Annotated[str, Owner.MINTD]
    versioning: Annotated[bool, Owner.MINTD]
    dvc: Annotated[DvcStorage, Owner.MINTD]


class DataProductOutput(BaseModel):
    path: Annotated[str, Owner.PIPELINE]
    description: Annotated[str, Owner.PIPELINE]
    primary: Annotated[bool, Owner.PIPELINE]
    last_published: Annotated[str, Owner.PIPELINE]


class DataProducts(BaseModel):
    primary: Annotated[str | None, Owner.PIPELINE] = None
    outputs: Annotated[list[DataProductOutput], Owner.PIPELINE] = Field(default_factory=list)


class Mirror(BaseModel):
    url: Annotated[str, Owner.USER]
    purpose: Annotated[str, Owner.USER]


class Repository(BaseModel):
    github_url: Annotated[str, Owner.MINTD]
    default_branch: Annotated[str, Owner.MINTD]
    visibility: Annotated[str, Owner.MINTD]
    mirror: Annotated[Mirror, Owner.USER]


class Status(BaseModel):
    state: Annotated[str, Owner.USER]
    last_updated: Annotated[datetime, Owner.PIPELINE]
    last_published_version: Annotated[str, Owner.PIPELINE]


# ---------------------------------------------------------------------------
# Metadata (top-level)
# ---------------------------------------------------------------------------


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")  # tightened in slice 6

    schema_version: Annotated[Literal["2.0"], Owner.MINTD]
    mint: Annotated[Mint, Owner.MINTD]
    project: Annotated[Project, Owner.MINTD]
    metadata: Annotated[ProjectMetadataBlock, Owner.USER]
    ownership: Annotated[Ownership, Owner.USER]
    access_control: Annotated[AccessControl, Owner.USER]
    governance: Annotated[Governance, Owner.USER]
    storage: Annotated[Storage | None, Owner.MINTD] = None
    data_products: Annotated[DataProducts, Owner.PIPELINE] = Field(default_factory=DataProducts)
    repository: Annotated[Repository, Owner.MINTD]
    status: Annotated[Status, Owner.PIPELINE]

    @classmethod
    def from_json_file(cls, path: Path) -> "Metadata":
        """Read file → parse JSON → validate via Pydantic.

        Raises FileNotFoundError if the file is missing, json.JSONDecodeError
        if the bytes don't parse as JSON, or pydantic.ValidationError if the
        shape doesn't match the model.
        """
        data = path.read_text()
        return cls.model_validate_json(data)

    def to_catalog_entry(self) -> CatalogEntry:
        """Project this Metadata onto a CatalogEntry. The catalog stores
        metadata.json verbatim except for the paths in CATALOG_EXCLUDED_PATHS
        (mint-internal provenance fields that describe the file, not the
        project).
        """
        dumped = self.model_dump(mode="json")
        for path in CATALOG_EXCLUDED_PATHS:
            dumped.pop(path, None)
        return CatalogEntry.model_validate(dumped)


# ---------------------------------------------------------------------------
# Field introspection
# ---------------------------------------------------------------------------


def _unwrap_container(tp: Any) -> Any:
    """Repeatedly unwrap Optional[T] and list[T] until reaching a non-container.

    Examples: Optional[Storage] -> Storage; list[AccessTeam] -> AccessTeam;
    Optional[list[AccessTeam]] -> AccessTeam.
    """
    while True:
        origin = get_origin(tp)
        if origin is Union or origin is types.UnionType:
            non_none = [a for a in get_args(tp) if a is not type(None)]
            if len(non_none) == 1:
                tp = non_none[0]
                continue
        elif origin is list:
            args = get_args(tp)
            if len(args) == 1:
                tp = args[0]
                continue
        return tp


def field_metadata(model_class: type[BaseModel], field_path: str) -> Owner:
    """Return the `Owner` annotation for a dotted field path.

    Examples:
        field_metadata(Metadata, "project.name") -> Owner.MINTD
        field_metadata(Metadata, "metadata.description") -> Owner.USER
        field_metadata(Metadata, "storage.dvc.remote_name") -> Owner.MINTD

    Raises KeyError if the path doesn't exist or the leaf has no Owner annotation.
    """
    parts = field_path.split(".")
    current: type[BaseModel] = model_class
    for i, part in enumerate(parts):
        if part not in current.model_fields:
            raise KeyError(f"Field '{part}' not found on {current.__name__}")
        info = current.model_fields[part]
        if i == len(parts) - 1:
            for m in info.metadata:
                if isinstance(m, Owner):
                    return m
            raise KeyError(f"No Owner annotation on '{field_path}'")
        inner = _unwrap_container(info.annotation)
        if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
            raise KeyError(f"Cannot descend into non-model field '{part}' at '{field_path}'")
        current = inner
    raise KeyError("Empty field path")
