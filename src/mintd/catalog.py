"""Catalog client — the registry-side seam.

Slice 2 introduced the `CatalogClient` Protocol and `InMemoryCatalogClient`.
Slice 3 adds `GitCatalogClient` (production: git + gh PR) and `status()` on
the Protocol. The two implementations are interchangeable — every caller
goes through `CatalogClient`.

The four-method core (register / update / fetch / list) plus `status()` is
the only public surface for catalog access. No other module reads or writes
catalog files directly.

Audience filter: `Metadata.to_catalog_entry()` projects to the canonical
CATALOG-audience subset. Slice 3's `_catalog_serializer.py` extends this
with an advisory tier (PRODUCER_CONTRACT fields + `last_synced_at`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .model import Metadata


def _pr_url(registry_repo_url: str, pr_number: int) -> str | None:
    """Build a github.com PR URL from a registry repo URL + PR number.

    Returns None when the registry URL isn't a recognizable GitHub
    repo (e.g. a file:// path in tests, or a self-hosted host).
    """
    m = re.search(
        r"(?:github\.com[:/])([^/]+)/([^/.]+)(?:\.git)?/?$",
        registry_repo_url,
    )
    if not m:
        return None
    org, repo = m.group(1), m.group(2)
    return f"https://github.com/{org}/{repo}/pull/{pr_number}"


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
    pr_number: int | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class UpdateResult:
    name: str
    changes: list[FieldChange]
    dry_run: bool
    pr_number: int | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class CatalogFilter:
    project_type: str | None = None


@dataclass(frozen=True)
class RegistrationStatus:
    """Result of `client.status(name)`.

    For `InMemoryCatalogClient`, only REGISTERED / NOT_FOUND are possible;
    PR-lifecycle states are git-backed concerns.

    For `GitCatalogClient`, PENDING carries the open PR number so callers
    can link to it (`https://github.com/<repo>/pull/<pr_number>`).
    """
    state: Literal["registered", "pending", "not_found"]
    pr_number: int | None = None


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

    @property
    def name(self) -> str:
        return self._nested("project", "name")

    @property
    def project_type(self) -> str:
        return self._nested("project", "type")

    @property
    def description(self) -> str:
        return self._nested("metadata", "description")

    @property
    def repo_url(self) -> str:
        return self._nested("repository", "github_url")

    def _nested(self, *keys: str) -> str:
        """Walk the dumped tree by keys; return ''  on any missing/non-str."""
        cur: Any = self.model_dump()
        for k in keys:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(k)
            if cur is None:
                return ""
        return cur if isinstance(cur, str) else ""


# ---------------------------------------------------------------------------
# CatalogClient interface (Protocol — structural typing)
# ---------------------------------------------------------------------------

class CatalogClient(Protocol):
    """Structural interface for catalog access. `InMemoryCatalogClient` and
    `GitCatalogClient` both implement it — no inheritance, just shape.
    """

    def register(self, metadata: Metadata, *, dry_run: bool = False) -> RegisterResult: ...
    def update(self, metadata: Metadata, *, dry_run: bool = False) -> UpdateResult: ...
    def fetch(self, name: str) -> CatalogEntry: ...
    def list(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]: ...
    def status(self, name: str) -> RegistrationStatus: ...
    def sync(self) -> int:
        """Refresh local cache from upstream. Returns the entry count.

        For in-memory implementations, this is a no-op that returns the
        current in-process count. For git-backed implementations, this
        forces a `git pull` of the registry cache before reporting.
        """
        ...


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

    def status(self, name: str) -> RegistrationStatus:
        """In-memory has no PR lifecycle — either registered or not found."""
        if name in self._entries:
            return RegistrationStatus(state="registered")
        return RegistrationStatus(state="not_found")

    def sync(self) -> int:
        """No-op for in-memory; returns the current entry count."""
        return len(self._entries)


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
# GitCatalogClient — production: writes via git + gh PR, reads via local cache
# ---------------------------------------------------------------------------


class GitCatalogClient:
    """Production CatalogClient.

    register / update open a branch + PR against the registry repo. fetch /
    list read from a local clone (`_catalog_cache.py`) that's kept fresh
    transparently on every read. status() resolves PR-pending entries against
    a local state file (`pending_registrations.py`) before falling back to
    a `gh pr list` query.

    All subprocess interaction goes through the injected `RegistryGitOps` —
    tests pass a fake, production passes the default `SubprocessRegistryGitOps`.
    """

    _PENDING_FILE = ".mintd_pending.json"

    def __init__(
        self,
        registry_repo_url: str,
        *,
        work_dir: Path,
        git_ops: "RegistryGitOps | None" = None,
    ) -> None:
        from ._catalog_cache import CatalogCache
        from ._registry_git_ops import SubprocessRegistryGitOps
        from .pending_registrations import PendingRegistrations

        self._registry_repo_url = registry_repo_url
        self._work_dir = work_dir
        self._git_ops = git_ops if git_ops is not None else SubprocessRegistryGitOps()
        self._cache = CatalogCache(
            work_dir=work_dir,
            registry_url=registry_repo_url,
            git_ops=self._git_ops,
        )
        self._pending = PendingRegistrations(path=work_dir / self._PENDING_FILE)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def fetch(self, name: str) -> CatalogEntry:
        self._cache.ensure_fresh()
        entry = self._cache.read_entry(name)
        if entry is None:
            raise CatalogNotFound(name)
        return entry

    def list(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]:
        self._cache.ensure_fresh()
        return self._cache.list_entries(filter)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def register(self, metadata: "Metadata", *, dry_run: bool = False) -> RegisterResult:
        from ._catalog_serializer import deserialize, serialize

        self._cache.ensure_fresh()
        name = metadata.project.name

        existing = self._cache.read_entry(name)
        if existing is not None:
            raise CatalogAlreadyExists(name)
        if self._pending.find(name) is not None:
            # A PR is already open for this name — fail loudly per slice-3
            # decision α (refuse on PR conflict).
            raise CatalogAlreadyExists(f"{name} (PR pending)")

        if dry_run:
            return RegisterResult(name=name, dry_run=True)

        content = serialize(metadata)
        entry = deserialize(content)
        branch = f"register/{name}"
        pr = self._commit_and_pr(
            branch=branch,
            entry=entry,
            content=content,
            commit_message=f"Register {name}",
            pr_title=f"Register {name}",
            pr_body=f"Catalog entry for `{name}`.",
        )
        self._record_pending(name=name, pr_number=pr, kind="register")
        return RegisterResult(
            name=name, dry_run=False,
            pr_number=pr,
            pr_url=_pr_url(self._registry_repo_url, pr),
        )

    def update(self, metadata: "Metadata", *, dry_run: bool = False) -> UpdateResult:
        from ._catalog_serializer import deserialize, serialize

        self._cache.ensure_fresh()
        name = metadata.project.name

        existing = self._cache.read_entry(name)
        if existing is None:
            raise CatalogNotFound(name)

        new_content = serialize(metadata)
        new_entry = deserialize(new_content)

        changes = _diff_entries(existing, new_entry)

        if not changes:
            return UpdateResult(
                name=name, changes=[], dry_run=dry_run,
                pr_number=None, pr_url=None,
            )

        if dry_run:
            return UpdateResult(name=name, changes=changes, dry_run=True)

        branch = f"update/{name}"
        pr = self._commit_and_pr(
            branch=branch,
            entry=new_entry,
            content=new_content,
            commit_message=f"Update {name}",
            pr_title=f"Update {name}",
            pr_body=f"Update for catalog entry `{name}`.",
        )
        self._record_pending(name=name, pr_number=pr, kind="update")
        return UpdateResult(
            name=name, changes=changes, dry_run=False,
            pr_number=pr,
            pr_url=_pr_url(self._registry_repo_url, pr),
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self, name: str) -> RegistrationStatus:
        self._cache.ensure_fresh()
        if self._cache.read_entry(name) is not None:
            return RegistrationStatus(state="registered")
        pending = self._pending.find(name)
        if pending is not None:
            return RegistrationStatus(state="pending", pr_number=pending.pr_number)
        return RegistrationStatus(state="not_found")

    def sync(self) -> int:
        """Force-refresh the registry cache; returns the entry count."""
        self._cache.ensure_fresh()
        return len(self._cache.list_entries())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _commit_and_pr(
        self,
        *,
        branch: str,
        entry: CatalogEntry,
        content: str,
        commit_message: str,
        pr_title: str,
        pr_body: str,
    ) -> int:
        self._git_ops.checkout_new_branch(self._work_dir, branch)
        self._cache.write_entry(entry, content)
        self._git_ops.commit_all(self._work_dir, commit_message)
        self._git_ops.push_branch(self._work_dir, branch)
        return self._git_ops.open_pr(
            self._work_dir,
            title=pr_title,
            body=pr_body,
            head=branch,
        )

    def _record_pending(
        self,
        *,
        name: str,
        pr_number: int,
        kind: Literal["register", "update"],
    ) -> None:
        from .pending_registrations import PendingRegistration

        self._pending.add(
            PendingRegistration(
                name=name,
                pr_number=pr_number,
                kind=kind,
                created_at=datetime.now(timezone.utc),
            )
        )


if TYPE_CHECKING:
    from ._registry_git_ops import RegistryGitOps
