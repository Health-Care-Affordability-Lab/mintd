"""Tests for Frictionless Table Schema generation."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import pandas as pd

from mintd.utils.schema import (
    extract_stata_metadata,
    infer_table_schema,
    generate_schema_file,
    FRICTIONLESS_SCHEMA_URL,
)


class TestExtractStataMetadata:
    """Tests for Stata variable and value label extraction."""

    def test_extracts_variable_labels(self, tmp_path):
        """Test extracting variable labels from .dta file."""
        # Create a simple DataFrame and save as Stata
        df = pd.DataFrame({
            "age": [25, 30, 35],
            "income": [50000, 60000, 70000],
        })
        dta_path = tmp_path / "test.dta"
        df.to_stata(
            dta_path,
            variable_labels={"age": "Patient age in years", "income": "Annual income"},
            write_index=False,
        )

        metadata = extract_stata_metadata(dta_path)

        assert metadata["age"]["label"] == "Patient age in years"
        assert metadata["income"]["label"] == "Annual income"

    def test_extracts_value_labels(self, tmp_path):
        """Test extracting value labels (categorical) from .dta file."""
        # pandas requires int column for value_labels, not Categorical
        df = pd.DataFrame({
            "gender": [1, 2, 1],
        })
        dta_path = tmp_path / "test.dta"
        df.to_stata(
            dta_path,
            value_labels={"gender": {1: "Male", 2: "Female"}},
            write_index=False,
        )

        metadata = extract_stata_metadata(dta_path)

        assert "categories" in metadata["gender"]
        categories = metadata["gender"]["categories"]
        assert {"value": 1, "label": "Male"} in categories
        assert {"value": 2, "label": "Female"} in categories

    def test_handles_missing_labels(self, tmp_path):
        """Test handling .dta files without labels."""
        df = pd.DataFrame({"x": [1, 2, 3]})
        dta_path = tmp_path / "test.dta"
        df.to_stata(dta_path, write_index=False)

        metadata = extract_stata_metadata(dta_path)

        assert metadata["x"]["label"] is None
        assert "categories" not in metadata["x"]

    def test_nonexistent_file_raises(self):
        """Test that nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            extract_stata_metadata(Path("/nonexistent/file.dta"))


class TestInferTableSchema:
    """Tests for Frictionless Table Schema inference."""

    def test_csv_schema_inference(self, tmp_path):
        """Test schema inference from CSV file."""
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("name,age,salary\nAlice,30,50000\nBob,25,60000\n")

        schema = infer_table_schema(csv_path)

        assert "$schema" in schema
        assert schema["$schema"] == FRICTIONLESS_SCHEMA_URL
        assert "fields" in schema

        field_names = [f["name"] for f in schema["fields"]]
        assert "name" in field_names
        assert "age" in field_names
        assert "salary" in field_names

    def test_csv_type_detection(self, tmp_path):
        """Test that types are correctly inferred."""
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("text,num,date\nhello,123,2024-01-15\nworld,456,2024-02-20\n")

        schema = infer_table_schema(csv_path)

        fields_by_name = {f["name"]: f for f in schema["fields"]}
        assert fields_by_name["text"]["type"] == "string"
        assert fields_by_name["num"]["type"] == "integer"
        assert fields_by_name["date"]["type"] == "date"

    def test_stata_with_labels(self, tmp_path):
        """Test schema inference from Stata file with labels."""
        # Use plain integers (not Categorical) for value_labels to work
        df = pd.DataFrame({
            "patient_id": [1, 2, 3],
            "diagnosis": [1, 2, 1],
        })
        dta_path = tmp_path / "test.dta"
        df.to_stata(
            dta_path,
            variable_labels={"patient_id": "Unique patient ID", "diagnosis": "Primary diagnosis code"},
            value_labels={"diagnosis": {1: "Diabetes", 2: "Hypertension"}},
            write_index=False,
        )

        schema = infer_table_schema(dta_path)

        fields_by_name = {f["name"]: f for f in schema["fields"]}

        # Check variable label mapped to title
        assert fields_by_name["patient_id"].get("title") == "Unique patient ID"

        # Check value labels mapped to categories
        diag = fields_by_name["diagnosis"]
        assert "categories" in diag
        assert {"value": 1, "label": "Diabetes"} in diag["categories"]

    def test_excel_schema_inference(self, tmp_path):
        """Test schema inference from Excel file."""
        excel_path = tmp_path / "data.xlsx"
        df = pd.DataFrame({"col_a": [1, 2], "col_b": ["x", "y"]})
        df.to_excel(excel_path, index=False)

        schema = infer_table_schema(excel_path)

        field_names = [f["name"] for f in schema["fields"]]
        assert "col_a" in field_names
        assert "col_b" in field_names

    def test_missing_values_configured(self, tmp_path):
        """Test that missing values are configured for Stata compatibility."""
        csv_path = tmp_path / "data.csv"
        csv_path.write_text("x\n1\nNA\n.\n")

        schema = infer_table_schema(csv_path)

        assert "missingValues" in schema
        assert "." in schema["missingValues"]  # Stata missing
        assert "NA" in schema["missingValues"]
        assert "" in schema["missingValues"]

    def test_unsupported_format_raises(self, tmp_path):
        """Test that unsupported formats raise ValueError."""
        txt_path = tmp_path / "data.txt"
        txt_path.write_text("some text")

        with pytest.raises(ValueError, match="Unsupported"):
            infer_table_schema(txt_path)


class TestGenerateSchemaFile:
    """Tests for full schema file generation."""

    def test_generates_combined_schema(self, tmp_path):
        """Test generating schema for multiple files."""
        # Create data directory with multiple files
        data_dir = tmp_path / "data" / "final"
        data_dir.mkdir(parents=True)

        (data_dir / "file1.csv").write_text("a,b\n1,2\n3,4\n")
        (data_dir / "file2.csv").write_text("x,y,z\n1,2,3\n")

        output_path = tmp_path / "schema.json"

        generate_schema_file(data_dir, output_path)

        assert output_path.exists()
        with open(output_path) as f:
            result = json.load(f)

        assert "generated_at" in result
        assert "generator" in result
        assert result["generator"] == "mintd"
        assert "files" in result
        assert len(result["files"]) == 2

    def test_includes_observation_count(self, tmp_path):
        """Test that observation count is included."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "test.csv").write_text("x\n1\n2\n3\n4\n5\n")

        output_path = tmp_path / "schema.json"
        generate_schema_file(data_dir, output_path)

        with open(output_path) as f:
            result = json.load(f)

        assert result["files"][0]["observations"] == 5

    def test_empty_directory_creates_empty_schema(self, tmp_path):
        """Test that empty directory creates schema with no files."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        output_path = tmp_path / "schema.json"

        generate_schema_file(data_dir, output_path)

        with open(output_path) as f:
            result = json.load(f)

        assert result["files"] == []

    def test_schema_has_frictionless_reference(self, tmp_path):
        """Test that each file schema has $schema reference."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "test.csv").write_text("x\n1\n")

        output_path = tmp_path / "schema.json"
        generate_schema_file(data_dir, output_path)

        with open(output_path) as f:
            result = json.load(f)

        file_schema = result["files"][0]["schema"]
        assert file_schema["$schema"] == FRICTIONLESS_SCHEMA_URL
