"""Consumer-side dependency view.

Unifies the two ways DVC records imports on the consumer side:
  - standalone `.dvc` files (from `dvc import`)
  - `dvc.lock` stage deps with a `repo:` block (from pipeline stages)

`scan_imports()` walks both and returns a deduplicated list of typed
`DataDependency` records. PR 7's `mintd check --upgrades` consumes this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict


class NotAnImportError(Exception):
    """Raised when a `.dvc` file has no `deps[*].repo` — it's a `dvc add` track,
    not a `dvc import`. `scan_imports` catches and skips."""


class DataDependency(BaseModel):
    """Typed view over one imported dependency on the consumer side."""

    model_config = ConfigDict(frozen=True)

    source: Path
    kind: Literal["dvc_file", "dvc_lock_stage"]
    producer_repo: str
    contract_pin: str
    output_path: str
    local_path: str
    artifact_md5: str | None = None
    stage_name: str | None = None

    @classmethod
    def from_dvc_file(cls, path: Path) -> "DataDependency":
        data = _read_yaml(path)
        deps = data.get("deps") or []
        outs = data.get("outs") or []
        if not deps or not isinstance(deps[0], dict) or "repo" not in deps[0]:
            raise NotAnImportError(f"{path} has no deps[*].repo — not an import")

        dep = deps[0]
        repo = dep["repo"]
        out = outs[0] if outs else {}
        return cls(
            source=path,
            kind="dvc_file",
            producer_repo=repo["url"],
            contract_pin=repo["rev_lock"],
            output_path=dep["path"],
            local_path=out.get("path", ""),
            artifact_md5=out.get("md5"),
            stage_name=None,
        )

    @classmethod
    def from_dvc_lock_stage(
        cls,
        stage_name: str,
        stage_block: dict[str, Any],
        lock_path: Path,
    ) -> list["DataDependency"]:
        """Extract every dep with a `repo:` block from one stage."""
        results: list["DataDependency"] = []
        for dep in stage_block.get("deps") or []:
            if not isinstance(dep, dict) or "repo" not in dep:
                continue
            repo = dep["repo"]
            results.append(
                cls(
                    source=lock_path,
                    kind="dvc_lock_stage",
                    producer_repo=repo["url"],
                    contract_pin=repo["rev_lock"],
                    output_path="",
                    local_path=dep.get("path", ""),
                    artifact_md5=dep.get("md5"),
                    stage_name=stage_name,
                )
            )
        return results


def scan_imports(
    repo_root: Path,
    *,
    under: str = "data/imports",
) -> list[DataDependency]:
    """Walk both dependency-recording shapes and return the deduplicated union.

    Dedup key: `(producer_repo, local_path, contract_pin)`. `.dvc` file form
    wins on collision (it's the more granular record).
    """
    results: list[DataDependency] = []

    imports_dir = repo_root / under
    if imports_dir.exists():
        for dvc_path in sorted(imports_dir.rglob("*.dvc")):
            try:
                results.append(DataDependency.from_dvc_file(dvc_path))
            except NotAnImportError:
                continue

    lock_path = repo_root / "dvc.lock"
    if lock_path.exists():
        lock = _read_yaml(lock_path)
        for stage_name, stage_block in (lock.get("stages") or {}).items():
            if isinstance(stage_block, dict):
                results.extend(
                    DataDependency.from_dvc_lock_stage(stage_name, stage_block, lock_path)
                )

    return _dedup(results)


def _dedup(deps: list[DataDependency]) -> list[DataDependency]:
    seen: dict[tuple[str, str, str], DataDependency] = {}
    for dep in deps:
        key = (dep.producer_repo, dep.local_path, dep.contract_pin)
        existing = seen.get(key)
        if existing is None:
            seen[key] = dep
        elif existing.kind == "dvc_lock_stage" and dep.kind == "dvc_file":
            seen[key] = dep
    return list(seen.values())


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        return {}
    return data
