"""A consumer project: `metadata.json` plus the imports that point at
producers.

What no existing builder can do is attach a **second** producer.
`_stage_project` (`tests/test_data.py:43-55`) copies one canonical `.dvc`
fixture into `data/imports/`, so the case where two producers both land an out
called `final` — the collision issue09 is about — cannot be constructed at all.
Here the imports are a list.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
from pathlib import Path

import pytest

from tests._harness.git import _git

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"


@dataclasses.dataclass(frozen=True)
class Import:
    """One `dvc import` pointer.

    `local_path` is deliberately free to collide across imports: that is the
    shape being modelled, not an accident.
    """

    name: str
    producer_url: str
    pin: str
    producer_path: str = "outputs/final/"
    local_path: str = "final"
    md5: str = "e8f3a2b1c4d5e6f7a8b9c0d1e2f3a4b5"


def write_import(project: Path, imp: Import, *, under: str = "data/imports") -> Path:
    """Write the `.dvc` shape `dvc import` produces — producer-side path at
    `deps[0].path`, coordinates at `deps[0].repo`, consumer-side path at
    `outs[0].path`. Matches `tests/fixtures/dvc_files/standalone_import.dvc`.
    """
    dest = project / under / f"{imp.name}.dvc"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "outs:\n"
        f"  - md5: {imp.md5}\n"
        "    size: 12345\n"
        f"    path: {imp.local_path}\n"
        "deps:\n"
        f"  - path: {imp.producer_path}\n"
        "    repo:\n"
        f"      url: {imp.producer_url}\n"
        "      rev: main\n"
        f"      rev_lock: {imp.pin}\n",
        encoding="utf-8",
    )
    return dest


def build_consumer_project(
    root: Path,
    *,
    imports: list[Import] | None = None,
) -> Path:
    """A consumer tree: `metadata.json` plus `imports`."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(V2_MINIMAL, root / "metadata.json")
    for imp in imports or []:
        write_import(root, imp)
    return root


def set_github_url(project: Path, url: str) -> None:
    """Rewrite `repository.github_url` in place."""
    path = project / "metadata.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["repository"]["github_url"] = url
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def consumer_project(tmp_path: Path):
    """Returns the builder, not a tree — a test that needs two producers needs
    to say so.

    Each call gets its own directory. A shared one silently merges the second
    build into the first: same `Path` back, both sets of imports on disk, and
    `metadata.json` re-copied over any edit — a test that then asserts on "its"
    tree passes while measuring the other one.
    """
    counter = itertools.count()

    def build(**kwargs) -> Path:
        return build_consumer_project(
            tmp_path / f"consumer-{next(counter)}", **kwargs
        )

    return build


@pytest.fixture
def fresh_project(tmp_path: Path):
    """A project as a researcher has it before any import: metadata.json, git
    and dvc initialized, all committed. One folder per call.

    `mintd init` would make it, but init refuses to run without a terminal."""
    from dvc.repo import Repo

    def build(name: str = "project") -> Path:
        root = build_consumer_project(tmp_path / name)
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        meta["project"].update(type="project", name="analysis", full_name="prj_analysis")
        meta["data_products"] = {"primary": None, "outputs": []}  # as `mintd init` writes it
        (root / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        _git(["init", "-b", "main", str(root)])
        Repo.init(str(root)).close()
        _git(["add", "-A"], cwd=root)
        _git(["commit", "-m", "fresh project"], cwd=root)
        return root

    return build
