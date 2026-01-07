"""Data ingestion script for test_dvc.

NOTE: This script runs from the src/ directory. Data paths use ../data/
"""

import sys
from pathlib import Path

# Import mint utilities (located in same src/ directory)
from _mint_utils import (
    setup_project_directory, 
    ParameterAwareLogger,
    RAW_DIR, 
    INTERMEDIATE_DIR, 
    FINAL_DIR,
    get_data_paths
)

# =============================================================================
# UPDATING FOR NEW DATA (e.g., annual releases)
# =============================================================================
# Data versioning is handled by DVC. When new data becomes available:
#
# 1. Update this script to point to the new data source
# 2. Run the pipeline: dvc repro
# 3. Commit changes: git add . && git commit -m "Update to 2024 data"
# 4. Push data and code: dvc push && git push
#
# DVC tracks all versions - use `dvc diff` to see changes between versions
# and `git log` / `dvc checkout` to access previous versions.
# =============================================================================


def main():
    """Main ingestion workflow."""
    # Initialize logging (validates directory and creates log file)
    logger = ParameterAwareLogger("ingest")
    logger.log("Starting data ingestion for test_dvc...")

    # Get data paths and ensure directories exist
    paths = get_data_paths()
    for name, path in paths.items():
        if name != 'logs':  # logs already created by logger
            path.mkdir(parents=True, exist_ok=True)

    logger.log(f"Raw data directory: {RAW_DIR}")
    logger.log(f"Intermediate data directory: {INTERMEDIATE_DIR}")
    logger.log(f"Final data directory: {FINAL_DIR}")

    # TODO: Implement data ingestion logic
    # - Download data from APIs/databases
    # - Extract archives using zipfile/patool
    # - Initial data validation with pandas
    # - Save to RAW_DIR/

    # Example:
    # import requests
    # import pandas as pd
    #
    # # Download data
    # response = requests.get("https://example.com/data.csv")
    # raw_file = RAW_DIR / "downloaded_data.csv"
    # with open(raw_file, 'wb') as f:
    #     f.write(response.content)
    #
    # # Quick validation
    # df = pd.read_csv(raw_file)
    # logger.log(f"Downloaded {len(df)} rows with columns: {list(df.columns)}")

    logger.log("Data ingestion completed successfully.")
    logger.close()


if __name__ == "__main__":
    main()