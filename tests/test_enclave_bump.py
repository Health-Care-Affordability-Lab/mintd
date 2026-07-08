from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mintd.catalog import CatalogClient, CatalogEntry
from mintd.check import CheckFinding
from mintd.data import BumpBlocked, ImportNotFound, PrimaryRemovedAtHead
from mintd.enclave import ApprovedProduct, EnclaveManifest, TransferredItem, enclave_bump
from mintd.model import Metadata
from mintd.producer import ProducerView

REPO_URL = "https://github.com/example-org/provider-xw"
PIN_SHA = "a" * 40
HEAD_SHA = "b" * 40

# A fully compliant minimal Metadata for the view
FULL_METADATA = {
    "schema_version": "2.0",
    "mint": {"version": "1.0", "commit_hash": "x"},
    "project": {"type": "data", "name": "provider-xw", "full_name": "Provider XW", "created_at": "2026-05-14T00:00:00", "created_by": "user"},
    "metadata": {"description": "desc", "tags": ["topic:x"]},
    "ownership": {"team": "t", "maintainers": ["u"]},
    "access_control": {"teams": [{"name": "t", "permission": "read"}]},
    "governance": {"classification": "public", "contract_info": "p:x"},
    "repository": {"github_url": REPO_URL, "default_branch": "main", "visibility": "public", "mirror": {"url": "m", "purpose": "backup"}},
    "data_products": {"primary": "data/primary/"},
    "status": {"state": "active", "last_updated": "2026-05-14T00:00:00", "last_published_version": "1.0.0"}
}

class InMemoryCatalogClient(CatalogClient):
    def __init__(self):
        self._entries = {}
    def register(self, name, entry):
        self._entries[name] = entry
    def fetch(self, name):
        return self._entries[name]

def _make_manifest(tmp_path: Path, **kwargs) -> Path:
    m = EnclaveManifest(
        enclave_name="test",
        approved_products=[ApprovedProduct(repo="provider-xw", registry_entry="cat", pin=PIN_SHA)],
        **kwargs
    )
    p = tmp_path / "enclave_manifest.yaml"
    m.save(p)
    return p

def test_bump_up_to_date_returns_none(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    
    finding = CheckFinding(severity="info", section="consumer", message="up to date", source=p, field_path="approved_products[provider-xw]", kind="up_to_date")
    result = enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])
    assert result is None

def test_bump_with_drift_rewrites_manifest(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    
    def factory(url):
        view = ProducerView(repo="provider-xw", pin=HEAD_SHA, metadata=Metadata.model_validate(FULL_METADATA))
        return view, HEAD_SHA
        
    finding = CheckFinding(severity="warning", section="consumer", message="upgrade available: ...", source=p, field_path="approved_products[provider-xw]", kind="drift")
    enclave_bump(client, manifest_path=p, name="provider-xw", producer_view_factory=factory, check_findings=[finding])

    assert EnclaveManifest.load(p).approved_products[0].pin == HEAD_SHA

def test_bump_name_not_imported_raises_import_not_found(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    with pytest.raises(ImportNotFound):
        enclave_bump(client, manifest_path=p, name="unknown")

def test_bump_pin_missing_raises_bump_blocked(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    finding = CheckFinding(severity="error", section="consumer", message="producer pin missing: ...", source=p, field_path="approved_products[provider-xw]", kind="pin_missing")
    with pytest.raises(BumpBlocked):
        enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])

def test_bump_unreachable_raises_bump_blocked(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    finding = CheckFinding(severity="warning", section="consumer", message="producer unreachable: ...", source=p, field_path="approved_products[provider-xw]", kind="unreachable")
    with pytest.raises(BumpBlocked):
        enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])

def test_bump_head_primary_removed_raises_primary_removed_at_head(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    
    broken_meta = FULL_METADATA.copy()
    broken_meta["data_products"] = {}
    
    def broken_factory(url):
        view = ProducerView(repo="p", pin=HEAD_SHA, metadata=Metadata.model_validate(broken_meta))
        return view, HEAD_SHA
        
    finding = CheckFinding(severity="warning", section="consumer", message="upgrade available: ...", source=p, field_path="approved_products[provider-xw]", kind="drift")
    with pytest.raises(PrimaryRemovedAtHead):
        enclave_bump(client, manifest_path=p, name="provider-xw", producer_view_factory=broken_factory, check_findings=[finding])

def test_bump_writes_pin_through_append_only_save(tmp_path):
    p = _make_manifest(tmp_path, transferred=[
        TransferredItem(repo="p", contract_pin="c", artifact_pin="a", transfer_date=date(2026, 5, 14), transfer_id="t1", local_path="lp")
    ])
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    
    def factory(url):
        view = ProducerView(repo="provider-xw", pin=HEAD_SHA, metadata=Metadata.model_validate(FULL_METADATA))
        return view, HEAD_SHA
        
    finding = CheckFinding(severity="warning", section="consumer", message="upgrade available: ...", source=p, field_path="approved_products[provider-xw]", kind="drift")
    enclave_bump(client, manifest_path=p, name="provider-xw", producer_view_factory=factory, check_findings=[finding])

    m = EnclaveManifest.load(p)
    assert m.approved_products[0].pin == HEAD_SHA
    assert len(m.transferred) == 1
    assert m.transferred[0].transfer_id == "t1"


def test_bump_schema_too_old_raises_bump_blocked(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message=f"producer at pin {PIN_SHA[:7]} uses schema_version 1.5 (expected 2.0)",
        source=p,
        field_path="approved_products[provider-xw]",
        kind="schema_too_old",
    )
    with pytest.raises(BumpBlocked) as ei:
        enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])
    assert ei.value.finding is finding


def test_bump_metadata_invalid_raises_bump_blocked(tmp_path):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message=f"producer metadata invalid at pin {PIN_SHA[:7]}: validation error",
        source=p,
        field_path="approved_products[provider-xw]",
        kind="metadata_invalid",
    )
    with pytest.raises(BumpBlocked) as ei:
        enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])
    assert ei.value.finding is finding


def test_bump_consumes_provided_check_findings_without_recomputing(
    tmp_path, monkeypatch
):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()

    def must_not_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("check_project must not be called when check_findings is provided")

    monkeypatch.setattr("mintd.enclave.check_project", must_not_call, raising=False)

    finding = CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=p,
        field_path="approved_products[provider-xw]",
        kind="up_to_date",
    )
    result = enclave_bump(
        client, manifest_path=p, name="provider-xw", check_findings=[finding]
    )
    assert result is None


def test_bump_default_uses_check_project_when_no_findings_passed(
    tmp_path, monkeypatch
):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    calls: list[tuple[Path, dict[str, Any]]] = []

    finding = CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=p,
        field_path="approved_products[provider-xw]",
        kind="up_to_date",
    )

    def recorder(path: Path, **kwargs: Any) -> list[CheckFinding]:
        calls.append((path, kwargs))
        return [finding]

    monkeypatch.setattr("mintd.check.check_project", recorder)

    result = enclave_bump(client, manifest_path=p, name="provider-xw")

    assert result is None
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] == {"upgrades": True, "client": client}


def test_bump_default_uses_producer_view_at_head(tmp_path, monkeypatch):
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register(
        "provider-xw",
        CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}),
    )
    captured: list[str] = []

    def stub(repo: str) -> tuple[Any, str]:
        captured.append(repo)
        return SimpleNamespace(primary_or_raise=lambda: "outputs/new.parquet"), HEAD_SHA

    monkeypatch.setattr("mintd.enclave.ProducerView.at_head", stub)

    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="upgrade available: producer now publishes 'X'",
        source=p,
        field_path="approved_products[provider-xw]",
        kind="drift",
    )
    enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])

    assert captured == [REPO_URL]
    assert EnclaveManifest.load(p).approved_products[0].pin == HEAD_SHA


def _must_not_call_check_project(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("check_project must not be called on the --force path")


def test_bump_force_repins_to_head_skipping_finding_gate(tmp_path, monkeypatch):
    """`--force` bypasses check_project entirely and repins to HEAD."""
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    monkeypatch.setattr("mintd.check.check_project", _must_not_call_check_project)

    def factory(url):
        view = ProducerView(repo="provider-xw", pin=HEAD_SHA, metadata=Metadata.model_validate(FULL_METADATA))
        return view, HEAD_SHA

    result = enclave_bump(
        client, manifest_path=p, name="provider-xw", force=True, producer_view_factory=factory
    )

    assert result == p
    assert EnclaveManifest.load(p).approved_products[0].pin == HEAD_SHA


def test_bump_force_already_at_head_is_noop(tmp_path, monkeypatch):
    """When the pin already equals HEAD, `--force` is a no-op (returns None)."""
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    monkeypatch.setattr("mintd.check.check_project", _must_not_call_check_project)

    def factory(url):
        # HEAD == the manifest's current pin (PIN_SHA).
        view = ProducerView(repo="provider-xw", pin=PIN_SHA, metadata=Metadata.model_validate(FULL_METADATA))
        return view, PIN_SHA

    result = enclave_bump(
        client, manifest_path=p, name="provider-xw", force=True, producer_view_factory=factory
    )

    assert result is None
    assert EnclaveManifest.load(p).approved_products[0].pin == PIN_SHA


def test_bump_force_primary_removed_raises(tmp_path, monkeypatch):
    """`--force` still validates the primary at HEAD."""
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    monkeypatch.setattr("mintd.check.check_project", _must_not_call_check_project)

    broken_meta = FULL_METADATA.copy()
    broken_meta["data_products"] = {}

    def broken_factory(url):
        view = ProducerView(repo="p", pin=HEAD_SHA, metadata=Metadata.model_validate(broken_meta))
        return view, HEAD_SHA

    with pytest.raises(PrimaryRemovedAtHead):
        enclave_bump(
            client, manifest_path=p, name="provider-xw", force=True, producer_view_factory=broken_factory
        )


def test_bump_force_validates_primary_even_when_already_at_head(tmp_path, monkeypatch):
    """Primary validation runs BEFORE the pin==HEAD no-op check: an
    already-at-HEAD product whose primary was removed at HEAD raises
    PrimaryRemovedAtHead rather than silently no-op'ing."""
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    monkeypatch.setattr("mintd.check.check_project", _must_not_call_check_project)

    broken_meta = FULL_METADATA.copy()
    broken_meta["data_products"] = {}

    def broken_factory(url):
        # HEAD == the manifest's current pin (PIN_SHA), but primary removed.
        view = ProducerView(repo="p", pin=PIN_SHA, metadata=Metadata.model_validate(broken_meta))
        return view, PIN_SHA

    with pytest.raises(PrimaryRemovedAtHead):
        enclave_bump(
            client, manifest_path=p, name="provider-xw", force=True, producer_view_factory=broken_factory
        )


def test_bump_force_source_path_subscription_skips_primary_validation(tmp_path, monkeypatch):
    """A source_path/all subscription does not depend on data_products.primary,
    so `--force` must repin it even when the producer has no primary at HEAD —
    validating primary would wrongly block the repin."""
    m = EnclaveManifest(
        enclave_name="test",
        approved_products=[
            ApprovedProduct(repo="provider-xw", registry_entry="cat", pin=PIN_SHA, source_path="data/custom/")
        ],
    )
    p = tmp_path / "enclave_manifest.yaml"
    m.save(p)
    client = InMemoryCatalogClient()
    client.register("provider-xw", CatalogEntry.model_validate({"repository": {"github_url": REPO_URL}}))
    monkeypatch.setattr("mintd.check.check_project", _must_not_call_check_project)

    primary_less = FULL_METADATA.copy()
    primary_less["data_products"] = {}

    def factory(url):
        view = ProducerView(repo="provider-xw", pin=HEAD_SHA, metadata=Metadata.model_validate(primary_less))
        return view, HEAD_SHA

    result = enclave_bump(
        client, manifest_path=p, name="provider-xw", force=True, producer_view_factory=factory
    )

    assert result == p
    assert EnclaveManifest.load(p).approved_products[0].pin == HEAD_SHA


def test_enclave_bump_missing_kind_raises_bump_blocked(tmp_path):
    """A consumer-section finding without `kind` is a regression contract
    violation post-slice-9; `enclave_bump` must raise `BumpBlocked` rather
    than silently dispatching."""
    p = _make_manifest(tmp_path)
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="upgrade available: ...",
        source=p,
        field_path="approved_products[provider-xw]",
        # kind deliberately omitted (default None)
    )

    with pytest.raises(BumpBlocked) as ei:
        enclave_bump(client, manifest_path=p, name="provider-xw", check_findings=[finding])

    assert ei.value.finding is finding
