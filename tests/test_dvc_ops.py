"""Tests for `_FakeDvcOps` — protocol conformance + stub round-trip."""

from __future__ import annotations

from pathlib import Path

from mintd._dvc_ops import DvcOps
from mintd.imports import DataDependency

from tests._fakes.dvc_ops import _FakeDvcOps


def test_fake_satisfies_protocol() -> None:
    fake: DvcOps = _FakeDvcOps()
    assert callable(fake.import_)


def test_fake_records_call(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "cms_based"

    fake.import_(
        repo_url="https://github.com/example-org/provider-xw",
        path="outputs/cms_based/",
        dest=dest,
        rev="abc123",
        force=True,
    )

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.repo_url == "https://github.com/example-org/provider-xw"
    assert call.path == "outputs/cms_based/"
    assert call.dest == dest
    assert call.rev == "abc123"
    assert call.force is True


def test_fake_writes_parseable_stub(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "cms_based"

    produced = fake.import_(
        repo_url="https://github.com/example-org/provider-xw",
        path="outputs/cms_based/",
        dest=dest,
    )

    assert produced == tmp_path / "cms_based.dvc"
    assert produced.exists()

    dep = DataDependency.from_dvc_file(produced)
    assert dep.producer_repo == "https://github.com/example-org/provider-xw"
    assert dep.output_path == "outputs/cms_based/"
    assert dep.local_path == "cms_based"


def test_fake_handles_file_paths_with_suffix(tmp_path: Path) -> None:
    fake = _FakeDvcOps()
    dest = tmp_path / "main.parquet"

    produced = fake.import_(
        repo_url="https://github.com/example-org/p",
        path="outputs/main.parquet",
        dest=dest,
    )

    # Real `dvc import` writes <dest>.dvc, not <stem>.dvc.
    assert produced == tmp_path / "main.parquet.dvc"
