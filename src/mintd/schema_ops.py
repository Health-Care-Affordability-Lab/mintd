"""Frictionless Table Schema generation for `mintd data schema generate`.

Algorithm ported from v1 (`mintd/src/mintd/utils/schema.py`). pandas +
pyarrow are loaded lazily inside the entry functions so this module
imports cleanly on default mintd installs (no `[schema]` extra) — the
ImportError surfaces only when the CLI actually runs the command. See
SLICE-41.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

FRICTIONLESS_SCHEMA_URL = "https://specs.frictionlessdata.io/schemas/table-schema.json"

SUPPORTED_EXTENSIONS = {".csv", ".dta", ".json", ".parquet"}

DEFAULT_MISSING_VALUES = ["", "NA", "."]


class SchemaExtraNotInstalled(Exception):
    """Raised when `mintd data schema generate` runs without the `[schema]` extra."""


def _lazy_pandas() -> Any:
    try:
        import pandas as pd  # type: ignore[import-untyped,import-not-found]
    except ImportError as exc:
        raise SchemaExtraNotInstalled(
            "schema generation requires the [schema] extra"
        ) from exc
    return pd


def _sanitize_for_json(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(item) for item in obj]
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return obj
    # numpy scalars (e.g. int32 from pandas StataReader.value_labels()) carry
    # an `.item()` method that unboxes to a native Python scalar — json.dump
    # can't serialize numpy ints/floats directly.
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes, bool, int)):
        try:
            return obj.item()
        except (AttributeError, ValueError):
            pass
    return obj


def find_project_root(start_dir: Path | None = None) -> Path:
    """Walk up looking for metadata.json. Raises FileNotFoundError if none."""
    current = (start_dir or Path.cwd()).resolve()
    while True:
        if (current / "metadata.json").exists():
            return current
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(
                "Could not find metadata.json in any parent directory. "
                "Are you inside a mintd project?"
            )
        current = parent


def extract_stata_metadata(file_path: Path) -> dict[str, dict[str, Any]]:
    """Return {column: {label, categories?}} from a .dta via pandas' StataReader."""
    pd = _lazy_pandas()
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    metadata: dict[str, dict[str, Any]] = {}
    with pd.io.stata.StataReader(file_path) as reader:
        variable_labels = reader.variable_labels()
        value_labels = reader.value_labels()

    for col in variable_labels.keys():
        var_label = variable_labels.get(col)
        col_meta: dict[str, Any] = {"label": var_label if var_label else None}
        if col in value_labels:
            col_meta["categories"] = [
                {"value": k, "label": v} for k, v in value_labels[col].items()
            ]
        metadata[col] = col_meta
    return metadata


def _read_dataframe(file_path: Path) -> Any:
    pd = _lazy_pandas()
    ext = file_path.suffix.lower()
    if ext == ".csv":
        return pd.read_csv(file_path)
    if ext == ".dta":
        return pd.read_stata(file_path, convert_categoricals=False)
    if ext == ".json":
        return pd.read_json(file_path)
    if ext == ".parquet":
        # pandas dispatches to pyarrow by default; the [schema] extra pins it.
        return pd.read_parquet(file_path)
    raise ValueError(f"Unsupported file format: {ext}")


def _pandas_dtype_to_frictionless(dtype: str) -> str:
    dtype = str(dtype).lower()
    if dtype.startswith("int"):
        return "integer"
    if dtype.startswith("float"):
        return "number"
    if dtype == "object" or dtype.startswith("str"):
        return "string"
    if dtype.startswith("bool"):
        return "boolean"
    if dtype.startswith("datetime"):
        return "datetime"
    if "date" in dtype:
        return "date"
    if dtype == "category":
        return "string"
    return "string"


def _infer_field_type(series: Any) -> str:
    pd = _lazy_pandas()
    dtype = str(series.dtype).lower()
    if dtype not in ("object", "str", "string"):
        return _pandas_dtype_to_frictionless(dtype)
    non_null = series.dropna()
    if len(non_null) == 0:
        return "string"
    sample = non_null.head(100)
    try:
        pd.to_datetime(sample, format="%Y-%m-%d", errors="raise")
        return "date"
    except (ValueError, TypeError):
        pass
    try:
        pd.to_datetime(sample, format="ISO8601", errors="raise")
        return "datetime"
    except (ValueError, TypeError):
        pass
    return "string"


def infer_table_schema(
    file_path: Path,
    stata_metadata: dict[str, dict[str, Any]] | None = None,
    _dataframe: Any = None,
) -> dict[str, Any]:
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file format: {ext}")
    if ext == ".dta" and stata_metadata is None:
        stata_metadata = extract_stata_metadata(file_path)
    df = _dataframe if _dataframe is not None else _read_dataframe(file_path)

    fields: list[dict[str, Any]] = []
    for col in df.columns:
        field: dict[str, Any] = {"name": col, "type": _infer_field_type(df[col])}
        if stata_metadata and col in stata_metadata:
            col_meta = stata_metadata[col]
            if col_meta.get("label"):
                field["title"] = col_meta["label"]
            if "categories" in col_meta:
                field["categories"] = col_meta["categories"]
        fields.append(field)

    return {
        "$schema": FRICTIONLESS_SCHEMA_URL,
        "fields": fields,
        "missingValues": DEFAULT_MISSING_VALUES,
    }


def generate_schema_file(
    data_dir: Path,
    output_path: Path,
    recursive: bool = True,
) -> None:
    """Walk ``data_dir`` for supported files; write one combined JSON.

    Raises:
        SchemaExtraNotInstalled: pandas (and/or pyarrow for parquet) missing.
        FileNotFoundError: no supported data files in ``data_dir``.
        RuntimeError: all candidate files failed to process.
    """
    _lazy_pandas()  # fail fast before scanning the directory

    data_files: list[Path] = []
    for ext in SUPPORTED_EXTENSIONS:
        pattern = f"**/*{ext}" if recursive else f"*{ext}"
        data_files.extend(data_dir.glob(pattern))
    data_files = sorted(data_files)

    if not data_files:
        raise FileNotFoundError(
            f"No supported data files found in {data_dir}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    files_schemas: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    for data_file in data_files:
        try:
            df = _read_dataframe(data_file)
            schema = infer_table_schema(data_file, _dataframe=df)
            files_schemas.append({
                "filename": data_file.name,
                "path": data_file.relative_to(data_dir).as_posix(),
                "observations": len(df),
                "columns": len(df.columns),
                "schema": schema,
            })
        except Exception as e:
            failures.append((data_file.name, str(e)))

    if not files_schemas:
        joined = "; ".join(f"{n}: {e}" for n, e in failures)
        raise RuntimeError(
            f"All {len(data_files)} data files in {data_dir} failed to process. "
            f"Details: {joined}"
        )

    combined = _sanitize_for_json({
        "generator": "mintd",
        "schema_standard": "frictionless-table-schema",
        "files": files_schemas,
    })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(combined, f, indent=2)
