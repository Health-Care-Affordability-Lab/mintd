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
pip install git+https://github.com/health-care-affordability-lab/mintd.git
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

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 4. Troubleshooting

#### "The term 'mintd' is not recognized"
This usually means the Python scripts folder is not in your system PATH. You have two options:

**Option A: Run as a Python Module (Easiest)**
You can always run mintd commands by adding `python -m` before them. This bypasses PATH issues entirely.

```powershell
# Correct usage:
python -m mintd create data --name my_study --lang python
```

**Option B: Use the executable explicitly**
If the command `mintd` isn't found, try adding the `.exe` extension:

```powershell
mintd.exe create data --name my_study --lang python
```

**Option C: Fix your PATH (GUI)**
1.  Search Windows for "Edit the system environment variables".
2.  Click **Environment Variables**.
3.  Under "User variables", edit **Path**.
4.  Add the path shown in the warning message during install (e.g., `C:\Users\YourName\AppData\Roaming\Python\Python312\Scripts`).
5.  Restart PowerShell.

**Option C: Fix your PATH (PowerShell Command)**
You can run this command to verify the path and permanently add it. Replace the path below with the one from your warning message:

```powershell
# Automatically find the Python User Scripts path
$ScriptPath = python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"

if (-not $ScriptPath) {
    Write-Error "Could not determine Python script path. Ensure Python is installed."
} else {
    Write-Host "Found Python Scripts at: $ScriptPath"

    # 1. Add to current session
    $env:PATH += ";$ScriptPath"

    # 2. Add permanently
    $CurrentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($CurrentPath -notlike "*$ScriptPath*") {
        [Environment]::SetEnvironmentVariable("Path", $CurrentPath + ";$ScriptPath", "User")
        Write-Host "Path updated! Restart your terminal for changes to take full effect."
    } else {
        Write-Host "Path is already correctly configured."
    }
}
```

