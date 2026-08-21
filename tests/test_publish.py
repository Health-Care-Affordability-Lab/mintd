import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch
from mintd.publish import (
    publish_project, CatalogUpdateFailed, DvcPushFailed, TagFailed, PublishError,
    VersionNotIncreasing
)
from mintd._dvc_ops import DvcNotInstalled, DvcPushError
from mintd._registry_git_ops import (
    GhNotInstalled, GitOpError, GitTagAlreadyExists, PRConflictError,
    RegistryBranchExists,
)
from tests._enclave_fixtures import client_with_provider_xw, stage_enclave_manifest
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.registry_git_ops import _FakeRegistryGitOps

class _FakeCatalogClient:
    """Minimal fake; slice-32 publish flow calls fetch() for the catalog
    diff. Default returns CatalogNotFound so dry-run / new-project tests
    treat it as first-publish (no catalog diff)."""

    def __init__(self, entries: dict | None = None) -> None:
        self._entries = entries or {}

    def update(self, metadata, *, dry_run=False, reporter=None): pass

    def fetch(self, name):
        from mintd.catalog import CatalogNotFound
        if name not in self._entries:
            raise CatalogNotFound(name)
        return self._entries[name]

def _seed_project(tmp_path: Path) -> Path:
    proj = tmp_path / "project"
    proj.mkdir()
    metadata = json.loads((Path(__file__).parent / "fixtures/metadata_v2_minimal.json").read_text(encoding="utf-8"))
    metadata["mint"]["version"] = "0.1.0"
    (proj / "metadata.json").write_text(json.dumps(metadata))
    subprocess.run(["git", "init", "-b", "main"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=proj, check=True)
    subprocess.run(["git", "add", "metadata.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=proj, check=True)
    return proj

@pytest.fixture(autouse=True)
def mock_check_project():
    with patch("mintd.publish.check_project", return_value=[]):
        yield

def test_publish_dry_run_returns_diff(tmp_path):
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    
    result = publish_project(
        project_path=proj,
        version="0.1.2",
        dry_run=True,
        client=_FakeCatalogClient(),
        dvc_ops=dvc,
        git_ops=git,
    )
    
    assert result.dry_run
    assert result.version == "0.1.2"
    assert "0.1.0" in (proj / "metadata.json").read_text(encoding="utf-8")
    assert len(dvc.push_calls) == 0
    assert len(git.tag_calls) == 0
    assert len(git.reset_hard_calls) == 0
    assert any(c.field_path == "mint.version" for c in result.diff)

def test_publish_increments_patch_version(tmp_path):
    proj = _seed_project(tmp_path)
    
    result = publish_project(
        project_path=proj,
        client=_FakeCatalogClient(),
        dvc_ops=_FakeDvcOps(),
        git_ops=_FakeRegistryGitOps(),
    )
    
    assert result.version == "0.1.1"
    assert "0.1.1" in (proj / "metadata.json").read_text(encoding="utf-8")

def test_publish_calls_catalog_update_last(tmp_path):
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    order = []
    
    class _OrderedClient:
        def update(self, meta, *, dry_run=False, reporter=None): order.append("update")
    
    dvc.push = lambda *a, **k: order.append("push")
    git.tag = lambda *a, **k: order.append("tag")
    
    publish_project(
        project_path=proj,
        client=_OrderedClient(),
        dvc_ops=dvc,
        git_ops=git,
    )
    
    assert order == ["push", "tag", "update"]

def test_publish_refuses_decreasing_version(tmp_path):
    proj = _seed_project(tmp_path)
    with pytest.raises(VersionNotIncreasing):
        publish_project(
            project_path=proj,
            version="0.0.9",
            client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

def test_publish_allows_equal_version_for_retry(tmp_path):
    proj = _seed_project(tmp_path)
    m = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    m["mint"]["version"] = "0.2.1"
    (proj / "metadata.json").write_text(json.dumps(m))
    subprocess.run(["git", "add", "metadata.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "bump"], cwd=proj, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip()
    
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    result = publish_project(
        project_path=proj,
        version="0.2.1",
        client=_FakeCatalogClient(),
        dvc_ops=dvc,
        git_ops=git,
    )
    
    assert result.version == "0.2.1"
    assert len(dvc.push_calls) == 1
    assert git.tag_calls[0].name == "v0.2.1"
    # Slice 35: same-version retries restamp status.last_updated etc., so
    # local_diff is non-empty and a new metadata commit is expected. v1 parity.
    new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip()
    assert new_head != head_sha
    parents = subprocess.run(
        ["git", "rev-list", "--count", f"{head_sha}..HEAD"],
        cwd=proj, capture_output=True, text=True,
    ).stdout.strip()
    assert parents == "1"

def test_publish_refuses_invalid_semver(tmp_path):
    proj = _seed_project(tmp_path)
    with pytest.raises(VersionNotIncreasing):
        publish_project(
            project_path=proj,
            version="not.a.version",
            client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

def test_publish_rolls_back_metadata_on_dvc_push_failure(tmp_path):
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    dvc.push_raises = DvcPushError("dvc push exited 1")

    with pytest.raises(DvcPushFailed):
        publish_project(
            project_path=proj,
            version="0.1.1",
            client=_FakeCatalogClient(),
            dvc_ops=dvc,
            git_ops=git,
        )
    
    # Read the file and assert version is 0.1.0 (it should be rolled back)
    content = (proj / "metadata.json").read_text(encoding="utf-8")
    print(f"DEBUG: after rollback failure, version is {content}")
    assert '"version": "0.1.0"' in content
    assert len(git.reset_hard_calls) == 0 # no commit yet

def test_publish_idempotent_retry_does_not_reset_hard_head1(tmp_path):
    proj = _seed_project(tmp_path)
    m = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    m["mint"]["version"] = "0.2.1"
    (proj / "metadata.json").write_text(json.dumps(m))
    subprocess.run(["git", "add", "metadata.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "bump"], cwd=proj, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip()
    
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    dvc.push_raises = DvcPushError("dvc push exited 1")

    with pytest.raises(DvcPushFailed):
        publish_project(
            project_path=proj,
            version="0.2.1",
            client=_FakeCatalogClient(),
            dvc_ops=dvc,
            git_ops=git,
        )
    
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip() == head_sha
    assert len(git.reset_hard_calls) == 0

def test_publish_resets_to_head_on_commit_failure(tmp_path):
    proj = _seed_project(tmp_path)
    git = _FakeRegistryGitOps()
    git.commit_all_raises = GitOpError(["git", "commit"], "failed")
    
    with pytest.raises(PublishError):
        publish_project(
            project_path=proj,
            version="0.1.1",
            client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(),
            git_ops=git,
        )
    
    assert len(git.reset_hard_calls) == 1
    assert git.reset_hard_calls[0].ref == "HEAD"

def test_publish_skips_dvc_rollback_after_tag_failure(tmp_path):
    proj = _seed_project(tmp_path)
    git = _FakeRegistryGitOps()
    git.tag_raises = GitTagAlreadyExists("v0.1.1", str(proj))
    
    with pytest.raises(TagFailed) as excinfo:
        publish_project(
            project_path=proj,
            version="0.1.1",
            client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(),
            git_ops=git,
        )
    
    assert "0.1.1" in (proj / "metadata.json").read_text(encoding="utf-8")
    assert "git reset" not in excinfo.value.recovery_hint


def test_publish_rolls_back_when_dvc_not_installed(tmp_path):
    """Regression: DvcNotInstalled (DvcOpError subclass, not DvcPushError)
    must also trigger the metadata rollback. Slice-15 review v2 P1."""
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    dvc.push_raises = DvcNotInstalled("`dvc` binary not found on PATH.")

    with pytest.raises(DvcPushFailed):
        publish_project(
            project_path=proj,
            version="0.1.1",
            client=_FakeCatalogClient(),
            dvc_ops=dvc,
            git_ops=git,
        )

    # File rolled back to the original 0.1.0.
    content = (proj / "metadata.json").read_text(encoding="utf-8")
    assert '"version": "0.1.0"' in content


# ---------------------------------------------------------------------------
# Slice 32 — preview gate + data_products validation
# ---------------------------------------------------------------------------

def test_publish_dry_run_returns_preview_no_side_effects(tmp_path):
    """Slice 32: dry-run returns a fully-populated PublishPreview and
    does NOT push, commit, or tag."""
    proj = _seed_project(tmp_path)
    git = _FakeRegistryGitOps()
    git.current_commit_value = "abc1234"
    dvc = _FakeDvcOps()
    result = publish_project(
        project_path=proj,
        version="0.1.1",
        client=_FakeCatalogClient(),
        dvc_ops=dvc,
        git_ops=git,
        dry_run=True,
    )
    assert result.preview is not None
    assert result.preview.new_version == "0.1.1"
    assert result.preview.current_version == "0.1.0"
    assert result.preview.working_tree_commit == "abc1234"
    assert result.preview.new_metadata.mint.version == "0.1.1"
    # No side effects under dry-run.
    assert len(dvc.push_calls) == 0
    assert len(git.tag_calls) == 0


def test_publish_blocked_when_primary_missing(tmp_path, monkeypatch):
    """Slice 32: data_products.primary unset -> PublishBlocked via
    check_project. Locally override the autouse mock_check_project."""
    from mintd.check import CheckFinding
    from mintd.publish import PublishBlocked
    proj = _seed_project(tmp_path)
    monkeypatch.setattr(
        "mintd.publish.check_project",
        lambda *a, **kw: [
            CheckFinding(
                severity="error",
                section="producer",
                message="data_products.primary is not set",
                kind="data_products_primary_missing",
            )
        ],
    )
    with pytest.raises(PublishBlocked):
        publish_project(
            project_path=proj,
            client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )


def test_publish_not_blocked_for_non_data_type_missing_primary(tmp_path, monkeypatch):
    """Slice 45: a fresh non-data repo (no primary) must clear the publish
    preflight. Exercises the REAL check_project (overriding the autouse mock),
    so this asserts the type-gating in check.py reaches the publish gate."""
    from mintd.check import check_project as real_check_project
    proj = _seed_project(tmp_path)
    meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    meta["project"]["type"] = "code"
    meta["data_products"] = {"primary": None, "outputs": []}
    (proj / "metadata.json").write_text(json.dumps(meta))
    monkeypatch.setattr("mintd.publish.check_project", real_check_project)

    result = publish_project(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=_FakeCatalogClient(),
        dvc_ops=_FakeDvcOps(),
        git_ops=_FakeRegistryGitOps(),
    )

    assert result.dry_run
    assert result.version == "0.1.1"


# ---------------------------------------------------------------------------
# Slice 35 — publish stamps last_updated / last_published_version / outputs[].last_published
# ---------------------------------------------------------------------------


def test_prepare_publish_stamps_timestamps_on_outputs_and_status(tmp_path):
    """`prepare_publish` writes status.last_updated, status.last_published_version,
    and every outputs[*].last_published from a single canonical `now` — the
    fix for the empty-catalog-diff bug that crashed publish on idempotent
    catalog updates (v1 parity restored)."""
    from datetime import datetime, timezone
    from mintd.publish import prepare_publish

    proj = _seed_project(tmp_path)
    pinned = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    preview = prepare_publish(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=_FakeCatalogClient(),
        git_ops=_FakeRegistryGitOps(),
        now=pinned,
    )
    assert preview.new_metadata.status.last_updated == pinned
    assert preview.new_metadata.status.last_published_version == "0.1.1"
    assert preview.new_metadata.data_products.outputs
    for o in preview.new_metadata.data_products.outputs:
        assert o.last_published == pinned.isoformat()


def test_prepare_publish_one_canonical_timestamp(tmp_path):
    """All outputs share the same byte-identical stamp string — one canonical
    `now()` per publish, not one per output."""
    from datetime import datetime, timezone
    from mintd.publish import prepare_publish

    proj = _seed_project(tmp_path)
    # Add a second output so the singleton assertion is meaningful.
    m = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    existing_output = m["data_products"]["outputs"][0]
    second = dict(existing_output)
    second["path"] = "data/final/extra/"
    second["primary"] = False
    m["data_products"]["outputs"].append(second)
    (proj / "metadata.json").write_text(json.dumps(m))
    subprocess.run(["git", "add", "metadata.json"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "add second output"], cwd=proj, check=True)

    pinned = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    preview = prepare_publish(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=_FakeCatalogClient(),
        git_ops=_FakeRegistryGitOps(),
        now=pinned,
    )
    stamps = {o.last_published for o in preview.new_metadata.data_products.outputs}
    assert len(stamps) == 1
    assert stamps == {pinned.isoformat()}


def test_prepare_publish_dry_run_also_stamps(tmp_path):
    """Dry-run preview must reflect the stamped fields so the catalog diff
    is honest about what would be written — no on-disk side effects."""
    from datetime import datetime, timezone
    from mintd.publish import prepare_publish

    proj = _seed_project(tmp_path)
    pinned = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)
    preview = prepare_publish(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=_FakeCatalogClient(),
        git_ops=_FakeRegistryGitOps(),
        now=pinned,
    )
    assert preview.new_metadata.status.last_updated == pinned
    assert preview.new_metadata.data_products.outputs[0].last_published == pinned.isoformat()
    # No write on dry-run: file on disk still has the seeded values.
    on_disk = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    assert on_disk["mint"]["version"] == "0.1.0"


def test_publish_full_flow_produces_nonempty_catalog_diff(tmp_path):
    """End-to-end: a same-version retry that produced 0 catalog diff (the
    original bug) now always produces ≥ 3 changes (status × 2 + per-output
    last_published × N), so client.update() never reaches an empty-commit."""
    from mintd.catalog import _diff_entries
    from mintd.model import Metadata
    from mintd.publish import prepare_publish

    proj = _seed_project(tmp_path)
    meta = Metadata.from_json_file(proj / "metadata.json")
    existing_entry = meta.to_catalog_entry()
    client = _FakeCatalogClient(entries={meta.project.name: existing_entry})

    preview = prepare_publish(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=client,
        git_ops=_FakeRegistryGitOps(),
    )

    diff = _diff_entries(existing_entry, preview.new_metadata.to_catalog_entry())
    paths = {c.field_path for c in diff}
    assert "status.last_updated" in paths
    assert "status.last_published_version" in paths
    # _diff_entries compares outputs as one list-level change rather than
    # per-element; the new last_published stamp shows up inside that entry.
    outputs_change = next(c for c in diff if c.field_path == "data_products.outputs")
    after_first_output = outputs_change.after[0]
    assert after_first_output["last_published"]  # non-empty
    assert after_first_output["last_published"] != ""
    assert len(diff) >= 3


def test_prepare_publish_uses_project_name_not_full_name_for_catalog_fetch(tmp_path):
    """Slice 32 reviewer P1: catalog fetch key is project.name (not
    project.full_name). Verify by registering under name only — a
    full_name lookup would miss and silently mislabel as first_publish."""
    from mintd.publish import prepare_publish
    from mintd.model import Metadata
    proj = _seed_project(tmp_path)
    meta = Metadata.from_json_file(proj / "metadata.json")
    fake_entries = {meta.project.name: meta.to_catalog_entry()}
    client = _FakeCatalogClient(entries=fake_entries)
    preview = prepare_publish(
        project_path=proj,
        version=None,
        dry_run=True,
        client=client,
        git_ops=_FakeRegistryGitOps(),
    )
    # name lookup hits the registered entry → NOT first_publish.
    assert preview.first_publish is False
    assert preview.project_name == meta.project.name


# ---------------------------------------------------------------------------
# Slice 36 — Pattern C: phase relabeling in _apply_publish
# ---------------------------------------------------------------------------


class _RecordingReporter:
    def __init__(self) -> None:
        self.labels: list[str] = []

    def update_status(self, msg: str) -> None:
        self.labels.append(msg)


def test_apply_publish_updates_status_between_phases(tmp_path):
    """All five phase labels appear in order when local_diff is non-empty."""
    from mintd.publish import _apply_publish, prepare_publish
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    git = _FakeRegistryGitOps()
    client = _FakeCatalogClient()
    preview = prepare_publish(
        project_path=proj, version="0.1.1", dry_run=False,
        client=client, git_ops=git,
    )
    rep = _RecordingReporter()
    _apply_publish(
        preview,
        project_path=proj, client=client, dvc_ops=dvc, git_ops=git,
        message=None,
        reporter=rep,  # type: ignore[arg-type]
    )
    assert rep.labels == [
        "Writing metadata.json...",
        "Pushing data to DVC...",
        "Committing version bump...",
        "Tagging release...",
        "Updating catalog entry...",
    ]

# --- Step 5 (catalog update) must never leak a traceback -------------------
# The failure lands after dvc push, the metadata commit, and the tag have all
# succeeded, so the recovery hint has to say so.

class _FailingCatalogClient(_FakeCatalogClient):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def update(self, metadata, *, dry_run=False, reporter=None):
        raise self._exc


@pytest.mark.parametrize("exc", [
    RegistryBranchExists(["git", "push"], "! [rejected] update/x (fetch first)", "update/x"),
    PRConflictError(branch="update/x", existing_pr=42),
])
def test_publish_catalog_branch_collision_reports_partial_state(tmp_path, exc):
    proj = _seed_project(tmp_path)

    with pytest.raises(CatalogUpdateFailed) as raised:
        publish_project(
            project_path=proj,
            client=_FailingCatalogClient(exc),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

    err = raised.value
    assert err.pushed is True
    assert err.tagged is True
    hint = err.recovery_hint or ""
    assert "update/x" in hint
    assert "DVC push" in hint
    assert "tag v0.1.1" in hint
    assert "mintd publish 0.1.1" in hint
    # The failing run already created the tag; a bare rerun would die at step 4.
    assert "git tag -d v0.1.1" in hint


def test_publish_catalog_gh_missing_reports_partial_state(tmp_path):
    """`update` now shells out to `gh pr list` before branching, so a missing
    or unauthenticated gh surfaces one call earlier than it used to."""
    proj = _seed_project(tmp_path)

    with pytest.raises(CatalogUpdateFailed) as raised:
        publish_project(
            project_path=proj,
            client=_FailingCatalogClient(GhNotInstalled("gh not found")),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

    assert raised.value.pushed is True
    assert "mintd publish 0.1.1" in (raised.value.recovery_hint or "")


def test_publish_pr_conflict_hint_does_not_advise_deleting_a_live_branch(tmp_path):
    """`PRConflictError` means the push SUCCEEDED and only `gh pr create` was
    refused. The rejected-push advice ("ask an admin to delete the branch")
    would close a live PR and discard what was just pushed."""
    proj = _seed_project(tmp_path)

    with pytest.raises(CatalogUpdateFailed) as raised:
        publish_project(
            project_path=proj,
            client=_FailingCatalogClient(
                PRConflictError(branch="update/x", existing_pr=42)
            ),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

    hint = raised.value.recovery_hint or ""
    assert "gh pr list --head update/x" in hint
    assert "delete" not in hint
    assert "already has commits mintd does not have" not in hint


def test_publish_registry_branch_exists_hint_does_advise_merge_or_delete(tmp_path):
    """The mirror case: the push was rejected, so nothing of ours is on the
    branch and clearing it is the right remedy."""
    proj = _seed_project(tmp_path)

    with pytest.raises(CatalogUpdateFailed) as raised:
        publish_project(
            project_path=proj,
            client=_FailingCatalogClient(
                RegistryBranchExists(["git", "push"], "! [rejected] (fetch first)", "update/x")
            ),
            dvc_ops=_FakeDvcOps(),
            git_ops=_FakeRegistryGitOps(),
        )

    hint = raised.value.recovery_hint or ""
    assert "merge or delete it" in hint


# ---------------------------------------------------------------------------
# P2a (issue13) — the publish gate must use the client it already holds
#
# Both tests below override the autouse `mock_check_project`; without that they
# pass vacuously in either direction. Precedent: :330.
# ---------------------------------------------------------------------------


def _seed_enclave_consumer(tmp_path):
    """A project that imports one approved enclave product, committed."""
    proj = _seed_project(tmp_path)
    stage_enclave_manifest(proj)
    subprocess.run(["git", "add", "enclave_manifest.yaml"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-m", "add enclave manifest"], cwd=proj, check=True)
    return proj


def test_prepare_publish_passes_the_catalog_client_to_check(tmp_path, monkeypatch):
    """A project with one approved product is publishable: the gate runs the
    real check_project with the client prepare_publish already requires."""
    from mintd.check import check_project as real_check_project
    from mintd.publish import prepare_publish

    monkeypatch.setattr("mintd.publish.check_project", real_check_project)
    proj = _seed_enclave_consumer(tmp_path)

    preview = prepare_publish(
        project_path=proj,
        version="0.1.1",
        dry_run=True,
        client=client_with_provider_xw(),
        git_ops=_FakeRegistryGitOps(),
    )

    assert preview.new_version == "0.1.1"


def test_publish_full_flow_with_enclave_manifest_reaches_the_tag(tmp_path, monkeypatch):
    """The whole transaction, not just the gate: a real `git tag -a` lands."""
    from mintd.check import check_project as real_check_project
    from mintd.model import Metadata

    monkeypatch.setattr("mintd.publish.check_project", real_check_project)
    proj = _seed_enclave_consumer(tmp_path)
    client = client_with_provider_xw()
    # Step 5 updates this project's own catalog entry; register it first.
    client.register(Metadata.from_json_file(proj / "metadata.json"))

    result = publish_project(
        project_path=proj,
        version="0.1.1",
        dry_run=False,
        client=client,
        dvc_ops=_FakeDvcOps(),
        git_ops=_FakeRegistryGitOps(),
    )

    assert result.tagged
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "v0.1.1" in tags


# ---------------------------------------------------------------------------
# issue28 — publish must stop eating keys the researcher authored
# ---------------------------------------------------------------------------
# The sub-models are extra="ignore", so a hand-added `ownership.slack` is gone
# by the time publish dumps the parsed model. Writing that dump back over the
# file made the loss durable. Every assertion here re-reads the file from disk:
# asserting on the returned model passes while the file is destroyed.


def _seed_project_with_strays(tmp_path: Path) -> Path:
    """A seeded project whose metadata.json carries three user-authored keys
    the model does not declare — one per block the scaffold generates, plus one
    inside an `outputs[]` entry (the case a local_diff replay would miss,
    because `_dict_diff` does not recurse into lists)."""
    proj = _seed_project(tmp_path)
    meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    meta["ownership"]["slack"] = "#my-channel"
    meta["metadata"]["doi"] = "10.5281/zenodo.1234"
    meta["data_products"]["outputs"][0]["units"] = "USD"
    (proj / "metadata.json").write_text(json.dumps(meta, indent=2))
    subprocess.run(["git", "commit", "-am", "add my own notes"], cwd=proj, check=True)
    return proj


def _publish(proj: Path, version: str = "0.1.1", **kwargs):
    return publish_project(
        project_path=proj, version=version, dry_run=False,
        client=_FakeCatalogClient(), dvc_ops=_FakeDvcOps(),
        git_ops=_FakeRegistryGitOps(), **kwargs,
    )


def test_publish_preserves_user_authored_nested_fields(tmp_path, monkeypatch):
    """Runs the REAL check_project (overriding the autouse mock), so this also
    pins that a project carrying hand-added keys still clears the publish
    preflight — the gate has to let the write happen for the fix to matter."""
    from mintd.check import check_project as real_check_project
    proj = _seed_project_with_strays(tmp_path)
    monkeypatch.setattr("mintd.publish.check_project", real_check_project)

    _publish(proj)

    on_disk = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    assert on_disk["ownership"]["slack"] == "#my-channel"
    assert on_disk["metadata"]["doi"] == "10.5281/zenodo.1234"
    assert on_disk["mint"]["version"] == "0.1.1"


def test_publish_preserves_keys_inside_output_entries(tmp_path):
    proj = _seed_project_with_strays(tmp_path)

    _publish(proj)

    entry = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))["data_products"]["outputs"][0]
    assert entry["units"] == "USD"
    assert entry["last_published"], "publish still owns and stamps last_published"


@pytest.mark.parametrize("arm", ["diff_nonempty", "diff_empty"])
def test_publish_write_is_lossless(tmp_path, arm):
    """A real bump gains the bumped fields and loses nothing; a no-op publish
    does not touch the file at all. The diff_empty arm is the durable guard for
    the issue21 interaction: when the `last_updated` stamp becomes conditional,
    this still holds."""
    from datetime import datetime, timezone
    from mintd.metadata_migrate import _find_dropped_keys
    from mintd.publish import _apply_publish, prepare_publish

    pinned = datetime(2026, 5, 11, tzinfo=timezone.utc)
    proj = _seed_project_with_strays(tmp_path)
    if arm == "diff_empty":
        # Pre-stamp the file into exactly the state a 0.1.0 publish produces,
        # so nothing changes and step 1 never fires.
        meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
        meta["status"]["last_updated"] = pinned.isoformat()
        meta["status"]["last_published_version"] = "0.1.0"
        meta["data_products"]["outputs"][0]["last_published"] = pinned.isoformat()
        (proj / "metadata.json").write_text(json.dumps(meta, indent=2))
        subprocess.run(["git", "commit", "-am", "already published"], cwd=proj, check=True)

    before = (proj / "metadata.json").read_text(encoding="utf-8")
    preview = prepare_publish(
        project_path=proj,
        version="0.1.0" if arm == "diff_empty" else "0.1.1",
        dry_run=False, client=_FakeCatalogClient(),
        git_ops=_FakeRegistryGitOps(), now=pinned,
    )
    assert (preview.local_diff == []) is (arm == "diff_empty")

    _apply_publish(
        preview, project_path=proj, client=_FakeCatalogClient(),
        dvc_ops=_FakeDvcOps(), git_ops=_FakeRegistryGitOps(), message=None,
    )
    after = (proj / "metadata.json").read_text(encoding="utf-8")

    if arm == "diff_empty":
        assert after == before
    else:
        assert _find_dropped_keys(json.loads(before), json.loads(after)) == []
        assert json.loads(after)["mint"]["version"] == "0.1.1"


def test_publish_rollback_restores_the_users_nested_keys(tmp_path):
    """The new merge-then-write path must not break step 2's rollback."""
    proj = _seed_project_with_strays(tmp_path)
    before = (proj / "metadata.json").read_text(encoding="utf-8")
    dvc = _FakeDvcOps()
    dvc.push_raises = DvcPushError("dvc push exited 1")

    with pytest.raises(DvcPushFailed):
        publish_project(
            project_path=proj, version="0.1.1", dry_run=False,
            client=_FakeCatalogClient(), dvc_ops=dvc,
            git_ops=_FakeRegistryGitOps(),
        )

    assert (proj / "metadata.json").read_text(encoding="utf-8") == before
    assert json.loads(before)["ownership"]["slack"] == "#my-channel"


def test_publish_preview_still_lists_the_bumped_fields(tmp_path):
    """The preview the user reads is unchanged by the write change."""
    proj = _seed_project_with_strays(tmp_path)

    result = publish_project(
        project_path=proj, version="0.1.1", dry_run=True,
        client=_FakeCatalogClient(), dvc_ops=_FakeDvcOps(),
        git_ops=_FakeRegistryGitOps(),
    )

    paths = {c.field_path for c in result.diff}
    assert "mint.version" in paths
    assert "status.last_updated" in paths


def test_publish_write_does_not_reshuffle_a_file_with_no_strays(tmp_path):
    """The overlay must be a no-op for everyone who never hand-edited anything:
    the plain model dump's bytes, same key order. Without this,
    modeled-but-absent keys (`storage`) migrate to the end of the file and every
    user's first publish after the change carries a spurious reordering. The
    trailing newline is the one intended difference — every other
    metadata.json writer already emits it (`_render.py`, `init.py`,
    `metadata_migrate.py`), so publish had been silently stripping it."""
    from mintd.model import Metadata

    proj = _seed_project(tmp_path)

    _publish(proj)

    on_disk = (proj / "metadata.json").read_text(encoding="utf-8")
    canonical = Metadata.from_json_file(proj / "metadata.json").model_dump_json(indent=2)
    assert on_disk == canonical + "\n"


def test_publish_maps_corrupt_metadata_between_preview_and_apply(tmp_path):
    """The confirm prompt is an unbounded window in which the file can be
    edited into invalid JSON. The overlay parses it, so that has to surface as
    a PublishError with a hint, not a raw JSONDecodeError traceback."""
    from mintd.publish import _apply_publish, prepare_publish

    proj = _seed_project_with_strays(tmp_path)
    preview = prepare_publish(
        project_path=proj, version="0.1.1", dry_run=False,
        client=_FakeCatalogClient(), git_ops=_FakeRegistryGitOps(),
    )
    (proj / "metadata.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(PublishError) as exc:
        _apply_publish(
            preview, project_path=proj, client=_FakeCatalogClient(),
            dvc_ops=_FakeDvcOps(), git_ops=_FakeRegistryGitOps(), message=None,
        )

    assert "no longer valid JSON" in str(exc.value)
    assert exc.value.recovery_hint
    assert not exc.value.pushed


def test_apply_publish_pushes_the_project_path_not_the_process_cwd(
    tmp_path, monkeypatch
):
    """`mintd publish --path ../proj` pushes ../proj's data, not this repo's.

    `publish.py`'s step 2 called a bare `dvc_ops.push()`, so dvc uploaded from
    whatever repo the process was standing in while the surrounding steps —
    the metadata write, the commit, the tag — all operated on `project_path`.
    Publishing a project by path from inside another DVC repo therefore pushed
    the wrong bytes and then tagged the right repo to say it had done so.
    """
    proj = _seed_project(tmp_path)
    dvc = _FakeDvcOps()
    monkeypatch.chdir(tmp_path)

    publish_project(
        project_path=proj,
        version="0.1.2",
        client=_FakeCatalogClient(),
        dvc_ops=dvc,
        git_ops=_FakeRegistryGitOps(),
    )

    assert [c.cwd for c in dvc.push_calls] == [proj]
    assert dvc.push_calls[0].cwd != Path.cwd()
