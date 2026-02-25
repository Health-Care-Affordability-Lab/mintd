# Development

## Setup Development Environment

```bash
# Clone repository
git clone <repository-url>
cd mintd

# Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install with development dependencies
uv sync --dev
# or
pip install -e ".[dev]"
```

## Project Structure

```
mintd/
├── src/mintd/           # Main package
│   ├── cli/             # CLI commands (Click-based)
│   ├── files/           # Template files (.j2 Jinja2 templates)
│   ├── templates.py     # Template handling
│   ├── registry.py      # Data Commons Registry integration
│   └── ...
├── tests/               # Test suite
├── docs/                # Documentation (MkDocs)
├── stata/               # Stata ado files
└── pyproject.toml       # Project configuration
```

## Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=mintd

# Run specific test file
pytest tests/test_templates.py

# Run with verbose output
pytest -v
```

## Code Quality

```bash
# Lint with ruff
ruff check src/

# Format with ruff
ruff format src/

# Type check with mypy
mypy src/mintd/
```

## Building Documentation

```bash
# Install docs dependencies
pip install -e ".[docs]"

# Serve docs locally
mkdocs serve

# Build docs
mkdocs build
```

## Dependencies

| Package | Purpose |
|---------|---------|
| `click>=8.0` | CLI framework |
| `gitpython>=3.1` | Git operations |
| `jinja2>=3.0` | Template rendering |
| `rich>=13.0` | Terminal output formatting |
| `keyring>=24.0` | Secure credential storage |
| `boto3>=1.28` | S3-compatible storage |
| `pyyaml>=6.0` | YAML configuration |
| `dvc>=3.0` | Data Version Control |
| `pygithub>=2.0.0` | GitHub API integration |
| `requests>=2.25.0` | HTTP client |

## Development Dependencies

| Package | Purpose |
|---------|---------|
| `pytest>=7.0` | Testing framework |
| `ruff>=0.1` | Linting and formatting |
| `mypy>=1.0` | Static type checking |

## Adding New Templates

1. Create Jinja2 template in `src/mintd/files/`
2. Register in appropriate template class (`DataTemplate`, `ProjectTemplate`, etc.)
3. Add tests in `tests/test_templates.py`

## Adding New CLI Commands

1. Create command module in `src/mintd/cli/`
2. Register with Click group in `src/mintd/cli/__init__.py`
3. Add tests in `tests/test_cli.py`
