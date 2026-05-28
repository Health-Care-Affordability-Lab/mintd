"""Tests for `mintd.schema_ops` — Frictionless schema generation.

Mirrors v1's `tests/test_cli_schema.py` adapted to v2's pandas-via-extras
model. The unit tests assume pandas is installed (the dev env includes
the `[schema]` extra via `mintd[schema]` self-reference); the
extra-not-installed path is tested by mocking the ImportError.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pandas as pd
import pytest

from mintd.schema_ops import (
    SUPPORTED_EXTENSIONS,
    SchemaExtraNotInstalled,
    extract_stata_metadata,
    find_project_root,
    generate_schema_file,
    infer_table_schema,
)


# ---------------- helpers ----------------

def _write_csv(p: Path, rows: int = 3) -> None:
    pd.DataFrame({"a": list(range(rows)), "b": [f"v{i}" for i in range(rows)]}).to_csv(
        p, index=False
    )


def _write_json(p: Path, rows: int = 3) -> None:
    pd.DataFrame({"a": list(range(rows)), "b": [0.1 * i for i in range(rows)]}).to_json(p)


def _write_parquet(p: Path, rows: int = 3) -> None:
    pd.DataFrame({"a": list(range(rows)), "b": [f"v{i}" for i in range(rows)]}).to_parquet(p)


def _write_dta(p: Path, with_labels: bool = False) -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "grp": [1, 2, 1]})
    if with_labels:
        df.to_stata(
            p,
            write_index=False,
            variable_labels={"id": "Subject ID", "grp": "Treatment Group"},
            value_labels={"grp": {1: "Control", 2: "Treated"}},
        )
    else:
        df.to_stata(p, write_index=False)


# ---------------- supported format dispatch ----------------

def test_supported_extensions_are_dta_csv_json_parquet() -> None:
    assert SUPPORTED_EXTENSIONS == {".csv", ".dta", ".json", ".parquet"}


@pytest.mark.parametrize("writer,ext", [
    (_write_csv, ".csv"),
    (_write_json, ".json"),
    (_write_parquet, ".parquet"),
    (_write_dta, ".dta"),
])
def test_generate_schema_file_supports_format(tmp_path: Path, writer, ext: str) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    writer(data_dir / f"x{ext}")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out, recursive=True)

    payload = json.loads(out.read_text())
    assert payload["generator"] == "mintd"
    assert payload["schema_standard"] == "frictionless-table-schema"
    assert len(payload["files"]) == 1
    file_entry = payload["files"][0]
    assert file_entry["filename"] == f"x{ext}"
    assert file_entry["observations"] == 3
    assert file_entry["columns"] == 2


# ---------------- combined output across formats ----------------

def test_generate_schema_file_emits_combined_file_across_formats(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_csv(data_dir / "a.csv")
    _write_parquet(data_dir / "b.parquet")
    _write_dta(data_dir / "c.dta")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out, recursive=True)

    payload = json.loads(out.read_text())
    names = sorted(f["filename"] for f in payload["files"])
    assert names == ["a.csv", "b.parquet", "c.dta"]


# ---------------- recursive vs non-recursive ----------------

def test_recursive_walks_subdirs(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "nested").mkdir(parents=True)
    _write_csv(data_dir / "top.csv")
    _write_csv(data_dir / "nested" / "deep.csv")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out, recursive=True)

    payload = json.loads(out.read_text())
    paths = sorted(f["path"] for f in payload["files"])
    assert paths == ["nested/deep.csv", "top.csv"]


def test_no_recursive_skips_subdirs(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "nested").mkdir(parents=True)
    _write_csv(data_dir / "top.csv")
    _write_csv(data_dir / "nested" / "deep.csv")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out, recursive=False)

    payload = json.loads(out.read_text())
    paths = sorted(f["path"] for f in payload["files"])
    assert paths == ["top.csv"]


# ---------------- Stata richness ----------------

def test_dta_with_labels_populates_title_and_categories(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_dta(data_dir / "labeled.dta", with_labels=True)
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out, recursive=True)

    fields = json.loads(out.read_text())["files"][0]["schema"]["fields"]
    by_name = {f["name"]: f for f in fields}
    assert by_name["id"]["title"] == "Subject ID"
    assert by_name["grp"]["title"] == "Treatment Group"
    assert by_name["grp"]["categories"] == [
        {"value": 1, "label": "Control"},
        {"value": 2, "label": "Treated"},
    ]


def test_extract_stata_metadata_returns_empty_for_unlabeled(tmp_path: Path) -> None:
    p = tmp_path / "plain.dta"
    _write_dta(p, with_labels=False)
    meta = extract_stata_metadata(p)
    for col_meta in meta.values():
        assert col_meta["label"] is None
        assert "categories" not in col_meta


# ---------------- byte-determinism ----------------

def test_two_runs_produce_byte_identical_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_dta(data_dir / "x.dta", with_labels=True)
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"

    generate_schema_file(data_dir, out_a, recursive=True)
    generate_schema_file(data_dir, out_b, recursive=True)

    assert out_a.read_bytes() == out_b.read_bytes()


# ---------------- error paths ----------------

def test_empty_data_dir_raises_filenotfound(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="No supported data files"):
        generate_schema_file(empty, tmp_path / "schema.json", recursive=True)


def test_unsupported_format_in_infer_raises_valueerror(tmp_path: Path) -> None:
    p = tmp_path / "x.xlsx"
    p.write_bytes(b"")
    with pytest.raises(ValueError, match="Unsupported file format"):
        infer_table_schema(p)


def test_find_project_root_raises_when_no_metadata(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="metadata.json"):
        find_project_root(tmp_path)


def test_find_project_root_walks_up(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text("{}")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path.resolve()


# ---------------- missing-extra path ----------------

def test_raises_schema_extra_not_installed_when_pandas_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When pandas can't be imported, _lazy_pandas() raises
    SchemaExtraNotInstalled. CLI translates that to the reinstall hint."""
    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "pandas":
            raise ImportError("simulated: pandas not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Need to force re-import inside the function (since pandas may already be cached).
    import sys
    sys.modules.pop("pandas", None)

    with pytest.raises(SchemaExtraNotInstalled, match=r"\[schema\] extra"):
        generate_schema_file(tmp_path, tmp_path / "schema.json", recursive=True)
