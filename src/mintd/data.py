"""Orchestration for the `mintd data ...` command family.

Slice 4: `import_product` — catalog lookup → path resolution → `dvc import`.
The CLI surface (PR 8) is a thin shell over `import_product`; PR 8 also
adds `render_imports` here, and later slices add `bump_imports`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._dvc_ops import DvcOps
from .catalog import CatalogClient


class MissingPrimaryDataProduct(Exception):
    """The catalog entry has no `data_products.primary` and no `--path`/`--all`."""


class RevRequiresExplicitPath(Exception):
    """`--rev` was supplied without `--path`. Until PR 6's ProducerView lands,
    we can't resolve `data_products.primary` at a non-HEAD commit."""


class ImportDestinationExists(Exception):
    """A `.dvc` file already exists at the destination. The consumer resolves
    by passing `force=True` or removing the file first."""


def import_product(
    client: CatalogClient,
    dvc_ops: DvcOps,
    name: str,
    *,
    dest_root: Path,
    path: str | None = None,
    rev: str | None = None,
    all_outputs: bool = False,
    force: bool = False,
) -> list[Path]:
    """Catalog-driven `dvc import`. Returns the list of `.dvc` files written."""

    entry = client.fetch(name)
    dumped = entry.model_dump()

    if rev is not None and path is None and not all_outputs:
        raise RevRequiresExplicitPath(
            f"--rev {rev!r} requires an explicit --path until ProducerView lands (PR 6)"
        )

    paths = _resolve_paths(dumped, path=path, all_outputs=all_outputs, name=name)
    repo_url = _require_repo_url(dumped, name=name)

    produced: list[Path] = []
    for p in paths:
        dest = dest_root / Path(p.rstrip("/")).name
        target_dvc = dest.parent / (dest.name + ".dvc")
        if target_dvc.exists() and not force:
            raise ImportDestinationExists(
                f"{target_dvc} already exists; pass force=True or remove it"
            )
        produced.append(
            dvc_ops.import_(
                repo_url=repo_url,
                path=p,
                dest=dest,
                rev=rev,
                force=force,
            )
        )
    return produced


def _resolve_paths(
    entry: dict[str, Any],
    *,
    path: str | None,
    all_outputs: bool,
    name: str,
) -> list[str]:
    data_products = entry.get("data_products") or {}

    if all_outputs:
        outputs = data_products.get("outputs") or []
        return [o["path"] for o in outputs if isinstance(o, dict) and "path" in o]

    if path is not None:
        return [path]

    primary = data_products.get("primary")
    if not primary:
        raise MissingPrimaryDataProduct(
            f"catalog entry {name!r} has no data_products.primary; pass --path or --all"
        )
    return [primary]


def _require_repo_url(entry: dict[str, Any], *, name: str) -> str:
    repo = entry.get("repository") or {}
    url = repo.get("github_url")
    if not url:
        raise ValueError(f"catalog entry {name!r} has no repository.github_url")
    return url
