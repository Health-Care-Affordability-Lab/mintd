"""A synthetic DVC pipeline project — `dvc.yaml` + `dvc.lock` that **real dvc
parses**.

Two things make this different from the raw-text lock writers in
`tests/test_fast_sync.py`. First, nothing in the tree authors a `foreach`
stage, so the `base` (dvc.yaml) vs `base@a` / `base@b` (dvc.lock) name split —
the shape every lab pipeline actually has — is unrepresentable today. Second,
`dvc.yaml`'s `outs` entries must be plain strings; the `outs: [{path: …}]`
mapping form real dvc rejects with ``'./dvc.yaml' validation failed``, which is
how a fixture can be "valid YAML" and still describe a project dvc would never
accept.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from tests._harness.git import _git

#: A real 32-hex md5. `parse_dvc_lock_outs` skips outs with neither `md5` nor
#: `files`, so a placeholder here silently empties the result.
MD5_A = "0123456789abcdef0123456789abcdef"
MD5_B = "fedcba9876543210fedcba9876543210"
MD5_FLAT = "aaaabbbbccccddddaaaabbbbccccdddd"

_NOOP = "python -c pass"


def _write_dvc_dir(project: Path, *, remote: str = "origin") -> None:
    cfg = project / ".dvc" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "[core]\n"
        f"    remote = {remote}\n"
        f"['remote \"{remote}\"']\n"
        "    url = s3://test-bucket\n",
        encoding="utf-8",
    )


def build_synthetic_project(root: Path, *, foreach: bool = False) -> Path:
    """A project dvc can read. `foreach=True` authors the templated form."""
    root.mkdir(parents=True, exist_ok=True)
    # dvc refuses to operate outside an SCM repo unless `core.no_scm` is set,
    # and setting it would make this fixture a project no researcher has.
    _git(["init", "-b", "main", str(root)])
    _write_dvc_dir(root)

    if foreach:
        (root / "dvc.yaml").write_text(
            "stages:\n"
            "  base:\n"
            "    foreach:\n"
            "      a: {out: data/a.parquet}\n"
            "      b: {out: data/b.parquet}\n"
            "    do:\n"
            f"      cmd: {_NOOP}\n"
            "      outs:\n"
            "        - ${item.out}\n",
            encoding="utf-8",
        )
        (root / "dvc.lock").write_text(
            "schema: '2.0'\n"
            "stages:\n"
            "  base@a:\n"
            f"    cmd: {_NOOP}\n"
            "    outs:\n"
            "    - path: data/a.parquet\n"
            f"      md5: {MD5_A}\n"
            "      size: 11\n"
            "  base@b:\n"
            f"    cmd: {_NOOP}\n"
            "    outs:\n"
            "    - path: data/b.parquet\n"
            f"      md5: {MD5_B}\n"
            "      size: 22\n",
            encoding="utf-8",
        )
    else:
        (root / "dvc.yaml").write_text(
            "stages:\n"
            "  build:\n"
            f"    cmd: {_NOOP}\n"
            "    outs:\n"
            "      - data/final.parquet\n",
            encoding="utf-8",
        )
        (root / "dvc.lock").write_text(
            "schema: '2.0'\n"
            "stages:\n"
            "  build:\n"
            f"    cmd: {_NOOP}\n"
            "    outs:\n"
            "    - path: data/final.parquet\n"
            f"      md5: {MD5_FLAT}\n"
            "      size: 33\n",
            encoding="utf-8",
        )
    return root


@pytest.fixture
def synthetic_project(tmp_path: Path):
    """One directory per call — sharing one would let a later
    `synthetic_project(foreach=True)` overwrite the `dvc.yaml` / `dvc.lock` an
    earlier flat build is still holding a `Path` to, with no error (`git init`
    on an existing repo is idempotent)."""
    counter = itertools.count()

    def build(**kwargs) -> Path:
        return build_synthetic_project(
            tmp_path / f"synthetic-{next(counter)}", **kwargs
        )

    return build
