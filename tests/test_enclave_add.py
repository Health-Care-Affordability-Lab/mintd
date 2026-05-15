"""Tests for `mintd.enclave.enclave_add` — slice 12 first-time-subscription."""

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from mintd.catalog import CatalogEntry, CatalogNotFound
from mintd.enclave import (
    AlreadyApproved,
    ApprovedProduct,
    EnclaveManifest,
    TransferredItem,
    enclave_add,
)
from mintd.model import Metadata
from mintd.producer import MissingPrimaryDataProduct, ProducerView

REPO_URL = "https://github.com/example-org/provider-xw"
PIN_SHA = "a" * 40
HEAD_SHA = "b" * 40

FULL_METADATA: dict[str, Any] = {
    "schema_version": "2.0",
    "mint": {"version": "1.0", "commit_hash": "x"},
    "project": {
        "type": "data",
        "name": "provider-xw",
        "full_name": "Provider XW",
        "created_at": "2026-05-14T00:00:00",
        "created_by": "user",
    },
    "metadata": {"description": "desc", "tags": ["topic:x"]},
    "ownership": {"team": "t", "maintainers": ["u"]},
    "access_control": {"teams": [{"name": "t", "permission": "read"}]},
    "governance": {"classification": "public", "contract_info": "p:x"},
    "repository": {
        "github_url": REPO_URL,
        "default_branch": "main",
        "visibility": "public",
        "mirror": {"url": "m", "purpose": "backup"},
    },
    "data_products": {"primary": "data/primary/"},
    "status": {
        "state": "active",
        "last_updated": "2026-05-14T00:00:00",
        "last_published_version": "1.0.0",
    },
}

NO_PRIMARY_METADATA: dict[str, Any] = {
    **FULL_METADATA,
    "data_products": {"primary": None},
}


class _Client:
    """Minimal CatalogClient supporting only fetch (the only method enclave_add uses)."""

    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}

    def register(self, name: str, github_url: str = REPO_URL) -> None:
        self._entries[name] = CatalogEntry.model_validate(
            {"project": {"name": name, "type": "data"}, "repository": {"github_url": github_url}}
        )

    def fetch(self, name: str) -> CatalogEntry:
        if name not in self._entries:
            raise CatalogNotFound(name)
        return self._entries[name]


def _factory_returning(view: ProducerView, sha: str = HEAD_SHA):
    def factory(repo_url: str) -> tuple[ProducerView, str]:
        return view, sha
    return factory


def _full_view() -> ProducerView:
    return ProducerView(repo=REPO_URL, pin=HEAD_SHA, metadata=Metadata.model_validate(FULL_METADATA))


def _no_primary_view() -> ProducerView:
    return ProducerView(
        repo=REPO_URL, pin=HEAD_SHA, metadata=Metadata.model_validate(NO_PRIMARY_METADATA)
    )


def test_add_creates_new_manifest(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"

    result = enclave_add(
        client,
        manifest_path=path,
        name="provider-xw",
        pin=PIN_SHA,
    )

    assert result == path
    manifest = EnclaveManifest.load(path)
    assert len(manifest.approved_products) == 1
    assert manifest.approved_products[0].repo == "provider-xw"
    assert manifest.approved_products[0].pin == PIN_SHA
    assert manifest.enclave_name == tmp_path.name


def test_add_appends_to_existing_manifest(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    client.register("other-repo")
    path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="test",
        approved_products=[ApprovedProduct(repo="other-repo", registry_entry="x", pin=PIN_SHA)],
    ).save(path)

    enclave_add(client, manifest_path=path, name="provider-xw", pin=HEAD_SHA)

    manifest = EnclaveManifest.load(path)
    repos = [ap.repo for ap in manifest.approved_products]
    assert repos == ["other-repo", "provider-xw"]


def test_add_duplicate_raises_already_approved(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"
    enclave_add(client, manifest_path=path, name="provider-xw", pin=PIN_SHA)

    with pytest.raises(AlreadyApproved) as ei:
        enclave_add(client, manifest_path=path, name="provider-xw", pin=HEAD_SHA)
    assert ei.value.name == "provider-xw"
    assert ei.value.manifest_path == path


def test_add_unknown_repo_raises_catalog_not_found(tmp_path: Path) -> None:
    client = _Client()
    path = tmp_path / "enclave_manifest.yaml"

    with pytest.raises(CatalogNotFound):
        enclave_add(client, manifest_path=path, name="ghost", pin=PIN_SHA)


def test_add_explicit_pin_used_verbatim(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"

    enclave_add(client, manifest_path=path, name="provider-xw", pin="abc123")

    assert EnclaveManifest.load(path).approved_products[0].pin == "abc123"


def test_add_no_pin_resolves_head(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"
    captured: list[str] = []

    def factory(repo_url: str) -> tuple[ProducerView, str]:
        captured.append(repo_url)
        return _full_view(), HEAD_SHA

    enclave_add(
        client,
        manifest_path=path,
        name="provider-xw",
        producer_view_factory=factory,
    )

    assert captured == [REPO_URL]
    assert EnclaveManifest.load(path).approved_products[0].pin == HEAD_SHA


def test_add_with_source_path(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"

    enclave_add(
        client,
        manifest_path=path,
        name="provider-xw",
        pin=PIN_SHA,
        source_path="outputs/x.parquet",
    )

    ap = EnclaveManifest.load(path).approved_products[0]
    assert ap.source_path == "outputs/x.parquet"
    assert ap.all is False


def test_add_preserves_transferred(tmp_path: Path) -> None:
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"
    transferred = [
        TransferredItem(
            repo="legacy-repo",
            contract_pin="c",
            artifact_pin="a",
            transfer_date=date(2026, 5, 14),
            transfer_id=f"t{i}",
            local_path=f"lp{i}",
        )
        for i in range(2)
    ]
    EnclaveManifest(enclave_name="test", transferred=transferred).save(path)

    enclave_add(client, manifest_path=path, name="provider-xw", pin=PIN_SHA)

    reloaded = EnclaveManifest.load(path)
    assert len(reloaded.transferred) == 2
    assert reloaded.transferred[0].transfer_id == "t0"
    assert reloaded.transferred[1].transfer_id == "t1"


def test_add_head_no_primary_no_overrides_raises(tmp_path: Path) -> None:
    """If HEAD's primary is None AND no --source-path / --all is given,
    `enclave_add` raises MissingPrimaryDataProduct (Decision #3α)."""
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"

    factory = _factory_returning(_no_primary_view())

    with pytest.raises(MissingPrimaryDataProduct):
        enclave_add(
            client,
            manifest_path=path,
            name="provider-xw",
            producer_view_factory=factory,
        )


def test_add_head_no_primary_with_source_path_succeeds(tmp_path: Path) -> None:
    """When --source-path is given, missing primary is fine — user chose
    the path explicitly."""
    client = _Client()
    client.register("provider-xw")
    path = tmp_path / "enclave_manifest.yaml"

    factory = _factory_returning(_no_primary_view())

    enclave_add(
        client,
        manifest_path=path,
        name="provider-xw",
        source_path="outputs/x.parquet",
        producer_view_factory=factory,
    )

    ap = EnclaveManifest.load(path).approved_products[0]
    assert ap.source_path == "outputs/x.parquet"
    assert ap.pin == HEAD_SHA
