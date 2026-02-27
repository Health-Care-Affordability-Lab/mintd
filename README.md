# Mintd - Lab Project Scaffolding Tool

A comprehensive Python CLI tool that automates the creation of standardized research project repositories with pre-configured version control, data versioning, **mandatory language selection (Python/R/Stata)**, and **Data Commons Registry integration**.

> [!NOTE]
> **Full Documentation**: [https://cooper-lab.github.io/mint/](https://cooper-lab.github.io/mint/)

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
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <repository-url>
cd mintd
uv sync --dev
```

### Using pip

```bash
pip install git+https://github.com/Cooper-lab/mint.git
```

## Quick Start

```bash
# Create a data processing project (language required)
mintd create data --name healthcare_analysis --lang python

# Create a research analysis project
mintd create project --name cost_study --lang r

# Create an infrastructure package
mintd create infra --name stat_tools --lang python

# Create with Registry Integration
mintd create data --name healthcare_analysis --lang python --register

# Create a secure enclave workspace
mintd create enclave --name secure_workspace
```

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

See the [Documentation](https://cooper-lab.github.io/mint/) for detailed guides on configuration, Stata integration, and advanced usage.