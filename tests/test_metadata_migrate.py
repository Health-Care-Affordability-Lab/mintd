"""Tests for slice-22 ``mintd update metadata`` schema 1.x → 2.0 migration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mintd import cli
from mintd.metadata_migrate import (
    MetadataAlreadyV2,
    MetadataMigrateError,
    apply_metadata_migration,
    migrate_v1_to_v2,
)
from mintd.model import Metadata


FIXTURE = Path(__file__).parent / "fixtures" / "metadata_v1_real_world.json"


# ---------- pure migrate_v1_to_v2 ----------------------------------------


def test_migrate_real_world_v1_sample() -> None:
    """The lab's actual v1 metadata.json migrates cleanly + validates."""
    v1 = json.loads(FIXTURE.read_text(encoding="utf-8"))
    v2, _report = migrate_v1_to_v2(v1)
    metadata = Metadata.model_validate(v2)
    assert metadata.schema_version == "2.0"
    assert metadata.project.name == v1["project"]["name"]
    assert metadata.metadata.description == v1["project"]["description"]


def test_migrate_moves_description_and_tags() -> None:
    v1 = {
        "schema_version": "1.0",
        "project": {"type": "data", "name": "x", "full_name": "data_x",
                    "created_at": "2026-01-01T00:00:00Z", "created_by": "t",
                    "description": "desc-x", "tags": ["a", "b"]},
    }
    v2, report = migrate_v1_to_v2(v1)
    assert v2["metadata"]["description"] == "desc-x"
    assert v2["metadata"]["tags"] == ["a", "b"]
    assert "description" not in v2["project"]
    assert "tags" not in v2["project"]
    assert ("project.description", "metadata.description") in report.moved
    assert ("project.tags", "metadata.tags") in report.moved


def test_migrate_drops_language_field() -> None:
    v1 = {"schema_version": "1.0", "language": "stata", "project": {}}
    v2, report = migrate_v1_to_v2(v1)
    assert "language" not in v2
    assert "language" in report.dropped


def test_migrate_defaults_last_published_version() -> None:
    v1 = {
        "schema_version": "1.0",
        "project": {},
        "status": {"state": "active", "last_updated": "2026-01-01T00:00:00Z"},
    }
    v2, report = migrate_v1_to_v2(v1)
    assert v2["status"]["last_published_version"] == ""
    assert "status.last_published_version" in report.defaulted


def test_migrate_defaults_data_products() -> None:
    v1 = {"schema_version": "1.0", "project": {}}
    v2, report = migrate_v1_to_v2(v1)
    assert v2["data_products"] == {"primary": None, "outputs": []}
    assert "data_products" in report.defaulted


def test_migrate_data_products_primary_list_coerced_to_first_element() -> None:
    """v1 sometimes stored data_products.primary as a list; v2 requires str."""
    v1 = {
        "schema_version": "1.0",
        "project": {},
        "data_products": {
            "primary": ["outputs/cms/", "outputs/aha/", "outputs/stable/"],
            "outputs": [
                {"path": "outputs/cms/", "description": "CMS", "primary": True,
                 "last_published": "v1.0"},
            ],
        },
    }
    v2, report = migrate_v1_to_v2(v1)
    assert v2["data_products"]["primary"] == "outputs/cms/"
    assert any("primary (coerced from list)" in d for d in report.defaulted)


def test_migrate_outputs_default_missing_required_fields() -> None:
    """Real-world v1 outputs lack `last_published` (always) and sometimes
    `primary` (on non-primary entries); migration defaults both."""
    v1 = {
        "schema_version": "1.0",
        "project": {},
        "data_products": {
            "primary": "deriveddata/hosppanel/",
            "outputs": [
                {"path": "deriveddata/hosppanel/", "description": "panel",
                 "primary": True},
                {"path": "deriveddata/transpanel/", "description": "trans"},
            ],
        },
    }
    v2, report = migrate_v1_to_v2(v1)
    assert v2["data_products"]["outputs"][0]["last_published"] == ""
    assert v2["data_products"]["outputs"][1]["last_published"] == ""
    assert v2["data_products"]["outputs"][1]["primary"] is False
    assert "data_products.outputs[].last_published" in report.defaulted
    assert "data_products.outputs[].primary" in report.defaulted
    # And the result passes v2 validation.
    Metadata.model_validate({
        **v2,
        "mint": {"version": "0.0.1", "commit_hash": ""},
        "project": {"type": "data", "name": "x", "full_name": "data_x",
                    "created_at": "2026-01-01T00:00:00Z", "created_by": "t"},
        "metadata": {"description": "", "tags": []},
        "ownership": {"team": "", "maintainers": ["t"]},
        "access_control": {"teams": []},
        "governance": {"classification": "private", "contract_info": ""},
        "repository": {"github_url": "", "default_branch": "main",
                       "visibility": "private",
                       "mirror": {"url": "", "purpose": ""}},
        "status": {"state": "active", "last_updated": "2026-01-01T00:00:00Z",
                   "last_published_version": ""},
    })


def test_migration_report_lists_all_dropped_fields() -> None:
    """The vendored real-world fixture should drop every v1-only field
    enumerated in the spec."""
    v1 = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _, report = migrate_v1_to_v2(v1)
    for expected in (
        "language",
        "schema",
        "lifecycle",
        "storage",
        "project.display_name",
        "metadata.version",
        "metadata.mint_version",
    ):
        assert expected in report.dropped, f"missing: {expected}"


# ---------- apply_metadata_migration -------------------------------------


def test_apply_writes_v2_atomically(tmp_path: Path) -> None:
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    apply_metadata_migration(tmp_path)
    written = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == "2.0"
    # Tmp sibling was renamed away cleanly.
    assert not (tmp_path / "metadata.json.tmp").exists()
    # And the resulting JSON round-trips through Metadata.
    Metadata.model_validate(written)


def test_apply_dry_run_does_not_write(tmp_path: Path) -> None:
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    before = (tmp_path / "metadata.json").read_text(encoding="utf-8")
    apply_metadata_migration(tmp_path, dry_run=True)
    assert (tmp_path / "metadata.json").read_text(encoding="utf-8") == before


def test_apply_idempotent_on_v2_input(tmp_path: Path) -> None:
    """Running migration on a v2 file raises MetadataAlreadyV2."""
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    apply_metadata_migration(tmp_path)
    with pytest.raises(MetadataAlreadyV2):
        apply_metadata_migration(tmp_path)


def test_apply_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apply_metadata_migration(tmp_path)


def test_apply_validation_failure_surfaces_field_path(tmp_path: Path) -> None:
    """A synthetic v1 file with a required v2 sub-field broken raises
    MetadataMigrateError naming the failing field path."""
    v1 = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Break a required v2 field: ownership.maintainers must be a list.
    v1["ownership"]["maintainers"] = "not-a-list"
    (tmp_path / "metadata.json").write_text(json.dumps(v1))
    with pytest.raises(MetadataMigrateError) as exc:
        apply_metadata_migration(tmp_path)
    assert "ownership" in str(exc.value)
    assert "maintainers" in str(exc.value)


# ---------- CLI smoke ----------------------------------------------------


def test_cli_update_metadata_dry_run_preserves_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    before = (tmp_path / "metadata.json").read_text(encoding="utf-8")
    rc = cli.main(["update", "metadata", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert (tmp_path / "metadata.json").read_text(encoding="utf-8") == before
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "1.0" in out and "2.0" in out


def test_cli_update_metadata_already_v2_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    cli.main(["update", "metadata", str(tmp_path)])  # first run migrates
    rc = cli.main(["update", "metadata", str(tmp_path)])  # second run
    assert rc == 1
    assert "already v2" in capsys.readouterr().out


def test_cli_update_metadata_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copy(FIXTURE, tmp_path / "metadata.json")
    rc = cli.main(["--json", "update", "metadata", str(tmp_path), "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_before"] == "1.0"
    assert payload["schema_after"] == "2.0"
    assert isinstance(payload["moved"], list)
    assert isinstance(payload["defaulted"], list)
    assert isinstance(payload["dropped"], list)


def test_cli_update_metadata_validation_failure_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v1 = json.loads(FIXTURE.read_text(encoding="utf-8"))
    v1["ownership"]["maintainers"] = "not-a-list"
    (tmp_path / "metadata.json").write_text(json.dumps(v1))
    rc = cli.main(["update", "metadata", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ownership.maintainers" in err  # dotted path is load-bearing


def test_cli_update_metadata_missing_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins the FileNotFoundError → exit 1 branch in `_handle_update_metadata`."""
    rc = cli.main(["update", "metadata", str(tmp_path)])
    assert rc == 1
    assert "no metadata.json" in capsys.readouterr().err


def test_migrate_handles_null_data_products(tmp_path: Path) -> None:
    """Defensive: malformed v1 with `data_products: null` (vs absent) used to
    crash with TypeError; should now coerce to empty defaults."""
    v2, report = migrate_v1_to_v2({
        "schema_version": "1.0",
        "project": {},
        "data_products": None,
    })
    assert v2["data_products"] == {"primary": None, "outputs": []}
    assert "data_products" in report.defaulted


def test_migrate_handles_explicit_null_output_fields() -> None:
    """Some v1 files set `outputs[].last_published: null` instead of omitting;
    the migration must default the same way as for absent keys."""
    v2, report = migrate_v1_to_v2({
        "schema_version": "1.0",
        "project": {},
        "data_products": {
            "primary": "p",
            "outputs": [{
                "path": "p",
                "description": "d",
                "primary": None,
                "last_published": None,
            }],
        },
    })
    assert v2["data_products"]["outputs"][0]["primary"] is False
    assert v2["data_products"]["outputs"][0]["last_published"] == ""
