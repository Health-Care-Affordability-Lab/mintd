"""Tests for check_project().

These tests pin the producer-section validation behavior. Consumer and
environment sections are added in later slices; for slice 1, they're
expected to return [].
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from typing import get_args

from mintd._dvc_state import DeliveryState, delivery
from mintd._registry_git_ops import GitOpError
from mintd.check import _UNDELIVERABLE_KINDS, CheckFinding, check_project
from mintd.model import DataProductOutput, DataProducts, Metadata
from mintd.producer import ProducerError, ProducerView
from tests._dvc_fixtures import (
    REMOTE,
    stage_dvc_project,
    stage_undeliverable_primary,
    write_lock,
    write_pipeline,
    write_pointer,
)


FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _write_metadata(project_dir: Path, mutate=None) -> None:
    """Copy the minimal fixture into project_dir/metadata.json.

    If `mutate` is provided, it's called with the parsed dict and may modify
    it in place before the file is written.
    """
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
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


def _view_with_primary(
    primary: str | None,
    pin: str = "4f7c2a1abcd1234567890abcdef0123456789abc",
    last_published: str = "2023-01-01T00:00:00Z",
) -> ProducerView:
    meta = Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8"))
    meta = meta.model_copy(
        update={
            "data_products": DataProducts(
                primary=primary,
                outputs=[
                    DataProductOutput(
                        path=primary,
                        description="desc",
                        primary=True,
                        last_published=last_published,
                    )
                ] if primary else []
            )
        }
    )
    return ProducerView(repo="example-org/provider-xw", pin=pin, metadata=meta)


#: The repo string `_view_with_primary` bakes in — pointer reads key on the
#: VIEW's repo, so the fetcher stores must use this, not the `.dvc`'s URL.
_VIEW_REPO = "example-org/provider-xw"
#: `deps[0].path` in `standalone_import.dvc`, and its pointer-file name.
_OUT = "outputs/cms_based/"
_OUT_PTR = "outputs/cms_based.dvc"
_HEAD = "b" * 40


def _pointer_doc(md5: str, path: str = "cms_based") -> bytes:
    """A minimal producer-side `.dvc` pointer document."""
    return f"outs:\n- md5: {md5}\n  path: {path}\n".encode()


def _fetcher_serving(path_store: dict[tuple[str, str, str], bytes]):
    from tests._fakes.producer import StaticFetcher

    return StaticFetcher({}, path_store=path_store)


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
    another_dvc.write_text(another_dvc.read_text(encoding="utf-8").replace("provider-xw", "other"))

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
    fetcher = _fetcher_serving(
        {(_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32)}
    )

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "info"
    assert consumer_findings[0].kind == "up_to_date"
    assert consumer_findings[0].message == "up to date"


def test_upgrades_reports_drift(tmp_path: Path):
    """D-C: drift is the pointer md5 moving pin-vs-HEAD — same path, new
    bytes. (The old rule compared primary *paths*; a routine data refresh
    was invisible.)"""
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    pin_view = _view_with_primary("outputs/cms_based/", pin=_PIN)
    head_view = _view_with_primary("outputs/cms_based/", pin=_HEAD)
    factory = _factory_by_pin({_PIN: pin_view, "": head_view})
    fetcher = _fetcher_serving(
        {
            (_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc("c" * 32),
        }
    )

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].severity == "warning"
    assert consumer_findings[0].kind == "drift"
    assert (
        consumer_findings[0].message
        == "producer's HEAD differs from your pin: outputs/cms_based/ changed"
    )


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


def test_head_probe_failure_is_drift_unknown_not_up_to_date(tmp_path: Path):
    """The pin resolves from the on-disk cache, HEAD needs the wire. Both
    arms used to paint that green, so an offline or air-gapped consumer
    reported every product clean forever."""
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    factory = _factory_by_pin(
        {
            _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
            "": ProducerError.unreachable("repo", "HEAD", "Could not resolve host: github.com"),
        }
    )

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    finding = consumer_findings[0]
    assert finding.kind == "drift_unknown"
    assert finding.severity == "warning"
    assert finding.message == (
        "cannot determine drift: cannot read the producer's HEAD "
        "(unreachable: Could not resolve host: github.com)"
    )
    assert finding.hint is not None and _PIN[:7] in finding.hint
    assert _PIN not in finding.hint  # the short sha, not the full one


def test_head_probe_failure_reports_a_broken_head_too(tmp_path: Path):
    """Not only transport: a producer whose metadata is unreadable at HEAD
    was hidden by the same degrade, and the reason reaches the user."""
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    factory = _factory_by_pin(
        {
            _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
            "": ProducerError.metadata_missing("repo", "b" * 40),
        }
    )

    findings = check_project(tmp_path, upgrades=True, producer_view_factory=factory)
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    assert consumer_findings[0].kind == "drift_unknown"
    assert "metadata_missing" in consumer_findings[0].message


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
    
    (tmp_path / "data" / "imports" / "dep2.dvc").write_text((tmp_path / "data" / "imports" / "dep2.dvc").read_text(encoding="utf-8").replace("provider-xw", "other2"))
    (tmp_path / "data" / "imports" / "dep3.dvc").write_text((tmp_path / "data" / "imports" / "dep3.dvc").read_text(encoding="utf-8").replace("provider-xw", "other3"))
    
    def factory(repo: str, pin: str):
        if repo == "https://github.com/example-org/other2" and pin != "":
            return ProducerError.unreachable("repo", pin, "failed")
        return _view_with_primary("outputs/cms_based/")

    fetcher = _fetcher_serving(
        {(_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32)}
    )
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
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
    assert consumer_findings[0].severity == "warning"
    assert consumer_findings[0].kind == "drift_unknown"
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

from tests._enclave_fixtures import (  # noqa: E402
    client_with_provider_xw as _client_with_provider_xw,
    stage_enclave_manifest as _stage_enclave_manifest,
)


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


class _UnreachableRegistryClient:
    """Catalog client whose every read fails the way an unreachable registry
    does: `CatalogCache.ensure_fresh` shells out to `git clone`, which raises
    `GitOpError` out of `_registry_git_ops`."""

    def fetch(self, name: str):
        raise GitOpError(["git", "clone", "--depth=1", "/nonexistent/registry.git"],
                         "fatal: repository '/nonexistent/registry.git' does not exist")


@pytest.mark.parametrize("upgrades", [False, True])
def test_unreachable_registry_becomes_a_finding_not_an_exception(tmp_path: Path, upgrades: bool):
    """A registry that cannot be reached is a documented failure path, not a
    traceback. Both arms: `_resolve_approved_product_url` is called above the
    `if not upgrades` branch, so plain `check` hits the network too."""
    _write_metadata(tmp_path)
    _stage_enclave_manifest(tmp_path)

    findings = check_project(tmp_path, upgrades=upgrades, client=_UnreachableRegistryClient())
    consumer_findings = [f for f in findings if f.section == "consumer"]

    assert len(consumer_findings) == 1
    f = consumer_findings[0]
    assert f.kind == "catalog_unresolved"
    assert f.severity == "error"
    assert "provider-xw" in f.message
    # Reports what git said rather than asserting a cause: the same exception
    # covers an unreachable registry and a corrupt local cache.
    assert "does not exist" in f.message
    assert f.hint is not None
    assert f.source == tmp_path / "enclave_manifest.yaml"


def test_catalog_read_failure_does_not_blame_the_network_for_a_local_cache_fault(
    tmp_path: Path,
):
    """A corrupt registry cache raises the same GitOpError as an unreachable
    remote. The finding must carry git's own words and offer both remedies."""
    _write_metadata(tmp_path)
    _stage_enclave_manifest(tmp_path)

    class _CorruptCacheClient:
        def fetch(self, name: str):
            raise GitOpError(
                ["git", "checkout", "-f", "main"],
                "error: pathspec 'main' did not match any file(s) known to git",
            )

    findings = check_project(tmp_path, client=_CorruptCacheClient())
    f = [f for f in findings if f.section == "consumer"][0]

    assert "did not match any file" in f.message
    assert "registry cache" in (f.hint or "")


# ---------------------------------------------------------------------------
# Slice 30 — storage drift detection
# ---------------------------------------------------------------------------

def test_check_emits_bucket_empty_finding(tmp_path: Path):
    """Producer with bucket="" and a populated .dvc/config emits one
    BUCKET_EMPTY error whose hint names the bucket from .dvc/config."""
    def _add_empty_bucket(d):
        d["storage"] = {
            "provider": "s3",
            "bucket": "",
            "prefix": "lab/p/",
            "endpoint": "",
            "versioning": True,
            "dvc": {"remote_name": "p"},
        }
    _write_metadata(tmp_path, _add_empty_bucket)
    dvc_cfg = tmp_path / ".dvc" / "config"
    dvc_cfg.parent.mkdir(parents=True, exist_ok=True)
    dvc_cfg.write_text(
        "[core]\n    remote = p\n"
        '[remote "p"]\n    url = s3://cooper-globus/lab/p/\n'
    )

    findings = check_project(tmp_path)
    storage_findings = [f for f in findings if f.kind and f.kind.startswith("storage_")]
    assert len(storage_findings) == 1
    assert storage_findings[0].kind == "storage_bucket_empty"
    assert storage_findings[0].severity == "error"
    assert storage_findings[0].hint is not None
    assert "cooper-globus" in storage_findings[0].hint


def test_check_no_storage_finding_on_healthy(tmp_path: Path):
    """Producer with matching metadata.storage + .dvc/config emits no
    storage-section findings (INITIALIZED)."""
    def _add_healthy_storage(d):
        d["storage"] = {
            "provider": "s3",
            "bucket": "cooper-globus",
            "prefix": "lab/p/",
            "endpoint": "",
            "versioning": True,
            "dvc": {"remote_name": "p"},
        }
    _write_metadata(tmp_path, _add_healthy_storage)
    dvc_cfg = tmp_path / ".dvc" / "config"
    dvc_cfg.parent.mkdir(parents=True, exist_ok=True)
    dvc_cfg.write_text(
        "[core]\n    remote = p\n"
        '[remote "p"]\n    url = s3://cooper-globus/lab/p/\n'
    )

    findings = check_project(tmp_path)
    storage_findings = [f for f in findings if f.kind and f.kind.startswith("storage_")]
    assert storage_findings == []


# ---------------------------------------------------------------------------
# Slice 32 — data_products.primary validation
# ---------------------------------------------------------------------------

def test_check_emits_primary_missing_when_data_products_empty(tmp_path: Path):
    """Slice 32: producer-blocking error when data_products.primary is unset."""
    def _clear(d):
        d["data_products"] = {"primary": None, "outputs": []}
    _write_metadata(tmp_path, _clear)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert len(dp) == 1
    assert dp[0].kind == "data_products_primary_missing"
    assert dp[0].severity == "error"
    assert dp[0].field_path == "data_products.primary"
    assert dp[0].hint is not None
    assert "outputs[]" in dp[0].hint


def test_check_emits_primary_mismatch_when_primary_not_in_outputs(tmp_path: Path):
    def _mismatch(d):
        d["data_products"] = {
            "primary": "data/final/",
            "outputs": [{"path": "data/other/", "description": "", "primary": False, "last_published": ""}],
        }
    _write_metadata(tmp_path, _mismatch)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert len(dp) == 1
    assert dp[0].kind == "data_products_primary_mismatch"
    assert dp[0].hint is not None
    assert "data/other/" in dp[0].hint


def test_check_no_data_products_finding_when_primary_matches_output(tmp_path: Path):
    """Fixture defaults to publish-valid (slice 32); confirm zero findings."""
    _write_metadata(tmp_path, None)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert dp == []


# ---------------------------------------------------------------------------
# Slice 45 — primary mandatory only for `data`-type projects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("project_type", ["code", "project", "enclave"])
def test_check_no_primary_finding_for_non_data_types(tmp_path: Path, project_type: str):
    """A missing primary must not block non-data project types (code/project/
    enclave) — they may publish no consumable data product."""
    def _make(d):
        d["project"]["type"] = project_type
        d["data_products"] = {"primary": None, "outputs": []}
    _write_metadata(tmp_path, _make)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert dp == []


@pytest.mark.parametrize("project_type", ["data"])
def test_check_primary_missing_still_fires_for_data_type(
    tmp_path: Path, project_type: str
):
    """`data` is the only type that requires a primary — a missing one is still
    a blocking error. Parametrized to guard against the rule silently widening."""
    def _make(d):
        d["project"]["type"] = project_type
        d["data_products"] = {"primary": None, "outputs": []}
    _write_metadata(tmp_path, _make)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert len(dp) == 1
    assert dp[0].kind == "data_products_primary_missing"
    assert dp[0].severity == "error"


def test_check_primary_mismatch_fires_for_non_data_type(tmp_path: Path):
    """The mismatch branch is type-agnostic: a non-data repo that *declares* a
    primary must still declare it correctly (slice 45, decision #2)."""
    def _make(d):
        d["project"]["type"] = "project"
        d["data_products"] = {
            "primary": "data/x/",
            "outputs": [{"path": "data/y/", "description": "", "primary": False, "last_published": ""}],
        }
    _write_metadata(tmp_path, _make)
    findings = check_project(tmp_path)
    dp = [f for f in findings if f.kind and f.kind.startswith("data_products_")]
    assert len(dp) == 1
    assert dp[0].kind == "data_products_primary_mismatch"


def test_consumer_summary_distinguishes_all_from_primary(tmp_path: Path):
    """P5: the summary render paraphrased `subscription_label` and lost the
    `<all>` case, so an all-outputs subscription read as `<primary>`."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="a" * 40, all=True),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="a" * 40),
    ]).save(manifest_path)
    client = _client_with_provider_xw()

    findings = check_project(tmp_path, client=client)
    messages = [f.message for f in findings if f.source == manifest_path]

    assert any("<all>" in m for m in messages)
    assert sum("<primary>" in m for m in messages) == 1


def test_consumer_walk_resolves_each_producer_view_once(tmp_path: Path):
    """P5 made a repo able to hold several rows, and the walk resolved producer
    HEAD once per ROW. `try_at(repo, "")` "always pays the round-trip" by its
    own docstring, so a three-subscription repo tripled the network cost of
    every `check --upgrades` -- and `enclave bump` pays it again."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="a" * 40,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="a" * 40,
                        source_path="data/final/b"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="a" * 40),
    ]).save(manifest_path)

    calls: list[tuple[str, str]] = []
    real = ProducerView.try_at

    def counting_factory(repo_url: str, pin: str):
        calls.append((repo_url, pin))
        return real(repo_url, pin)

    check_project(tmp_path, upgrades=True, client=_client_with_provider_xw(),
                  producer_view_factory=counting_factory)

    assert len(calls) == len(set(calls)), f"resolved the same view twice: {calls}"
    # Three rows, one producer, two distinct pins asked for (the recorded one
    # and the HEAD sentinel) -> at most two resolves, not six.
    assert len(calls) <= 2, f"expected <=2 resolves for 3 rows of one repo, got {len(calls)}"


# ---------------------------------------------------------------------------
# D-C — the md5 drift rule truth table
#
# Drift = the producer's DVC pointer md5 for your path differs pin-vs-HEAD.
# Producer metadata carries no per-output content identity (`last_published`
# is a per-publish stamp), so the pointer is the ground truth. An unreadable
# pointer is `drift_unknown`, never `up_to_date`.
# ---------------------------------------------------------------------------


def _staged_pair(tmp_path: Path):
    """The fixture import plus a pin-view/HEAD-view factory pair at distinct
    pins — the shape `_view_with_primary`'s single hardcoded pin cannot
    express."""
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    pin_view = _view_with_primary("outputs/cms_based/", pin=_PIN)
    head_view = _view_with_primary("outputs/cms_based/", pin=_HEAD)
    return _factory_by_pin({_PIN: pin_view, "": head_view})


@pytest.mark.parametrize(
    "pin_ptr,head_ptr,expected_kind",
    [
        # pointer md5 identical -> up_to_date
        (_pointer_doc("a" * 32), _pointer_doc("a" * 32), "up_to_date"),
        # row's bytes changed (publish ran or not -- stamps are irrelevant,
        # see test_drift_ignores_last_published_stamps) -> drift
        (_pointer_doc("a" * 32), _pointer_doc("c" * 32), "drift"),
        # directory product: the `.dir` manifest hash moves -> drift
        (_pointer_doc("a" * 32 + ".dir"), _pointer_doc("c" * 32 + ".dir"), "drift"),
        # row absent at pin, present at HEAD -> drift (published after the
        # pin; a real, bumpable upgrade -- R1's newly-reachable path)
        (None, _pointer_doc("c" * 32), "drift"),
        # row removed at HEAD -> drift_unknown (a bump has no target)
        (_pointer_doc("a" * 32), None, "drift_unknown"),
        # no .dvc and no dvc.lock at either end -> drift_unknown
        (None, None, "drift_unknown"),
    ],
    ids=[
        "identical",
        "bytes-changed",
        "dir-manifest-moved",
        "absent-at-pin",
        "removed-at-head",
        "absent-both",
    ],
)
def test_md5_rule_truth_table(tmp_path: Path, pin_ptr, head_ptr, expected_kind):
    factory = _staged_pair(tmp_path)
    store: dict[tuple[str, str, str], bytes] = {}
    if pin_ptr is not None:
        store[(_VIEW_REPO, _PIN, _OUT_PTR)] = pin_ptr
    if head_ptr is not None:
        store[(_VIEW_REPO, _HEAD, _OUT_PTR)] = head_ptr

    findings = check_project(
        tmp_path,
        upgrades=True,
        producer_view_factory=factory,
        fetcher=_fetcher_serving(store),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert len(consumer) == 1
    assert consumer[0].kind == expected_kind
    # No cell is an error: check exit codes are unchanged (R4).
    assert consumer[0].severity in ("info", "warning")


def test_absent_at_pin_drift_message_states_the_difference_not_a_direction(
    tmp_path: Path,
):
    """BACKLOG 1b review-round-3 item 3: the md5 rule establishes INEQUALITY
    only — nothing asks whether HEAD descends from the pin (an ancestry query
    the ProducerView seam does not carry). A producer whose HEAD is behind the
    pin — a filesystem-path producer, or a remote HEAD that is not the default
    branch — must not read as an "upgrade"; the message states the one fact
    that is known."""
    factory = _staged_pair(tmp_path)
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_fetcher_serving(
            {(_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc("c" * 32)}
        ),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["drift"]
    assert consumer[0].message == (
        "producer's HEAD differs from your pin: outputs/cms_based/ is "
        "published at HEAD but not at your pin 4f7c2a1"
    )


def test_drift_ignores_last_published_stamps(tmp_path: Path):
    """M13c's falsifier, both directions: bytes changed with the stamp
    unchanged is DRIFT (the user's case — producer committed, never ran
    `mintd publish`); a restamp with the bytes unchanged is up_to_date."""
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")

    same_stamp = "2023-01-01T00:00:00Z"
    # bytes changed, stamp unchanged -> drift
    factory = _factory_by_pin({
        _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN, last_published=same_stamp),
        "": _view_with_primary("outputs/cms_based/", pin=_HEAD, last_published=same_stamp),
    })
    fetcher = _fetcher_serving({
        (_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32),
        (_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc("c" * 32),
    })
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["drift"]

    # stamp changed, bytes unchanged -> up_to_date
    factory = _factory_by_pin({
        _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN, last_published=same_stamp),
        "": _view_with_primary("outputs/cms_based/", pin=_HEAD, last_published="2026-01-01T00:00:00Z"),
    })
    fetcher = _fetcher_serving({
        (_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32),
        (_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc("a" * 32),
    })
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["up_to_date"]


def test_pointer_unreadable_at_head_only_is_drift_unknown(tmp_path: Path):
    """Network failure on ONE end must not degrade to a verdict."""
    from tests._fakes.producer import StaticFetcher
    from mintd.producer import FetchError

    class _HeadUnreachableFetcher(StaticFetcher):
        def fetch_path_at(self, repo: str, pin: str, path: str) -> bytes:
            if pin == _HEAD:
                raise FetchError.unreachable(repo, pin, "network down")
            return super().fetch_path_at(repo, pin, path)

    factory = _staged_pair(tmp_path)
    fetcher = _HeadUnreachableFetcher(
        {}, path_store={(_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32)}
    )

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == ["drift_unknown"]
    assert consumer[0].severity == "warning"


def test_add_only_elsewhere_is_up_to_date(tmp_path: Path):
    """M11's falsifier: another out in the same dvc.lock moved; the ROW's
    md5 did not. A rule that grabs the first out over-fires here."""
    factory = _staged_pair(tmp_path)

    def lock(other_md5: str) -> bytes:
        # The row's out is deliberately NOT first.
        return (
            "stages:\n"
            "  build:\n"
            "    outs:\n"
            f"    - md5: {other_md5}\n"
            "      path: outputs/other/\n"
            "    - md5: " + "a" * 32 + "\n"
            "      path: outputs/cms_based/\n"
        ).encode()

    fetcher = _fetcher_serving({
        # no per-path .dvc at either rev -> the walk falls back to dvc.lock
        (_VIEW_REPO, _PIN, "dvc.lock"): lock("1" * 32),
        (_VIEW_REPO, _HEAD, "dvc.lock"): lock("2" * 32),
    })

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["up_to_date"]


def _files_pointer(*pairs: tuple[str, str], path: str = "cms_based") -> bytes:
    """A version_aware (files-format) pointer: a `files:` list and NO
    top-level md5 — what dvc writes for a DIRECTORY out on a version_aware
    remote, which `_init_ops` turns on for every scaffolded producer."""
    body = "".join(
        f"  - relpath: {relpath}\n    md5: {md5}\n    version_id: v{i}\n"
        for i, (relpath, md5) in enumerate(pairs)
    )
    return f"outs:\n- path: {path}\n  files:\n{body}".encode()


@pytest.mark.parametrize(
    "pin_files,head_files,expected_kind",
    [
        # identical per-file hashes -> up_to_date
        ((("a.csv", "1" * 32),), (("a.csv", "1" * 32),), "up_to_date"),
        # one file's bytes moved -> drift
        ((("a.csv", "1" * 32),), (("a.csv", "9" * 32),), "drift"),
        # a file was added to the directory -> drift
        (
            (("a.csv", "1" * 32),),
            (("a.csv", "1" * 32), ("b.csv", "2" * 32)),
            "drift",
        ),
        # a file was removed from the directory -> drift
        (
            (("a.csv", "1" * 32), ("b.csv", "2" * 32)),
            (("a.csv", "1" * 32),),
            "drift",
        ),
    ],
    ids=["identical", "file-changed", "file-added", "file-removed"],
)
def test_version_aware_files_format_pointer_is_readable(
    tmp_path: Path, pin_files, head_files, expected_kind
):
    """A files-format out carries NO top-level md5, so a rule that reads only
    `md5` reported `drift_unknown` forever for the DEFAULT lab shape (a
    directory product on a version_aware remote) and permanently blocked
    every bump — while the `.dir` truth-table cell stayed green, because
    `md5: <hash>.dir` is the OTHER directory shape.

    Mutation: drop the `files:` branch from `_out_identity` -> every cell
    here becomes `drift_unknown`.
    """
    factory = _staged_pair(tmp_path)
    findings = check_project(
        tmp_path,
        upgrades=True,
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(*pin_files),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(*head_files),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == [expected_kind]
    assert consumer[0].severity in ("info", "warning")


def test_files_format_identity_ignores_entry_order(tmp_path: Path):
    """The digest is over SORTED pairs: dvc is free to reorder `files:`
    without the content changing, and a reorder must not read as drift."""
    factory = _staged_pair(tmp_path)
    a, b = ("a.csv", "1" * 32), ("b.csv", "2" * 32)
    findings = check_project(
        tmp_path,
        upgrades=True,
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(a, b),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(b, a),
        }),
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["up_to_date"]


def test_lock_basename_sibling_does_not_answer_for_a_removed_path(tmp_path: Path):
    """In `dvc.lock` the out paths are already repo-relative, so the basename
    fallback let `archive/final` answer for a subscribed `data/final` that is
    GONE at HEAD. That reported `drift` (or `up to date` on an md5 tie) where
    the truth is `drift_unknown` — and `drift` then sends `bump_import` on to
    rmtree the payload before an import that cannot succeed.

    Mutation: restore the unconditional basename fallback -> this reddens.
    """
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    factory = _factory_by_pin({
        _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
        "": _view_with_primary("outputs/cms_based/", pin=_HEAD),
    })

    def lock(*entries: tuple[str, str]) -> bytes:
        outs = "".join(f"    - md5: {md5}\n      path: {p}\n" for p, md5 in entries)
        return ("stages:\n  build:\n    outs:\n" + outs).encode()

    fetcher = _fetcher_serving({
        # The subscribed path exists at the pin beside a same-leaf sibling...
        (_VIEW_REPO, _PIN, "dvc.lock"): lock(
            ("outputs/cms_based", "a" * 32), ("archive/cms_based", "b" * 32)
        ),
        # ...and is GONE at HEAD, leaving only the sibling.
        (_VIEW_REPO, _HEAD, "dvc.lock"): lock(("archive/cms_based", "b" * 32)),
    })

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["drift_unknown"]


def _scaffold_lock(md5: str, *, wdir: str = "code", out: str = "../outputs/cms_based/") -> bytes:
    """A `dvc.lock` shaped like the one `dvc repro` writes for a mintd data
    scaffold: the stage declares `wdir: code` and its out is recorded
    RELATIVE TO THAT WDIR (`../data/final/`), not to the repo root."""
    return (
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: python build.py\n"
        f"    wdir: {wdir}\n"
        "    outs:\n"
        f"    - path: {out}\n"
        f"      md5: {md5}\n"
    ).encode()


def _scaffold_yaml(wdir: str = "code", out: str = "../outputs/cms_based/") -> bytes:
    return (
        "stages:\n"
        "  build:\n"
        "    cmd: python build.py\n"
        f"    wdir: {wdir}\n"
        "    outs:\n"
        f"      - {out}\n"
    ).encode()


@pytest.mark.parametrize(
    "pin_md5,head_md5,expected_kind",
    [
        ("a" * 32 + ".dir", "c" * 32 + ".dir", "drift"),
        ("a" * 32 + ".dir", "a" * 32 + ".dir", "up_to_date"),
    ],
    ids=["bytes-moved", "unchanged"],
)
def test_wdir_relative_lock_out_resolves(tmp_path: Path, pin_md5, head_md5, expected_kind):
    """`dvc.lock` records outs relative to the stage's `wdir`, and the mintd
    data scaffold emits `wdir: code` + `outs: - ../data/final/`. Comparing
    the raw string against the repo-relative subscribed path matched nothing,
    so D-C was inert for EVERY `mintd init data` producer and reported a
    false "not published at the producer's HEAD".

    A pipeline out has no per-path `.dvc` (publish never runs `dvc add`), so
    the lock is the only pointer source — there is no second chance.

    Mutation W1: drop the wdir join -> both cells become drift_unknown.
    """
    factory = _staged_pair(tmp_path)
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _HEAD, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _PIN, "dvc.lock"): _scaffold_lock(pin_md5),
            (_VIEW_REPO, _HEAD, "dvc.lock"): _scaffold_lock(head_md5),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == [expected_kind]


def test_dvc_yaml_stage_without_wdir_defaults_to_repo_root(tmp_path: Path):
    """A `dvc.yaml` that EXISTS but whose stage omits `wdir` — dvc's own
    default is the repo root. Distinct from the no-`dvc.yaml` case below:
    here `wdir_map` runs and must supply ".", not guess a source dir.

    Mutation W2b: default a missing `wdir` key to anything but "." -> the
    out no longer resolves and this becomes drift_unknown.
    """
    factory = _staged_pair(tmp_path)
    yaml_no_wdir = b"stages:\n  build:\n    cmd: python build.py\n    outs:\n      - outputs/cms_based/\n"
    def lock(m: str) -> bytes:
        return (
            "stages:\n  build:\n    cmd: python build.py\n    outs:\n"
            f"    - path: outputs/cms_based\n      md5: {m}\n"
        ).encode()
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): yaml_no_wdir,
            (_VIEW_REPO, _HEAD, "dvc.yaml"): yaml_no_wdir,
            (_VIEW_REPO, _PIN, "dvc.lock"): lock("a" * 32),
            (_VIEW_REPO, _HEAD, "dvc.lock"): lock("c" * 32),
        }),
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["drift"]


def _rootrel_lock(md5: str) -> bytes:
    """A lock whose out is already repo-relative — a `dvc add` producer."""
    return (
        "stages:\n  build:\n    outs:\n"
        f"    - path: outputs/cms_based\n      md5: {md5}\n"
    ).encode()


def test_absent_dvc_yaml_does_not_degrade_to_drift_unknown(tmp_path: Path):
    """A producer that genuinely has NO `dvc.yaml` (PATH_MISSING): every
    stage defaults to `wdir="."`, the pre-normalization behaviour, correct
    for a `dvc add` producer. The extra fetch must not break it.

    Mutation W5: treat an absent dvc.yaml as drift_unknown -> reddens.
    """
    factory = _staged_pair(tmp_path)
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_fetcher_serving({   # note: no dvc.yaml served at either rev
            (_VIEW_REPO, _PIN, "dvc.lock"): _rootrel_lock("a" * 32),
            (_VIEW_REPO, _HEAD, "dvc.lock"): _rootrel_lock("c" * 32),
        }),
    )
    assert [f.kind for f in findings if f.section == "consumer"] == ["drift"]


def test_unreadable_dvc_yaml_is_never_a_verdict(tmp_path: Path):
    """`PATH_MISSING` (the producer has no dvc.yaml) and a TRANSPORT failure
    are not interchangeable. Collapsing both to `{}` meant that, for the
    scaffold shape, an unresolvable out was dropped as escaping the root and
    read back as `_POINTER_ABSENT` — manufacturing a `drift` finding out of
    one network blip, which `bump` then acts on.

    The md5s here are IDENTICAL at both revs: the honest answer is
    `up_to_date` if readable, `drift_unknown` if not. `drift` is the bug.

    Mutation W6: return `{}` instead of `None` for a non-PATH_MISSING
    FetchError -> this reddens with kind == "drift".
    """
    from mintd.producer import FetchError
    from tests._fakes.producer import StaticFetcher

    class _YamlUnreachableAtPin(StaticFetcher):
        def fetch_path_at(self, repo: str, pin: str, path: str) -> bytes:
            if path == "dvc.yaml" and pin == _PIN:
                raise FetchError.unreachable(repo, pin, "network down")
            return super().fetch_path_at(repo, pin, path)

    factory = _staged_pair(tmp_path)
    same = "a" * 32 + ".dir"
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_YamlUnreachableAtPin({}, path_store={
            (_VIEW_REPO, _PIN, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _HEAD, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _PIN, "dvc.lock"): _scaffold_lock(same),
            (_VIEW_REPO, _HEAD, "dvc.lock"): _scaffold_lock(same),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["drift_unknown"], (
        f"a transport failure became {consumer[0].kind!r}: {consumer[0].message}"
    )


def test_dvc_yaml_is_fetched_once_per_repo_and_rev(tmp_path: Path):
    """The wdir memo is keyed (repo, rev) — coarser than the pointer memo's
    (repo, rev, path) — so one `dvc.yaml` serves every subscribed path of
    that repo. Three rows must not mean three fetches per rev.

    Mutation W4: key the memo on (repo, rev, path) -> fetch count triples.
    """
    from mintd.enclave import ApprovedProduct, EnclaveManifest
    from tests._fakes.producer import StaticFetcher

    _write_metadata(tmp_path)
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path=f"data/final/{leaf}")
        for leaf in ("a", "b", "c")
    ]).save(tmp_path / "enclave_manifest.yaml")

    store = {(_VIEW_REPO, rev, "dvc.yaml"): _scaffold_yaml() for rev in (_PIN, _HEAD)}
    for rev, md5 in ((_PIN, "a" * 32), (_HEAD, "c" * 32)):
        store[(_VIEW_REPO, rev, "dvc.lock")] = (
            "stages:\n  build:\n    wdir: code\n    outs:\n"
            + "".join(
                f"    - path: ../data/final/{leaf}\n      md5: {md5}\n"
                for leaf in ("a", "b", "c")
            )
        ).encode()

    fetcher = StaticFetcher({}, path_store=store)
    check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=_factory_by_pin({
            _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
            "": _view_with_primary("outputs/cms_based/", pin=_HEAD),
        }),
        fetcher=fetcher,
    )

    fetched = [path for _, _, path in fetcher.path_calls]
    assert fetched.count("dvc.yaml") == 2, (
        f"expected one dvc.yaml per rev for 3 rows of one repo, got "
        f"{fetched.count('dvc.yaml')}"
    )
    # The LOCK is the same document for every path of a repo at a rev, and it
    # was refetched and reparsed per row — N round-trips where one does.
    assert fetched.count("dvc.lock") == 2, (
        f"expected one dvc.lock per rev for 3 rows of one repo, got "
        f"{fetched.count('dvc.lock')}"
    )


def test_lock_stage_dep_resolves_via_primary_fallback(tmp_path: Path):
    """M12's falsifier: `from_dvc_lock_stage` records `output_path=""`;
    without the `or primary` fallback every pipeline-stage import would
    resolve an empty path and report the same verdict forever."""
    _write_metadata(tmp_path)
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: python build.py\n"
        "    deps:\n"
        "    - path: cms_based\n"
        "      repo:\n"
        "        url: https://github.com/example-org/provider-xw\n"
        f"        rev_lock: {_PIN}\n"
        "    outs: []\n",
        encoding="utf-8",
    )
    pin_view = _view_with_primary("outputs/cms_based/", pin=_PIN)
    head_view = _view_with_primary("outputs/cms_based/", pin=_HEAD)
    factory = _factory_by_pin({_PIN: pin_view, "": head_view})
    fetcher = _fetcher_serving({
        (_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32),
        (_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc("c" * 32),
    })

    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory, fetcher=fetcher
    )
    consumer = [f for f in findings if f.section == "consumer"]

    # Resolved via the pin view's primary -> a real verdict, not a stuck one.
    assert [f.kind for f in consumer] == ["drift"]


def test_all_outputs_row_ignores_a_member_with_no_pointer(tmp_path: Path):
    """`outputs[]` is hand-maintained metadata: nothing requires an entry to
    have a top-level DVC pointer. Folding head-side absence into `unreadable`
    let one such member (`docs/` here) veto the whole map — `drift_unknown`,
    which blocks `enclave bump` for the repo permanently — while the member
    that ACTUALLY moved was ignored. Only `None` is a read failure.

    Mutation: re-add `head_map[p] == _POINTER_ABSENT` to `unreadable` ->
    this reddens with drift_unknown.
    """
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN, all=True),
    ]).save(tmp_path / "enclave_manifest.yaml")

    meta = Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8"))
    meta = meta.model_copy(update={"data_products": DataProducts(
        primary="data/final/",
        outputs=[
            DataProductOutput(path="data/final/", description="d", primary=True,
                              last_published=""),
            DataProductOutput(path="docs/", description="d", primary=False,
                              last_published=""),
        ],
    )})
    factory = _factory_by_pin({
        _PIN: ProducerView(repo=_VIEW_REPO, pin=_PIN, metadata=meta),
        "": ProducerView(repo=_VIEW_REPO, pin=_HEAD, metadata=meta),
    })

    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        # `docs/` has no pointer at either rev; `data/final/` really moved.
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "data/final.dvc"): _pointer_doc("a" * 32, "final"),
            (_VIEW_REPO, _HEAD, "data/final.dvc"): _pointer_doc("c" * 32, "final"),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == ["drift"], consumer[0].message
    assert consumer[0].message == (
        "producer's HEAD differs from your pin (<all>): data/final/ changed"
    )


def test_all_outputs_row_reaches_the_comparator_from_the_manifest(tmp_path: Path):
    """The test above drives `_drift_finding_from_views` directly, so nothing
    exercised the enclave call site's `all_outputs=ap.all` wiring: changing
    it to a hardcoded `False` survived the entire suite, i.e. the `--all` map
    comparison could be deleted from production invisibly.

    Mutation: `all_outputs=ap.all` -> `all_outputs=False` at the enclave call
    site -> this reddens (the single-path lane reads only the primary, so the
    non-primary member's move is missed and the row reports up_to_date).
    """
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN, all=True),
    ]).save(manifest_path)

    meta = Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8"))
    meta = meta.model_copy(update={"data_products": DataProducts(
        primary="data/final/",
        outputs=[
            DataProductOutput(path="data/final/", description="d", primary=True,
                              last_published=""),
            DataProductOutput(path="data/extract/", description="d", primary=False,
                              last_published=""),
        ],
    )})
    factory = _factory_by_pin({
        _PIN: ProducerView(repo=_VIEW_REPO, pin=_PIN, metadata=meta),
        "": ProducerView(repo=_VIEW_REPO, pin=_HEAD, metadata=meta),
    })
    # Only the NON-primary member moves. A row that silently degraded to the
    # single-path lane would read `data/final/` alone and say up to date.
    fetcher = _fetcher_serving({
        (_VIEW_REPO, _PIN, "data/final.dvc"): _pointer_doc("a" * 32, "final"),
        (_VIEW_REPO, _HEAD, "data/final.dvc"): _pointer_doc("a" * 32, "final"),
        (_VIEW_REPO, _PIN, "data/extract.dvc"): _pointer_doc("b" * 32, "extract"),
        (_VIEW_REPO, _HEAD, "data/extract.dvc"): _pointer_doc("c" * 32, "extract"),
    })

    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory, fetcher=fetcher,
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == ["drift"]
    assert "data/extract/" in consumer[0].message


@pytest.mark.parametrize("abs_wdir_at", ["pin", "head"], ids=["pin", "head"])
def test_a_stage_dropped_for_an_absolute_wdir_is_never_a_verdict(
    tmp_path: Path, abs_wdir_at: str
) -> None:
    """`wdir_map` maps an absolute `wdir` to `None` and the lock walker drops
    that stage — correct, it cannot be resolved against a repo root. But a
    dropped stage's outs are INVISIBLE, and invisible reads exactly like
    absent: with the stage dropped at one rev and resolving at the other, the
    comparator manufactured "published at HEAD but not at your pin" and `bump`
    re-pinned on it. The md5s here are IDENTICAL at both revs, so `drift` is
    always the wrong answer.

    Nothing covered the absolute-wdir path at all before this.

    Mutation: stop reporting the drop from `_lock_with_resolved_paths` ->
    both cells become drift.
    """
    factory = _staged_pair(tmp_path)
    same = "a" * 32
    yamls = {
        rev: _scaffold_yaml(wdir="/absolute/build" if at == abs_wdir_at else "code")
        for rev, at in ((_PIN, "pin"), (_HEAD, "head"))
    }
    findings = check_project(
        tmp_path, upgrades=True, producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): yamls[_PIN],
            (_VIEW_REPO, _HEAD, "dvc.yaml"): yamls[_HEAD],
            (_VIEW_REPO, _PIN, "dvc.lock"): _scaffold_lock(same),
            (_VIEW_REPO, _HEAD, "dvc.lock"): _scaffold_lock(same),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["drift_unknown"], consumer[0].message
    # And for the right REASON: the pin side is unreadable, not "the producer
    # removed this output", which is what an unreported drop reads as.
    assert "no readable" in consumer[0].message


def test_an_orphan_lock_stage_leaves_absence_a_verdict(tmp_path: Path) -> None:
    """Where the orphan guard lives is a `check` decision, not only a
    fast-sync one — and this is the ONLY test that pins it.

    The guard belongs in `parse_dvc_lock_outs`'s own loop. Put it in the
    shared `stage_wdir` instead and it returns `None` for an orphan, which to
    this comparator already means "absolute wdir, stage dropped": the lock
    walker sets `dropped`, `_pointer_md5` flips `absent_so_far`, and every
    absent path on a producer carrying ONE stale stage stops being a verdict
    and becomes `drift_unknown`. Both existing wdir pins stay green under
    either placement (measured), so without this test the decision is prose.

    The second assertion states the accepted cost in the open:
    `_lock_with_resolved_paths` still resolves an orphan stage's outs, so a
    subscription to one still gets a byte verdict on data no `dvc pull` can
    fetch. S1 deliberately does not close that — it is a check-side fix.

    Mutation: move the guard from `parse_dvc_lock_outs` into `stage_wdir`,
    returning `None` for an orphan -> `data/nowhere.csv` flips `'<absent>'`
    -> `None` and `data/raw.csv` flips `'b'*32` -> `None`; this reddens while
    `test_a_stage_dropped_for_an_absolute_wdir_is_never_a_verdict` and
    `test_wdir_relative_lock_from_real_dvc_drives_check` stay green.
    """
    from mintd.check import _POINTER_ABSENT, _pointer_md5

    fetcher = _fetcher_serving({
        (_VIEW_REPO, _PIN, "dvc.yaml"): (
            b"stages:\n"
            b"  clean:\n"
            b"    wdir: code\n"
            b"    cmd: run\n"
            b"    outs:\n"
            b"      - ../data/clean.csv\n"
        ),
        (_VIEW_REPO, _PIN, "dvc.lock"): (
            b"schema: '2.0'\n"
            b"stages:\n"
            b"  clean:\n"
            b"    cmd: run\n"
            b"    outs:\n"
            b"    - path: ../data/clean.csv\n"
            b"      md5: " + b"a" * 32 + b"\n"
            b"  ingest:\n"
            b"    cmd: run\n"
            b"    outs:\n"
            b"    - path: data/raw.csv\n"
            b"      md5: " + b"b" * 32 + b"\n"
        ),
    })

    # A path nothing tracks stays ABSENT — evidence, not "cannot tell".
    assert _pointer_md5(
        fetcher, _VIEW_REPO, _PIN, "data/nowhere.csv", {},
    ) == _POINTER_ABSENT
    # The accepted cost: the orphan's own out still reads back a verdict.
    assert _pointer_md5(
        fetcher, _VIEW_REPO, _PIN, "data/raw.csv", {},
    ) == "b" * 32


def test_a_subpath_row_no_longer_blocks_its_drifting_sibling(tmp_path: Path) -> None:
    """The compound shape D1 makes dangerous: one repo, two rows, one of them a
    path inside the other. `enclave_bump` blocks the WHOLE repo on any row it
    cannot evaluate (one pin per repo, so a partial bump is not a thing), so
    the subpath row's `drift_unknown` vetoed a sibling with real drift — bare
    `mintd enclave bump` exited 2, which is exactly the command `enclave add`'s
    advisory points at.

    Mutation: drop the enclosing-out fallback in `_match_out_md5` -> BumpBlocked.
    """
    from mintd.enclave import ApprovedProduct, EnclaveManifest, enclave_bump

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path=p)
        for p in ("outputs/cms_based/", "outputs/cms_based/b.csv")
    ]).save(manifest_path)

    client = _client_with_provider_xw()
    findings = check_project(
        tmp_path, upgrades=True, client=client,
        producer_view_factory=_factory_by_pin({
            _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
            "": _view_with_primary("outputs/cms_based/", pin=_HEAD),
        }),
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(
                ("a.csv", "a" * 32), ("b.csv", "b" * 32)),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(
                ("a.csv", "a" * 32), ("b.csv", "z" * 32)),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert sorted(f.kind or "" for f in consumer) == ["drift", "drift"], [
        f.message for f in consumer
    ]

    def factory(url: str):
        return _view_with_primary("outputs/cms_based/", pin=_HEAD), _HEAD

    enclave_bump(
        client, manifest_path=manifest_path, name="provider-xw",
        producer_view_factory=factory, check_findings=findings,
    )

    assert EnclaveManifest.load(manifest_path).approved_products[0].pin == _HEAD


def _subscribe(tmp_path: Path, source_path: str):
    """An enclave subscribed to ONE producer path, plus the pin/HEAD factory."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path=source_path),
    ]).save(tmp_path / "enclave_manifest.yaml")
    return _factory_by_pin({
        _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
        "": _view_with_primary("outputs/cms_based/", pin=_HEAD),
    })


@pytest.mark.parametrize(
    "head_a,head_b,expected_kind",
    [
        ("a" * 32, "b" * 32, "up_to_date"),
        ("z" * 32, "b" * 32, "up_to_date"),
        ("a" * 32, "z" * 32, "drift"),
    ],
    ids=["nothing-moved", "sibling-moved", "subscribed-file-moved"],
)
def test_path_inside_a_tracked_out_is_answered_by_that_out(
    tmp_path: Path, head_a: str, head_b: str, expected_kind: str
):
    """A subscription to a file INSIDE a tracked directory has no `.dvc` of its
    own: `outputs/cms_based/b.csv` when the producer tracks
    `outputs/cms_based/`. Matching only the exact path found nothing, and the
    absence was rendered as "not published at the producer's HEAD" — a false
    statement about the producer, and `drift_unknown` blocks `enclave bump`
    for EVERY row of that repo, permanently.

    files-format carries per-file md5s in the fetched document, so the answer
    is exact: a SIBLING moving must stay invisible.

    Mutation: drop the enclosing-out fallback in `_match_out_md5` -> every
    cell becomes drift_unknown.
    """
    factory = _subscribe(tmp_path, "outputs/cms_based/b.csv")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(
                ("a.csv", "a" * 32), ("b.csv", "b" * 32)),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(
                ("a.csv", head_a), ("b.csv", head_b)),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == [expected_kind], consumer[0].message


@pytest.mark.parametrize(
    "head_md5,expected_kind",
    [("a" * 32 + ".dir", "up_to_date"), ("c" * 32 + ".dir", "drift")],
    ids=["unchanged", "directory-moved"],
)
def test_path_inside_a_dir_out_falls_back_to_the_directory_hash(
    tmp_path: Path, head_md5: str, expected_kind: str
):
    """`md5: <hash>.dir` carries no per-file detail — the manifest is a cache
    object, never in git — so the enclosing directory's hash is the only
    signal. Conservative by construction: a sibling's change reads as this
    path's drift, which costs a churn re-pin. Never a wrong "up to date",
    which is what `drift_unknown` here would have blocked forever instead.
    """
    factory = _subscribe(tmp_path, "outputs/cms_based/b.csv")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _pointer_doc("a" * 32 + ".dir"),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _pointer_doc(head_md5),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == [expected_kind], consumer[0].message


def test_path_removed_from_a_tracked_out_is_not_a_silent_up_to_date(tmp_path: Path):
    """The enclosing-out fallback must not turn a DELETED file into a verdict
    about its neighbours: `b.csv` gone from HEAD's manifest is absence, and a
    bump has no target for it."""
    factory = _subscribe(tmp_path, "outputs/cms_based/b.csv")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(
                ("a.csv", "a" * 32), ("b.csv", "b" * 32)),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(("a.csv", "a" * 32)),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["drift_unknown"]
    assert "not published at the producer's HEAD" in consumer[0].message
    # `subscription_label` for a source_path row IS the path, so the
    # parenthetical repeated what the sentence had already said.
    assert consumer[0].message.count("outputs/cms_based/b.csv") == 1, (
        consumer[0].message
    )
    # `drift_unknown` covers five states; only two are transport failures, and
    # a fixed "check the network" misdiagnosed this one at a working network.
    assert consumer[0].hint is not None
    assert "network" not in consumer[0].hint
    assert "pin to an older rev" in consumer[0].hint


def _scaffold_lock_outs(*outs: tuple[str, str]) -> bytes:
    """`_scaffold_lock` with N outs under its one `wdir: code` stage, in the
    order given."""
    body = "".join(f"    - path: {path}\n      md5: {md5}\n" for path, md5 in outs)
    return (
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: python build.py\n"
        "    wdir: code\n"
        "    outs:\n" + body
    ).encode()


#: A directory primary tracked as per-file lock outs BENEATH it — the shape
#: the scaffold's `validate` stage writes, and 18 of 22 catalog products.
_A, _B, _OTHER = "../outputs/cms_based/a.csv", "../outputs/cms_based/b.csv", "../outputs/other.csv"
_PIN_LOCK = _scaffold_lock_outs((_A, "a" * 32), (_B, "b" * 32), (_OTHER, "0" * 32))


def _directory_over_lock_outs(tmp_path: Path, head_lock: bytes, *, all_outputs=False):
    """Findings for a subscription to `outputs/cms_based/` when only a
    scaffold-shaped `dvc.lock` exists at both revs — no `.dvc` anywhere."""
    if all_outputs:
        from mintd.enclave import ApprovedProduct, EnclaveManifest

        _write_metadata(tmp_path)
        EnclaveManifest(enclave_name="e", approved_products=[
            ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN, all=True),
        ]).save(tmp_path / "enclave_manifest.yaml")
        factory = _factory_by_pin({
            _PIN: _view_with_primary("outputs/cms_based/", pin=_PIN),
            "": _view_with_primary("outputs/cms_based/", pin=_HEAD),
        })
    else:
        factory = _subscribe(tmp_path, "outputs/cms_based/")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _HEAD, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _PIN, "dvc.lock"): _PIN_LOCK,
            (_VIEW_REPO, _HEAD, "dvc.lock"): head_lock,
        }),
    )
    return [f for f in findings if f.section == "consumer"]


@pytest.mark.parametrize(
    "head_lock,expected_kind",
    [
        (_PIN_LOCK, "up_to_date"),
        (_scaffold_lock_outs((_A, "a" * 32), (_B, "z" * 32), (_OTHER, "0" * 32)), "drift"),
        (_scaffold_lock_outs((_A, "a" * 32), (_B, "b" * 32), (_OTHER, "9" * 32)), "up_to_date"),
        (_scaffold_lock_outs((_A, "a" * 32), (_B, "b" * 32), (_OTHER, "0" * 32),
                             ("../outputs/cms_based/c.csv", "c" * 32)), "drift"),
        (_scaffold_lock_outs((_A, "a" * 32), (_OTHER, "0" * 32)), "drift"),
    ],
    ids=["unchanged", "file-beneath-moved", "unrelated-out-moved", "file-added-beneath",
         "file-removed-beneath"],
)
def test_directory_over_lock_outs_beneath_it_gets_a_verdict(
    tmp_path: Path, head_lock: bytes, expected_kind: str
):
    """A declared DIRECTORY tracked as per-file outs beneath it has no pointer
    of its own. The comparator knew "exact" and "enclosing" only, so every
    such subscription read as absent — `drift_unknown`, "not published at the
    producer's HEAD", and `--bump` blocked for 18 of 22 catalog products.

    The outs beneath answer together: any of them moving, or one appearing,
    is drift; an unrelated out moving is not.

    Mutation: delete the `beneath` branch in `_match_out_md5` -> every cell
    becomes drift_unknown.
    """
    consumer = _directory_over_lock_outs(tmp_path, head_lock)
    assert [f.kind for f in consumer] == [expected_kind], consumer[0].message


def test_directory_beneath_identity_ignores_out_order(tmp_path: Path):
    """Mutation: drop `sorted(...)` on `beneath` -> drift."""
    reversed_lock = _scaffold_lock_outs((_OTHER, "0" * 32), (_B, "b" * 32), (_A, "a" * 32))
    consumer = _directory_over_lock_outs(tmp_path, reversed_lock)
    assert [f.kind for f in consumer] == ["up_to_date"], consumer[0].message


@pytest.mark.parametrize(
    "winner", ["../outputs/cms_based/", "../outputs/"], ids=["exact", "enclosing"]
)
def test_exact_and_enclosing_still_win_over_beneath(tmp_path: Path, winner: str):
    """A lock carrying an exact (or enclosing) out for the path AND outs
    beneath it answers with the exact/enclosing identity: that out is
    unchanged, so the verdict is up_to_date even though an out beneath moved.

    Mutation: move the `beneath` return above `enclosing` -> the enclosing
    cell reddens with drift.
    """
    def lock(beneath_md5: str) -> bytes:
        return _scaffold_lock_outs((winner, "d" * 32 + ".dir"), (_A, beneath_md5))
    factory = _subscribe(tmp_path, "outputs/cms_based/")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _HEAD, "dvc.yaml"): _scaffold_yaml(),
            (_VIEW_REPO, _PIN, "dvc.lock"): lock("a" * 32),
            (_VIEW_REPO, _HEAD, "dvc.lock"): lock("z" * 32),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["up_to_date"], consumer[0].message


def test_all_outputs_directory_member_over_lock_outs_reports_drift(tmp_path: Path):
    """The `--all` arm counts absent-at-both as equal, so a directory member
    over lock outs read CLEAN while a file beneath it moved — a silent wrong
    up_to_date, the bug class this lane exists to kill.

    Mutation: delete the `beneath` branch -> up_to_date.
    """
    moved = _scaffold_lock_outs((_A, "a" * 32), (_B, "z" * 32), (_OTHER, "0" * 32))
    consumer = _directory_over_lock_outs(tmp_path, moved, all_outputs=True)
    assert [f.kind for f in consumer] == ["drift"], consumer[0].message
    assert "outputs/cms_based/" in consumer[0].message


def test_unresolvable_path_hint_does_not_assert_the_producer_withdrew_it(tmp_path: Path):
    """No out at, above, or beneath the path at HEAD is `drift_unknown`, and
    the hint says what was CHECKED — it used to assert "the producer no longer
    publishes this output", which was false for every directory primary and
    sent researchers to drop live subscriptions.

    Mutation: restore the old hint text -> red.
    """
    gone = _scaffold_lock_outs((_OTHER, "0" * 32))
    consumer = _directory_over_lock_outs(tmp_path, gone)
    assert [f.kind for f in consumer] == ["drift_unknown"], consumer[0].message
    hint = consumer[0].hint
    assert hint is not None
    assert "no longer publishes" not in hint
    assert "network" not in hint
    assert "pin to an older rev" in hint and "drop the subscription" in hint


@pytest.mark.parametrize(
    "head_entries,expected_kind",
    [
        ((("sub/a.csv", "a" * 32), ("b.csv", "b" * 32)), "up_to_date"),
        ((("sub/a.csv", "z" * 32), ("b.csv", "b" * 32)), "drift"),
        ((("sub/a.csv", "a" * 32), ("b.csv", "z" * 32)), "up_to_date"),
        ((("b.csv", "b" * 32),), "drift_unknown"),
    ],
    ids=["unchanged", "file-beneath-moved", "sibling-outside-moved", "subdirectory-gone"],
)
def test_subdirectory_of_a_files_format_out_is_answered_by_the_entries_beneath_it(
    tmp_path: Path, head_entries, expected_kind: str
):
    """A files-format manifest lists FILES; a subscription to a subDIRECTORY
    inside the out has no entry of its own and read as absent. The entries
    beneath it answer together, exactly as lock outs beneath a declared
    directory do; a sibling outside the subdirectory stays invisible.

    Mutation: drop the `startswith(subpath + "/")` collection in
    `_out_identity` -> every cell becomes drift_unknown.
    """
    factory = _subscribe(tmp_path, "outputs/cms_based/sub/")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, _OUT_PTR): _files_pointer(
                ("sub/a.csv", "a" * 32), ("b.csv", "b" * 32)),
            (_VIEW_REPO, _HEAD, _OUT_PTR): _files_pointer(*head_entries),
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == [expected_kind], consumer[0].message


def test_a_dropped_stage_never_feeds_the_beneath_digest(tmp_path: Path):
    """`_pointer_md5` already refuses to call absence a verdict when a stage
    was dropped for an unresolvable `wdir`. The beneath digest is a SET
    identity, so a dropped member would read as "vanished" — drift, which
    `bump` acts on, over bytes that never moved. Lock identical at both revs;
    only HEAD's `dvc.yaml` gives the second stage an absolute `wdir`.

    Mutation: drop the `dropped_a_stage and md5.endswith(".outs")` guard in
    `_pointer_md5` -> drift.
    """
    def two_stage_yaml(other_wdir: str) -> bytes:
        return (
            "stages:\n"
            "  build:\n    cmd: python build.py\n    wdir: code\n"
            "    outs:\n      - ../outputs/cms_based/a.csv\n"
            "  other:\n    cmd: python other.py\n"
            f"    wdir: {other_wdir}\n"
            "    outs:\n      - ../outputs/cms_based/b.csv\n"
        ).encode()
    lock = (
        "schema: '2.0'\nstages:\n"
        "  build:\n    cmd: python build.py\n    wdir: code\n    outs:\n"
        f"    - path: ../outputs/cms_based/a.csv\n      md5: {'a' * 32}\n"
        "  other:\n    cmd: python other.py\n    wdir: code\n    outs:\n"
        f"    - path: ../outputs/cms_based/b.csv\n      md5: {'b' * 32}\n"
    ).encode()
    factory = _subscribe(tmp_path, "outputs/cms_based/")
    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory,
        fetcher=_fetcher_serving({
            (_VIEW_REPO, _PIN, "dvc.yaml"): two_stage_yaml("code"),
            (_VIEW_REPO, _HEAD, "dvc.yaml"): two_stage_yaml("/abs/code"),
            (_VIEW_REPO, _PIN, "dvc.lock"): lock,
            (_VIEW_REPO, _HEAD, "dvc.lock"): lock,
        }),
    )
    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == ["drift_unknown"], consumer[0].message


# ---------------------------------------------------------------------------
# D-D — each consumer row carries its own subscription label
# ---------------------------------------------------------------------------


def test_enclave_rows_carry_their_own_subscription_label(tmp_path: Path):
    """Three rows of one repo: ONE repo-keyed field_path (D1 — enclave_bump
    fans over all rows under it), THREE distinct messages. `_render_findings`
    prints only the message, so the label must live there."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/b"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN),
    ]).save(manifest_path)
    client = _client_with_provider_xw()

    pin_view = _view_with_primary("outputs/cms_based/", pin=_PIN)
    head_view = _view_with_primary("outputs/cms_based/", pin=_HEAD)
    factory = _factory_by_pin({_PIN: pin_view, "": head_view})
    fetcher = _fetcher_serving({
        (_VIEW_REPO, rev, ptr): _pointer_doc("a" * 32, path)
        for rev in (_PIN, _HEAD)
        for ptr, path in (
            ("data/final/a.dvc", "a"),
            ("data/final/b.dvc", "b"),
            (_OUT_PTR, "cms_based"),
        )
    })

    findings = check_project(
        tmp_path, upgrades=True, client=client,
        producer_view_factory=factory, fetcher=fetcher,
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert len(consumer) == 3
    assert {f.field_path for f in consumer} == {"approved_products[provider-xw]"}
    assert [f.message for f in consumer] == [
        "up to date (data/final/a)",
        "up to date (data/final/b)",
        "up to date (<primary>)",
    ]


def test_enclave_head_unreachable_is_labelled_and_not_up_to_date(tmp_path: Path):
    """Parity with the dvc arm: an unreachable HEAD is `drift_unknown`, not
    a clean row. Still one labelled line per row — unlabelled it prints N
    identical lines, the exact complaint D-D exists to fix."""
    from mintd.data import BumpBlocked
    from mintd.enclave import ApprovedProduct, EnclaveManifest, enclave_bump

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/b"),
    ]).save(manifest_path)
    client = _client_with_provider_xw()

    def factory(repo: str, pin: str):
        if pin == "":
            return ProducerError.unreachable(repo, pin, "network down")
        return _view_with_primary("outputs/cms_based/", pin=_PIN)

    findings = check_project(
        tmp_path, upgrades=True, client=client, producer_view_factory=factory,
        fetcher=_fetcher_serving({}),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert {f.kind for f in consumer} == {"drift_unknown"}
    # `enclave_bump` finds its rows by `source` AND `field_path`; get either
    # wrong and the offline bump raises ImportNotFound instead of blocking.
    assert {f.field_path for f in consumer} == {"approved_products[provider-xw]"}
    assert all(f.hint and _PIN[:7] in f.hint for f in consumer)
    assert [f.message for f in consumer] == [
        "cannot determine drift (data/final/a): cannot read the producer's "
        "HEAD (unreachable: network down)",
        "cannot determine drift (data/final/b): cannot read the producer's "
        "HEAD (unreachable: network down)",
    ]

    # The enclave twin of `test_bump_blocks_when_the_producer_head_is_unreachable`,
    # and the only binding on the `source=` the enclave arm passes: offline,
    # `enclave bump` must block rather than silently no-op.
    with pytest.raises(BumpBlocked) as ei:
        enclave_bump(client, manifest_path=manifest_path, name="provider-xw",
                     check_findings=findings)
    assert ei.value.finding.kind == "drift_unknown"


def test_enclave_error_findings_are_labelled_too(tmp_path: Path):
    """An unreachable repo with three subscription rows otherwise prints
    three identical unlabeled error lines — `_error_finding_for` kept the
    twin of the defect D-D fixed in the up-to-date and degrade arms."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/b"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN),
    ]).save(manifest_path)

    def factory(repo: str, pin: str):
        return ProducerError.unreachable(repo, pin, "network down")

    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory, fetcher=_fetcher_serving({}),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert {f.kind for f in consumer} == {"unreachable"}
    assert [f.message for f in consumer] == [
        "producer unreachable: network down (data/final/a)",
        "producer unreachable: network down (data/final/b)",
        "producer unreachable: network down (<primary>)",
    ]


def test_enclave_empty_pin_findings_are_labelled_too(tmp_path: Path):
    """Two same-repo rows both with an empty pin otherwise produce two
    byte-identical unlabeled error lines (same message, same field_path) —
    the empty-pin arm kept the defect D-D fixed three lines below it."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="",
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin="",
                        source_path="data/final/b"),
    ]).save(manifest_path)

    def factory(repo: str, pin: str):
        return ProducerError.unreachable(repo, pin, "network down")

    findings = check_project(
        tmp_path, upgrades=True, client=_client_with_provider_xw(),
        producer_view_factory=factory, fetcher=_fetcher_serving({}),
    )
    consumer = [f for f in findings if f.section == "consumer"]

    assert {f.kind for f in consumer} == {"pin_missing"}
    assert [f.message for f in consumer] == [
        "approved product 'provider-xw' has an empty pin (data/final/a)",
        "approved product 'provider-xw' has an empty pin (data/final/b)",
    ]


def test_enclave_catalog_client_none_findings_are_labelled_too(tmp_path: Path):
    """Two same-repo rows with no catalog client otherwise print two
    byte-identical unlabeled error lines (same message, same field_path) —
    the client-None arm kept the defect D-D fixed in its sibling arms."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/b"),
    ]).save(manifest_path)

    findings = check_project(tmp_path)
    consumer = [f for f in findings if f.section == "consumer"]

    assert {f.kind for f in consumer} == {"catalog_unresolved"}
    assert [f.message for f in consumer] == [
        "catalog client not provided; cannot resolve producer URL for"
        " provider-xw (data/final/a)",
        "catalog client not provided; cannot resolve producer URL for"
        " provider-xw (data/final/b)",
    ]


def test_enclave_catalog_resolve_failure_findings_are_labelled_too(tmp_path: Path):
    """Two same-repo rows against an unreachable registry otherwise print two
    byte-identical unlabeled error lines — the resolve-failure arm was the
    last unlabeled per-row error emission in the enclave walk."""
    from mintd.enclave import ApprovedProduct, EnclaveManifest

    _write_metadata(tmp_path)
    manifest_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="e", approved_products=[
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/a"),
        ApprovedProduct(repo="provider-xw", registry_entry="e", pin=_PIN,
                        source_path="data/final/b"),
    ]).save(manifest_path)

    findings = check_project(tmp_path, client=_UnreachableRegistryClient())
    consumer = [f for f in findings if f.section == "consumer"]

    assert {f.kind for f in consumer} == {"catalog_unresolved"}
    msgs = [f.message for f in consumer]
    assert msgs[0].endswith(" (data/final/a)")
    assert msgs[1].endswith(" (data/final/b)")
    # Identical apart from the label: same repo-level failure, but each line
    # says which subscription row it belongs to.
    assert msgs[0].removesuffix(" (data/final/a)") == msgs[1].removesuffix(
        " (data/final/b)"
    )

# ---------------------------------------------------------------------------
# An empty pin is refused by PLAIN `check`, not only by `--upgrades`
# ---------------------------------------------------------------------------

# `tests/test_producer.py` pins the same guard on both lanes under
# `upgrades=True`. These are its `upgrades=False` siblings: an empty pin is a
# pure manifest-validity fault needing no network, so gating it behind
# `--upgrades` made plain `mintd check` (and `publish` / `registry register`,
# which both call `check_project(upgrades=False)`) render an unpinned import
# as a clean `[info]` summary line and exit 0.
#
# Parametrized over whitespace because the guard's `.strip()` had no killing
# test on either lane, and this is the path where it matters: `try_at` reads
# `"   "` as a real rev, so under `--upgrades` a whitespace pin still fails at
# the producer, but here nothing resolves anything and it would render clean.
#
# `rev_lock:` (YAML null) and a deleted `rev_lock` key are the two spellings a
# hand-edit actually produces, and both used to escape this guard by crashing
# before it: `DataDependency` took `repo["rev_lock"]` straight, so `check`
# (and `publish` / `registry register`) died with a raw pydantic
# `ValidationError` / `KeyError` traceback instead of reporting `pin_missing`.


@pytest.mark.parametrize(
    "rev_lock_yaml",
    ["rev_lock: ''", "rev_lock: '   '", "rev_lock:", ""],
    ids=["empty", "whitespace", "null", "absent"],
)
def test_plain_check_refuses_an_empty_dvc_rev_lock(tmp_path: Path, rev_lock_yaml: str):
    _write_metadata(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    dvc_file = tmp_path / "data" / "imports" / "standalone_import.dvc"
    dvc_file.write_text(
        dvc_file.read_text(encoding="utf-8").replace(f"rev_lock: {_PIN}", rev_lock_yaml),
        encoding="utf-8",
    )

    findings = check_project(tmp_path)  # no --upgrades
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == ["pin_missing"]
    assert consumer[0].severity == "error"
    assert consumer[0].source == dvc_file
    # An error with no way out is half a finding: `--dvc-arg=--no-exec`
    # produces this state without any hand-editing, so the line has to say
    # how to get back. Mutation: drop the `hint=` -> this reddens.
    assert consumer[0].hint and "mintd data import" in consumer[0].hint


@pytest.mark.parametrize("pin_yaml", ["''", "'   '"])
def test_plain_check_refuses_an_empty_manifest_pin(tmp_path: Path, pin_yaml: str):
    _write_metadata(tmp_path)
    manifest = _stage_enclave_manifest(tmp_path)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(f"pin: {_PIN}", f"pin: {pin_yaml}"),
        encoding="utf-8",
    )

    findings = check_project(tmp_path, client=_client_with_provider_xw())  # no --upgrades
    consumer = [f for f in findings if f.section == "consumer"]

    assert [f.kind for f in consumer] == ["pin_missing"]
    assert consumer[0].severity == "error"
    assert consumer[0].field_path == "approved_products[provider-xw]"
    # Names the row's own repo, so the hint is copy-pasteable rather than a
    # shape to fill in. Mutation: drop the `hint=` -> this reddens.
    assert consumer[0].hint and "mintd enclave add provider-xw" in consumer[0].hint


# ---------------------------------------------------------------------------
# The delivery gate — a declared output `mintd data import` cannot land is an
# error. Real pointer files from tests/_dvc_fixtures.py; the gate reads files
# and nothing else, so there is no double anywhere in this section.
# ---------------------------------------------------------------------------


def _gate(findings: list[CheckFinding]) -> list[CheckFinding]:
    return [f for f in findings if f.kind and f.kind.startswith("data_products_output_")]


def test_check_errors_on_a_dir_pointer_without_version_ids(tmp_path: Path) -> None:
    """The fleet's dominant failure as a fixture: `data/final/` declared and
    tracked as a `.dir` out that was pushed before the remote became
    version-aware, so the pointer carries no S3 version ID and a consumer's
    import lands an empty directory at exit 0. Exactly ONE finding: the
    storage block matches `.dvc/config`, and the primary is also outputs[0]
    (one path, one finding, under the field that declares it first).

    Mutation: count every out as pinned in `_dvc_state._coverage` -> reddens.
    """
    stage_undeliverable_primary(tmp_path)

    findings = check_project(tmp_path)

    assert len(findings) == 1, findings
    f = findings[0]
    assert f.severity == "error"
    assert f.kind == "data_products_output_unpinned"
    assert f.field_path == "data_products.primary"
    assert f.source == tmp_path / "metadata.json"
    assert "'data/final/'" in f.message and "0/1 files" in f.message
    assert f.hint and "mintd data push" in f.hint
    assert "commit and push the rewritten" in f.hint  # importers read the pushed commit


def test_check_is_clean_when_the_dir_pointer_is_pinned(tmp_path: Path) -> None:
    """Same shape with the version ID dvc records on a version-aware push."""
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final", is_dir=True, version_id="v-1")

    assert check_project(tmp_path) == []


def test_check_ignores_version_ids_on_an_md5_remote(tmp_path: Path) -> None:
    """Without `version_aware = true` the S3 key is the md5, plain `dvc pull`
    serves the out, and a missing version ID is not a defect. This is also
    why the harness's local-directory remotes never trip the gate.

    Mutation: drop `version_aware and` from the pin arms in `delivery` -> reddens.
    """
    stage_undeliverable_primary(tmp_path, version_aware=False)

    assert check_project(tmp_path) == []


@pytest.mark.parametrize("b_version_id", ["v-2", None], ids=["both-pinned", "b-unpinned"])
def test_check_directory_over_per_file_pointers(tmp_path: Path, b_version_id: str | None) -> None:
    """Direction 3 — the ordinary lab shape: `data/final/` declared, two
    per-file pointers beneath it. Both pinned -> clean. One unpinned -> one
    finding that names that file and not its pinned sibling.

    Mutation: stop collecting `unpinned` in `_coverage` -> the hint loses the
    file name and reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final/a.csv", version_id="v-1")
    write_pointer(tmp_path, "data/final/b.csv", version_id=b_version_id)

    gate = _gate(check_project(tmp_path))

    if b_version_id:
        assert gate == []
    else:
        assert [f.kind for f in gate] == ["data_products_output_unpinned"]
        assert "data/final/b.csv" in gate[0].message
        assert gate[0].hint and "data/final/b.csv" in gate[0].hint
        assert "data/final/a.csv" not in gate[0].hint


def test_check_errors_on_a_declared_path_dvc_tracks_nowhere(tmp_path: Path) -> None:
    """A declaration can be fiction. The hint names what DVC does track, so a
    typo is visible, and the command that would make the claim true.

    Mutation: return `()` for `tracked` in `delivery` -> reddens on the hint.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/other.csv")

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_untracked"]
    assert "is not a DVC-tracked output" in gate[0].message
    assert gate[0].hint and "data/other.csv" in gate[0].hint
    assert "'mintd data add'" in gate[0].hint


def test_check_errors_on_a_subpath_missing_from_a_files_manifest(tmp_path: Path) -> None:
    """A files-format directory out carries its manifest inline, so a declared
    subpath it does not hold is decidable offline. The primary (the directory
    itself) stays clean; only the fictional row errors, under its own field.

    Mutation: drop the `if not inside` arm in `delivery` -> reddens.
    """
    stage_dvc_project(tmp_path, declared=("data/final/", "data/final/b.csv"))
    write_pointer(tmp_path, "data/final", files={"a.csv": "v-1"})

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_missing_from_manifest"]
    assert gate[0].field_path == "data_products.outputs[1].path"


@pytest.mark.parametrize("version_aware", [True, False], ids=["va", "md5"])
def test_check_errors_on_a_cache_false_lock_out(tmp_path: Path, version_aware: bool) -> None:
    """`cache: false` means dvc never pushes the out, so no remote layout can
    deliver it — the finding fires on either remote type.

    Mutation: drop the `use_cache` arm in `_never_pushed` -> reddens.
    """
    stage_dvc_project(tmp_path, declared=("data/final/x.csv",), version_aware=version_aware)
    write_pipeline(tmp_path, "data/final/x.csv", cache=False)

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_not_pullable"]
    assert "cache: false" in gate[0].message


def test_check_skips_the_delivery_gate_without_a_dvc_dir(tmp_path: Path) -> None:
    """No `.dvc/` means no DVC truth to compare against: a bare metadata.json
    (every minimal-fixture test in this file) is not a product that promised
    anything DVC-delivered, and `mintd init` always creates the directory.
    A pointer for some other path lying beside it does not change that (the
    shape is UNTRACKED, which fires on either remote layout, so this test
    cannot pass by accident of the version-aware rule).

    Mutation: delete the `.is_dir()` guard in `_producer_findings` -> this and
    every no-finding assertion on the minimal fixture redden.
    """
    _write_metadata(tmp_path)  # declares data/final/
    write_pointer(tmp_path, "data/other.csv")  # DVC tracks something ELSE: untracked if the gate ran
    assert not (tmp_path / ".dvc").exists()

    assert check_project(tmp_path) == []


# (id, setup, declared, predicate state, kinds `check` must emit)
_PARITY_ROWS = [
    ("untracked", lambda p: write_pointer(p, "data/raw.csv"),
     "data/final/", "untracked", {"data_products_output_untracked"}),
    ("not-pullable", lambda p: write_pipeline(p, "data/final/x.csv", cache=False),
     "data/final/x.csv", "not_pullable", {"data_products_output_not_pullable"}),
    ("unpinned", lambda p: write_pointer(p, "data/final", is_dir=True, version_id=None),
     "data/final/", "unpinned", {"data_products_output_unpinned"}),
    ("partial", lambda p: (write_pointer(p, "data/final/a.csv"),
                           write_pointer(p, "data/final/b.csv", version_id=None)),
     "data/final/", "partial", {"data_products_output_unpinned"}),
    ("missing-from-manifest", lambda p: write_pointer(p, "data/final", files={"a.csv": "v-1"}),
     "data/final/b.csv", "missing_from_manifest", {"data_products_output_missing_from_manifest"}),
    ("pinned-beside-a-cache-false-sibling", lambda p: (write_pointer(p, "data/final/a.csv"),
                                                       write_pipeline(p, "data/final/b.csv", cache=False)),
     "data/final/", "pinned", set()),
    ("via-parent-pinned", lambda p: write_pointer(p, "data/final", is_dir=True, version_id="v-1"),
     "data/final/x.csv", "via_parent", set()),
    ("via-parent-unpinned", lambda p: write_pointer(p, "data/final", is_dir=True, version_id=None),
     "data/final/x.csv", "unpinned", {"data_products_output_unpinned"}),
    ("pinned", lambda p: write_pointer(p, "data/final", is_dir=True, version_id="v-1"),
     "data/final/", "pinned", set()),
]


@pytest.mark.parametrize("row", _PARITY_ROWS, ids=[r[0] for r in _PARITY_ROWS])
def test_delivery_state_and_finding_kind_agree(tmp_path: Path, row) -> None:
    """The gate and the predicate are one judgement: for every state the kind
    `check` emits is the one `_UNDELIVERABLE_KINDS` maps it to, the message
    carries the predicate's own detail, and the two deliverable states emit
    nothing. `via-parent-unpinned` pins the inheritance decision: a subpath
    under an unpinned `.dir` out errors with its parent instead of being
    blessed as undecidable; `via-parent-pinned` pins the other half — no
    warning, because warnings block nothing and would print on every subpath
    of every healthy directory out.

    Mutation: `return Delivery("via_parent", …)` for every strict-ancestor
    match before the coverage tally -> the `via-parent-unpinned` row reddens.
    """
    _, setup, declared, state, kinds = row
    stage_dvc_project(tmp_path, declared=(declared,))
    setup(tmp_path)

    verdict = delivery(tmp_path, declared, REMOTE, version_aware=True)
    gate = _gate(check_project(tmp_path))

    assert verdict.state == state, verdict
    assert {f.kind for f in gate} == kinds
    for f in gate:
        assert verdict.detail in f.message


def test_every_undeliverable_state_has_a_finding_kind() -> None:
    """A state added to the predicate without a kind would KeyError on the
    first product that reaches it; the two deliverable states are the only
    ones allowed to map to nothing."""
    assert set(_UNDELIVERABLE_KINDS) == set(get_args(DeliveryState)) - {"pinned", "via_parent"}


def test_check_ignores_a_never_pushed_out_beside_pullable_siblings(tmp_path: Path) -> None:
    """A `cache: false` out beneath a declared directory, next to pinned
    siblings, is NOT an undeliverable promise. A lab pipeline commonly writes
    `data/final/validation_report.txt` as a `cache: false` metric beside the
    data it pushes (a live product does exactly this and imports cleanly);
    dvc never stores the file, and the pull lane drops uncached outs rather
    than failing on them. A directory declaration promises what dvc stores. (A path that
    resolves to NOTHING BUT never-pushed outs still errors -- see the
    cache-false test above.)

    Mutation: count never-pushed siblings as unpinned files in the tally ->
    reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final/a.csv", version_id="v-1")
    write_pipeline(tmp_path, "data/final/b.csv", cache=False)

    assert _gate(check_project(tmp_path)) == []


def test_check_refuses_a_declaration_that_differs_only_by_case(tmp_path: Path) -> None:
    """`outs_matching` folds case (right on the producer's own, often
    case-insensitive, filesystem); delivery must not, because a Linux
    consumer's `dvc import Data/Final/` finds nothing. The hint names the
    spelling DVC tracks.

    Mutation: `exact = matched` in `delivery` -> reddens.
    """
    stage_dvc_project(tmp_path, declared=("Data/Final/",))
    write_pointer(tmp_path, "data/final", is_dir=True, version_id="v-1")

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_untracked"]
    assert "only by case" in gate[0].message
    assert gate[0].hint and "data/final" in gate[0].hint


def test_check_untracked_hint_names_the_pipeline_stage_when_dvc_yaml_declares_the_path(
    tmp_path: Path,
) -> None:
    """Every fresh scaffold, once its primary is filled in: dvc.yaml's
    `validate` stage declares `../data/final/` under `wdir: code`, nothing has
    produced it, so there is no dvc.lock entry and DVC tracks nothing. The
    hint states that fact -- the stage name and the missing lock entry -- so
    the producer knows the path is the pipeline's to produce, not `dvc add`'s
    (which refuses a path a stage declares). Running the stage deletes what is
    in `data/final/` first -- a file placed there by hand is in neither git nor
    the DVC cache -- so the hint says so.

    Mutation: `_pipeline_stage_declaring` returns None -> reddens; drop
    `_STAGE_RUN_DELETES` from the hint -> reddens.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "dvc.yaml").write_text(
        "stages:\n  validate:\n    wdir: code\n    cmd: run\n    outs:\n    - ../data/final/\n",
        encoding="utf-8",
    )

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_untracked"]
    assert gate[0].hint and "stage 'validate'" in gate[0].hint and "no entry" in gate[0].hint
    assert "dvc deletes a stage's outputs before it runs" in gate[0].hint


def test_check_reports_an_unreadable_dvc_lock_instead_of_a_traceback(tmp_path: Path) -> None:
    """A dvc.lock dvc itself would reject (here: a bare-string out, valid YAML
    in the wrong shape) is this project's defect, reported as one error
    finding that names the file -- `check`, `publish` and `register` all
    route through the gate, and none of them may traceback on it.

    Mutation: make `_unreadable_dvc_files` accept any YAML shape -> the
    lock reaches the parser, and this reddens on the kind.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "dvc.yaml").write_text(
        "stages:\n  build:\n    cmd: run\n    outs:\n    - data/final\n", encoding="utf-8"
    )
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n    - data/final\n", encoding="utf-8"
    )

    findings = check_project(tmp_path)

    assert [f.kind for f in findings] == ["dvc_state_unreadable"]
    assert findings[0].severity == "error"
    assert findings[0].source == tmp_path / "dvc.lock"
    assert "not in the shape dvc writes" in findings[0].message


def test_check_case_mismatch_hint_names_the_tracked_spelling(tmp_path: Path) -> None:
    """When the only thing wrong is the case, the repair is the spelling: the
    hint must name what DVC tracks, not send the producer to `dvc repro` for
    an out that already exists in dvc.lock.

    Mutation: drop the `twins` branch in `_delivery_hint` -> reddens.
    """
    stage_dvc_project(tmp_path)
    write_pipeline(tmp_path, "Data/Final", files={"a.csv": "v-1"})

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_untracked"]
    assert gate[0].hint and "Data/Final" in gate[0].hint and "only by case" in gate[0].hint
    assert "dvc repro" not in gate[0].hint and "no entry" not in gate[0].hint


def test_check_missing_from_manifest_hint_names_the_owning_stage(tmp_path: Path) -> None:
    """The ordinary lab shape for this state: `data/final/` is a pipeline
    stage's files-format out and an outputs[] row names a file the manifest
    lacks. The hint names the stage that owns the directory -- and no command:
    `dvc add` refuses a stage-declared path -- plus the warning that running
    the stage deletes anything placed in the directory by hand.

    Mutation: `_pipeline_stage_declaring` returns None -> reddens; drop
    `_STAGE_RUN_DELETES` from the hint -> reddens.
    """
    stage_dvc_project(tmp_path, declared=("data/final/", "data/final/old.csv"))
    write_pipeline(tmp_path, "data/final", files={"new.csv": "v-1"})

    gate = _gate(check_project(tmp_path))

    assert [f.kind for f in gate] == ["data_products_output_missing_from_manifest"]
    assert gate[0].hint and "stage 'build'" in gate[0].hint
    assert "dvc deletes a stage's outputs before it runs" in gate[0].hint
    assert "dvc repro" not in gate[0].hint and "mintd data add" not in gate[0].hint


def test_check_reports_a_corrupt_pointer_instead_of_calling_its_out_untracked(tmp_path: Path) -> None:
    """A `.dvc` file dvc cannot read (here: the conflict markers a merge
    leaves) vanishes from the out enumeration, so without this guard its out
    reads as "DVC tracks nothing" with a `mintd data add` repair that dvc
    refuses. Name the file, and verify nothing until it is fixed.

    Mutation: `_unreadable_dvc_files` returns [] -> reddens.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_text(
        "<<<<<<< HEAD\nouts:\n- md5: aaaa.dir\n  path: final\n=======\nouts:\n- md5: bbbb.dir\n  path: final\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    findings = check_project(tmp_path)

    assert [f.kind for f in findings] == ["dvc_state_unreadable"]
    assert findings[0].source == tmp_path / "data" / "final.dvc"
    assert "not readable as YAML" in findings[0].message
    assert findings[0].hint and "conflict markers" in findings[0].hint


def test_check_reports_a_conflicted_dvc_lock_without_a_traceback(tmp_path: Path) -> None:
    """The most common DVC merge conflict: two branches ran the pipeline and
    the merge left markers in dvc.lock. The lock parser swallows the YAML
    error into "no outs" (so the gate would call every stage output never
    produced), and the consumer walk parses the same file and raises. The
    producer section names dvc.lock; the consumer section says the imports
    went unchecked -- no traceback.

    Mutation: drop the `except` around the consumer walk in `check_project`
    -> yaml's ScannerError escapes and this reddens.
    """
    stage_dvc_project(tmp_path)
    write_pipeline(tmp_path, "data/final", files={"a.csv": "v-1"})
    lock = tmp_path / "dvc.lock"
    lock.write_text(
        "<<<<<<< HEAD\n" + lock.read_text(encoding="utf-8") + "=======\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    findings = check_project(tmp_path)

    assert [(f.section, f.kind) for f in findings] == [
        ("producer", "dvc_state_unreadable"),
        ("consumer", "dvc_state_unreadable"),
    ]
    assert findings[0].source == tmp_path / "dvc.lock"
    assert "could not read dvc.lock" in findings[0].message
    assert "imports were not checked" in findings[1].message


def test_check_reports_an_empty_pointer_as_unreadable(tmp_path: Path) -> None:
    """A 0-byte .dvc file (a truncated write, a bad checkout) parses to None,
    yields no outs, and would otherwise read as untracked."""
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_text("", encoding="utf-8")

    findings = check_project(tmp_path)

    assert [f.kind for f in findings] == ["dvc_state_unreadable"]
    assert "empty" in findings[0].message


def test_check_reports_a_lock_the_parser_rejects_after_the_screen(tmp_path: Path) -> None:
    """The screen checks shape, not every value: a lock out with `size:
    twelve` passes it and raises inside the lock parser. That is still this
    project's defect to report by name, not a traceback.

    Mutation: narrow the `except Exception` around the gate in
    `_producer_findings` -> the ValueError escapes and this reddens.
    """
    stage_dvc_project(tmp_path)
    (tmp_path / "dvc.yaml").write_text(
        "stages:\n  build:\n    cmd: run\n    outs:\n    - data/final\n", encoding="utf-8"
    )
    (tmp_path / "dvc.lock").write_text(
        "schema: '2.0'\nstages:\n  build:\n    cmd: run\n    outs:\n"
        "    - path: data/final\n      md5: " + "b" * 32 + "\n      size: twelve\n",
        encoding="utf-8",
    )

    findings = check_project(tmp_path)

    assert [f.kind for f in findings] == ["dvc_state_unreadable"]
    assert findings[0].source == tmp_path / "dvc.lock"
    assert "could not read dvc.lock" in findings[0].message


def test_check_reports_a_non_utf8_pointer_as_unreadable(tmp_path: Path) -> None:
    """A pointer saved as Latin-1 or UTF-16 is not something dvc wrote; the
    screen reads every DVC file as UTF-8 and must report, not traceback."""
    stage_dvc_project(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "final.dvc").write_bytes(b"outs:\n- path: final\n  md5: r\xe9vis\xe9\n")

    findings = check_project(tmp_path)

    assert [f.kind for f in findings] == ["dvc_state_unreadable"]
    assert "not UTF-8" in findings[0].message


def test_check_reports_a_conflicted_dvc_yaml_instead_of_calling_its_out_untracked(tmp_path: Path) -> None:
    """Two branches edit a stage and the merge leaves markers in dvc.yaml. The
    lock parser reads dvc.yaml for each stage's wdir and swallows the YAML
    error, so a pinned `../data/final/` under `wdir: code` would resolve
    against the root and read as untracked.

    Mutation: drop dvc.yaml from the loop in `_unreadable_dvc_files` -> the
    false "untracked" comes back and this reddens.
    """
    stage_dvc_project(tmp_path)
    write_lock(tmp_path, "validate", "../data/final/", files={"a.csv": "v-1"})
    (tmp_path / "dvc.yaml").write_text(
        "stages:\n  validate:\n    wdir: code\n<<<<<<< HEAD\n    cmd: run\n=======\n    cmd: run --all\n"
        ">>>>>>> theirs\n    outs:\n    - ../data/final/\n",
        encoding="utf-8",
    )

    findings = check_project(tmp_path)

    assert [(f.section, f.kind) for f in findings] == [("producer", "dvc_state_unreadable")]
    assert findings[0].source == tmp_path / "dvc.yaml"


def test_check_reports_an_impossible_date_in_dvc_yaml_instead_of_a_traceback(tmp_path: Path) -> None:
    """YAML reads an unquoted `2023-02-29` as a date and raises a bare
    ValueError, not a YAMLError; dvc rejects the file too. Still this
    project's defect to report by name, not a traceback.

    Mutation: narrow `load`'s `except Exception` in `_unreadable_dvc_files`
    to yaml's own errors -> the ValueError escapes and this reddens.
    """
    stage_dvc_project(tmp_path)
    write_pipeline(tmp_path, "data/final", files={"a.csv": "v-1"})
    dvc_yaml = tmp_path / "dvc.yaml"
    dvc_yaml.write_text(
        dvc_yaml.read_text(encoding="utf-8") + "    meta:\n      reviewed: 2023-02-29\n", encoding="utf-8"
    )

    findings = check_project(tmp_path)

    assert {f.kind for f in findings} == {"dvc_state_unreadable"}
    assert findings[0].source == tmp_path / "dvc.yaml"
    assert "day is out of range" in findings[0].message


def test_check_ignores_a_git_ref_named_like_a_pointer(tmp_path: Path) -> None:
    """git allows a branch or tag named `fix-final.dvc`; its ref and reflog
    are files under .git/ that dvc never reads. Handing them to the screen
    as unreadable pointers blocked check, publish and register on a healthy
    product.

    Mutation: stop pruning `.git` in `discover_all_outs` -> reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final", files={"a.csv": "v-1"})
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run([*git, "branch", "fix-final.dvc"], cwd=tmp_path, check=True)

    assert check_project(tmp_path) == []


def test_check_keeps_consumer_findings_when_an_unrelated_pointer_is_unreadable(tmp_path: Path) -> None:
    """A corrupt pointer outside data/imports/ stops the delivery gate, not
    the consumer section: the walk never reads it, so it runs and its
    findings stay -- `data import --bump` and `enclave bump` read them and
    would otherwise report "no consumer finding".

    Mutation: skip the consumer walk whenever an unreadable finding exists
    -> the consumer rows vanish and this reddens.
    """
    stage_dvc_project(tmp_path)
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "standalone_import.dvc")
    (tmp_path / "data" / "final.dvc").write_text("<<<<<<< HEAD\nouts: []\n=======\n", encoding="utf-8")

    findings = check_project(tmp_path)

    assert [f.kind for f in findings if f.section == "producer"] == ["dvc_state_unreadable"]
    assert any(f.section == "consumer" for f in findings)


def test_check_reports_a_conflicted_import_pointer_without_a_traceback(tmp_path: Path) -> None:
    """A merge-conflicted pointer under data/imports/ is the other file the
    consumer walk parses. Producer section names it; consumer section says
    the imports went unchecked; nothing escapes.

    Mutation: drop the `except` around the consumer walk -> yaml's
    ScannerError escapes and this reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final", is_dir=True, version_id="v-1")
    d = tmp_path / "data" / "imports" / "provider-xw"
    d.mkdir(parents=True)
    (d / "final.dvc").write_text("<<<<<<< HEAD\nouts: []\n=======\ndeps: []\n>>>>>>> theirs\n", encoding="utf-8")

    findings = check_project(tmp_path)

    assert [(f.section, f.kind) for f in findings] == [
        ("producer", "dvc_state_unreadable"),
        ("consumer", "dvc_state_unreadable"),
    ]
    assert findings[0].source == d / "final.dvc"


def test_check_keeps_a_healthy_import_beside_an_empty_one(tmp_path: Path) -> None:
    """The consumer walk tolerates a 0-byte import pointer (it is not an
    import) -- so the healthy sibling's consumer finding must survive, as it
    does on main. Predicting "will the walk crash?" from the file's path
    dropped it; reading the fact (did it crash?) does not.

    Mutation: skip the consumer walk whenever an unreadable finding exists
    -> the sibling's finding vanishes and this reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final", is_dir=True, version_id="v-1")
    _stage_dvc_fixture(tmp_path, "standalone_import.dvc", "data_b/final.dvc")
    (tmp_path / "data" / "imports" / "data_a").mkdir(parents=True)
    (tmp_path / "data" / "imports" / "data_a" / "final.dvc").write_text("", encoding="utf-8")

    findings = check_project(tmp_path)

    assert [f.kind for f in findings if f.section == "producer"] == ["dvc_state_unreadable"]
    consumer = [f for f in findings if f.section == "consumer"]
    assert len(consumer) == 1
    assert consumer[0].source == tmp_path / "data" / "imports" / "data_b" / "final.dvc"


def test_check_does_not_swallow_a_consumer_walk_failure_it_cannot_attribute(tmp_path: Path) -> None:
    """The `except` around the consumer walk swallows a failure only when the
    producer section has already named an unreadable DVC file. Anything else
    is a bug and must surface -- an incomplete `check` reported as complete
    is worse than a traceback. Here: an import pointer whose `repo:` is a
    string, which passes the shape screen and breaks the walk.

    Mutation: swallow unconditionally -> no exception, this reddens.
    """
    stage_dvc_project(tmp_path)
    write_pointer(tmp_path, "data/final", is_dir=True, version_id="v-1")
    d = tmp_path / "data" / "imports" / "provider-xw"
    d.mkdir(parents=True)
    (d / "final.dvc").write_text(
        "outs:\n- path: final\n  md5: " + "c" * 32 + "\n  size: 1\n"
        "deps:\n- path: outputs/final\n  repo: not-a-mapping\n",
        encoding="utf-8",
    )

    with pytest.raises(TypeError):
        check_project(tmp_path)
