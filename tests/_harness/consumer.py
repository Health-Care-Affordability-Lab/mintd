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
import yaml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"
ENCLAVE_MANIFEST = FIXTURES / "enclave_manifest_v2_minimal.yaml"


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
    enclave: bool = False,
    enclave_pin: str | None = None,
) -> Path:
    """A consumer tree. With `enclave=True` it also carries an
    `enclave_manifest.yaml`, which is the variant `check.py`'s enclave arm
    walks and which nothing in the tree composes a project around today.

    `enclave_pin` re-points the approved product at a pin that actually
    exists — the checked-in fixture's `4f7c2a1…` resolves nowhere, so without
    this the arm can only ever be walked as far as its first error.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(V2_MINIMAL, root / "metadata.json")
    for imp in imports or []:
        write_import(root, imp)
    if enclave:
        manifest = yaml.safe_load(ENCLAVE_MANIFEST.read_text(encoding="utf-8"))
        if enclave_pin is not None:
            for ap in manifest["approved_products"]:
                ap["pin"] = enclave_pin
        (root / "enclave_manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
    return root


def set_github_url(project: Path, url: str) -> None:
    """Rewrite `repository.github_url` in place."""
    path = project / "metadata.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["repository"]["github_url"] = url
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def consumer_project(tmp_path: Path):
    """Returns the builder, not a tree — a test that needs two producers, or
    the enclave variant, needs to say so.

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
