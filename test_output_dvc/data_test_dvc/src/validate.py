"""Data validation script for test_dvc.

NOTE: This script runs from the src/ directory. Data paths use ../data/
"""

import sys
from pathlib import Path
import pandas as pd

# Import mint utilities (located in same src/ directory)
from _mint_utils import (
    setup_project_directory,
    ParameterAwareLogger,
    RAW_DIR,
    INTERMEDIATE_DIR,
    FINAL_DIR,
    get_data_paths
)


def validate_dataset(df, dataset_name, logger):
    """Run validation checks on a dataset."""
    issues = []

    # Check for missing values
    missing_cols = df.columns[df.isnull().any()].tolist()
    if missing_cols:
        issues.append(f"Missing values in columns: {missing_cols}")

    # Check for duplicate rows
    duplicates = df.duplicated().sum()
    if duplicates > 0:
        issues.append(f"Found {duplicates} duplicate rows")

    # Check data types
    numeric_cols = df.select_dtypes(include=['number']).columns
    if len(numeric_cols) == 0:
        issues.append("No numeric columns found")

    # Basic statistics
    logger.log(f"Validation for {dataset_name}:")
    logger.log(f"  Rows: {len(df)}")
    logger.log(f"  Columns: {len(df.columns)}")
    logger.log(f"  Numeric columns: {len(numeric_cols)}")

    if issues:
        logger.log("  Issues found:")
        for issue in issues:
            logger.log(f"    - {issue}")
    else:
        logger.log("  [OK] No issues found")

    return len(issues) == 0


def main():
    """Main validation workflow."""
    # Initialize logging (validates directory and creates log file)
    logger = ParameterAwareLogger("validate")
    logger.log("Starting data validation for test_dvc...")

    # Get data paths and ensure directories exist
    paths = get_data_paths()
    for name, path in paths.items():
        if name != 'logs':  # logs already created by logger
            path.mkdir(parents=True, exist_ok=True)

    logger.log(f"Raw data directory: {RAW_DIR}")
    logger.log(f"Intermediate data directory: {INTERMEDIATE_DIR}")
    logger.log(f"Final data directory: {FINAL_DIR}")

    # TODO: Implement comprehensive validation logic
    # - Load cleaned data from INTERMEDIATE_DIR/
    # - Run quality checks (missing values, duplicates, data types)
    # - Business rule validation
    # - Statistical checks
    # - Save validation reports to FINAL_DIR/

    # Example validation
    # intermediate_files = list(INTERMEDIATE_DIR.glob("*.csv"))
    # all_valid = True
    #
    # for intermediate_file in intermediate_files:
    #     df = pd.read_csv(intermediate_file)
    #     is_valid = validate_dataset(df, intermediate_file.stem, logger)
    #     all_valid = all_valid and is_valid
    #
    # if all_valid:
    #     logger.log("[OK] All datasets passed validation")
    # else:
    #     logger.log("[FAIL] Some datasets failed validation - review issues above")

    logger.log("Data validation completed successfully.")
    logger.close()


if __name__ == "__main__":
    main()