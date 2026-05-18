"""Local clone of the registry repo.

`GitCatalogClient` uses one of these to read/write catalog yaml files.
The cache itself doesn't shell out — git operations go through the
`RegistryGitOps` Protocol; only filesystem reads/writes happen here.

Layout in the cache mirrors the production registry repo:
    <work_dir>/
      catalog/
        data/<name>.yaml
        code/<name>.yaml
        project/<name>.yaml
        enclave/<name>.yaml
"""

from __future__ import annotations

from pathlib import Path

from ._catalog_serializer import deserialize
from ._registry_git_ops import RegistryGitOps
from .catalog import CatalogEntry, CatalogFilter

# Project types correspond to the four catalog subdirectories. The model's
# `project.type` literal is the source of truth; this list must stay in sync.
_TYPE_DIRS = ("data", "code", "project", "enclave")


class CatalogCache:
    """Disk-backed cache of the registry repo.

    Initialization is lazy — the first `ensure_fresh()` call clones if no
    repo is present, otherwise fetches + resets to `origin/main`.

    Invariants:
      - After a successful `ensure_fresh()`, the working tree matches
        `origin/main`.
      - `read_entry` / `list_entries` always read what's currently on disk.
        Callers that want fresh data must call `ensure_fresh()` first.
      - `write_entry` writes to the working tree only — pushing is the
        caller's job (via `RegistryGitOps`).
    """

    def __init__(
        self,
        *,
        work_dir: Path,
        registry_url: str,
        git_ops: RegistryGitOps,
    ) -> None:
        self._work_dir = work_dir
        self._registry_url = registry_url
        self._git_ops = git_ops

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def ensure_fresh(self) -> None:
        """Clone if absent, otherwise fetch + reset --hard origin/main."""
        if not (self._work_dir / ".git").exists():
            self._work_dir.parent.mkdir(parents=True, exist_ok=True)
            self._git_ops.clone(self._registry_url, self._work_dir)
            return
        self._git_ops.fetch(self._work_dir)
        self._git_ops.reset_hard(self._work_dir, "origin/main")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_entry(self, name: str) -> CatalogEntry | None:
        """Return the deserialized CatalogEntry for `name`, or None if missing.

        Searches all four `catalog/<type>/` subdirectories — entry names are
        unique across types (validated by tests).
        """
        path = self._find_entry_path(name)
        if path is None:
            return None
        return deserialize(path.read_text(encoding="utf-8"))

    def list_entries(self, filter: CatalogFilter | None = None) -> list[CatalogEntry]:
        """Walk all catalog yaml files, optionally filter by project type."""
        catalog_dir = self._work_dir / "catalog"
        if not catalog_dir.is_dir():
            return []

        results: list[CatalogEntry] = []
        type_dirs = [filter.project_type] if filter and filter.project_type else _TYPE_DIRS
        for type_name in type_dirs:
            subdir = catalog_dir / type_name
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.glob("*.yaml")):
                results.append(deserialize(path.read_text(encoding="utf-8")))
        return results

    # ------------------------------------------------------------------
    # Writes (working tree only — caller pushes)
    # ------------------------------------------------------------------

    def write_entry(self, entry: CatalogEntry, content: str) -> Path:
        """Write `content` (a yaml string) to the working tree at the
        location implied by the entry's project.type and project.name.

        Returns the path written for the caller to log/diagnose.
        """
        project_type = self._entry_project_type(entry)
        name = self._entry_project_name(entry)
        target_dir = self._work_dir / "catalog" / project_type
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}.yaml"
        target.write_text(content, encoding="utf-8")
        return target

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_entry_path(self, name: str) -> Path | None:
        catalog_dir = self._work_dir / "catalog"
        if not catalog_dir.is_dir():
            return None
        for type_name in _TYPE_DIRS:
            candidate = catalog_dir / type_name / f"{name}.yaml"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _entry_project_name(entry: CatalogEntry) -> str:
        dumped = entry.model_dump()
        project = dumped.get("project") or {}
        name = project.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("CatalogEntry missing project.name")
        return name

    @staticmethod
    def _entry_project_type(entry: CatalogEntry) -> str:
        dumped = entry.model_dump()
        project = dumped.get("project") or {}
        project_type = project.get("type")
        if project_type not in _TYPE_DIRS:
            raise ValueError(f"CatalogEntry has invalid project.type: {project_type!r}")
        return project_type
