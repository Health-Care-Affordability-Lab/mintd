"""Tests for `DataDependency` + `scan_imports`."""

from __future__ import annotations

from pathlib import Path

import pytest

from mintd.imports import DataDependency, NotAnImportError, scan_imports

FIXTURES = Path(__file__).parent / "fixtures" / "dvc_files"


def test_from_dvc_file_parses_standalone_import() -> None:
    dep = DataDependency.from_dvc_file(FIXTURES / "standalone_import.dvc")

    assert dep.kind == "dvc_file"
    assert dep.producer_repo == "https://github.com/example-org/provider-xw"
    assert dep.contract_pin == "4f7c2a1abcd1234567890abcdef0123456789abc"
    assert dep.output_path == "outputs/cms_based/"
    assert dep.local_path == "cms_based"
    assert dep.artifact_md5 == "e8f3a2b1c4d5e6f7a8b9c0d1e2f3a4b5"
    assert dep.stage_name is None


def test_from_dvc_file_skips_non_import() -> None:
    with pytest.raises(NotAnImportError):
        DataDependency.from_dvc_file(FIXTURES / "dvc_add_only.dvc")


def test_from_dvc_lock_yields_per_repo_dep() -> None:
    lock_path = FIXTURES / "dvc.lock"
    import yaml

    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))

    ingest_deps = DataDependency.from_dvc_lock_stage(
        "ingest_external", lock["stages"]["ingest_external"], lock_path
    )
    local_deps = DataDependency.from_dvc_lock_stage(
        "local_only", lock["stages"]["local_only"], lock_path
    )

    assert len(ingest_deps) == 1
    assert len(local_deps) == 0

    dep = ingest_deps[0]
    assert dep.kind == "dvc_lock_stage"
    assert dep.producer_repo == "https://github.com/example-org/provider-yy"
    assert dep.contract_pin == "aaaabbbbccccddddeeeeffff0011223344556677"
    assert dep.local_path == "data/imports/staging/"
    assert dep.output_path == ""
    assert dep.stage_name == "ingest_external"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_imports_walks_both_sources(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "alpha.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )
    _write(tmp_path / "dvc.lock", (FIXTURES / "dvc.lock").read_text(encoding="utf-8"))

    deps = scan_imports(tmp_path)

    assert len(deps) == 2
    kinds = {d.kind for d in deps}
    assert kinds == {"dvc_file", "dvc_lock_stage"}


def test_scan_imports_dedup_dvc_file_wins(tmp_path: Path) -> None:
    # `.dvc` file: producer-xw, local_path "cms_based",
    # pin 4f7c2a1abcd1234567890abcdef0123456789abc.
    _write(
        tmp_path / "data" / "imports" / "cms.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )
    # dvc.lock stage referencing the same triple.
    _write(
        tmp_path / "dvc.lock",
        "schema: '2.0'\n"
        "stages:\n"
        "  ingest:\n"
        "    cmd: true\n"
        "    deps:\n"
        "      - path: cms_based\n"
        "        repo:\n"
        "          url: https://github.com/example-org/provider-xw\n"
        "          rev_lock: 4f7c2a1abcd1234567890abcdef0123456789abc\n",
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_scan_imports_handles_missing_dvc_lock(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "alpha.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_scan_imports_handles_no_imports(tmp_path: Path) -> None:
    assert scan_imports(tmp_path) == []


def test_scan_imports_skips_dvc_add_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "data" / "imports" / "produced.dvc",
        (FIXTURES / "dvc_add_only.dvc").read_text(encoding="utf-8"),
    )
    _write(
        tmp_path / "data" / "imports" / "real.dvc",
        (FIXTURES / "standalone_import.dvc").read_text(encoding="utf-8"),
    )

    deps = scan_imports(tmp_path)

    assert len(deps) == 1
    assert deps[0].kind == "dvc_file"


def test_data_dependency_is_frozen() -> None:
    dep = DataDependency.from_dvc_file(FIXTURES / "standalone_import.dvc")
    with pytest.raises(Exception):
        dep.producer_repo = "mutated"  # type: ignore[misc]
