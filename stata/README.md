# Stata Integration for mint

This directory contains Stata integration files for the mint lab project scaffolding tool.

## Installation

### Option 1: Automated Installation (Recommended)

The easiest way to install mint for Stata is using the automated installer, which handles both the Stata package and Python package installation:

```stata
// Automated installation (installs everything)
mint_installer

// Or force reinstallation
mint_installer, force

// Verify installation
help prjsetup

// Test the installation
prjsetup, type(data) name(test_install)
```

### Option 2: Manual Installation via net install

If you prefer manual installation, you can use Stata's built-in package manager:

```stata
// Install Stata package from GitHub
net install mint, from("https://github.com/your-org/mint/raw/main/stata/")

// Install Python package (choose one method)
python: import subprocess; subprocess.run(["pip", "install", "mint"])
python: import subprocess; subprocess.run(["pip", "install", "-e", "/path/to/mint"])

// Verify installation
help prjsetup

// Test the installation
prjsetup, type(data) name(test_install)
```

**Note:** Replace `your-org` with the actual GitHub organization/user name where the mint repository is hosted.

### Option 2: Manual Installation

If the above doesn't work, you can install manually:

1. **Download the Stata files:**
   - `prjsetup.ado`
   - `prjsetup.sthlp`

2. **Install in Stata's personal ado directory:**
   ```bash
   # macOS
   cp prjsetup.ado ~/Library/Application\ Support/Stata/ado/personal/
   cp prjsetup.sthlp ~/Library/Application\ Support/Stata/ado/personal/

   # Windows
   copy prjsetup.ado "%USERPROFILE%\Documents\Stata\ado\personal\"
   copy prjsetup.sthlp "%USERPROFILE%\Documents\Stata\ado\personal\"

   # Linux
   cp prjsetup.ado ~/ado/personal/
   cp prjsetup.sthlp ~/ado/personal/
   ```

3. **Restart Stata** after copying the files to ensure they're recognized.

3. **Install the mint Python package:**
   ```stata
   // Install from PyPI (when available)
   python: import subprocess; subprocess.run(["pip", "install", "mint"])

   // Or for development/testing
   python: import subprocess; subprocess.run(["pip", "install", "-e", "/path/to/mint"])
   ```

## Features

### Automatic Python Package Installation

The `prjsetup` command includes automatic installation of the Python `mint` package. If the Python package is not found when you run a command, `prjsetup` will:

1. First try to install from PyPI: `pip install mint`
2. If that fails, try to install from the local source directory
3. Provide clear error messages and manual installation instructions if both methods fail

This means that in most cases, you only need to install the Stata package - the Python package will be installed automatically when needed.

### Troubleshooting

#### "command prjsetup not found"
- Ensure the `.ado` file is in your Stata ado path
- Try restarting Stata
- Check that the file wasn't corrupted during download

### "mint package not installed"
- Verify that Python integration is working in Stata: `python: print("Hello")`
- Check that pip is available in Stata's Python environment
- Try installing mint manually: `python: import subprocess; subprocess.run(["pip", "install", "mint"])`

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
prjsetup, type(data) name(test_install)

// Check that project_path macro is set
display "`project_path'"

// Verify Python integration
python: import mint; print("mint version:", mint.__version__)
```

2. **Install the Stata files:**
   Copy `prjsetup.ado` and `prjsetup.sthlp` to your Stata personal ado directory.

   **On macOS:**
   ```bash
   cp prjsetup.ado ~/Library/Application\ Support/Stata/ado/personal/
   cp prjsetup.sthlp ~/Library/Application\ Support/Stata/ado/personal/
   ```

   **On Windows:**
   ```cmd
   copy prjsetup.ado "%USERPROFILE%\Documents\Stata\ado\personal\"
   copy prjsetup.sthlp "%USERPROFILE%\Documents\Stata\ado\personal\"
   ```

   **On Linux:**
   ```bash
   cp prjsetup.ado ~/ado/personal/
   cp prjsetup.sthlp ~/ado/personal/
   ```

## Usage

Once installed, you can create projects directly from Stata:

```stata
// Create a data repository
prjsetup, type(data) name(medicare_claims)

// Create a research project
prjsetup, type(project) name(hospital_closures)

// Create in a specific location
prjsetup, type(data) name(mydata) path(/path/to/projects)

// Skip git/dvc initialization
prjsetup, type(infra) name(mypackage) nogit nodvc

// Use custom DVC bucket
prjsetup, type(data) name(mydata) bucket(my-custom-bucket)

// Access the created project path
prjsetup, type(project) name(analysis)
display "`project_path'"
```

## Requirements

- **Stata 16+** with Python integration enabled
- **Python** with the mint package installed
- **Git** and **DVC** (optional, for version control)

## Help

For help within Stata:

```stata
help prjsetup
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