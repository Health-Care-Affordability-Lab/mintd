# Stata Integration

mintd provides seamless integration with Stata 16+ through native Python commands.

## Cross-Platform Stata Detection

mintd automatically detects Stata executables on Windows, macOS, and Linux:

- **macOS/Linux**: Searches PATH for `stata-mp` or `stata` using `shutil.which()`
- **Windows**: Uses `where.exe` to find Stata installations, including common Program Files paths
- **Fallback**: Allows manual override in `~/.mintd/config.yaml`

**Automatic Detection Examples:**
```bash
# macOS/Linux - detects stata-mp
$ mintd create data --name analysis --lang stata
✅ Stata detected: stata-mp

# Windows - detects Stata installation
> mintd create data --name analysis --lang stata
✅ Stata detected: C:\Program Files\Stata18\StataMP-64.exe
```

**Manual Configuration:**
```yaml
# ~/.mintd/config.yaml
tools:
  stata:
    executable: "/custom/path/to/stata"  # Override auto-detection
    detected_path: "stata-mp"            # Last successful detection
```

## Platform-Aware Script Execution

Scripts run from the `code/` directory with platform-appropriate commands:

**macOS/Linux:**
```yaml
cmd: cd code && stata-mp -b do ingest.do
```

**Windows:**
```yaml
cmd: cd code & stata-mp -b do ingest.do
```

This ensures consistent path handling regardless of platform, with Stata scripts using relative paths (`../data/` for data directories).

## Installation for Stata Users

**Option 1: Automated Installation (Recommended)**
```stata
// Automated installation (installs Stata package + Python package)
mintd_installer

// Verify installation
help mintd
```

**Option 2: Via Stata's net install**
```stata
// Install Stata package from GitHub (may not work if repository is private)
net install mintd, from("https://github.com/Cooper-lab/mint/raw/main/stata/")

// If net install fails, use the automated installer instead:
mintd_installer, github

// Install Python package
python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])

// Verify installation
help mintd
```

**Option 3: Manual Installation**

1. **Install mintd in Stata's Python environment**:
   ```stata
   python: import subprocess; subprocess.run(["pip", "install", "git+https://github.com/Cooper-lab/mint.git"])
   ```

2. **Install Stata files**:
   Copy `stata/mintd.ado` and `stata/mintd.sthlp` to your Stata ado directory.

3. **Usage in Stata**:
   ```stata
   // Create projects directly from Stata
   mintd, type(data) name(hospital_project)
   mintd, type(project) name(analysis) path(/projects)
   mintd, type(infra) name(tools) nogit

   // Access created project path
   mintd, type(data) name(mydata)
   display "`project_path'"
   ```

## Stata Command Reference

```stata
mintd, type(string) name(string) [path(string) nogit nodvc bucket(string)]

Options:
  type(string)     - Project type: data, project, infra
  name(string)     - Project name
  path(string)     - Output directory (default: current)
  nogit           - Skip Git initialization
  nodvc           - Skip DVC initialization
  bucket(string)  - Custom DVC bucket name
```
