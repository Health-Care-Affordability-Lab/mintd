# mint - Lab Project Scaffolding Tool

A comprehensive Python CLI tool that automates the creation of standardized research project repositories with pre-configured version control, data versioning, **mandatory language selection (Python/R/Stata)**, and **Data Commons Registry integration**. Version 1.0.0 includes full GitOps-based project registration without requiring personal access tokens, plus auto-generated utilities for logging, project validation, and schema generation.

## Features

### Core Functionality
- 🚀 **Rapid Project Setup**: Create standardized research projects in seconds
- 📊 **Multi-Language Support**: Python, R, and Stata with mandatory language selection
- 🔄 **Version Control**: Automatic Git and DVC initialization with cloud storage
- ☁️ **Cloud Storage**: S3-compatible storage support (AWS, Wasabi, MinIO)
- 📁 **Standardized Structure**: Consistent directory layouts for different project types
- 🔧 **CLI & API**: Command-line interface and Python API
- 📈 **Stata Integration**: Native Stata commands for seamless workflow
- 🛠️ **Mint Utilities**: Auto-generated utilities for logging, project validation, and schema generation
- 📝 **Parameter-Aware Logging**: Automatic logging with parameter-based filenames (e.g., `ingest_2023.log`)
- 🔖 **Version Tracking**: Metadata includes mint version and commit hash for reproducibility

### 🎉 Data Commons Registry Integration (v1.0.0)
- 🏛️ **Automatic Project Registration**: Tokenless GitOps-based cataloging
- 🔐 **Secure Access Control**: Automatic permission synchronization via GitHub Actions
- 📋 **Registry Management**: CLI commands for registration status and management
- 🔄 **Offline Mode**: Graceful handling with automatic retry when registry is unreachable
- 🚫 **Zero Token Management**: Uses SSH keys and GitHub CLI instead of personal access tokens

## Installation

### Using uv (Recommended)

```bash
# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install mint
git clone <repository-url>
cd mint
uv sync --dev
```

### Using pip

```bash
# Install directly from git (PyPI not available)
pip install git+https://github.com/Cooper-lab/mint.git

# Install from source (development)
git clone https://github.com/Cooper-lab/mint.git
cd mint
pip install -e ".[dev]"

# Verify installation
python verify_installation.py
```

**Version 1.0.0** includes complete Data Commons Registry integration with tokenless GitOps-based project registration, plus mandatory language selection, parameter-aware logging, and auto-generated utility scripts.

### Requirements

**Core Requirements:**
- **Python**: 3.9+
- **Optional**: Git, DVC for version control features
- **Stata**: 16+ for Stata integration

**Registry Integration (Optional):**
- **SSH Key**: Configured for GitHub (`ssh-keygen -t ed25519 -C "your_email@example.com"`)
- **GitHub CLI**: Installed and authenticated (`gh auth login`)
- **Registry Access**: Push permissions to the Data Commons Registry repository

## Quick Start

### Basic Usage

```bash
# Create a data processing project (language required)
mint create data --name healthcare_analysis --lang python

# Create a research analysis project
mint create project --name cost_study --lang r

# Create an infrastructure package
mint create infra --name stat_tools --lang python
```

### With Registry Integration

```bash
# Create projects with automatic registration to Data Commons Registry
mint create data --name healthcare_analysis --lang python --register
mint create project --name cost_study --lang stata --register

# Check registration status
mint registry status healthcare_analysis

# Register existing projects
mint registry register --path /path/to/existing/project
```

### With Custom Options

```bash
# Create in specific directory with custom settings
mint create data --name mydata --path /projects --bucket my-custom-bucket

# Create projects with specific programming languages (now required)
mint create data --name healthcare --lang r
mint create project --name analysis --lang stata

# Skip version control initialization
mint create project --name analysis --no-git --no-dvc

# Register with Data Commons Registry
mint create data --name healthcare --register

# Use current directory (when in existing git repo)
cd existing-git-repo
mint create data --name mydata --use-current-repo
```

## Using Existing Git Repositories

mint supports scaffolding projects directly in existing git repositories using the `--use-current-repo` flag. This is useful when you want to add mint's standardized project structure to an existing codebase.

### Requirements
- You must be in a git repository
- Only works with git-initialized directories

### Example Usage

```bash
# Navigate to existing git repository
cd my-existing-project

# Scaffold mint project structure in current directory
mint create data --name healthcare-data --use-current-repo

# Result: Project files created directly in my-existing-project/
# ├── README.md (mint-generated)
# ├── metadata.json
# ├── data/
# ├── src/
# └── .gitignore
```

### What Happens
- **No subdirectory created**: Unlike normal usage, no `data_healthcare-data/` folder is created
- **Git integration**: Uses existing git repository, adds and commits new files
- **File conflicts**: Warning displayed if existing files would be overwritten
- **Same functionality**: All other mint features work normally (DVC, templates, etc.)

### When to Use
- Adding mint structure to existing research projects
- Converting legacy projects to standardized format
- Working within established repository conventions
- Collaborating on projects with existing git history

## Project Types

### Data Projects (`data_*`)

For data products and processing pipelines. Supports Python, R, and Stata:

**Python-focused:**
```
data_healthcare/
├── README.md                 # Project documentation
├── metadata.json            # Project metadata (with mint version)
├── requirements.txt         # Python dependencies
├── logs/                    # Script execution logs
├── data/
│   ├── raw/                 # Raw data (DVC tracked)
│   ├── intermediate/        # Processed data (DVC tracked)
│   └── final/               # Analysis-ready data (DVC tracked)
├── src/
│   ├── _mint_utils.py       # Mint utilities (auto-generated)
│   ├── ingest.py           # Data acquisition
│   ├── clean.py            # Data cleaning
│   └── validate.py         # Data validation
├── .gitignore
├── .dvcignore
└── dvc.yaml                # Pipeline configuration (python commands)
```

**R-focused:**
```
data_healthcare/
├── README.md                 # Project documentation
├── metadata.json            # Project metadata (with mint version)
├── DESCRIPTION              # R package description
├── renv.lock               # R environment snapshot
├── logs/                    # Script execution logs
├── data/
│   ├── raw/                 # Raw data (DVC tracked)
│   ├── intermediate/        # Processed data (DVC tracked)
│   └── final/               # Analysis-ready data (DVC tracked)
├── src/
│   ├── _mint_utils.R        # Mint utilities (auto-generated)
│   ├── ingest.R            # Data acquisition
│   ├── clean.R             # Data cleaning
│   └── validate.R          # Data validation
├── .gitignore
├── .dvcignore
└── dvc.yaml                # Pipeline configuration (Rscript commands)
```

**Stata-focused:**
```
data_healthcare/
├── README.md                 # Project documentation
├── metadata.json            # Project metadata (with mint version)
├── logs/                    # Script execution logs
├── data/
│   ├── raw/                 # Raw data (DVC tracked)
│   ├── intermediate/        # Processed data (DVC tracked)
│   └── final/               # Analysis-ready data (DVC tracked)
├── src/
│   ├── _mint_utils.do       # Mint utilities (auto-generated)
│   ├── ingest.do           # Data acquisition
│   ├── clean.do            # Data cleaning
│   └── validate.do         # Data validation
├── .gitignore
├── .dvcignore
└── dvc.yaml                # Pipeline configuration (stata -b do commands)
```

### Research Projects (`prj__*`)

For analysis and research projects:

```
prj__cost_study/
├── README.md
├── metadata.json
├── requirements.txt
├── renv.lock               # R environment (if used)
├── data/                   # Project data
├── src/
│   ├── analysis/          # Python analysis scripts
│   ├── stata/             # Stata do-files
│   └── r/                 # R analysis scripts
├── output/
│   ├── figures/           # Generated plots
│   └── tables/            # Generated tables
├── docs/                  # Documentation
├── .Rprofile              # R configuration
├── .gitignore
└── .dvcignore
```

### Infrastructure Projects (`infra_*`)

For reusable packages and tools:

```
infra_stat_tools/
├── README.md
├── metadata.json
├── pyproject.toml          # Package configuration
├── src/
│   └── stat_tools/        # Main package
├── tests/
│   └── __init__.py
└── docs/
```

## Mint Utilities

mint automatically generates utility files (`_mint_utils.{py|R|do}`) that provide common functionality for all project scripts:

### Project Directory Validation
- Ensures scripts run from the correct project root directory
- Provides clear error messages if executed from wrong location
- Automatically detects project root via `metadata.json` or `.git`

### Parameter-Aware Logging
- Creates timestamped log files with parameter-based names
- Examples: `ingest_2023.log`, `clean_v2.log`, `validate_20241222_143052.log`
- Logs include: command executed, parameters, start/end times, working directory
- Complements DVC versioning with execution audit trails

### Schema Generation
- Extracts variable metadata from data files (CSV, DTA, RDS, etc.)
- Captures: variable names, types, labels, observation counts
- Outputs JSON schema for Data Commons Registry integration
- Useful for data dictionary creation and validation

### Usage in Scripts

**Python:**
```python
from _mint_utils import setup_project_directory, ParameterAwareLogger

# Validate project directory and set up logging
logger = ParameterAwareLogger("ingest")
logger.log("Starting data ingestion...")

# Your script code here
logger.log("Processing completed successfully.")
logger.close()
```

**R:**
```r
source("_mint_utils.R")

# Set up logging
logger <- ParameterAwareLogger("clean")
logger$log("Starting data cleaning...")

# Your script code here
logger$log("Cleaning completed successfully.")
logger$close()
```

**Stata:**
```stata
do _mint_utils.do

* Initialize logging
ParameterAwareLogger clean
log_message "Starting data validation..."

* Your script code here
log_message "Validation completed successfully."
close_logger
```

### Updating Utilities

When mint is updated, you can refresh the utility files without touching your scripts:

```bash
mint update utils
```

This command:
- Regenerates `_mint_utils.*` files with latest features
- Updates mint version information in `metadata.json`
- Preserves all your custom scripts and data

## CLI Reference

### Main Commands

```bash
mint --help                    # Show help
mint --version                 # Show version (1.0.0)
```

### Project Creation

```bash
mint create data --name <name> [OPTIONS]
mint create project --name <name> [OPTIONS]
mint create infra --name <name> [OPTIONS]

Options:
  -n, --name TEXT       Project name (required)
  -p, --path PATH       Output directory (default: current)
  --lang TEXT          Primary programming language (python|r|stata, REQUIRED)
  --no-git             Skip Git initialization
  --no-dvc             Skip DVC initialization
  --bucket TEXT        Custom DVC bucket name
  --register           Register project with Data Commons Registry
  --use-current-repo   Use current directory as project root (when in existing git repo)
```

### Configuration

```bash
mint config show                    # Show current config
mint config setup                   # Interactive setup
mint config setup --set KEY VALUE  # Set specific value
mint config setup --set-credentials # Set storage credentials
```

### Registry Management

```bash
mint registry register --path <path>     # Register existing project
mint registry status <project_name>      # Check registration status
mint registry sync                       # Process pending registrations
```

### Utility Management

```bash
mint update utils                        # Update mint utilities to latest version
```

## Stata Integration

mint provides seamless integration with Stata 16+ through native Python commands.

### Installation for Stata Users

**Option 1: Automated Installation (Recommended)**
```stata
// Automated installation (installs Stata package + Python package)
mint_installer

// Verify installation
help mint
```

**Option 2: Via Stata's net install**
```stata
// Install Stata package from GitHub (may not work if repository is private)
net install mint, from("https://github.com/Cooper-lab/mint/raw/main/stata/")

// If net install fails, use the automated installer instead:
mint_installer, github

// Install Python package
python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])

// Verify installation
help mint
```

**Option 3: Manual Installation**

1. **Install mint in Stata's Python environment**:
   ```stata
   python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])
   ```

2. **Install Stata files**:
   Copy `stata/mint.ado` and `stata/mint.sthlp` to your Stata ado directory.

3. **Usage in Stata**:
   ```stata
   // Create projects directly from Stata
   mint, type(data) name(medicare_data)
   mint, type(project) name(analysis) path(/projects)
   mint, type(infra) name(tools) nogit

   // Access created project path
   mint, type(data) name(mydata)
   display "`project_path'"
   ```

### Stata Command Reference

```stata
mint, type(string) name(string) [path(string) nogit nodvc bucket(string)]

Options:
  type(string)     - Project type: data, project, infra
  name(string)     - Project name
  path(string)     - Output directory (default: current)
  nogit           - Skip Git initialization
  nodvc           - Skip DVC initialization
  bucket(string)  - Custom DVC bucket name
```

## Configuration Guide

### Initial Setup

Run the interactive configuration:

```bash
mint config setup
```

This will prompt for:
- **Storage Provider**: S3-compatible service (AWS, Wasabi, MinIO)
- **Endpoint**: Service endpoint URL (leave blank for AWS)
- **Region**: AWS region
- **Bucket Prefix**: Prefix for project bucket names
- **Author**: Your name
- **Organization**: Your lab/organization

### Configuration File

Settings are stored in `~/.mint/config.yaml`:

```yaml
storage:
  provider: "s3"
  endpoint: ""           # For non-AWS services
  region: "us-east-1"
  bucket_prefix: "mylab"
  versioning: true

registry:
  url: "https://github.com/cooper-lab/data-commons-registry"

defaults:
  author: "Jane Researcher"
  organization: "Economics Lab"
```

### Manual Configuration

```bash
# Set individual values
mint config setup --set storage.bucket_prefix mylab
mint config setup --set defaults.author "Jane Doe"
mint config setup --set registry.url "https://github.com/your-org/registry"

# Configure storage credentials
mint config setup --set-credentials
```

## S3 Storage Setup

mint supports S3-compatible storage for data versioning with DVC.

### AWS S3

```bash
mint config setup
# Select: AWS S3
# Region: us-east-1 (or your preferred region)
# Bucket prefix: your-lab-name
```

### Wasabi

```bash
mint config setup
# Select: S3-compatible
# Endpoint: https://s3.wasabisys.com
# Region: us-east-1
# Bucket prefix: your-lab-name
```

### MinIO

```bash
mint config setup
# Select: S3-compatible
# Endpoint: https://your-minio-server.com
# Region: us-east-1
# Bucket prefix: your-lab-name
```

### Credentials

Store credentials securely:

```bash
mint config setup --set-credentials
# Enter your access key and secret key
```

Credentials are stored in your system's secure keychain.

## Registry Configuration

### Setting Up Registry Access

Before using registry features, configure your environment:

```bash
# Option 1: Environment variable (recommended for shared environments)
export MINT_REGISTRY_URL=https://github.com/your-org/data-commons-registry

# Option 2: Config file setting
mint config setup --set registry.url https://github.com/your-org/data-commons-registry
```

### Registry Prerequisites

Registry integration requires:
- **SSH Key**: Generate and add to GitHub (`ssh-keygen -t ed25519 -C "your_email@example.com"`)
- **GitHub CLI**: Install and authenticate (`gh auth login`)
- **Repository Access**: Push permissions to the registry repository

### Testing Registry Connection

```bash
# Test registry access
mint registry status test-project

# This will verify your SSH key and gh CLI authentication
```

## Registry Integration

mint integrates with a Data Commons Registry for automatic project cataloging and access control enforcement.

### Prerequisites

Registry integration requires:
- SSH key configured for GitHub
- GitHub CLI (`gh`) installed and authenticated: `gh auth login`
- Push access to the registry repository

### Registry Configuration

```bash
# Set registry URL (required for registration)
mint config setup --set registry.url https://github.com/your-org/data-commons-registry

# Or set via environment variable
export MINT_REGISTRY_URL=https://github.com/your-org/data-commons-registry
```

### Registration Workflow

```bash
# Create project with automatic registration
mint create data --name medicare_data --lang python --register

# Behind the scenes:
# 1. Project scaffolding (Git/DVC setup)
# 2. Clone registry repository via SSH
# 3. Generate catalog entry YAML
# 4. Create feature branch: register-medicare_data
# 5. Commit catalog entry + push branch
# 6. Open PR via GitHub CLI
# 7. Return PR URL to user

# Output:
# ✅ Created: data_medicare_data
# 📋 Registration PR: https://github.com/org/registry/pull/123
```

### Registry Management Commands

```bash
# Register existing projects
mint registry register --path /path/to/project

# Check registration status
mint registry status medicare_data

# Process pending registrations (when offline)
mint registry sync
```

### Registry Features

- **✅ Tokenless Operation**: Uses SSH keys + GitHub CLI instead of personal tokens
- **✅ Offline Mode**: Queues registrations when network unavailable
- **✅ Automatic Retry**: Processes pending registrations on next run
- **✅ PR Tracking**: Provides links to registration pull requests
- **✅ Access Control**: Automatic permission synchronization via GitHub Actions

## Python API

Use mint programmatically in Python:

```python
from mint import create_project

# Create a project (language is now required)
result = create_project(
    project_type="data",
    name="my_analysis",
    language="python",            # Required: "python", "r", or "stata"
    path="/projects",
    init_git=True,
    init_dvc=True,
    bucket_name="custom-bucket",  # Optional
    register_project=True         # Register with Data Commons Registry
)

print(f"Created: {result.full_name}")
print(f"Location: {result.path}")
if result.registration_url:
    print(f"Registration PR: {result.registration_url}")
```

## Development

### Setup Development Environment

```bash
# Clone repository
git clone <repository-url>
cd mint

# Install with development dependencies
uv sync --dev
# or
pip install -e ".[dev]"
```

### Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=mint --cov-report=html

# Run specific test file
pytest tests/test_api.py
```

### Code Quality

```bash
# Linting and formatting
ruff check src/
ruff format src/

# Type checking
mypy src/
```

### Build and Release

```bash
# Build distribution
python -m build

# Upload to PyPI
twine upload dist/*
```

## Troubleshooting

### Git/DVC Not Found

If you see warnings about git/dvc not being available:
- Install Git: https://git-scm.com/
- Install DVC: `pip install dvc`
- Projects will still be created successfully

### S3 Access Issues

- Verify credentials: `mint config setup --set-credentials`
- Check bucket permissions
- Ensure bucket prefix is configured

### Stata Integration Issues

- Verify Stata 16+ with Python support
- Check that mint is installed in Stata's Python environment
- Ensure .ado files are in Stata's ado path

## Contributing

1. Fork the repository
2. Create a feature branch
3. Write tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## License

[License information to be added]

## Support

- **Documentation**: This README and docstrings
- **Issues**: GitHub Issues
- **Discussions**: GitHub Discussions

For questions about specific project types or configurations, please refer to the documentation or open an issue.