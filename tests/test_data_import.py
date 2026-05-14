"""Tests for `import_product` orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mintd.catalog import CatalogNotFound, InMemoryCatalogClient
from mintd.data import (
    ImportDestinationExists,
    MissingPrimaryDataProduct,
    RevRequiresExplicitPath,
    import_product,
)
from mintd.model import Metadata

from tests._fakes.dvc_ops import _FakeDvcOps

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _register(
    client: InMemoryCatalogClient,
    name: str = "provider_xw",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    data = json.loads(MINIMAL.read_text())
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


def test_import_product_rev_without_path_rejected(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    with pytest.raises(RevRequiresExplicitPath):
        import_product(
            client, fake, "provider_xw", rev="abc123", dest_root=tmp_path
        )
    assert fake.calls == []


def test_import_product_rev_with_path_passes_through(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    import_product(
        client,
        fake,
        "provider_xw",
        path="outputs/x.csv",
        rev="abc123",
        dest_root=tmp_path,
    )

    assert fake.calls[0].rev == "abc123"


def test_import_product_missing_primary_raises(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client)  # no primary, no outputs
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
