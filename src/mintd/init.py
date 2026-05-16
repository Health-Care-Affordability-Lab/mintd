"""Project scaffolding — `mintd init`.

Renders the legacy `mintd create <type>` file set through the vendored
Jinja templates in `src/mintd/files/`, runs `git init`, and (for
non-enclave types) `dvc init`. Returns the project path and the list of
rendered files so the CLI can print per-file output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ._init_ops import InitOps, SubprocessInitOps
from ._templates import InitNameInvalid, render_scaffold


_DVC_INIT_TYPES: frozenset[str] = frozenset({"data", "code", "project"})


class InitDestinationExists(Exception):
    """`metadata.json` already exists at the target. Refusing to overwrite."""


def init_project(
    *,
    project_type: Literal["data", "code", "project", "enclave"],
    name: str,
    target_dir: Path,
    language: Literal["python", "r", "stata"] = "python",
    use_current_repo: bool = False,
    ops: InitOps | None = None,
) -> tuple[Path, list[Path]]:
    """Initialize a fresh mintd project.

    By default, scaffolds into ``target_dir/{project_type}_{name}`` (matching
    legacy ``mintd create``'s default). Pass ``use_current_repo=True`` to
    scaffold into ``target_dir`` directly — useful when retrofitting an
    existing git repo.

    Renders all language- and type-specific templates from
    ``src/mintd/files/`` through Jinja, runs ``git init`` (unless the
    directory is already a git repo), and for non-enclave types runs
    ``dvc init``. Returns ``(project_path, written_files)``.
    """
    if use_current_repo:
        project_path = target_dir
    else:
        project_path = target_dir / f"{project_type}_{name}"

    project_path.mkdir(parents=True, exist_ok=True)
    metadata_path = project_path / "metadata.json"
    if metadata_path.exists():
        raise InitDestinationExists(metadata_path)

    written = render_scaffold(
        project_type=project_type,
        name=name,
        language=language,
        target_dir=project_path,
    )

    ops = ops or SubprocessInitOps()
    ops.git_init(project_path)
    if project_type in _DVC_INIT_TYPES:
        ops.dvc_init(project_path)

    return project_path, written


__all__ = ["init_project", "InitDestinationExists", "InitNameInvalid"]
