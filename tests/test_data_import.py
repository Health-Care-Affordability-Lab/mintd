"""Tests for `import_product` orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mintd.catalog import CatalogNotFound, InMemoryCatalogClient
from mintd.data import (
    ImportDestinationExists,
    MissingPrimaryDataProduct,
    import_product,
)
from mintd.model import Metadata
from mintd.producer import FetchError, ProducerError, ProducerView

from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.producer import ErroringFetcher, StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _register(
    client: InMemoryCatalogClient,
    name: str = "provider_xw",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    data["repository"]["github_url"] = f"https://github.com/example-org/{name}"
    if mutate is not None:
        mutate(data)
    client.register(Metadata.model_validate(data))


def _with_primary(primary: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = primary

    return mutate


def _with_outputs(*paths: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"] = [
            {
                "path": p,
                "description": "",
                "primary": i == 0,
                "last_published": "",
            }
            for i, p in enumerate(paths)
        ]

    return mutate


def test_import_product_uses_primary_when_no_path(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", dest_root=tmp_path
    )

    assert len(produced) == 1
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.path == "outputs/main.parquet"
    assert call.repo_url == "https://github.com/example-org/provider_xw"
    assert call.dest == tmp_path / "main.parquet"


def test_import_product_path_override(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    import_product(
        client, fake, "provider_xw", path="outputs/other.csv", dest_root=tmp_path
    )

    assert fake.calls[0].path == "outputs/other.csv"
    assert fake.calls[0].dest == tmp_path / "other.csv"


def test_import_product_all_outputs_loops(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(
        client,
        mutate=_with_outputs("outputs/a.csv", "outputs/b.csv", "outputs/c.csv"),
    )
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", all_outputs=True, dest_root=tmp_path
    )

    assert len(produced) == 3
    assert [c.path for c in fake.calls] == [
        "outputs/a.csv",
        "outputs/b.csv",
        "outputs/c.csv",
    ]


def _producer_bytes(
    *,
    primary: str | None = "outputs/at_rev.parquet",
    outputs: list[dict[str, Any]] | None = None,
) -> bytes:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"]["primary"] = primary
    if outputs is not None:
        data["data_products"]["outputs"] = outputs
    return json.dumps(data).encode()


def test_import_product_rev_without_path_resolves_via_producer_view(
    tmp_path: Path,
) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/from_catalog.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = StaticFetcher(
        {(repo_url, "abc123"): _producer_bytes(primary="outputs/at_rev.parquet")}
    )

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    import_product(
        client,
        fake,
        "provider_xw",
        rev="abc123",
        dest_root=tmp_path,
        producer_view_factory=factory,
    )

    assert fake.calls[0].path == "outputs/at_rev.parquet"
    assert fake.calls[0].rev == "abc123"
    assert fake.calls[0].repo_url == repo_url


def test_import_product_propagates_producer_error(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = ErroringFetcher(FetchError.pin_missing(repo_url, "abc123"))

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(ProducerError) as ei:
        import_product(
            client,
            fake,
            "provider_xw",
            rev="abc123",
            dest_root=tmp_path,
            producer_view_factory=factory,
        )

    assert ei.value.reason == ProducerError.Reason.PIN_MISSING
    assert fake.calls == []


def test_import_product_rev_without_path_no_primary_raises(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = StaticFetcher({(repo_url, "abc123"): _producer_bytes(primary=None)})

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(MissingPrimaryDataProduct):
        import_product(
            client,
            fake,
            "provider_xw",
            rev="abc123",
            dest_root=tmp_path,
            producer_view_factory=factory,
        )


def test_import_product_default_factory_is_producer_view_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    captured: list[tuple[str, str]] = []

    def stub(repo: str, pin: str) -> Any:
        captured.append((repo, pin))
        return SimpleNamespace(primary_or_raise=lambda: "outputs/from_stub.parquet")

    monkeypatch.setattr("mintd.data.ProducerView.at", stub)

    import_product(client, fake, "provider_xw", rev="abc123", dest_root=tmp_path)

    assert captured == [("https://github.com/example-org/provider_xw", "abc123")]
    assert fake.calls[0].path == "outputs/from_stub.parquet"


def test_import_product_rev_with_path_passes_through(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    def factory_must_not_run(r: str, p: str) -> ProducerView:
        pytest.fail("factory must not be called when --path is provided")

    import_product(
        client,
        fake,
        "provider_xw",
        path="outputs/x.csv",
        rev="abc123",
        dest_root=tmp_path,
        producer_view_factory=factory_must_not_run,
    )

    assert fake.calls[0].rev == "abc123"


def test_import_product_missing_primary_raises(tmp_path: Path) -> None:
    # Slice 32 fixture switched to publish-valid (with primary); clear
    # it explicitly here so this test exercises the missing-primary path.
    def _clear_primary(d):
        d["data_products"]["primary"] = None
        d["data_products"]["outputs"] = []
    client = InMemoryCatalogClient()
    _register(client, mutate=_clear_primary)
    fake = _FakeDvcOps()

    with pytest.raises(MissingPrimaryDataProduct):
        import_product(client, fake, "provider_xw", dest_root=tmp_path)


def test_import_product_unknown_name_raises(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    fake = _FakeDvcOps()

    with pytest.raises(CatalogNotFound):
        import_product(client, fake, "nope", dest_root=tmp_path)


def test_import_product_returns_produced_dvc_files(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", dest_root=tmp_path
    )

    assert produced == [tmp_path / "main.parquet.dvc"]
    assert produced[0].exists()


def test_import_product_refuses_existing_dvc(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "main.parquet.dvc").write_text("preexisting")

    with pytest.raises(ImportDestinationExists):
        import_product(client, fake, "provider_xw", dest_root=tmp_path)
    assert fake.calls == []


def test_import_product_force_overwrites(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "main.parquet.dvc").write_text("preexisting")

    produced = import_product(
        client, fake, "provider_xw", dest_root=tmp_path, force=True
    )

    assert len(produced) == 1
    assert fake.calls[0].force is True


def test_import_product_trailing_slash_in_path(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client)
    fake = _FakeDvcOps()

    import_product(
        client,
        fake,
        "provider_xw",
        path="outputs/cms_based/",
        dest_root=tmp_path,
    )

    assert fake.calls[0].dest == tmp_path / "cms_based"
