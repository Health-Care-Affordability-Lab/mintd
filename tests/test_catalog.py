"""Tests for `CatalogClient` — parameterized across both implementations.

Every test runs against `InMemoryCatalogClient` and `GitCatalogClient`. The
git-backed client uses a `_FakeRegistryGitOps` that does real local git but
stubs `gh` (auto-merging PRs to main so read-after-write semantics hold in
tests). This is the slice-3 retro's binding question: does the
`CatalogClient` Protocol seam hold up across the two implementations?
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mintd.catalog import (
    CatalogAlreadyExists,
    CatalogClient,
    CatalogFilter,
    CatalogNotFound,
    FieldChange,
    GitCatalogClient,
    InMemoryCatalogClient,
    RegisterResult,
    UpdateResult,
)
from mintd.model import Metadata

from tests._fakes.registry_git_ops import _FakeRegistryGitOps


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _load_metadata(
    name: str = "test_project",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> Metadata:
    """Load the minimal fixture as a Metadata instance, optionally renaming
    project.name and mutating the dict before validation.
    """
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    if mutate is not None:
        mutate(data)
    return Metadata.model_validate(data)


# ---------------------------------------------------------------------------
# Parameterized client fixture
# ---------------------------------------------------------------------------


@pytest.fixture(params=["in_memory", "git"])
def client(request, tmp_path: Path, remote_registry_empty: Path) -> CatalogClient:
    if request.param == "in_memory":
        return InMemoryCatalogClient()
    return GitCatalogClient(
        registry_repo_url=str(remote_registry_empty),
        work_dir=tmp_path / "cache",
        git_ops=_FakeRegistryGitOps(),
    )


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_stores_entry(client: CatalogClient) -> None:
    """register(metadata) on a fresh client stores the entry and makes it
    fetchable. Both implementations return the same projected entry."""
    m = _load_metadata(name="data_alpha")
    result = client.register(m)
    assert isinstance(result, RegisterResult)
    assert result.name == "data_alpha"
    assert result.dry_run is False

    fetched = client.fetch("data_alpha")
    expected = m.to_catalog_entry().model_dump()
    # Normalize through json so datetime vs iso-string differences (yaml
    # round-trip vs in-memory) don't matter.
    assert _round(fetched.model_dump()) == _round(expected)


def test_register_duplicate_raises(client: CatalogClient) -> None:
    """A second register() with the same project.name raises."""
    client.register(_load_metadata(name="dup"))
    with pytest.raises(CatalogAlreadyExists):
        client.register(_load_metadata(name="dup"))


def test_register_dry_run_does_not_mutate(client: CatalogClient) -> None:
    """register(dry_run=True) returns RegisterResult(dry_run=True) without
    persisting the entry. Subsequent fetch raises CatalogNotFound."""
    result = client.register(_load_metadata(name="ghost"), dry_run=True)
    assert result.dry_run is True
    assert result.name == "ghost"
    with pytest.raises(CatalogNotFound):
        client.fetch("ghost")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_missing_raises_not_found(client: CatalogClient) -> None:
    with pytest.raises(CatalogNotFound):
        client.fetch("nonexistent")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_returns_field_changes(client: CatalogClient) -> None:
    """update() with a mutated CATALOG field returns UpdateResult with one
    FieldChange describing the diff (canonical-tier only)."""
    client.register(_load_metadata(name="proj"))

    def change_desc(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = "updated description"

    result = client.update(_load_metadata(name="proj", mutate=change_desc))
    assert isinstance(result, UpdateResult)
    assert result.name == "proj"
    assert result.dry_run is False
    assert len(result.changes) == 1
    change = result.changes[0]
    assert isinstance(change, FieldChange)
    assert change.field_path == "metadata.description"
    assert change.before == ""
    assert change.after == "updated description"


def test_update_missing_raises_not_found(client: CatalogClient) -> None:
    with pytest.raises(CatalogNotFound):
        client.update(_load_metadata(name="never_registered"))


def test_catalog_update_empty_diff_short_circuits_no_git_ops(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """Slice 35 defensive: when the projected entry is byte-identical to the
    cached one (zero diff), `GitCatalogClient.update` must NOT invoke
    `commit_all` — a `git commit` on a clean tree would exit 1 and crash
    publish with a raw `CalledProcessError`. The early-return guards against
    that. Scoped to the git backend; `InMemoryCatalogClient` already handles
    empty diff cleanly."""

    git_ops = _FakeRegistryGitOps()
    git_client = GitCatalogClient(
        registry_repo_url=str(remote_registry_empty),
        work_dir=tmp_path / "cache",
        git_ops=git_ops,
    )
    git_client.register(_load_metadata(name="proj"))

    # Now arm the trap: any further commit_all must be the empty-diff bug
    # (the early-return should bypass commit_all entirely).
    def _trap(repo_dir: Path, message: str) -> None:
        raise AssertionError(
            "commit_all must not be called when the catalog diff is empty"
        )
    git_ops.commit_all = _trap  # type: ignore[assignment]

    # Re-update with byte-identical metadata → zero diff.
    result = git_client.update(_load_metadata(name="proj"))

    assert isinstance(result, UpdateResult)
    assert result.name == "proj"
    assert result.changes == []
    assert result.dry_run is False
    assert result.pr_number is None
    assert result.pr_url is None


def test_update_dry_run_returns_changes_without_mutating(client: CatalogClient) -> None:
    """update(dry_run=True) returns the would-be UpdateResult; a subsequent
    fetch shows the OLD entry's description."""
    client.register(_load_metadata(name="proj"))

    def change_desc(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = "would-be"

    result = client.update(_load_metadata(name="proj", mutate=change_desc), dry_run=True)
    assert result.dry_run is True
    assert len(result.changes) == 1

    after = client.fetch("proj")
    assert after.model_dump()["metadata"]["description"] == ""


def test_update_data_products_appears_in_changes(client: CatalogClient) -> None:
    """data_products.* is in the catalog post-2026-05-14 (audience filter
    dropped). Mutating it surfaces a FieldChange under `data_products`.

    Pre-drop, this test asserted `result.changes == []` because data_products
    was filtered out of the canonical projection. Now it's a normal catalog
    field and shows up in the diff like any other change.

    The "structural" register/update fix from slice 2 still holds: both paths
    go through `to_catalog_entry`, so the field can't be silently dropped on
    update but written on register. They produce identical entries.
    """
    client.register(_load_metadata(name="proj"))

    def add_output(data: dict[str, Any]) -> None:
        data["data_products"]["outputs"].append({
            "path": "out.parquet",
            "description": "primary output",
            "primary": True,
            "last_published": "2026-05-01",
        })

    updated = _load_metadata(name="proj", mutate=add_output)
    result = client.update(updated)

    assert any(c.field_path.startswith("data_products") for c in result.changes), (
        f"expected a data_products change in {result.changes}"
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty_returns_empty(client: CatalogClient) -> None:
    assert client.list() == []


def test_list_returns_all_registered(client: CatalogClient) -> None:
    client.register(_load_metadata(name="a"))
    client.register(_load_metadata(name="b"))
    client.register(_load_metadata(name="c"))
    assert len(client.list()) == 3


def test_list_filter_by_project_type(client: CatalogClient) -> None:
    def set_type(t: str) -> Callable[[dict[str, Any]], None]:
        def _m(data: dict[str, Any]) -> None:
            data["project"]["type"] = t
        return _m

    client.register(_load_metadata(name="d1", mutate=set_type("data")))
    client.register(_load_metadata(name="d2", mutate=set_type("data")))
    client.register(_load_metadata(name="c1", mutate=set_type("code")))

    entries = client.list(filter=CatalogFilter(project_type="data"))
    assert len(entries) == 2
    for e in entries:
        assert e.model_dump()["project"]["type"] == "data"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_not_found_on_unknown(client: CatalogClient) -> None:
    status = client.status("unknown")
    assert status.state == "not_found"
    assert status.pr_number is None


def test_status_registered_after_register(client: CatalogClient) -> None:
    """After register() succeeds, status() returns 'registered'.

    For InMemoryCatalogClient this is trivially true.
    For GitCatalogClient this works because the fake auto-merges the PR,
    so the entry lands on main and the cache picks it up on the next
    ensure_fresh.
    """
    client.register(_load_metadata(name="now_registered"))
    status = client.status("now_registered")
    assert status.state == "registered"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round(obj: Any) -> Any:
    """Normalize through json so datetime vs iso-string round-trips match."""
    return json.loads(json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# Slice 12 — CatalogEntry display shortcuts
# ---------------------------------------------------------------------------


def test_catalog_entry_name_property(client: CatalogClient) -> None:
    client.register(_load_metadata(name="provider-xw"))
    entry = client.fetch("provider-xw")
    assert entry.name == "provider-xw"


def test_catalog_entry_project_type_property(client: CatalogClient) -> None:
    client.register(_load_metadata(name="provider-xw"))
    entry = client.fetch("provider-xw")
    # Fixture default project.type is "data".
    assert entry.project_type == "data"


def test_catalog_entry_description_property(client: CatalogClient) -> None:
    def set_desc(d: dict[str, Any]) -> None:
        d["metadata"]["description"] = "a useful project"

    client.register(_load_metadata(name="provider-xw", mutate=set_desc))
    entry = client.fetch("provider-xw")
    assert entry.description == "a useful project"


def test_catalog_entry_repo_url_property(client: CatalogClient) -> None:
    def set_url(d: dict[str, Any]) -> None:
        d["repository"]["github_url"] = "https://github.com/example-org/provider-xw"

    client.register(_load_metadata(name="provider-xw", mutate=set_url))
    entry = client.fetch("provider-xw")
    assert entry.repo_url == "https://github.com/example-org/provider-xw"


# ---------------------------------------------------------------------------
# Slice 36 — Pattern C: phase relabeling via reporter.update_status
# ---------------------------------------------------------------------------


class _RecordingReporter:
    """Minimal stub recording every update_status call. Doesn't render
    anything; just appends labels to .labels in order."""

    def __init__(self) -> None:
        self.labels: list[str] = []

    def update_status(self, msg: str) -> None:
        self.labels.append(msg)


def test_catalog_register_updates_status_between_phases(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    git_client = GitCatalogClient(
        registry_repo_url=str(remote_registry_empty),
        work_dir=tmp_path / "cache",
        git_ops=_FakeRegistryGitOps(),
    )
    rep = _RecordingReporter()
    git_client.register(_load_metadata(name="proj"), reporter=rep)  # type: ignore[arg-type]
    assert rep.labels == [
        "Writing catalog entry...",
        "Committing to registry...",
        "Pushing to registry...",
        "Opening PR...",
    ]


def test_catalog_update_updates_status_between_phases(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    git_client = GitCatalogClient(
        registry_repo_url=str(remote_registry_empty),
        work_dir=tmp_path / "cache",
        git_ops=_FakeRegistryGitOps(),
    )
    # First register (without reporter), then update with reporter.
    git_client.register(_load_metadata(name="proj"))

    def change_desc(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = "updated"

    rep = _RecordingReporter()
    git_client.update(_load_metadata(name="proj", mutate=change_desc), reporter=rep)  # type: ignore[arg-type]
    assert rep.labels == [
        "Writing catalog entry...",
        "Committing to registry...",
        "Pushing to registry...",
        "Opening PR...",
    ]
