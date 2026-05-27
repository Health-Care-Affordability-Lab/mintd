"""Tests for `bump_import` (slice 7) and `ProducerView.at_head`.

`bump_import` consumes slice-6 `_consumer_findings` and re-resolves the
producer's `data_products.primary` at HEAD. These tests exercise the
severity dispatch, the batch `check_findings` injection seam, the default
`ProducerView.at_head` factory wiring, and the `(view, sha)` return shape
of `at_head`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mintd.catalog import InMemoryCatalogClient
from mintd.check import CheckFinding
from mintd.data import (
    BumpBlocked,
    ImportNotFound,
    PrimaryRemovedAtHead,
    bump_import,
)
from mintd.model import DataProductOutput, DataProducts, Metadata
from mintd.producer import ProducerView

from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.producer import StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"
STANDALONE_IMPORT = FIXTURES / "dvc_files" / "standalone_import.dvc"

REPO_URL = "https://github.com/example-org/provider-xw"
PIN_SHA = "4f7c2a1abcd1234567890abcdef0123456789abc"  # matches fixture
HEAD_SHA = "b" * 40


def _stage_project(
    tmp_path: Path, *, dvc_filename: str = "cms_based.dvc"
) -> Path:
    """Lay out a project with metadata.json + one canonical .dvc import.

    Returns the path to the staged `.dvc` file.
    """
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    imports_dir = tmp_path / "data" / "imports"
    imports_dir.mkdir(parents=True, exist_ok=True)
    dvc_path = imports_dir / dvc_filename
    shutil.copy(STANDALONE_IMPORT, dvc_path)
    return dvc_path


def _view_with_primary(primary: str | None) -> ProducerView:
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
                        last_published="2023-01-01T00:00:00Z",
                    )
                ]
                if primary
                else [],
            )
        }
    )
    return ProducerView(repo=REPO_URL, pin=HEAD_SHA, metadata=meta)


def _drift_finding(source: Path) -> CheckFinding:
    return CheckFinding(
        severity="warning",
        section="consumer",
        message=(
            "upgrade available: producer now publishes "
            "'outputs/new.parquet' (you have 'cms_based')"
        ),
        source=source,
        kind="drift",
    )


def _up_to_date_finding(source: Path) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=source,
        kind="up_to_date",
    )


# ---------------------------------------------------------------------------
# Severity dispatch
# ---------------------------------------------------------------------------


def test_bump_up_to_date_returns_none(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_up_to_date_finding(dvc_path)],
    )

    assert result.changed is False
    assert result.dvc_path is None
    assert result.new_pin is None
    assert fake.calls == []


def test_bump_with_drift_rewrites_dvc_file(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def factory(repo: str) -> tuple[ProducerView, str]:
        assert repo == REPO_URL
        return _view_with_primary("outputs/new.parquet"), HEAD_SHA

    produced = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert produced.changed is True
    assert produced.new_pin == HEAD_SHA
    assert produced.dvc_path is not None
    assert produced.dvc_path.name == "new.parquet.dvc"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.path == "outputs/new.parquet"
    assert call.rev == HEAD_SHA
    assert call.force is True
    assert call.repo_url == REPO_URL
    assert call.dest == dvc_path.parent / "new.parquet"


def test_bump_name_not_imported_raises_import_not_found(tmp_path: Path) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    (tmp_path / "data" / "imports").mkdir(parents=True)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    with pytest.raises(ImportNotFound):
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[],
        )
    assert fake.calls == []


def test_bump_pin_missing_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message=f"producer pin missing: {PIN_SHA[:7]} not found in {REPO_URL}",
        source=dvc_path,
        kind="pin_missing",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert ei.value.name == "cms_based"
    assert fake.calls == []


def test_bump_unreachable_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="producer unreachable: git archive timed out",
        source=dvc_path,
        kind="unreachable",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []


def test_bump_schema_too_old_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message=(
            f"producer at pin {PIN_SHA[:7]} uses schema_version 1.5 (expected 2.0)"
        ),
        source=dvc_path,
        kind="schema_too_old",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []


def test_bump_metadata_invalid_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message=f"producer metadata invalid at pin {PIN_SHA[:7]}: validation error",
        source=dvc_path,
        kind="metadata_invalid",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding


def test_bump_head_primary_removed_raises_primary_removed_at_head(
    tmp_path: Path,
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary(None), HEAD_SHA

    with pytest.raises(PrimaryRemovedAtHead) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert ei.value.name == "cms_based"
    assert ei.value.repo == REPO_URL
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Injection seams
# ---------------------------------------------------------------------------


def test_bump_consumes_provided_check_findings_without_recomputing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def must_not_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("check_project must not be called when check_findings is provided")

    monkeypatch.setattr("mintd.data.check_project", must_not_call)

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_up_to_date_finding(dvc_path)],
    )

    assert result.changed is False


def test_bump_default_uses_check_project_when_no_findings_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    calls: list[tuple[Path, dict[str, Any]]] = []

    def recorder(path: Path, **kwargs: Any) -> list[CheckFinding]:
        calls.append((path, kwargs))
        return [_up_to_date_finding(dvc_path)]

    monkeypatch.setattr("mintd.data.check_project", recorder)

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
    )

    assert result.changed is False
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] == {"upgrades": True}


def test_bump_default_uses_producer_view_at_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    captured: list[str] = []

    def stub(repo: str) -> tuple[Any, str]:
        captured.append(repo)
        return (
            SimpleNamespace(primary_or_raise=lambda: "outputs/new.parquet"),
            HEAD_SHA,
        )

    monkeypatch.setattr("mintd.data.ProducerView.at_head", stub)

    bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_drift_finding(dvc_path)],
    )

    assert captured == [REPO_URL]
    assert fake.calls[0].rev == HEAD_SHA
    assert fake.calls[0].path == "outputs/new.parquet"


# ---------------------------------------------------------------------------
# ProducerView.at_head primitive
# ---------------------------------------------------------------------------


def _producer_bytes(*, primary: str | None = "outputs/main.parquet") -> bytes:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"]["primary"] = primary
    return json.dumps(data).encode()


def test_at_head_returns_resolved_sha_not_symbolic_ref(tmp_path: Path) -> None:
    resolved = "deadbeef" * 5  # 40-char hex
    fetcher = StaticFetcher(
        {},
        head_store={REPO_URL: (_producer_bytes(primary="outputs/x.parquet"), resolved)},
    )

    view, sha = ProducerView.at_head(
        REPO_URL, fetcher=fetcher, cache_dir=tmp_path / "cache"
    )

    assert sha == resolved
    assert view.pin == resolved
    assert view.pin != "HEAD"
    # Cache file lives under the resolved SHA, never under "HEAD".
    cache_root = tmp_path / "cache"
    cache_files = list(cache_root.rglob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].name == f"{resolved}.json"
    assert "HEAD" not in str(cache_files[0])
    assert fetcher.head_calls == [REPO_URL]


# ---------------------------------------------------------------------------
# Slice 9 — defensive `kind is None` arm
# ---------------------------------------------------------------------------


def test_bump_missing_kind_raises_bump_blocked(tmp_path: Path) -> None:
    """A consumer-section finding without `kind` is a regression contract
    violation post-slice-9; `bump_import` must raise `BumpBlocked` rather
    than silently dispatching as no-op."""
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="upgrade available: producer now publishes 'X' (you have 'Y')",
        source=dvc_path,
        # kind deliberately omitted (default None)
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []
