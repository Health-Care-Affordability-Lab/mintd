"""Catalog client — the registry-side seam.

Slice 2 introduced the `CatalogClient` Protocol and `InMemoryCatalogClient`.
Slice 3 adds `GitCatalogClient` (production: git + gh PR) and `status()` on
the Protocol. The two implementations are interchangeable — every caller
goes through `CatalogClient`.

The four-method core (register / update / fetch / list) plus `status()` and
`sync()` is the only public surface for catalog access. No other module
reads or writes catalog files directly.

Projection: `Metadata.to_catalog_entry()` drops `model.CATALOG_EXCLUDED_PATHS`
and keeps the rest; `_catalog_serializer.py` dumps that to YAML unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict

from .errors import MintdError

if TYPE_CHECKING:
    from ._console import Reporter
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


class CatalogEntryInvalid(MintdError):
    """A catalog entry exists but cannot be read — unparseable YAML, or a shape
    `CatalogEntry` rejects. Distinct from `CatalogNotFound`: "run 'mintd data
    list'" is the wrong advice for a file that is right there and broken."""

    hint = "fix the entry in the registry repo, then 'mintd registry sync'"


class CatalogNotFound(MintdError):
    """Raised by fetch()/update() when the named entry is not in the catalog."""

    hint = "run 'mintd data list' to see available products"


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
    # True when the entry was stacked onto an already-open PR rather than
    # opening a new one.
    pr_reused: bool = False


@dataclass(frozen=True)
class CatalogFilter:
    project_type: str | None = None


@dataclass(frozen=True)
class RegistrationStatus:
    """Result of `client.status(name)`.

    For `InMemoryCatalogClient`, only "registered" / "not_found" are possible;
    PR-lifecycle states are git-backed concerns.

    For `GitCatalogClient`, "pending" carries the open PR number so callers
    can link to it (`https://github.com/<repo>/pull/<pr_number>`).
    """
    state: Literal["registered", "pending", "not_found"]
    pr_number: int | None = None


# ---------------------------------------------------------------------------
# CatalogEntry — projected subset of Metadata
# ---------------------------------------------------------------------------
#
# Decision:
#   - α (dynamic projection): `class CatalogEntry(BaseModel)` with extra='allow';
#     `to_catalog_entry()` drops `CATALOG_EXCLUDED_PATHS` from the dump, then
#     validates it into a CatalogEntry. The shape of CatalogEntry is *derived*
#     from Metadata.
#   - β (hand-defined): a parallel Pydantic class with the catalog fields spelled
#     out. Two sources of truth → drift risk.
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

    def register(
        self, metadata: Metadata, *, dry_run: bool = False,
        reporter: Optional["Reporter"] = None,
    ) -> RegisterResult: ...
    def update(
        self, metadata: Metadata, *, dry_run: bool = False,
        reporter: Optional["Reporter"] = None,
    ) -> UpdateResult: ...
    def fetch(self, name: str) -> CatalogEntry: ...
    def list(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]: ...
    def status(self, name: str) -> RegistrationStatus: ...
    def sync(self) -> int:
        """Refresh local cache from upstream. Returns the entry count.

        For in-memory implementations, this is a no-op that returns the
        current in-process count. For git-backed implementations, this runs
        the same registry-cache refresh every read does (clone, or fetch +
        hard reset to `origin/main`) before reporting.
        """
        ...
    def require_push_access(self) -> None:
        """Before publish writes anything: GitHub access for every URL
        ``update`` will push the registry to. A no-op without a git remote."""
        ...


# ---------------------------------------------------------------------------
# InMemoryCatalogClient — in-process, for tests
# ---------------------------------------------------------------------------

class InMemoryCatalogClient:
    """Catalog backed by an in-process dict. Used in tests."""

    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}

    @classmethod
    def of(cls, entries: list[CatalogEntry]) -> "InMemoryCatalogClient":
        """A client serving exactly ``entries``, by name: a registry checkout
        read once, as `rebuild_catalog` hands it to `check_project`."""
        client = cls()
        client._entries = {e.name: e for e in entries if e.name}
        return client

    def register(self, metadata: Metadata, *, dry_run: bool = False, reporter: Optional["Reporter"] = None) -> RegisterResult:
        name = metadata.project.name
        if name in self._entries:
            raise CatalogAlreadyExists(name)
        entry = metadata.to_catalog_entry()
        if not dry_run:
            self._entries[name] = entry
        return RegisterResult(name=name, dry_run=dry_run)

    def update(self, metadata: Metadata, *, dry_run: bool = False, reporter: Optional["Reporter"] = None) -> UpdateResult:
        name = metadata.project.name
        if name not in self._entries:
            raise CatalogNotFound(name)
        new_entry = metadata.to_catalog_entry()
        changes = _diff_entries(self._entries[name], new_entry)
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

    def require_push_access(self) -> None:
        return None

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
    a local state file (`pending_registrations.py`).

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

    def register(self, metadata: "Metadata", *, dry_run: bool = False, reporter: Optional["Reporter"] = None) -> RegisterResult:
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
            entry_name=name,
            content=content,
            commit_message=f"Register {name}",
            pr_title=f"Register {name}",
            pr_body=f"Catalog entry for `{name}`.",
            reporter=reporter,
        )
        self._record_pending(name=name, pr_number=pr, kind="register")
        return RegisterResult(
            name=name, dry_run=False,
            pr_number=pr,
            pr_url=_pr_url(self._registry_repo_url, pr),
        )

    def update(self, metadata: "Metadata", *, dry_run: bool = False, reporter: Optional["Reporter"] = None) -> UpdateResult:
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
        # Two separate questions, deliberately not conflated:
        #
        #   does the branch exist?  -> decides where we commit. Anything on the
        #       registry's copy of the branch that we don't have makes a push
        #       from a fresh main-based branch a non-fast-forward, forever.
        #       A squash-merged PR whose branch was never deleted looks exactly
        #       like this and has NO open PR.
        #   is a PR open on it?     -> decides whether to reuse or open one.
        #
        # Both are asked of the registry rather than of `.mintd_pending.json`,
        # which is per-machine local state (a PR opened from a laptop is
        # invisible on the cluster). Both are AFTER the no-changes and dry-run
        # early returns above, so neither path makes these two network calls.
        branch_exists = self._git_ops.remote_branch_exists(self._work_dir, branch)
        # No branch means no PR can be open on it — skip the `gh` call entirely.
        existing_pr = (
            self._git_ops.pr_exists_for_branch(self._work_dir, branch)
            if branch_exists else None
        )
        pr = self._commit_and_pr(
            branch=branch,
            entry=new_entry,
            entry_name=name,
            content=new_content,
            commit_message=f"Update {name}",
            pr_title=f"Update {name}",
            pr_body=f"Update for catalog entry `{name}`.",
            reporter=reporter,
            branch_exists=branch_exists,
            existing_pr=existing_pr,
        )
        self._record_pending(name=name, pr_number=pr, kind="update")
        return UpdateResult(
            name=name, changes=changes, dry_run=False,
            pr_number=pr,
            pr_url=_pr_url(self._registry_repo_url, pr),
            pr_reused=existing_pr is not None,
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

    def require_push_access(self) -> None:
        self._git_ops.require_github_access(self._work_dir, push=True)

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
        entry_name: str,
        content: str,
        commit_message: str,
        pr_title: str,
        pr_body: str,
        reporter: Optional["Reporter"] = None,
        branch_exists: bool = False,
        existing_pr: int | None = None,
    ) -> int:
        """Commit `entry` on `branch` and return the PR number carrying it.

        `branch_exists` decides WHERE the commit lands: on the registry's copy
        of the branch (stacked on its tip) or on a new branch off the
        freshly-reset origin/main. `existing_pr` decides whether a PR is
        opened afterwards or an already-open one is reused. They are
        independent — a squash-merged PR leaves its branch behind with no open
        PR, and branching off main there can never fast-forward the remote.
        """
        if branch_exists:
            if reporter and existing_pr is not None:
                reporter.update_status(f"Updating open PR #{existing_pr}...")
            self._git_ops.checkout_remote_branch(self._work_dir, branch)
            on_branch: CatalogEntry | None = self._cache.read_entry(entry_name)
        else:
            self._git_ops.checkout_new_branch(self._work_dir, branch)
            on_branch = None

        # Nothing to commit when the branch already carries exactly this entry
        # — the idempotent `mintd publish <same version>` retry the recovery
        # hint tells users to run. `git commit` exits 1 on a clean tree.
        #
        # Content comparison rather than `is_working_tree_clean`: the cache
        # tree is never reliably clean (untracked `.mintd_pending.json`), and
        # this compares the thing we actually care about.
        if on_branch is None or _diff_entries(on_branch, entry):
            if reporter:
                reporter.update_status("Writing catalog entry...")
            self._cache.write_entry(entry, content)
            if reporter:
                reporter.update_status("Committing to registry...")
            self._git_ops.commit_all(self._work_dir, commit_message)
            if reporter:
                reporter.update_status("Pushing to registry...")
            self._git_ops.push_branch(self._work_dir, branch)

        if existing_pr is not None:
            return existing_pr
        if reporter:
            reporter.update_status("Opening PR...")
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


# ---------------------------------------------------------------------------
# registry rebuild: each entry from its product's default branch
# ---------------------------------------------------------------------------

#: The leaves a rebuild copies from a product's `metadata.json` into its entry,
#: as dotted paths on `Metadata` ("[]" for each item of a list). Every other
#: field of the entry is the registry's and stays as the entry has it: the
#: registry's workflows act on some of them (repository permissions from
#: `access_control`, bucket policies from `storage`, a public mirror from
#: `repository.mirror.url`), so a product must never set them by editing its
#: own file. Pinned here until the model tags the registry's own fields
#: `Owner.REGISTRY`; a test then checks that the two agree.
COPIED_LEAVES: tuple[str, ...] = (
    "metadata.description",
    "metadata.tags",
    "ownership.team",
    "ownership.maintainers",
    "data_products.primary",
    "data_products.outputs[].path",
    "data_products.outputs[].description",
    "data_products.outputs[].primary",
    "status.state",
)

#: Fields the registry decides that a product's file may still say otherwise:
#: a difference is reported, never copied. A reviewed registry PR changes them.
_REPORTED: tuple[str, ...] = ("project.name", "storage", "governance.classification", "schema")


@dataclass(frozen=True)
class Rebuilt:
    """One entry's outcome in ``rebuild_catalog``.

    ``verdict``: "updated" (the entry takes the product's fields), "unchanged",
    "held" (the product is refused: the entry keeps its last good fields and
    records why in ``source.held``), "unreachable" (the repository couldn't be
    read, so the entry is untouched), or "skipped" (the entry itself can't be
    read or names no repository). ``reports`` are differences and leaks the
    registry should know of; none changes the entry. ``mintd_error``: the
    entry is held because mintd itself failed on the product (a type, key or
    attribute error out of `check_project`), which is more likely mintd's bug
    than the product's fault."""

    name: str
    entry: str
    verdict: Literal["updated", "unchanged", "held", "unreachable", "skipped"]
    commit: str | None = None
    why: str | None = None
    changes: tuple[FieldChange, ...] = ()
    reports: tuple[str, ...] = ()
    mintd_error: bool = False


def rebuild_catalog(registry: Path, *, git_ops: "RegistryGitOps", write: bool) -> list[Rebuilt]:
    """Rebuild every entry under ``registry``'s ``catalog/`` from its product.

    The entries already there are the join record: a product with no entry
    gets none. For each, the product's repository (the entry's own
    ``repository.github_url``) is cloned at the HEAD of its
    ``repository.default_branch``, one commit deep and without DVC data, and
    its ``metadata.json`` is read. Only ``COPIED_LEAVES`` are copied; the
    entry records the commit (``source.commit``) and its date
    (``status.last_updated``). An entry with nothing new is left byte for
    byte, so a second run makes no change. ``write=False`` reports what would
    change and writes nothing."""
    from ._catalog_cache import _TYPE_DIRS
    from ._catalog_serializer import deserialize

    paths = [path for type_dir in _TYPE_DIRS for path in sorted((registry / "catalog" / type_dir).glob("*.yaml"))]
    # The catalog as it stands, for check's enclave lookups: an enclave product
    # resolves the products it subscribes to through the catalog.
    readable, unreadable = [], []
    for path in paths:
        try:
            readable.append(deserialize(path.read_text(encoding="utf-8"), source=path))
        except (CatalogEntryInvalid, OSError, ValueError):
            unreadable.append(path.name)  # skipped by its own rebuild below, and named to its subscribers
    catalog = InMemoryCatalogClient.of(readable)
    return [
        _rebuild_entry(registry, path, git_ops=git_ops, write=write, catalog=catalog, unreadable=tuple(unreadable))
        for path in paths
    ]


def _rebuild_entry(
    registry: Path, path: Path, *, git_ops: "RegistryGitOps", write: bool, catalog: "CatalogClient",
    unreadable: tuple[str, ...] = (),
) -> Rebuilt:
    import copy
    import tempfile

    from ._catalog_serializer import deserialize, dump
    from ._registry_git_ops import GitOpError
    from ._subprocess import WallTimeoutExceeded
    from .errors import strip_userinfo

    rel = path.relative_to(registry).as_posix()
    try:
        old = deserialize(path.read_text(encoding="utf-8"), source=rel)
    except (CatalogEntryInvalid, OSError, ValueError):
        return Rebuilt(path.stem, rel, "skipped", why="the registry entry can't be read")
    entry = old.model_dump()
    name = old.name or path.stem
    url = old.repo_url
    if not url:
        return Rebuilt(name, rel, "skipped", why="the entry names no repository.github_url")
    with tempfile.TemporaryDirectory(prefix="mintd-rebuild-") as tmp:
        product = Path(tmp) / "product"
        try:
            # One commit deep, no DVC data and no LFS objects: only what git tracks at HEAD.
            git_ops.clone(url, product, shallow=True, branch=old._nested("repository", "default_branch") or None,
                          env={"GIT_LFS_SKIP_SMUDGE": "1"})
            sha, date = git_ops.head_commit(product)
        except GitOpError as exc:
            last = (exc.stderr or str(exc)).strip().splitlines()[-1:] or [""]
            return Rebuilt(name, rel, "unreachable", why=f"can't read the repository: {strip_userinfo(last[0])}")
        except (WallTimeoutExceeded, OSError) as exc:
            return Rebuilt(name, rel, "unreachable", why=f"can't read the repository ({type(exc).__name__})")
        why, meta, mintd_error = _refusal(product, catalog, unreadable)
    reports = _reports(entry, meta) if meta is not None else ()
    new = copy.deepcopy(entry)
    source = dict(new.get("source") or {})
    if why is not None:
        held = source.get("held")
        since = held.get("since") if isinstance(held, dict) else None
        source["held"] = {"since": since or date, "commit": sha, "why": why}
    else:
        assert meta is not None
        _copy_leaves(new, meta.model_dump(mode="json"))
        new.setdefault("status", {})["last_updated"] = date
        source.pop("held", None)
        source["commit"] = sha
    new["source"] = source
    if new == entry:
        return Rebuilt(name, rel, "held" if why else "unchanged", commit=sha, why=why, reports=reports,
                       mintd_error=mintd_error)
    if write:
        from ._atomic import _atomic_write_text

        _atomic_write_text(path, dump(new))
    changes = tuple(_dict_diff(entry, new))
    return Rebuilt(name, rel, "held" if why else "updated", commit=sha, why=why, changes=changes, reports=reports,
                   mintd_error=mintd_error)


def _refusal(
    product: Path, catalog: "CatalogClient", unreadable: tuple[str, ...] = (),
) -> tuple[str | None, "Metadata | None", bool]:
    """Why a product can't update its entry, or None; its metadata when it
    could be read; and whether the reason is mintd's own failure. Each reason
    is one line and quotes none of the product's values: it goes into a
    commit status and the registry's issue."""
    import yaml
    from pydantic import ValidationError

    from .check import check_project
    from .model import Metadata

    file = product / "metadata.json"
    if file.is_symlink():
        return "metadata.json is a link, which a rebuild doesn't follow", None, False
    if not file.is_file():
        return "the repository has no metadata.json", None, False
    try:
        meta = Metadata.from_json_file(file)
    except ValidationError as exc:
        why = f"metadata.json doesn't validate ({exc.error_count()} error(s)); run mintd check in its repository"
        return why, None, False
    except (OSError, ValueError):
        return "metadata.json can't be read", None, False
    paths = [o.path for o in meta.data_products.outputs]
    if meta.data_products.primary is not None:
        paths.append(meta.data_products.primary)
    if not all(_output_path_ok(p) for p in paths):
        return "an output path is empty, starts with '/' or '-', or holds a drive, '..', a backslash or a NUL", meta, False
    try:
        findings = check_project(product, upgrades=False, client=catalog)
    except (TypeError, AttributeError, LookupError) as exc:
        # More likely mintd's bug than the product's fault: one bad release would
        # hold every product, so the run says so and fails.
        why = f"mintd's error: check failed on this product ({type(exc).__name__}); tell mintd's maintainers"
        return why, meta, True
    except (OSError, ValueError, yaml.YAMLError, MintdError) as exc:
        return f"mintd check can't read the product ({type(exc).__name__}); run mintd check in its repository", meta, False
    errors = [f for f in findings if f.severity == "error"]
    if not errors:
        return None, meta, False
    kinds = sorted({str(f.kind) for f in errors})
    why = f"mintd check reports {len(errors)} error(s) ({', '.join(kinds)}); run mintd check in its repository"
    if "catalog_unresolved" in kinds and unreadable:
        why += f"; registry entries that can't be read: {', '.join(unreadable)}"
    return why, meta, False


def _output_path_ok(path: str) -> bool:
    """A declared output path a consumer can import: a relative posix path
    inside the product that git and dvc never read as an option. The rule the
    model is to apply to `DataProductOutput.path`, pinned here until it does."""
    from pathlib import PureWindowsPath

    return bool(path) and not path.startswith(("-", "/")) and "\\" not in path and "\x00" not in path \
        and ".." not in path.split("/") and not PureWindowsPath(path).drive


def _copy_leaves(entry: dict[str, Any], product: dict[str, Any]) -> None:
    """Set each of ``COPIED_LEAVES`` in ``entry`` from ``product``. A list's
    items are the product's, each starting from the entry's item at the same
    path, so a key the entry keeps on an item (a publish stamp, a key the model
    doesn't know) stays with it."""
    lists: dict[str, list[str]] = {}
    for leaf in COPIED_LEAVES:
        if "[]." in leaf:
            where, _, key = leaf.partition("[].")
            lists.setdefault(where, []).append(key)
            continue
        *parents, key = leaf.split(".")
        _at(entry, parents)[key] = _get(product, leaf)
    for where, keys in lists.items():
        *parents, field = where.split(".")
        kept = {item.get("path"): item for item in (_get(entry, where) or []) if isinstance(item, dict)}
        items = []
        for item in _get(product, where) or []:
            merged = dict(kept.get(item.get("path"), {}))
            merged.update({k: item.get(k) for k in keys})
            items.append(merged)
        _at(entry, parents)[field] = items


def _reports(entry: dict[str, Any], meta: "Metadata") -> tuple[str, ...]:
    from ._registry_git_ops import _credentials_out

    product = meta.model_dump(mode="json")
    out = [
        f"{field} differs from the entry's; the registry keeps its own (a registry PR changes it)"
        for field in _REPORTED
        if _get(product, field) is not None and _get(product, field) != _get(entry, field)
    ]
    url = meta.repository.github_url
    if _credentials_out(url) != url.strip():
        out.append("metadata.json holds a credential in repository.github_url; remove it from the file and revoke it")
    return tuple(out)


def _get(tree: dict[str, Any], dotted: str) -> Any:
    cur: Any = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _at(tree: dict[str, Any], parents: list[str]) -> dict[str, Any]:
    """The mapping at ``parents`` in ``tree``, made where missing."""
    cur = tree
    for part in parents:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = cur[part] = {}
        cur = nxt
    return cur
