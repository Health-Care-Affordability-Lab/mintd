import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch
from mintd.publish import (
    publish_project, DvcPushFailed, TagFailed, PublishError, VersionNotIncreasing
)
from mintd._dvc_ops import DvcNotInstalled, DvcPushError
from mintd._registry_git_ops import GitOpError, GitTagAlreadyExists
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.registry_git_ops import _FakeRegistryGitOps

class _FakeCatalogClient:
    """Minimal fake; slice-32 publish flow calls fetch() for the catalog
    diff. Default returns CatalogNotFound so dry-run / new-project tests
    treat it as first-publish (no catalog diff)."""

    def __init__(self, entries: dict | None = None) -> None:
        self._entries = entries or {}

    def update(self, metadata): pass

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
        def update(self, meta): order.append("update")
    
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
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip() == head_sha

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
