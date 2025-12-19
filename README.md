# mint - Lab Project Scaffolding Tool

A comprehensive Python CLI tool that automates the creation of standardized research project repositories with pre-configured version control, data versioning, and multi-language support.

## Features

- 🚀 **Rapid Project Setup**: Create standardized research projects in seconds
- 📊 **Multi-Language Support**: Python, R, and Stata integration
- 🔄 **Version Control**: Automatic Git and DVC initialization
- ☁️ **Cloud Storage**: S3-compatible storage support (AWS, Wasabi, MinIO)
- 📁 **Standardized Structure**: Consistent directory layouts for different project types
- 🔧 **CLI & API**: Command-line interface and Python API
- 📈 **Stata Integration**: Native Stata commands for seamless workflow

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
# Install from PyPI (when published)
pip install mint

# Or install directly from git
pip install git+https://github.com/your-org/mint.git

# Install from source (development)
git clone <repository-url>
cd mint
pip install -e ".[dev]"

# Verify installation
python verify_installation.py
```

### Requirements

- **Python**: 3.9+
- **Optional**: Git, DVC for version control features
- **Stata**: 16+ for Stata integration

## Quick Start

### Basic Usage

```bash
# Create a data processing project
mint create data --name healthcare_analysis

# Create a research analysis project
mint create project --name cost_study

# Create an infrastructure package
mint create infra --name stat_tools
```

### With Custom Options

```bash
# Create in specific directory with custom settings
mint create data --name mydata --path /projects --bucket my-custom-bucket

# Skip version control initialization
mint create project --name analysis --no-git --no-dvc
```

## Project Types

### Data Projects (`data_*`)

For data products and processing pipelines:

```
data_healthcare/
├── README.md                 # Project documentation
├── metadata.json            # Project metadata
├── requirements.txt         # Python dependencies
├── data/
│   ├── raw/                 # Raw data (DVC tracked)
│   ├── intermediate/        # Processed data
│   └── final/               # Final datasets
├── src/
│   ├── ingest.py           # Data acquisition
│   ├── clean.py            # Data cleaning
│   ├── validate.py         # Data validation
│   └── r/                  # R scripts (optional)
├── .gitignore
├── .dvcignore
└── dvc.yaml                # Pipeline configuration
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

## CLI Reference

### Main Commands

```bash
mint --help                    # Show help
mint --version                 # Show version
```

### Project Creation

```bash
mint create data --name <name> [OPTIONS]
mint create project --name <name> [OPTIONS]
mint create infra --name <name> [OPTIONS]

Options:
  -n, --name TEXT       Project name (required)
  -p, --path PATH       Output directory (default: current)
  --no-git             Skip Git initialization
  --no-dvc             Skip DVC initialization
  --bucket TEXT        Custom DVC bucket name
```

### Configuration

```bash
mint config show                    # Show current config
mint config setup                   # Interactive setup
mint config setup --set KEY VALUE  # Set specific value
mint config setup --set-credentials # Set storage credentials
```

## Stata Integration

mint provides seamless integration with Stata 16+ through native Python commands.

### Installation for Stata Users

**Option 1: Automated Installation (Recommended)**
```stata
// Automated installation (installs Stata package + Python package)
mint_installer

// Verify installation
help prjsetup
```

**Option 2: Via Stata's net install**
```stata
// Install Stata package from GitHub
net install mint, from("https://github.com/your-org/mint/raw/main/stata/")

// Install Python package
python: import subprocess; subprocess.run(["pip", "install", "mint"])

// Verify installation
help prjsetup
```

**Option 3: Manual Installation**

1. **Install mint in Stata's Python environment**:
   ```stata
   python: import subprocess; subprocess.run(["pip", "install", "mint"])
   ```

2. **Install Stata files**:
   Copy `stata/prjsetup.ado` and `stata/prjsetup.sthlp` to your Stata ado directory.

3. **Usage in Stata**:
   ```stata
   // Create projects directly from Stata
   prjsetup, type(data) name(medicare_data)
   prjsetup, type(project) name(analysis) path(/projects)
   prjsetup, type(infra) name(tools) nogit

   // Access created project path
   prjsetup, type(data) name(mydata)
   display "`project_path'"
   ```

### Stata Command Reference

```stata
prjsetup, type(string) name(string) [path(string) nogit nodvc bucket(string)]

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

defaults:
  author: "Jane Researcher"
  organization: "Economics Lab"
```

### Manual Configuration

```bash
# Set individual values
mint config setup --set storage.bucket_prefix mylab
mint config setup --set defaults.author "Jane Doe"

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

## Python API

Use mint programmatically in Python:

```python
from mint import create_project

# Create a project
result = create_project(
    project_type="data",
    name="my_analysis",
    path="/projects",
    init_git=True,
    init_dvc=True,
    bucket_name="custom-bucket"  # Optional
)

print(f"Created: {result.full_name}")
print(f"Location: {result.path}")
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