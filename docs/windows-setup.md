# Windows Setup Guide

Mintd is designed to be cross-platform, but Windows environments can present unique challenges. This guide offers two primary ways to set up your environment: **Windows Subsystem for Linux (WSL)** (Recommended for advanced users) and **Native Windows**.

## Option 1: Windows Subsystem for Linux (WSL)

WSL allows you to run a full Linux environment directly on Windows, unmodified. This is the recommended approach for data science workflows requiring compatibility with Linux-based HPC clusters.

### 1. Install WSL

Open PowerShell as Administrator and run:

```powershell
wsl --install
```

Restart your computer when prompted. This will install Ubuntu by default.

### 2. Install Dependencies (Inside WSL)

Open your new Ubuntu terminal and run:

```bash
# Update package lists
sudo apt update && sudo apt upgrade -y

# Install Python and Git
sudo apt install python3 python3-pip git -y

# Install uv (Recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 3. Install Mintd

```bash
git clone <repository-url>
cd mintd
uv sync --dev
```

You can now use `mintd` exactly as documented in the main guide.

---

## Option 2: Native Windows (PowerShell)

You can run `mintd` directly in PowerShell. This is fully supported, though you should rely on Python-based commands rather than shell scripts.

### 1. Prerequisites

- **Python 3.9+**: Install from [python.org](https://www.python.org/downloads/windows/) or the Microsoft Store.
  - *Ensure you check "Add Python to PATH" during installation.*
- **Git for Windows**: Install from [git-scm.com](https://git-scm.com/download/win).
- **PowerShell**: Make sure you have a modern version (PowerShell 7+ recommended).

### 2. Install Mintd

You can install `mintd` directly from GitHub using pip (simplest), or for development, you can use `uv`.

**Option A: Simple Install (Recommended for Users)**
```powershell
pip install git+https://github.com/Cooper-lab/mint.git
```

**Option B: Developer Install (for contributing code)**
```powershell
# Install uv
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Clone and sync
git clone <repository-url>
cd mintd
uv sync --dev
```

### 3. Windows-Specific Usage Tips

#### Enclave Workflows
Enclave projects generate helper scripts in `scripts/`. These represent Bash scripts which will not run natively on Windows. However, `mintd` includes a cross-platform Python CLI for all enclave operations.

**Instead of:**
```bash
./scripts/pull_data.sh --all
./scripts/package_transfer.sh
```

**Use:**
```powershell
python enclave_cli.py pull --all
python enclave_cli.py package
```

#### Path Separators
Mintd handles paths automatically, but when manually referencing files, remember to use backslashes (`\`) or quote your paths if they contain spaces.

#### Execution Policy
If you encounter permission errors running scripts, you may need to adjust your execution policy:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
