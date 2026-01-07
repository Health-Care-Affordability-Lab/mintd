"""Mint utility functions for test_dvc.

This module provides common utilities used across all project scripts.
DO NOT MODIFY - This file is managed by mint and will be overwritten.

NOTE: Scripts are expected to run from the src/ directory.
Data paths should use "../data/" or the get_data_paths() function.
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List


# =============================================================================
# PATH DEFINITIONS (relative to src/ where scripts run from)
# =============================================================================

DATA_DIR = Path("../data")
RAW_DIR = Path("../data/raw")
INTERMEDIATE_DIR = Path("../data/intermediate")
FINAL_DIR = Path("../data/final")
LOGS_DIR = Path("../logs")


def get_data_paths() -> Dict[str, Path]:
    """Get standard data directory paths.
    
    Returns:
        Dict with keys: 'data', 'raw', 'intermediate', 'final', 'logs'
    """
    return {
        'data': DATA_DIR,
        'raw': RAW_DIR,
        'intermediate': INTERMEDIATE_DIR,
        'final': FINAL_DIR,
        'logs': LOGS_DIR,
    }


def setup_project_directory() -> Path:
    """Validate we're running from src/ directory and return project root.

    This function checks if we're in the correct directory structure:
    - Preferred: Running from src/ (project indicators in parent)
    - Fallback: Running from project root (for backwards compatibility)

    Returns:
        Path: Project root directory

    Raises:
        RuntimeError: If not running from expected directory
    """
    global DATA_DIR, RAW_DIR, INTERMEDIATE_DIR, FINAL_DIR, LOGS_DIR
    
    current_dir = Path.cwd()

    # Look for project root indicators
    root_indicators = ['metadata.json', '.git']

    # Check if we're in src/ (project indicators in parent directory)
    parent_dir = current_dir.parent
    if any((parent_dir / indicator).exists() for indicator in root_indicators):
        # We're in src/, paths are already correct
        return parent_dir

    # Check if we're in project root (backwards compatibility)
    if any((current_dir / indicator).exists() for indicator in root_indicators):
        # Update paths for project-root execution
        DATA_DIR = Path("data")
        RAW_DIR = Path("data/raw")
        INTERMEDIATE_DIR = Path("data/intermediate")
        FINAL_DIR = Path("data/final")
        LOGS_DIR = Path("logs")
        return current_dir

    # Not in a valid location
    raise RuntimeError(
        f"Not running from expected directory.\n"
        f"Scripts should be run from src/ directory.\n"
        f"Current directory: {current_dir}\n"
        f"Please cd to the project's src/ directory and try again."
    )


class ParameterAwareLogger:
    """Logger that creates parameter-aware log files."""

    def __init__(self, script_name: str):
        """Initialize logger with script name.

        Args:
            script_name: Name of the script (e.g., 'ingest', 'clean', 'validate')
        """
        self.script_name = script_name
        self.start_time = datetime.now()
        self.project_root = setup_project_directory()

        # Create logs directory using the global LOGS_DIR path
        self.logs_dir = LOGS_DIR
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # Parse command line arguments to get parameters for log filename
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--year', '-y')
        parser.add_argument('--version', '-v')
        parser.add_argument('--param', '-p')

        # Try to parse known args, ignore unknown ones
        try:
            args, _ = parser.parse_known_args()
            params = []
            if args.year:
                params.append(args.year)
            if args.version:
                params.append(args.version)
            if args.param:
                params.append(args.param)

            # Check for positional arguments that might be parameters
            remaining_args = sys.argv[1:]
            if remaining_args and not remaining_args[0].startswith('-'):
                # First positional argument might be a parameter (like year)
                param_candidate = remaining_args[0]
                if param_candidate.isdigit() or len(param_candidate) <= 10:
                    params.append(param_candidate)

            self.params_suffix = '_'.join(params) if params else None
        except:
            self.params_suffix = None

        # Create log filename
        if self.params_suffix:
            log_filename = f"{self.script_name}_{self.params_suffix}.log"
        else:
            timestamp = self.start_time.strftime("%Y%m%d_%H%M%S")
            log_filename = f"{self.script_name}_{timestamp}.log"

        self.log_path = self.logs_dir / log_filename

        # Set up logging
        self.logger = logging.getLogger(script_name)
        self.logger.setLevel(logging.DEBUG)

        # Remove any existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

        # Create file handler
        file_handler = logging.FileHandler(self.log_path)
        file_handler.setLevel(logging.DEBUG)

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        file_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)

        # Log initial information
        self.logger.info("=" * 80)
        self.logger.info(f"Script: {self.script_name}")
        self.logger.info(f"Command: {' '.join(sys.argv)}")
        self.logger.info(f"Working directory: {os.getcwd()}")
        self.logger.info(f"Python version: {sys.version}")
        self.logger.info(f"Start time: {self.start_time.isoformat()}")
        self.logger.info("=" * 80)

    def log(self, message: str, level: str = "info"):
        """Log a message.

        Args:
            message: Message to log
            level: Log level (debug, info, warning, error, critical)
        """
        if level.lower() == "debug":
            self.logger.debug(message)
        elif level.lower() == "info":
            self.logger.info(message)
        elif level.lower() == "warning":
            self.logger.warning(message)
        elif level.lower() == "error":
            self.logger.error(message)
        elif level.lower() == "critical":
            self.logger.critical(message)

    def close(self):
        """Close the logger and log final information."""
        end_time = datetime.now()
        duration = end_time - self.start_time

        self.logger.info("=" * 80)
        self.logger.info(f"End time: {end_time.isoformat()}")
        self.logger.info(f"Duration: {duration}")
        self.logger.info("=" * 80)

        # Close handlers
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)


def generate_data_schema(data_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """Generate JSON schema from data file.

    Args:
        data_path: Path to data file (CSV, JSON, etc.)
        output_path: Optional path to save schema JSON

    Returns:
        Dict containing schema information
    """
    import pandas as pd

    try:
        # Read data based on file extension
        if data_path.suffix.lower() == '.csv':
            df = pd.read_csv(data_path)
        elif data_path.suffix.lower() == '.json':
            df = pd.read_json(data_path)
        elif data_path.suffix.lower() in ['.xlsx', '.xls']:
            df = pd.read_excel(data_path)
        else:
            raise ValueError(f"Unsupported file format: {data_path.suffix}")

        # Generate schema
        schema = {
            "filename": data_path.name,
            "filepath": str(data_path),
            "observations": len(df),
            "columns": len(df.columns),
            "variables": []
        }

        for col in df.columns:
            # Basic type detection
            dtype = str(df[col].dtype)
            if dtype.startswith('int'):
                var_type = "integer"
            elif dtype.startswith('float'):
                var_type = "numeric"
            elif dtype == 'object':
                var_type = "string"
            elif dtype.startswith('bool'):
                var_type = "boolean"
            else:
                var_type = dtype

            # Generate basic label from column name
            label = col.replace('_', ' ').replace('-', ' ').title()

            variable_info = {
                "name": col,
                "type": var_type,
                "label": label,
                "missing_count": df[col].isna().sum(),
                "unique_values": df[col].nunique() if df[col].dtype == 'object' else None
            }

            # Add numeric statistics for numeric columns
            if var_type in ["integer", "numeric"]:
                variable_info.update({
                    "min": float(df[col].min()) if not pd.isna(df[col].min()) else None,
                    "max": float(df[col].max()) if not pd.isna(df[col].max()) else None,
                    "mean": float(df[col].mean()) if not pd.isna(df[col].mean()) else None,
                    "std": float(df[col].std()) if not pd.isna(df[col].std()) else None
                })

            schema["variables"].append(variable_info)

        # Save schema if output path provided
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(schema, f, indent=2, default=str)

        return schema

    except Exception as e:
        raise RuntimeError(f"Failed to generate schema for {data_path}: {e}")


def find_data_files(directory: Path, extensions: List[str] = None) -> List[Path]:
    """Find data files in a directory.

    Args:
        directory: Directory to search
        extensions: File extensions to look for (default: common data formats)

    Returns:
        List of data file paths
    """
    if extensions is None:
        extensions = ['.csv', '.json', '.xlsx', '.xls', '.dta', '.rds', '.sav']

    data_files = []
    for ext in extensions:
        data_files.extend(directory.glob(f"**/*{ext}"))

    return sorted(data_files)