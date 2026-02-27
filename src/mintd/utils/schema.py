"""Frictionless Table Schema generation utilities.

This module provides functions to generate Frictionless Table Schema
from data files, with special handling for Stata .dta files to preserve
variable labels and value labels.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

FRICTIONLESS_SCHEMA_URL = "https://specs.frictionlessdata.io/schemas/table-schema.json"

# Supported file extensions
SUPPORTED_EXTENSIONS = {".csv", ".dta", ".xlsx", ".xls", ".json"}

# Default missing values (includes Stata's ".")
DEFAULT_MISSING_VALUES = ["", "NA", "."]


def extract_stata_metadata(file_path: Path) -> Dict[str, Dict[str, Any]]:
    """Extract variable labels and value labels from a Stata .dta file.

    Args:
        file_path: Path to the .dta file

    Returns:
        Dict mapping column names to metadata dicts containing:
        - label: Variable label (str or None)
        - categories: List of {value, label} dicts if value labels exist

    Raises:
        FileNotFoundError: If file does not exist
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    metadata: Dict[str, Dict[str, Any]] = {}

    # Use StataReader to access metadata
    with pd.io.stata.StataReader(file_path) as reader:
        variable_labels = reader.variable_labels()
        value_labels = reader.value_labels()

    # Get columns from variable_labels keys (always present for all columns)
    columns = list(variable_labels.keys())

    for col in columns:
        var_label = variable_labels.get(col)
        col_meta: Dict[str, Any] = {
            "label": var_label if var_label else None
        }

        # Value labels - check if column has associated labels
        if col in value_labels:
            col_meta["categories"] = [
                {"value": k, "label": v} for k, v in value_labels[col].items()
            ]

        metadata[col] = col_meta

    return metadata


def _read_dataframe(file_path: Path) -> pd.DataFrame:
    """Read a data file into a pandas DataFrame.

    Args:
        file_path: Path to the data file

    Returns:
        pandas DataFrame

    Raises:
        ValueError: If file format is not supported
    """
    ext = file_path.suffix.lower()

    if ext == ".csv":
        return pd.read_csv(file_path)
    elif ext == ".dta":
        return pd.read_stata(file_path)
    elif ext in {".xlsx", ".xls"}:
        return pd.read_excel(file_path)
    elif ext == ".json":
        return pd.read_json(file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def _pandas_dtype_to_frictionless(dtype: str) -> str:
    """Map pandas dtype to Frictionless field type.

    Args:
        dtype: pandas dtype as string

    Returns:
        Frictionless type string
    """
    dtype = str(dtype).lower()

    if dtype.startswith("int"):
        return "integer"
    elif dtype.startswith("float"):
        return "number"
    elif dtype == "object" or dtype.startswith("str"):
        return "string"
    elif dtype.startswith("bool"):
        return "boolean"
    elif dtype.startswith("datetime"):
        return "datetime"
    elif "date" in dtype:
        return "date"
    elif dtype == "category":
        return "string"  # Categories are strings with constraints
    else:
        return "string"  # Default fallback


def _infer_field_type(series: pd.Series) -> str:
    """Infer Frictionless field type from a pandas Series.

    Attempts to detect dates and other types from object/string columns.

    Args:
        series: pandas Series to analyze

    Returns:
        Frictionless type string
    """
    dtype = str(series.dtype).lower()

    # First try the simple dtype mapping for non-string types
    if dtype not in ("object", "str", "string"):
        return _pandas_dtype_to_frictionless(dtype)

    # For string/object columns, try to infer more specific types
    non_null = series.dropna()
    if len(non_null) == 0:
        return "string"

    # Try parsing as date (YYYY-MM-DD format)
    sample = non_null.head(100)
    try:
        pd.to_datetime(sample, format="%Y-%m-%d", errors="raise")
        return "date"
    except (ValueError, TypeError):
        pass

    # Try parsing as datetime (various formats)
    try:
        pd.to_datetime(sample, errors="raise")
        return "datetime"
    except (ValueError, TypeError):
        pass

    return "string"


def infer_table_schema(
    file_path: Path,
    stata_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Infer Frictionless Table Schema from a data file.

    Args:
        file_path: Path to the data file
        stata_metadata: Optional pre-extracted Stata metadata

    Returns:
        Frictionless Table Schema dict

    Raises:
        ValueError: If file format is not supported
        FileNotFoundError: If file does not exist
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file format: {ext}")

    # Extract Stata metadata if applicable
    if ext == ".dta" and stata_metadata is None:
        stata_metadata = extract_stata_metadata(file_path)

    # Read the data
    df = _read_dataframe(file_path)

    # Build fields
    fields: List[Dict[str, Any]] = []
    for col in df.columns:
        field: Dict[str, Any] = {
            "name": col,
            "type": _infer_field_type(df[col]),
        }

        # Add Stata metadata if available
        if stata_metadata and col in stata_metadata:
            col_meta = stata_metadata[col]
            if col_meta.get("label"):
                field["title"] = col_meta["label"]
            if "categories" in col_meta:
                field["categories"] = col_meta["categories"]

        fields.append(field)

    schema: Dict[str, Any] = {
        "$schema": FRICTIONLESS_SCHEMA_URL,
        "fields": fields,
        "missingValues": DEFAULT_MISSING_VALUES,
    }

    return schema


def generate_schema_file(
    data_dir: Path,
    output_path: Path,
    recursive: bool = True,
) -> None:
    """Generate a combined schema file for all data files in a directory.

    Args:
        data_dir: Directory containing data files
        output_path: Path to write the schema JSON file
        recursive: Whether to search subdirectories
    """
    # Find all data files
    data_files: List[Path] = []
    for ext in SUPPORTED_EXTENSIONS:
        pattern = f"**/*{ext}" if recursive else f"*{ext}"
        data_files.extend(data_dir.glob(pattern))
    data_files = sorted(data_files)

    # Generate schema for each file
    files_schemas: List[Dict[str, Any]] = []
    for data_file in data_files:
        try:
            df = _read_dataframe(data_file)
            schema = infer_table_schema(data_file)

            file_entry = {
                "filename": data_file.name,
                "path": str(data_file.relative_to(data_dir)),
                "observations": len(df),
                "columns": len(df.columns),
                "schema": schema,
            }
            files_schemas.append(file_entry)
        except Exception as e:
            # Log warning but continue with other files
            print(f"Warning: Could not process {data_file.name}: {e}")

    # Build combined output
    combined = {
        "generated_at": datetime.now().isoformat(),
        "generator": "mintd",
        "schema_standard": "frictionless-table-schema",
        "files": files_schemas,
    }

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write output
    with open(output_path, "w") as f:
        json.dump(combined, f, indent=2)
