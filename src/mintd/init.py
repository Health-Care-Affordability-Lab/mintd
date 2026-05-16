"""Project scaffolding — `mintd init`.

Writes a minimal-valid `metadata.json`, `.gitignore`, and runs git/DVC init.
The CLI layer is the only caller; this module never touches argparse.
"""

from __future__ import annotations

import getpass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ._init_ops import InitOps, SubprocessInitOps
from .model import Metadata


_DVC_INIT_TYPES: frozenset[str] = frozenset({"data", "code", "project"})


class InitDestinationExists(Exception):
    """`metadata.json` already exists at the target. Refusing to overwrite."""


def init_project(
    *,
    project_type: Literal["data", "code", "project", "enclave"],
    name: str,
    target_dir: Path,
    use_current_repo: bool = False,
    ops: InitOps | None = None,
) -> Path:
    """Initialize a fresh mintd project.

    By default, scaffolds into a new subdirectory ``target_dir/{type}_{name}``
    (matching legacy ``mintd``'s default). Pass ``use_current_repo=True`` to
    scaffold into ``target_dir`` directly — useful when retrofitting an
    existing git repo.

    Writes ``metadata.json`` + ``.gitignore``, runs ``git init`` (unless the
    project is already a git repo), and for non-enclave types ``dvc init``.
    Returns the actual project path that was scaffolded into.
    """
    if use_current_repo:
        project_path = target_dir
    else:
        project_path = target_dir / f"{project_type}_{name}"

    project_path.mkdir(parents=True, exist_ok=True)
    metadata_path = project_path / "metadata.json"
    if metadata_path.exists():
        raise InitDestinationExists(metadata_path)

    metadata_path.write_text(_metadata_template(project_type, name) + "\n")
    (project_path / ".gitignore").write_text(_GITIGNORE_TEMPLATE)

    ops = ops or SubprocessInitOps()
    ops.git_init(project_path)
    if project_type in _DVC_INIT_TYPES:
        ops.dvc_init(project_path)

    return project_path


def _metadata_template(
    project_type: Literal["data", "code", "project", "enclave"], name: str
) -> str:
    """Build a minimal-valid Metadata instance and dump as JSON."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    user = getpass.getuser()
    data = {
        "schema_version": "2.0",
        "mint": {"version": "0.0.1", "commit_hash": ""},
        "project": {
            "type": project_type,
            "name": name,
            "full_name": f"{project_type}_{name}",
            "created_at": now,
            "created_by": user,
        },
        "metadata": {"description": "", "tags": []},
        "ownership": {"team": "", "maintainers": [user]},
        "access_control": {"teams": []},
        "governance": {"classification": "private", "contract_info": ""},
        "data_products": {"primary": None, "outputs": []},
        "repository": {
            "github_url": "",
            "default_branch": "main",
            "visibility": "private",
            "mirror": {"url": "", "purpose": ""},
        },
        "status": {
            "state": "active",
            "last_updated": now,
            "last_published_version": "",
        },
    }
    # Round-trip through Metadata to validate the template is correct.
    return Metadata.model_validate(data).model_dump_json(indent=2)


_GITIGNORE_TEMPLATE = """\
# Python
__pycache__/
*.pyc
.venv/
venv/
dist/
build/
*.egg-info/

# DVC
*.tmp

# Editor / OS
.DS_Store
*.swp
.idea/
.vscode/
"""
