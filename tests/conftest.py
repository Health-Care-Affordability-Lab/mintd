"""Shared fixtures for slice-3 tests.

`remote_registry` builds a local bare git repo with a seeded catalog tree —
used by anything that needs a "registry to clone from" without going to
GitHub.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_remote(tmp_path: Path, *, with_seed: bool) -> Path:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)])

    seed = tmp_path / "_seed"
    _git(["clone", str(remote), str(seed)])
    _git(["-c", "init.defaultBranch=main", "checkout", "-b", "main"], cwd=seed)

    (seed / "catalog" / "data").mkdir(parents=True)
    (seed / "catalog" / "code").mkdir(parents=True)
    (seed / "catalog" / "project").mkdir(parents=True)
    (seed / "catalog" / "enclave").mkdir(parents=True)
    (seed / ".gitkeep").write_text("")  # ensure the initial commit isn't empty

    if with_seed:
        (seed / "catalog" / "data" / "seed_alpha.yaml").write_text(
            "project:\n"
            "  name: seed_alpha\n"
            "  type: data\n"
            "  full_name: data_seed_alpha\n"
            "metadata:\n"
            "  description: seed entry\n"
            "  tags: []\n"
        )

    _git(["-c", "user.email=test@mintd", "-c", "user.name=test", "add", "-A"], cwd=seed)
    _git(["-c", "user.email=test@mintd", "-c", "user.name=test", "commit", "-m", "initial"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    return remote


@pytest.fixture
def remote_registry(tmp_path: Path) -> Path:
    """A local bare git repo, seeded with one catalog entry on `main`.

    Use for cache tests that benefit from pre-existing content. Returns the
    bare repo's path (pass as `registry_url`).
    """
    return _init_remote(tmp_path, with_seed=True)


@pytest.fixture
def remote_registry_empty(tmp_path: Path) -> Path:
    """A local bare git repo with the catalog tree initialized but no
    seeded entries. Use for parameterized client tests where the test
    controls all visible entries."""
    return _init_remote(tmp_path, with_seed=False)
