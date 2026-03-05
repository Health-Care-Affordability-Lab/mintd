# Mintd - Lab Project Scaffolding Tool

A comprehensive Python CLI tool that automates the creation of standardized research project repositories with pre-configured version control, data versioning, **mandatory language selection (Python/R/Stata)**, and **Data Commons Registry integration**.

> [!NOTE]
> **Full Documentation**: [https://health-care-affordability-lab.github.io/mintd/](https://health-care-affordability-lab.github.io/mintd/)

## Features

- 🚀 **Rapid Project Setup**: Create standardized research projects in seconds
- 📊 **Multi-Language Support**: Python, R, and Stata
- 🔄 **Version Control**: Automatic Git and DVC initialization
- ☁️ **Cloud Storage**: S3-compatible storage support (AWS, Wasabi, MinIO)
- 🛠️ **Mintd Utilities**: Auto-generated utilities for logging and validation
- 🎉 **Registry Integration**: Tokenless GitOps-based project registration

## Quick Install

### Using uv (Recommended)

```bash
# Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install mintd as a CLI tool
uv tool install git+https://github.com/health-care-affordability-lab/mintd.git
```

### Using pip

```bash
pip install git+https://github.com/health-care-affordability-lab/mintd.git
```

> For development setup, see the [Development Guide](docs/development.md).

## Quick Start

```bash
# Create a data processing project (language required)
mintd create data --name healthcare_analysis --lang python

# Create a research analysis project
mintd create project --name cost_study --lang r

# Create with Registry Integration
mintd create data --name healthcare_analysis --lang python --register

# Create a secure enclave workspace
mintd create enclave --name secure_workspace
```

> **Note:** For reusable code packages, use standard language tooling (e.g., `uv init`, `poetry init`) instead of mintd.

## Updating Existing Repositories

If you have an existing mintd-managed repository and want to update to the latest schema with new fields:

```bash
# Add Frictionless Table Schema support
mintd update schema

# Auto-generate schema from existing data files
mintd update schema --generate

# Update metadata fields (sensitivity, mirror URL)
mintd update metadata
```

See the [Documentation](https://health-care-affordability-lab.github.io/mintd/) for detailed guides on configuration, Stata integration, and advanced usage.