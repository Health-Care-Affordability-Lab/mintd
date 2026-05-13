"""Typed Pydantic model for metadata.json.

This is the canonical in-memory representation of a mintd project's metadata.
Every read site goes through Metadata.from_json_file(); every write site goes
through model.model_dump_json().
"""

import types
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Owner × Audience annotations
# ---------------------------------------------------------------------------
#
# Every field on the model is annotated with:
#   - Owner: who is allowed to write it (USER, MINTD, PIPELINE, REGISTRY)
#   - Audience: who reads it as canonical (LOCAL, CATALOG, PRODUCER_CONTRACT, CONSUMER)
#
# These drive registry serialization (audience-based filter), mintd check
# warnings ("USER field looks tool-generated"), and the catalog vs. producer
# canonical-source split.


class Owner(StrEnum):
    USER = "user"
    MINTD = "mintd"
    PIPELINE = "pipeline"
    REGISTRY = "registry"


class Audience(StrEnum):
    LOCAL = "local"
    CATALOG = "catalog"
    PRODUCER_CONTRACT = "producer_contract"
    CONSUMER = "consumer"


@dataclass(frozen=True)
class FieldRole:
    owner: Owner
    audience: Audience


# Shorthand aliases for the most common (Owner, Audience) pairs.
_USER_CATALOG = FieldRole(Owner.USER, Audience.CATALOG)
_MINTD_LOCAL = FieldRole(Owner.MINTD, Audience.LOCAL)
_MINTD_CATALOG = FieldRole(Owner.MINTD, Audience.CATALOG)
_MINTD_PRODUCER = FieldRole(Owner.MINTD, Audience.PRODUCER_CONTRACT)
_PIPELINE_PRODUCER = FieldRole(Owner.PIPELINE, Audience.PRODUCER_CONTRACT)
_PIPELINE_CONSUMER = FieldRole(Owner.PIPELINE, Audience.CONSUMER)
_PIPELINE_LOCAL = FieldRole(Owner.PIPELINE, Audience.LOCAL)


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class Mint(BaseModel):
    version: Annotated[str, _MINTD_LOCAL]
    commit_hash: Annotated[str, _MINTD_LOCAL]


class Project(BaseModel):
    name: Annotated[str, _MINTD_CATALOG]
    type: Annotated[Literal["data", "code", "project", "enclave"], _MINTD_CATALOG]
    full_name: Annotated[str, _MINTD_CATALOG]
    created_at: Annotated[datetime, _MINTD_LOCAL]
    created_by: Annotated[str, _MINTD_LOCAL]


class ProjectMetadataBlock(BaseModel):
    description: Annotated[str, _USER_CATALOG]
    tags: Annotated[list[str], _USER_CATALOG]


class Ownership(BaseModel):
    team: Annotated[str, _USER_CATALOG]
    maintainers: Annotated[list[str], _USER_CATALOG]


class AccessTeam(BaseModel):
    name: Annotated[str, _USER_CATALOG]
    permission: Annotated[str, _USER_CATALOG]


class AccessControl(BaseModel):
    teams: Annotated[list[AccessTeam], _USER_CATALOG]


class Governance(BaseModel):
    classification: Annotated[str, _USER_CATALOG]
    contract_info: Annotated[str, _USER_CATALOG]


class DvcStorage(BaseModel):
    remote_name: Annotated[str, _MINTD_PRODUCER]


class Storage(BaseModel):
    provider: Annotated[str, _MINTD_PRODUCER]
    bucket: Annotated[str, _MINTD_PRODUCER]
    prefix: Annotated[str, _MINTD_PRODUCER]
    endpoint: Annotated[str, _MINTD_PRODUCER]
    versioning: Annotated[bool, _MINTD_PRODUCER]
    dvc: Annotated[DvcStorage, _MINTD_PRODUCER]


class DataProductOutput(BaseModel):
    # No pin_strategy field — settled in grilling; DVC handles all pinning cases.
    path: Annotated[str, _PIPELINE_CONSUMER]
    description: Annotated[str, _PIPELINE_CONSUMER]
    primary: Annotated[bool, _PIPELINE_CONSUMER]
    last_published: Annotated[str, _PIPELINE_CONSUMER]


class DataProducts(BaseModel):
    primary: Annotated[str | None, _PIPELINE_PRODUCER] = None
    outputs: Annotated[list[DataProductOutput], _PIPELINE_CONSUMER] = Field(default_factory=list)


class Mirror(BaseModel):
    url: Annotated[str, _USER_CATALOG]
    purpose: Annotated[str, _USER_CATALOG]


class Repository(BaseModel):
    github_url: Annotated[str, _MINTD_CATALOG]
    default_branch: Annotated[str, _MINTD_CATALOG]
    visibility: Annotated[str, _MINTD_CATALOG]
    mirror: Annotated[Mirror, _USER_CATALOG]


class Status(BaseModel):
    state: Annotated[str, _USER_CATALOG]
    last_updated: Annotated[datetime, _PIPELINE_LOCAL]
    last_published_version: Annotated[str, _PIPELINE_LOCAL]


# ---------------------------------------------------------------------------
# Metadata (top-level)
# ---------------------------------------------------------------------------


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")  # tightened in slice 6

    schema_version: Annotated[Literal["2.0"], _MINTD_LOCAL]
    mint: Annotated[Mint, _MINTD_LOCAL]
    project: Annotated[Project, _MINTD_CATALOG]
    metadata: Annotated[ProjectMetadataBlock, _USER_CATALOG]
    ownership: Annotated[Ownership, _USER_CATALOG]
    access_control: Annotated[AccessControl, _USER_CATALOG]
    governance: Annotated[Governance, _USER_CATALOG]
    storage: Annotated[Storage | None, _MINTD_PRODUCER] = None
    data_products: Annotated[DataProducts, _PIPELINE_PRODUCER] = Field(default_factory=DataProducts)
    repository: Annotated[Repository, _MINTD_CATALOG]
    status: Annotated[Status, _PIPELINE_LOCAL]

    @classmethod
    def from_json_file(cls, path: Path) -> "Metadata":
        """Read file → parse JSON → validate via Pydantic.

        Raises FileNotFoundError if the file is missing, json.JSONDecodeError
        if the bytes don't parse as JSON, or pydantic.ValidationError if the
        shape doesn't match the model.
        """
        data = path.read_text()
        return cls.model_validate_json(data)


# ---------------------------------------------------------------------------
# Field introspection
# ---------------------------------------------------------------------------


def _unwrap_optional(tp: Any) -> Any:
    """Return T given T | None (or Optional[T]). Pass through otherwise."""
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(tp) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return tp


def field_metadata(model_class: type[BaseModel], field_path: str) -> tuple[Owner, Audience]:
    """Return the (Owner, Audience) annotation for a dotted field path.

    Examples:
        field_metadata(Metadata, "project.name") -> (Owner.MINTD, Audience.CATALOG)
        field_metadata(Metadata, "storage.dvc.remote_name") -> (Owner.MINTD, Audience.PRODUCER_CONTRACT)

    Raises KeyError if the path doesn't exist or the leaf has no FieldRole.
    """
    parts = field_path.split(".")
    current: type[BaseModel] = model_class
    for i, part in enumerate(parts):
        if part not in current.model_fields:
            raise KeyError(f"Field '{part}' not found on {current.__name__}")
        info = current.model_fields[part]
        if i == len(parts) - 1:
            for m in info.metadata:
                if isinstance(m, FieldRole):
                    return (m.owner, m.audience)
            raise KeyError(f"No FieldRole annotation on '{field_path}'")
        inner = _unwrap_optional(info.annotation)
        if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
            raise KeyError(f"Cannot descend into non-model field '{part}' at '{field_path}'")
        current = inner
    raise KeyError(f"Empty field path")
