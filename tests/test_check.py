"""Tests for check_project().

These tests pin the producer-section validation behavior. Consumer and
environment sections are added in later slices; for slice 1, they're
expected to return [].
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mintd.check import CheckFinding, check_project
from mintd.model import DataProductOutput, DataProducts, Metadata
from mintd.producer import ProducerError, ProducerView


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


# ---------------------------------------------------------------------------
# Consumer section (slice 6)
# ---------------------------------------------------------------------------

# Test helpers — slice 6

def _stage_dvc_fixture(tmp_path: Path, src_name: str, dest_name: str) -> None:
    dest = tmp_path / "data" / "imports" / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "dvc_files" / src_name, dest)


def _view_with_primary(primary: str | None) -> ProducerView:
    meta = Metadata.model_validate_json(MINIMAL.read_text())
    meta = meta.model_copy(
        update={
            "data_products": DataProducts(
                primary=primary,
                outputs=[
                    DataProductOutput(
                        path=primary,
                        description="desc",
                        primary=True,
                        last_published="2023-01-01T00:00:00Z"
                    )
                ] if primary else []
            )
        }
    )
    return ProducerView(repo="example-org/provider-xw", pin="4f7c2a1abcd1234567890abcdef0123456789abc", metadata=meta)


def _factory_returning(view: ProducerView | ProducerError):
    def factory(repo: str, pin: str):
        return view
    return factory


def _factory_by_pin(mapping: dict[str, ProducerView | ProducerError]):
    def factory(repo: str, pin: str):
        return mapping[pin]
    return factory


def test_consumer_section_empty_when_no_imports(tmp_path: Path):
    _write_metadata(tmp_path)
    findings = check_project(tmp_path)
    assert not any(f.section == "consumer" for f in findings)


def test_consumer_section_summarizes_each_dep_without_upgrades(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "another_import.dvc")
    
    # Modify the second fixture's repo URL to prevent deduplication
    another_dvc = tmp_path / "data" / "imports" / "another_import.dvc"
    another_dvc.write_text(another_dvc.read_text().replace("provider-xw", "other"))

    findings = check_project(tmp_path)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 2
    for f in consumer_findings:
        assert f.severity == "info"
        assert f.source is not None
        assert f.source.parent == tmp_path / "data" / "imports"
        assert "imported " in f.message
        assert "4f7c2a1" in f.message


def test_check_project_legacy_signature_unchanged(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    findings = check_project(tmp_path)  # no kwargs
    consumer_findings = [f for f in findings if f.section == "consumer"]
    assert len(consumer_findings) == 1


def test_upgrades_reports_up_to_date(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    view = _view_with_primary("outputs/cms_based/")
    factory = _factory_returning(view)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "info"
    assert consumer_findings[0].message == "up to date"


def test_upgrades_reports_drift(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    # standalone_import.dvc has output_path="outputs/cms_based/"
    # Let pin have that, and HEAD have "outputs/new.parquet"
    pin_view = _view_with_primary("outputs/cms_based/")
    head_view = _view_with_primary("outputs/new.parquet")
    
    # contract_pin in fixture is "4f7c2a1abcd..."
    pin = "4f7c2a1abcd1234567890abcdef0123456789abc"
    factory = _factory_by_pin({pin: pin_view, "": head_view})

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "warning"
    assert "upgrade available: producer now publishes 'outputs/new.parquet'" in consumer_findings[0].message
    assert "you have 'outputs/cms_based/'" in consumer_findings[0].message


def test_upgrades_reports_unreachable(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    err = ProducerError.unreachable("repo", "pin", "git archive timed out")
    factory = _factory_returning(err)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "warning"
    assert "producer unreachable" in consumer_findings[0].message
    assert "git archive timed out" in consumer_findings[0].message


def test_upgrades_reports_pin_missing(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    pin = "4f7c2a1abcd1234567890abcdef0123456789abc"
    err = ProducerError.pin_missing("repo", pin)
    factory = _factory_returning(err)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "error"
    assert "producer pin missing" in consumer_findings[0].message


def test_upgrades_reports_metadata_missing(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    pin = "4f7c2a1abcd1234567890abcdef0123456789abc"
    err = ProducerError.metadata_missing("repo", pin)
    factory = _factory_returning(err)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "error"
    assert "producer has no metadata.json" in consumer_findings[0].message


def test_upgrades_reports_metadata_invalid(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    pin = "4f7c2a1abcd1234567890abcdef0123456789abc"
    err = ProducerError.metadata_invalid("repo", pin, "validation error at $.data_products.primary")
    factory = _factory_returning(err)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "error"
    assert "producer metadata invalid" in consumer_findings[0].message
    assert "validation error at $.data_products.primary" in consumer_findings[0].message


def test_upgrades_reports_schema_too_old(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    pin = "4f7c2a1abcd1234567890abcdef0123456789abc"
    err = ProducerError.schema_too_old("repo", pin, "1.1")
    factory = _factory_returning(err)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "warning"
    assert "uses schema_version 1.1" in consumer_findings[0].message
    assert "expected 2.0" in consumer_findings[0].message


def test_upgrades_walk_continues_after_one_error(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "dep1.dvc")
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "dep2.dvc")
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "dep3.dvc")
    
    (tmp_path / "data" / "imports" / "dep2.dvc").write_text((tmp_path / "data" / "imports" / "dep2.dvc").read_text().replace("provider-xw", "other2"))
    (tmp_path / "data" / "imports" / "dep3.dvc").write_text((tmp_path / "data" / "imports" / "dep3.dvc").read_text().replace("provider-xw", "other3"))
    
    def factory(repo: str, pin: str):
        if repo == "https://github.com/example-org/other2" and pin != "":
            return ProducerError.unreachable("repo", pin, "failed")
        return _view_with_primary("outputs/cms_based/")
        
    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 3
    # Depending on filesystem order, one of them will be the error.
    assert sum(1 for f in consumer_findings if f.severity == "warning") == 1
    assert sum(1 for f in consumer_findings if f.severity == "info") == 2


def test_upgrades_factory_called_once_per_dep_when_factory_at_head_errors(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    calls = []
    def factory(repo: str, pin: str):
        calls.append(pin)
        if pin == "":
            return ProducerError.pin_missing("repo", "")
        return _view_with_primary("outputs/cms_based/")

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "info"
    assert consumer_findings[0].message == "up to date"
    assert calls == ["4f7c2a1abcd1234567890abcdef0123456789abc", ""]


def test_upgrades_uses_producer_view_try_at_by_default(tmp_path: Path, monkeypatch):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    
    calls = []
    def mock_try_at(repo: str, pin: str):
        calls.append((repo, pin))
        return _view_with_primary("outputs/cms_based/")
        
    monkeypatch.setattr("mintd.check.ProducerView.try_at", staticmethod(mock_try_at))
    
    check_project(tmp_path, upgrades=True)
    
    assert len(calls) == 2  # once for pin, once for HEAD
    assert calls[0][1] == "4f7c2a1abcd1234567890abcdef0123456789abc"
    assert calls[1][1] == ""


def test_finding_source_field_round_trips(tmp_path: Path):
    (tmp_path / "metadata.json").write_text("not valid json{")
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    findings = check_project(tmp_path)
    producer_findings = [f for f in findings if f.section == "producer"]
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(producer_findings) > 0
    assert all(f.source is None for f in producer_findings)

    assert len(consumer_findings) == 1
    assert consumer_findings[0].source == tmp_path / "data" / "imports" / "standalone_import.dvc"


# ---------------------------------------------------------------------------
# Slice 8 — enclave manifest walker
# ---------------------------------------------------------------------------

import shutil as _shutil_for_enclave_tests  # noqa: E402

from mintd.catalog import InMemoryCatalogClient  # noqa: E402

_ENCLAVE_FIXTURE = FIXTURES / "enclave_manifest_v2_minimal.yaml"
_PROVIDER_XW_URL = "https://github.com/example-org/provider-xw"


def _stage_enclave_manifest(tmp_path: Path) -> Path:
    """Copy the minimal enclave manifest fixture into tmp_path."""
    dest = tmp_path / "enclave_manifest.yaml"
    _shutil_for_enclave_tests.copy(_ENCLAVE_FIXTURE, dest)
    return dest


def _client_with_provider_xw() -> InMemoryCatalogClient:
    client = InMemoryCatalogClient()
    data = json.loads(MINIMAL.read_text())
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = _PROVIDER_XW_URL
    client.register(Metadata.model_validate(data))
    return client


def test_consumer_section_walks_enclave_manifest_approved_products(tmp_path: Path):
    _write_metadata(tmp_path)
    manifest_path = _stage_enclave_manifest(tmp_path)
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    f = consumer_findings[0]
    assert f.source == manifest_path
    assert f.field_path == "approved_products[provider-xw]"


def test_consumer_section_walks_both_dvc_files_and_enclave_manifest(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    manifest_path = _stage_enclave_manifest(tmp_path)
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    dvc_findings = [f for f in consumer_findings if f.source != manifest_path]
    manifest_findings = [f for f in consumer_findings if f.source == manifest_path]
    assert len(dvc_findings) == 1
    assert dvc_findings[0].field_path is None
    assert len(manifest_findings) == 1
    assert manifest_findings[0].field_path == "approved_products[provider-xw]"


def test_consumer_section_handles_invalid_enclave_manifest(tmp_path: Path):
    _write_metadata(tmp_path)
    (tmp_path / "enclave_manifest.yaml").write_text("schema_version: '1.0'\nenclave_name: x\n")
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "error"
    assert consumer_findings[0].source == tmp_path / "enclave_manifest.yaml"


def test_consumer_section_empty_approved_products_emits_nothing(tmp_path: Path):
    _write_metadata(tmp_path)
    (tmp_path / "enclave_manifest.yaml").write_text(
        "schema_version: '2.0'\nenclave_name: my_workspace\napproved_products: []\n"
        "downloaded: []\ntransferred: []\n"
    )
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert consumer_findings == []


# ---------------------------------------------------------------------------
# Slice 9 — `kind` discriminator pins
# ---------------------------------------------------------------------------

_PIN = "4f7c2a1abcd1234567890abcdef0123456789abc"


@pytest.mark.parametrize(
    "result,expected_kind,expected_severity,message_fragment",
    [
        (ProducerError.unreachable("repo", _PIN, "timeout"), "unreachable", "warning", "producer unreachable"),
        (ProducerError.pin_missing("repo", _PIN), "pin_missing", "error", "producer pin missing"),
        (ProducerError.metadata_missing("repo", _PIN), "metadata_missing", "error", "producer has no metadata.json"),
        (ProducerError.metadata_invalid("repo", _PIN, "bad"), "metadata_invalid", "error", "producer metadata invalid"),
        (ProducerError.schema_too_old("repo", _PIN, "1.5"), "schema_too_old", "warning", "uses schema_version"),
    ],
)
def test_consumer_dvc_error_findings_assign_correct_kinds(
    tmp_path: Path, result, expected_kind, expected_severity, message_fragment
):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    factory = _factory_returning(result)

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    f = consumer_findings[0]
    assert f.kind == expected_kind
    assert f.severity == expected_severity
    assert message_fragment in f.message


def test_consumer_dvc_up_to_date_finding_has_up_to_date_kind(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    factory = _factory_returning(_view_with_primary("outputs/cms_based/"))

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].kind == "up_to_date"
    assert consumer_findings[0].message == "up to date"


def test_consumer_dvc_drift_finding_has_drift_kind(tmp_path: Path):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    pin_view = _view_with_primary("outputs/cms_based/")
    head_view = _view_with_primary("outputs/new.parquet")
    factory = _factory_by_pin({_PIN: pin_view, "": head_view})

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    f = consumer_findings[0]
    assert f.kind == "drift"
    assert f.severity == "warning"
    assert "upgrade available:" in f.message


@pytest.mark.parametrize(
    "result,expected_kind,expected_severity",
    [
        (ProducerError.unreachable("repo", _PIN, "timeout"), "unreachable", "warning"),
        (ProducerError.pin_missing("repo", _PIN), "pin_missing", "error"),
        (ProducerError.metadata_missing("repo", _PIN), "metadata_missing", "error"),
        (ProducerError.metadata_invalid("repo", _PIN, "bad"), "metadata_invalid", "error"),
        (ProducerError.schema_too_old("repo", _PIN, "1.5"), "schema_too_old", "warning"),
    ],
)
def test_consumer_manifest_error_findings_assign_correct_kinds(
    tmp_path: Path, result, expected_kind, expected_severity
):
    _write_metadata(tmp_path)
    _stage_enclave_manifest(tmp_path)
    client = _client_with_provider_xw()
    factory = _factory_returning(result)

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, client=client
    )
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    f = consumer_findings[0]
    assert f.kind == expected_kind
    assert f.severity == expected_severity
    assert f.source == tmp_path / "enclave_manifest.yaml"
    assert f.field_path == "approved_products[provider-xw]"


def test_consumer_manifest_invalid_finding_has_invalid_manifest_kind(tmp_path: Path):
    _write_metadata(tmp_path)
    (tmp_path / "enclave_manifest.yaml").write_text("schema_version: '1.0'\nenclave_name: x\n")
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].kind == "invalid_manifest"


def test_consumer_manifest_catalog_unresolved_finding_has_catalog_unresolved_kind(tmp_path: Path):
    """`client=None` path → kind='catalog_unresolved'."""
    _write_metadata(tmp_path)
    _stage_enclave_manifest(tmp_path)
    # No client passed; manifest walker emits a catalog_unresolved finding.

    findings = check_project(tmp_path)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].kind == "catalog_unresolved"
