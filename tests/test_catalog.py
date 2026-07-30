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


# ---------------------------------------------------------------------------
# Reusing an already-open update PR (git backend only — InMemoryCatalogClient
# has no PR lifecycle). Setup shape: register with the fake's default
# auto_merge_pr=True so the entry lands on main, then flip it off so the
# update PR stays genuinely open, the way production looks while a registry
# admin has not merged yet.
# ---------------------------------------------------------------------------


def _pr_open_client(
    tmp_path: Path, remote: Path,
) -> tuple[GitCatalogClient, _FakeRegistryGitOps]:
    git_ops = _FakeRegistryGitOps()
    client = GitCatalogClient(
        registry_repo_url=str(remote),
        work_dir=tmp_path / "cache",
        git_ops=git_ops,
    )
    client.register(_load_metadata(name="proj"))
    git_ops.auto_merge_pr = False
    git_ops.pr_exists_calls.clear()
    return client, git_ops


def _desc(text: str) -> Callable[[dict[str, Any]], None]:
    def mutate(data: dict[str, Any]) -> None:
        data["metadata"]["description"] = text
    return mutate


def _remote_show(remote: Path, ref: str) -> str:
    import subprocess
    return subprocess.run(
        ["git", f"--git-dir={remote}", "show", ref],
        capture_output=True, text=True, check=True,
    ).stdout


def test_update_asks_registry_for_open_pr_before_branching(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """AC5: `pr_exists_for_branch` is called from src/ — the authoritative,
    machine-independent check that replaces per-machine `.mintd_pending.json`
    for the update path.

    Only once the branch exists, though: no branch means no PR can be open on
    it, so the first update spends no `gh` call at all.
    """
    client, git_ops = _pr_open_client(tmp_path, remote_registry_empty)

    client.update(_load_metadata(name="proj", mutate=_desc("one")))
    assert git_ops.pr_exists_calls == []

    client.update(_load_metadata(name="proj", mutate=_desc("two")))
    assert git_ops.pr_exists_calls == ["update/proj"]


def test_update_reuses_open_pr_instead_of_opening_a_second(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """AC6: publishing twice before an admin merges reuses the open PR.

    Fails at HEAD~ with a non-fast-forward GitOpError from `push_branch` —
    this is the crash the issue is about.
    """
    client, git_ops = _pr_open_client(tmp_path, remote_registry_empty)

    r1 = client.update(_load_metadata(name="proj", mutate=_desc("one")))
    r2 = client.update(_load_metadata(name="proj", mutate=_desc("two")))

    assert r1.pr_reused is False
    assert r2.pr_reused is True
    assert r2.pr_number == r1.pr_number
    # No second PR was opened for the same product.
    assert sorted(git_ops.open_prs) == ["register/proj", "update/proj"]


def test_update_stacks_onto_remote_branch_tip(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """The reused PR carries the NEWER entry, and the branch is the old tip
    plus one commit — not a rival history forked off main."""
    client, _ = _pr_open_client(tmp_path, remote_registry_empty)

    client.update(_load_metadata(name="proj", mutate=_desc("one")))
    client.update(_load_metadata(name="proj", mutate=_desc("two")))

    # project.type in the minimal fixture is `data`, hence catalog/data/.
    on_branch = _remote_show(
        remote_registry_empty, "update/proj:catalog/data/proj.yaml",
    )
    assert "two" in on_branch
    assert "one" not in on_branch

    import subprocess
    count = subprocess.run(
        ["git", f"--git-dir={remote_registry_empty}", "rev-list", "--count",
         "main..update/proj"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert count == "2"


def test_update_reuse_path_skips_commit_when_branch_already_current(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """The idempotent retry the recovery hint tells users to run must not
    reach `git commit` — it exits 1 on a clean tree. Guarded by comparing the
    entry already on the branch, not `is_working_tree_clean` (the cache tree
    is never reliably clean: `.mintd_pending.json` is untracked)."""
    import subprocess
    client, _ = _pr_open_client(tmp_path, remote_registry_empty)

    client.update(_load_metadata(name="proj", mutate=_desc("one")))
    r2 = client.update(_load_metadata(name="proj", mutate=_desc("two")))

    def tip() -> str:
        return subprocess.run(
            ["git", f"--git-dir={remote_registry_empty}", "rev-parse", "update/proj"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    before = tip()
    # Third update with metadata identical to the second.
    r3 = client.update(_load_metadata(name="proj", mutate=_desc("two")))
    assert r3.pr_number == r2.pr_number
    assert tip() == before, "an empty commit was pushed onto the open PR branch"


def test_update_stacks_onto_a_leftover_branch_with_no_open_pr(
    tmp_path: Path, remote_registry_empty: Path,
) -> None:
    """A squash-merged PR leaves its branch behind, and `gh pr list --state
    open` reports nothing for it. Keying the branching decision on the PR
    rather than the branch would put us back on a main-based branch whose push
    can never fast-forward — wedging every later publish of that product.

    Shape reproduced here: the branch keeps a commit that main does not carry,
    the PR is gone, and the next update must still land.
    """
    import subprocess
    client, git_ops = _pr_open_client(tmp_path, remote_registry_empty)

    client.update(_load_metadata(name="proj", mutate=_desc("one")))

    # Squash-merge: main gains an equivalent commit with a different sha, and
    # nobody deletes the branch. Then the PR closes.
    seed = tmp_path / "squash"
    subprocess.run(["git", "clone", str(remote_registry_empty), str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@m", "-c", "user.name=t", "merge",
                    "--squash", "origin/update/proj"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@m", "-c", "user.name=t", "commit",
                    "-m", "Update proj (#1)"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=seed, check=True,
                   capture_output=True)
    git_ops.open_prs.pop("update/proj")

    result = client.update(_load_metadata(name="proj", mutate=_desc("three")))

    # A fresh PR — there was none to reuse — but on the SURVIVING branch.
    assert result.pr_reused is False
    assert result.pr_number is not None
    on_branch = _remote_show(
        remote_registry_empty, "update/proj:catalog/data/proj.yaml",
    )
    assert "three" in on_branch
