"""Tests for the local registry clone cache.

Uses real local git for the "remote" (a bare repo in tmp_path) and exercises
clone / fetch / read / write / list against it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mintd._catalog_cache import CatalogCache
from mintd._catalog_serializer import deserialize, serialize
from mintd.catalog import CatalogFilter
from mintd.model import Metadata

from tests._fakes.registry_git_ops import _FakeRegistryGitOps

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _make_cache(remote_registry: Path, work_dir: Path) -> CatalogCache:
    return CatalogCache(
        work_dir=work_dir,
        registry_url=str(remote_registry),
        git_ops=_FakeRegistryGitOps(),
    )


# ---------------------------------------------------------------------------
# ensure_fresh: clone / fetch
# ---------------------------------------------------------------------------


def test_ensure_fresh_clones_when_absent(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    assert (work / ".git").is_dir()
    assert (work / "catalog" / "data" / "seed_alpha.yaml").is_file()


def test_ensure_fresh_fetches_when_present(tmp_path: Path, remote_registry: Path) -> None:
    """A second ensure_fresh() does fetch + reset (no re-clone) and reverts
    local modifications to tracked files."""
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()

    seed_path = work / "catalog" / "data" / "seed_alpha.yaml"
    original = seed_path.read_text()
    seed_path.write_text("project:\n  name: hacked\n")

    cache.ensure_fresh()
    assert seed_path.read_text() == original, "reset --hard should revert local edits"


def test_ensure_fresh_picks_up_remote_change(tmp_path: Path, remote_registry: Path) -> None:
    """A change pushed to the remote shows up in the cache after the next
    ensure_fresh()."""
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    assert cache.read_entry("brand_new") is None

    # Push a new entry to the remote.
    other_clone = tmp_path / "other_clone"
    subprocess.run(["git", "clone", str(remote_registry), str(other_clone)], check=True)
    (other_clone / "catalog" / "data" / "brand_new.yaml").write_text(
        "project:\n  name: brand_new\n  type: data\n  full_name: data_brand_new\n"
        "metadata:\n  description: ''\n  tags: []\n"
    )
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                   cwd=str(other_clone), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "add brand_new"], cwd=str(other_clone), check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=str(other_clone), check=True)

    cache.ensure_fresh()
    entry = cache.read_entry("brand_new")
    assert entry is not None


# ---------------------------------------------------------------------------
# read_entry / list_entries
# ---------------------------------------------------------------------------


def test_read_entry_returns_none_for_missing(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    assert cache.read_entry("does_not_exist") is None


def test_read_entry_returns_seed(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    entry = cache.read_entry("seed_alpha")
    assert entry is not None
    assert entry.model_dump()["project"]["name"] == "seed_alpha"


def test_list_entries_walks_all_type_dirs(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    entries = cache.list_entries()
    names = {e.model_dump()["project"]["name"] for e in entries}
    assert "seed_alpha" in names


def test_list_entries_filter_by_type(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    entries = cache.list_entries(filter=CatalogFilter(project_type="data"))
    for e in entries:
        assert e.model_dump()["project"]["type"] == "data"
    # The seed is type=data, so it should appear.
    assert any(e.model_dump()["project"]["name"] == "seed_alpha" for e in entries)


def test_list_entries_filter_excludes_other_types(tmp_path: Path, remote_registry: Path) -> None:
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    # No code-type seed entries.
    entries = cache.list_entries(filter=CatalogFilter(project_type="code"))
    assert entries == []


# ---------------------------------------------------------------------------
# write_entry
# ---------------------------------------------------------------------------


def test_write_entry_stages_in_working_tree(tmp_path: Path, remote_registry: Path) -> None:
    """write_entry puts the file in the right subdirectory but doesn't push."""
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()

    import json
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = "fresh_entry"
    m = Metadata.model_validate(data)
    entry = deserialize(serialize(m))

    written = cache.write_entry(entry, serialize(m))
    assert written == work / "catalog" / "data" / "fresh_entry.yaml"
    assert written.is_file()


def test_write_entry_rejects_unknown_type(tmp_path: Path, remote_registry: Path) -> None:
    """A CatalogEntry whose project.type isn't one of the four literals can't
    be written."""
    from mintd.catalog import CatalogEntry
    work = tmp_path / "cache"
    cache = _make_cache(remote_registry, work)
    cache.ensure_fresh()
    bogus = CatalogEntry.model_validate({"project": {"name": "x", "type": "invalid"}})
    with pytest.raises(ValueError):
        cache.write_entry(bogus, "anything")
