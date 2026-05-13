"""Tests for check_project().

These tests pin the producer-section validation behavior. Consumer and
environment sections are added in later slices; for slice 1, they're
expected to return [].
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mintd.check import CheckFinding, check_project


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _write_metadata(project_dir: Path, mutate=None) -> None:
    """Copy the minimal fixture into project_dir/metadata.json.

    If `mutate` is provided, it's called with the parsed dict and may modify
    it in place before the file is written.
    """
    data = json.loads(MINIMAL.read_text())
    if mutate is not None:
        mutate(data)
    (project_dir / "metadata.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_check_clean_file_returns_empty(tmp_path: Path):
    """check_project against the minimal valid fixture returns []."""
    shutil.copy(MINIMAL, tmp_path / "metadata.json")

    findings = check_project(tmp_path)

    assert findings == []


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

def test_check_missing_file_returns_error(tmp_path: Path):
    """When metadata.json is absent from the project directory, return a single
    error finding describing the missing file.

    Acceptance:
      - len(findings) == 1
      - findings[0].severity == "error"
      - findings[0].section == "producer"
      - "metadata.json" appears in findings[0].message
    """
    findings = check_project(tmp_path)

    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, CheckFinding)
    assert f.severity == "error"
    assert f.section == "producer"
    assert "metadata.json" in f.message


def test_check_malformed_json_returns_error(tmp_path: Path):
    """When metadata.json contains malformed JSON, return an error finding.

    Acceptance:
      - At least one finding with severity="error", section="producer"
      - The message mentions JSON parsing
    """
    (tmp_path / "metadata.json").write_text("not valid json{")

    findings = check_project(tmp_path)

    assert any(
        f.severity == "error" and f.section == "producer" and "JSON" in f.message
        for f in findings
    )


def test_check_invalid_schema_returns_error(tmp_path: Path):
    """When metadata.json has schema_version="1.1", return an error finding.

    This is the hard-cut behavior — pre-2.0 metadata is rejected with a clear
    error pointing the user toward `mintd migrate`.

    Acceptance:
      - At least one finding with severity="error", section="producer"
      - field_path indicates the schema_version field
    """
    _write_metadata(tmp_path, mutate=lambda d: d.update(schema_version="1.1"))

    findings = check_project(tmp_path)

    assert any(
        f.severity == "error"
        and f.section == "producer"
        and f.field_path == "schema_version"
        for f in findings
    )


def test_check_missing_required_field_returns_error(tmp_path: Path):
    """When metadata.json is valid JSON but missing a required field
    (e.g., 'project.name'), return an error finding.

    Acceptance:
      - At least one finding with severity="error", section="producer"
      - field_path indicates the missing field
    """
    def drop_project_name(d):
        del d["project"]["name"]

    _write_metadata(tmp_path, mutate=drop_project_name)

    findings = check_project(tmp_path)

    assert any(
        f.severity == "error"
        and f.section == "producer"
        and f.field_path == "project.name"
        for f in findings
    )


# ---------------------------------------------------------------------------
# Section boundaries (slice 1 sanity check)
# ---------------------------------------------------------------------------

def test_check_returns_only_producer_findings_in_slice_1(tmp_path: Path):
    """In slice 1, consumer and environment sections always return [].

    Even when metadata.json is broken (errors in producer section), there
    are no findings with section="consumer" or section="environment".
    Slices 4 and 6 will add those; this test prevents accidental early
    additions.
    """
    # Deliberately broken metadata to maximize the chance any section helper
    # would have fired if it were wired up early.
    (tmp_path / "metadata.json").write_text("not valid json{")

    findings = check_project(tmp_path)

    assert all(f.section == "producer" for f in findings)
