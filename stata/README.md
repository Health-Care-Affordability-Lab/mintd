# Stata Integration for mint

This directory contains Stata integration files for the mint lab project scaffolding tool.

## Installation

### Option 1: Automated Installation (Recommended)

The easiest way to install mint for Stata is using the automated installer, which handles both the Stata package and Python package installation:

```stata
// Automated installation with virtual environment (recommended)
mint_installer

// Force reinstallation
mint_installer, force

// Install directly without virtual environment (use if venv fails)
mint_installer, novenv

// Install from specific local source path
mint_installer, pythonpath("/path/to/mint/source")

// Install latest development version from GitHub
mint_installer, github

// Verify installation
help mint

// Test the installation
mint, type(data) name(test_install)
```

### Option 2: Manual Installation via net install

If you prefer manual installation, you can use Stata's built-in package manager:

```stata
// Install Stata package from GitHub
net install mint, from("https://github.com/Cooper-lab/mint/raw/main/stata/")

// Install Python package (choose one method)
python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])
python: import subprocess; subprocess.run(["pip", "install", "-e", "/path/to/mint"])

// Verify installation
help mint

// Test the installation
mint, type(data) name(test_install)
```

**Note:** The repository is hosted at `Cooper-lab/mint` on GitHub.

### Option 2: Manual Installation

If the above doesn't work, you can install manually:

1. **Download the Stata files:**
   - `mint.ado`
   - `mint.sthlp`

2. **Install in Stata's personal ado directory:**
   ```bash
   # macOS
cp mint.ado ~/Library/Application\ Support/Stata/ado/personal/
cp mint.sthlp ~/Library/Application\ Support/Stata/ado/personal/

   # Windows
copy mint.ado "%USERPROFILE%\Documents\Stata\ado\personal\"
copy mint.sthlp "%USERPROFILE%\Documents\Stata\ado\personal\"

   # Linux
cp mint.ado ~/ado/personal/
cp mint.sthlp ~/ado/personal/
   ```

3. **Restart Stata** after copying the files to ensure they're recognized.

3. **Install the mint Python package:**
   ```stata
   // Install from GitHub (recommended)
   python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])

   // Or for development/testing
   python: import subprocess; subprocess.run(["pip", "install", "-e", "/path/to/mint"])
   ```

## Features

### Virtual Environment Isolation

By default, the automated installer creates a dedicated virtual environment (`.mint_venv/`) for the mint Python package, ensuring clean isolation from other Python packages and avoiding dependency conflicts. If virtual environment creation fails, the installer will throw an error - use the `novenv` option to install directly instead.

### Finding the Python Path

The `pythonpath()` option specifies the **local path** to the mint source code on your machine (not a GitHub URL). You only need this if automatic detection fails.

**To find the correct path:**

1. **If you cloned the repository:**
   ```bash
   # Use the path to your cloned mint repository
   mint_installer, pythonpath("/Users/username/projects/mint")
   ```

2. **Requirements:**
   - Must be a local directory path (not a URL)
   - Must contain a `pyproject.toml` file
   - The installer will run `pip install -e` from this directory

3. **Automatic detection:**
   The installer automatically tries to find the mint source relative to your Stata installation. You only need `pythonpath()` if automatic detection fails.

4. **Don't have the source locally?**
   **Option A: Automatic GitHub cloning**
   ```stata
   // Automatically clone and install from GitHub
   mint_installer, github
   ```

   **Option B: Manual cloning**
   ```bash
   # Clone the repository first
   git clone https://github.com/Cooper-lab/mint.git
   cd mint

   # Then run the installer with the path
   # (in Stata)
   mint_installer, pythonpath("/path/to/cloned/mint")
   ```

### Automatic Python Package Installation

The `mint` command includes automatic installation of the Python `mint` package. If the Python package is not found when you run a command, `mint` will:

1. First try to install from local source or GitHub
2. If that fails, try to install from PyPI: `pip install mint`
3. Provide clear error messages and manual installation instructions if both methods fail

This means that in most cases, you only need to install the Stata package - the Python package will be installed automatically when needed.

### Troubleshooting

#### "command mint not found"
- Ensure the `.ado` file is in your Stata ado path
- Try restarting Stata
- Check that the file wasn't corrupted during download

### "mint package not installed"
- Verify that Python integration is working in Stata: `python: print("Hello")`
- Check that pip is available in Stata's Python environment
- Try installing mint manually: `python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])`

### Permission issues
- On macOS, you may need to create the directory first:
  ```bash
  mkdir -p ~/Library/Application\ Support/Stata/ado/personal/
  ```
- Ensure you have write permissions to the ado directory

### Network issues with net install
- If `net install` fails, use manual installation
- Check your internet connection
- Verify the GitHub URL is correct

### Testing the installation
```stata
// Test basic functionality
mint, type(data) name(test_install)

// Check that project_path macro is set
display "`project_path'"

// Verify Python integration
python: import mint; print("mint version:", mint.__version__)
```

2. **Install the Stata files:**
   Copy `mint.ado` and `mint.sthlp` to your Stata personal ado directory.

   **On macOS:**
   ```bash
   cp mint.ado ~/Library/Application\ Support/Stata/ado/personal/
   cp mint.sthlp ~/Library/Application\ Support/Stata/ado/personal/
   ```

   **On Windows:**
   ```cmd
   copy mint.ado "%USERPROFILE%\Documents\Stata\ado\personal\"
   copy mint.sthlp "%USERPROFILE%\Documents\Stata\ado\personal\"
   ```

   **On Linux:**
   ```bash
   cp mint.ado ~/ado/personal/
   cp mint.sthlp ~/ado/personal/
   ```

## Usage

Once installed, you can create projects directly from Stata:

```stata
// Create a data repository
mint, type(data) name(medicare_claims)

// Create a research project
mint, type(project) name(hospital_closures)

// Create in a specific location
mint, type(data) name(mydata) path(/path/to/projects)

// Skip git/dvc initialization
mint, type(infra) name(mypackage) nogit nodvc

// Use custom DVC bucket
mint, type(data) name(mydata) bucket(my-custom-bucket)

// Access the created project path
mint, type(project) name(analysis)
display "`project_path'"
```

## Requirements

- **Stata 16+** with Python integration enabled
- **Python** with the mint package installed
- **Git** and **DVC** (optional, for version control)

## Help

For help within Stata:

```stata
help mint
```

## Troubleshooting

**"mint package not installed" error:**
- Ensure mint is installed in Stata's Python environment
- Check that Python integration is properly configured in Stata

**"command not found" error:**
- Verify the .ado files are in your Stata ado path
- Try restarting Stata after installation

**Permission issues:**
- On macOS, you may need to create the personal ado directory first:
  ```bash
  mkdir -p ~/Library/Application\ Support/Stata/ado/personal/
  ```