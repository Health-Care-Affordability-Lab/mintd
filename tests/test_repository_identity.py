"""Unit G — repository identity.

Two halves of one defect: `mintd init` writes an empty
`repository.github_url`, and `mintd check` does not object, so the empty
value reaches the catalog and every consumer `mintd data clone` against
that entry exits 1 (`data.py:300-304`).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mintd._templates._render import _render_metadata_json
from mintd.check import check_project


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _render(project_type: str, name: str, **ctx: object) -> dict:
    context: dict[str, object] = {
        "project_type": project_type,
        "project_name": name,
        "created_at": "2026-08-07T00:00:00Z",
        "created_by": "tester",
        "mint_version": "0.0.2",
        **ctx,
    }
    return json.loads(_render_metadata_json(context))


# ---------------------------------------------------------------------------
# The render half
# ---------------------------------------------------------------------------

def test_render_derives_github_url_from_registry_org():
    """Every scaffold gets a real URL, built from the same full_name the
    metadata's own project.full_name carries."""
    doc = _render("data", "ghurl-probe", registry_org="test-org")

    assert doc["repository"]["github_url"] == (
        "https://github.com/test-org/data_ghurl-probe"
    )
    assert doc["repository"]["github_url"].endswith(doc["project"]["full_name"])


def test_render_derives_github_url_for_every_scaffold_type():
    """All four scaffolds route through this one renderer."""
    for project_type, full_name in (
        ("data", "data_probe"),
        ("code", "probe"),
        ("project", "prj_probe"),
        ("enclave", "enclave_probe"),
    ):
        doc = _render(project_type, "probe", registry_org="test-org")
        assert doc["repository"]["github_url"] == (
            f"https://github.com/test-org/{full_name}"
        ), project_type


def test_render_leaves_github_url_empty_when_registry_org_is_unset():
    """Without an org there is nothing to derive. Empty beats
    `https://github.com//data_probe`, which check would accept."""
    doc = _render("data", "probe")

    assert doc["repository"]["github_url"] == ""


def test_render_leaves_github_url_empty_when_registry_org_is_blank():
    """A whitespace-only org is the same nothing as an absent one."""
    doc = _render("data", "probe", registry_org="   ")

    assert doc["repository"]["github_url"] == ""


# ---------------------------------------------------------------------------
# The check half
# ---------------------------------------------------------------------------

def _write(project_dir: Path, url: str) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["repository"]["github_url"] = url
    (project_dir / "metadata.json").write_text(json.dumps(data))


def test_check_errors_on_empty_github_url(tmp_path: Path):
    """The load-bearing half: hand-edited and migrated metadata cannot
    re-introduce the empty value the render fix stops producing."""
    _write(tmp_path, "")

    findings = check_project(tmp_path)

    matching = [f for f in findings if f.kind == "repository_github_url_missing"]
    assert len(matching) == 1, findings
    assert matching[0].severity == "error"
    assert matching[0].section == "producer"
    assert matching[0].field_path == "repository.github_url"
    assert matching[0].hint


def test_check_errors_on_whitespace_only_github_url(tmp_path: Path):
    _write(tmp_path, "   ")

    findings = check_project(tmp_path)

    assert [f.kind for f in findings if f.severity == "error"] == [
        "repository_github_url_missing"
    ]


def test_check_accepts_a_populated_github_url(tmp_path: Path):
    """Over-fire guard: the finding must not fire on a good entry."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")

    findings = check_project(tmp_path)

    assert findings == []


def test_check_accepts_a_github_url_that_is_not_the_derived_one(tmp_path: Path):
    """The derivation is a default, not a truth. A maintainer may legitimately
    point an entry at a mirror, a fork, or a repo that was renamed after
    registration -- and check has no network access to verify any of it. So
    check validates presence and never asserts the derived shape."""
    _write(tmp_path, "https://github.com/test-org/some-other-name")

    findings = check_project(tmp_path)

    assert findings == []
