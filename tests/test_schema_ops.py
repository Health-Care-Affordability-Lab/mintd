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

from mintd import cli
from mintd.schema_ops import (
    SUPPORTED_EXTENSIONS,
    ColumnNamesLookLikeData,
    SchemaExtraNotInstalled,
    extract_stata_metadata,
    find_project_root,
    generate_schema_file,
    infer_table_schema,
    parse_published_schema,
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
    # Committed and shown in the catalog: no row count (a value).
    assert set(file_entry) == {"filename", "path", "columns", "schema"}
    assert file_entry["columns"] == 2


def test_generate_schema_file_lists_files_in_the_same_order_on_every_os(tmp_path: Path) -> None:
    """The output is committed, so its file order must not depend on the OS:
    files sort by their relative path's characters (`B` before `a`), not by
    the platform's path comparison, which is case-insensitive on Windows.

    Mutation: sort the Path objects themselves -> red on Windows.
    """
    data_dir = tmp_path / "final"
    data_dir.mkdir()
    _write_csv(data_dir / "a.csv")
    _write_csv(data_dir / "B.csv")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out)

    assert [f["filename"] for f in json.loads(out.read_text())["files"]] == ["B.csv", "a.csv"]


@pytest.mark.parametrize(("name", "content", "rule"), [
    ("claims.csv", "06510,12.5\n06511,3.0\n", "have numeric names"),  # a CSV without its header row
    ("people.json", '{"10001": {"age": 3}, "10002": {"age": 5}}', "have numeric names"),  # keyed by IDs
    ("flags.csv", "M,CT\nM,NY\n", "match their own first-row value"),
    ("ingested.csv", "new_york,ct\nNew York,NY\n", "match their own first-row value"),  # headers renamed by ingest
])
def test_generate_schema_file_refuses_column_names_that_look_like_data(
    tmp_path: Path, name: str, content: str, rule: str
) -> None:
    """The output is committed: names with a digit and no letter, or equal to
    their own column's first value, are refused and nothing is written.
    Mutation: drop the check, or either half of it -> red."""
    data_dir = tmp_path / "final"
    data_dir.mkdir()
    (data_dir / name).write_text(content, encoding="utf-8")
    out = tmp_path / "schema.json"

    with pytest.raises(ColumnNamesLookLikeData, match=rule):
        generate_schema_file(data_dir, out)
    assert not out.exists()


@pytest.mark.parametrize("content", [
    "origin,CT,NY\nCT,0,5\n", "payer,medicare,medicaid\nmedicare,1,0\n",
    "category,total\ntotal,100\n", "N,mean,flag\n10,1.5,N\n",
])
def test_generate_schema_file_accepts_a_name_equal_to_another_columns_first_value(
    tmp_path: Path, content: str
) -> None:
    """Each name is compared with its own column's first value only.
    Mutation: compare with any first-row value -> red."""
    data_dir = tmp_path / "final"
    data_dir.mkdir()
    (data_dir / "x.csv").write_text(content, encoding="utf-8")
    out = tmp_path / "schema.json"

    generate_schema_file(data_dir, out)

    fields = json.loads(out.read_text())["files"][0]["schema"]["fields"]
    assert [f["name"] for f in fields] == content.split("\n")[0].split(",")


def test_schema_generate_cli_refuses_numeric_names_unless_allowed(tmp_path: Path, capsys) -> None:
    """The refusal names its own fix, with no generic format hint after it;
    `--allow-numeric-column-names` (committed in dvc.yaml's cmd) lets real
    numeric names through. Mutations: drop the flag's wiring, or let the
    generic ValueError handler catch the refusal -> red."""
    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "x.csv").write_text("fips,2019,2020\n09001,1,2\n", encoding="utf-8")
    argv = ["data", "schema", "generate", "--project-dir", str(tmp_path), "--data-dir", str(tmp_path / "final")]

    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "columns 2, 3 have numeric names" in err and "--allow-numeric-column-names" in err, err
    assert "hint" not in err
    assert cli.main([*argv, "--allow-numeric-column-names"]) == 0
    assert (tmp_path / "schemas" / "v1" / "schema.json").is_file()


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


def test_cli_hint_for_the_missing_extra_is_the_release_install_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hint once offered `uv tool install --force 'mintd[schema]'`, which
    installs whatever PyPI calls mintd. It now gives the release's install line."""
    import sys

    from mintd import cli

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "pandas":
            raise ImportError("simulated: pandas not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "pandas", raising=False)
    monkeypatch.setenv("COLUMNS", "80")  # narrower than the line

    rc = cli.main(["data", "schema", "generate", "--project-dir", str(tmp_path), "--data-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    # Whole, on one line, so copying it from the terminal gives a working command.
    assert cli._install_line(with_schema=True) in err
    assert "uv tool install" not in err


@pytest.mark.parametrize("windows", [False, True])
def test_the_reinstall_hint_names_the_shell_on_windows(windows: bool) -> None:
    from mintd import cli

    hint = cli._reinstall_hint(windows=windows)
    assert hint.endswith(cli._install_line(with_schema=True, windows=windows))
    assert ("in PowerShell" in hint) is windows


# ---------------- parse_published_schema ----------------

WRAPPER_3FILE = {
    "generator": "mintd",
    "schema_standard": "frictionless-table-schema",
    "files": [
        {
            "filename": "hospital.csv",
            "path": "hospital.csv",
            "observations": 1200,
            "columns": 2,
            "schema": {
                "fields": [
                    {"name": "id", "type": "integer", "title": "Hospital ID"},
                    {
                        "name": "name",
                        "type": "string",
                        "description": "Facility name",
                        "constraints": {"required": True},
                    },
                ]
            },
        },
        {
            "filename": "measures.csv",
            "path": "measures.csv",
            "observations": 50000,
            "columns": 1,
            "schema": {"fields": [{"name": "rate", "type": "number"}]},
        },
        {
            "filename": "year.parquet",
            "path": "year.parquet",
            "observations": None,
            "columns": 1,
            "schema": {"fields": [{"name": "yr", "type": "integer"}]},
        },
    ],
}


def test_parse_published_schema_wrapper_one_table_per_file() -> None:
    tables = parse_published_schema(json.dumps(WRAPPER_3FILE).encode())

    assert [t["filename"] for t in tables] == [
        "hospital.csv",
        "measures.csv",
        "year.parquet",
    ]
    assert tables[0]["observations"] == 1200
    assert tables[0]["columns"] == 2
    assert tables[0]["fields"][0] == {
        "name": "id",
        "type": "integer",
        "description": "Hospital ID",  # falls back to title when no description
        "required": False,
    }
    assert tables[0]["fields"][1] == {
        "name": "name",
        "type": "string",
        "description": "Facility name",
        "required": True,
    }
    assert tables[2]["observations"] is None


def test_parse_published_schema_round_trips_generate_schema_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _write_csv(data_dir / "first.csv", rows=5)
    _write_csv(data_dir / "second.csv", rows=2)
    out = tmp_path / "schema.json"
    generate_schema_file(data_dir, out, recursive=True)

    tables = parse_published_schema(out.read_bytes())

    assert {t["filename"] for t in tables} == {"first.csv", "second.csv"}
    first = next(t for t in tables if t["filename"] == "first.csv")
    assert first["observations"] is None  # the writer no longer records row counts
    assert [f["name"] for f in first["fields"]] == ["a", "b"]
    assert all(set(f) == {"name", "type", "description", "required"} for f in first["fields"])


def test_parse_published_schema_bare_frictionless() -> None:
    doc = {
        "fields": [
            {"name": "a", "type": "integer"},
            {"name": "b", "type": "string", "constraints": {"required": True}},
        ]
    }

    tables = parse_published_schema(json.dumps(doc).encode())

    assert len(tables) == 1
    assert tables[0]["filename"] == ""
    assert tables[0]["observations"] is None
    assert tables[0]["columns"] == 2
    assert tables[0]["fields"][1]["required"] is True


def test_parse_published_schema_json_schema() -> None:
    doc = {
        "properties": {
            "a": {"type": "integer", "description": "count"},
            "b": {"type": ["string", "null"]},
        },
        "required": ["a"],
    }

    tables = parse_published_schema(json.dumps(doc).encode())

    assert len(tables) == 1
    fields = tables[0]["fields"]
    assert fields[0] == {"name": "a", "type": "integer", "description": "count", "required": True}
    assert fields[1] == {"name": "b", "type": "string/null", "description": "", "required": False}


def test_parse_published_schema_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_published_schema(b"{not json")


def test_parse_published_schema_non_object_raises() -> None:
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_published_schema(b"[1, 2, 3]")


def test_parse_published_schema_unrecognized_shape_returns_empty() -> None:
    assert parse_published_schema(b'{"unrelated": "doc"}') == []
