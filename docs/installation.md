# Installation

## Using uv (Recommended)

[uv](https://docs.astral.sh/uv/) is a fast Python package manager that can install `mintd` as a global CLI tool in one command.

```bash
# Install uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install mintd from GitHub
uv tool install git+https://github.com/health-care-affordability-lab/mintd.git
```

This makes the `mintd` command available globally — no clone or virtual environment needed.

## Using pip

```bash
pip install git+https://github.com/health-care-affordability-lab/mintd.git
```

> [!TIP]
> **Windows Users**: Check out our [Windows Setup Guide](windows-setup.md) for native PowerShell and WSL instructions.

## Requirements

### Core Requirements
- **Python**: 3.9+
- **Optional**: Git, DVC for version control features
- **Stata**: 16+ for Stata integration

### Registry Integration (Optional)
- **SSH Key**: Configured for GitHub (`ssh-keygen -t ed25519 -C "your_email@example.com"`)
- **GitHub CLI**: Installed and authenticated (`gh auth login`)
- **Registry Access**: Push permissions to the Data Commons Registry repository

## Next Steps

After installation, proceed to the [Configuration Guide](configuration.md) to set up your environment.

For contributing or developing mintd itself, see the [Development Guide](development.md).

**Source Code**: [github.com/health-care-affordability-lab/mintd](https://github.com/health-care-affordability-lab/mintd)
