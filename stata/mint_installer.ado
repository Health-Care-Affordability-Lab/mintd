*! version 1.0.0
*! mint - Automated Installation Script
*! Handles both Stata package and Python package installation

program define mint_installer
    version 16.0
    syntax [anything] [, FORCE REPLACE FROM(string) PYTHONPATH(string)]

    display as text ""
    display as text "mint - Lab Project Scaffolding Tool Installer"
    display as text "=========================================="

    * Check if already installed
    capture which mint
    local already_installed = (_rc == 0)

    if `already_installed' & "`force'" == "" {
        display as text "mint Stata package appears to be already installed."
        display as text "Use 'mint_installer, force' to reinstall."
        exit
    }

    * Set default installation source
    if "`from'" == "" {
        local from "https://github.com/Cooper-lab/mint/raw/main/stata/"
    }

    display as text "Step 1: Installing Stata package from `from'"

    * Install Stata package
    capture net install mint, from("`from'") `replace'
    if _rc != 0 {
        display as error "Failed to install Stata package from `from'"
        display as error "Error code: " _rc
        exit _rc
    }

    display as result "✓ Stata package installed successfully"

    display as text ""
    display as text "Step 2: Installing Python package"

    * Install Python package
    python: _mint_install_python("`pythonpath'")

    display as text ""
    display as result "✓ Installation complete!"
    display as text ""
    display as text "Usage:"
    display as text "  mint, type(data) name(myproject)"
    display as text "  mint, type(project) name(analysis)"
    display as text "  mint, type(infra) name(package)"
    display as text ""
    display as text "For help: help mint"

end

python:
def _mint_install_python(pythonpath):
    """Install the mint Python package."""
    from sfi import SFIToolkit
    import subprocess
    import sys
    import os

    try:
        # Check if mint is already available
        try:
            import mint
            SFIToolkit.displayln("{result}✓ Python package 'mint' is already installed{reset}")
            return
        except ImportError:
            pass

        SFIToolkit.displayln("{text}Installing Python package 'mint'...{reset}")

        # If pythonpath is provided, use it
        if pythonpath:
            mint_path = pythonpath
            if os.path.exists(os.path.join(mint_path, "pyproject.toml")):
                SFIToolkit.displayln(f"{{text}}Installing from specified path: {mint_path}{{reset}}")
                result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", mint_path],
                                      capture_output=True, text=True, timeout=120)
            else:
                raise FileNotFoundError(f"pyproject.toml not found in {mint_path}")
        else:
            # Try PyPI first
            try:
                SFIToolkit.displayln("{text}Trying PyPI installation...{reset}")
                result = subprocess.run([sys.executable, "-m", "pip", "install", "mint"],
                                      capture_output=True, text=True, timeout=60)

                if result.returncode != 0:
                    raise subprocess.SubprocessError("PyPI installation failed")

            except subprocess.SubprocessError:
                # Try local installation based on Stata ado path
                ado_path = SFIToolkit.getStringLocal("c(sysdir_plus)")
                mint_path = os.path.dirname(ado_path)  # Go up one level from PLUS to find mint

                if os.path.exists(os.path.join(mint_path, "pyproject.toml")):
                    SFIToolkit.displayln("{text}PyPI installation failed, trying local installation...{reset}")
                    result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", mint_path],
                                          capture_output=True, text=True, timeout=120)
                else:
                    raise FileNotFoundError("Could not find mint package locally or on PyPI")

        if result.returncode == 0:
            SFIToolkit.displayln("{result}✓ Python package installed successfully{reset}")

            # Verify installation
            try:
                import mint
                SFIToolkit.displayln(f"{{text}}Version: {mint.__version__}{{reset}}")
            except ImportError:
                raise ImportError("Installation completed but import failed")

        else:
            SFIToolkit.displayln("{error}Installation failed{reset}")
            if result.stdout:
                SFIToolkit.displayln(f"stdout: {result.stdout}")
            if result.stderr:
                SFIToolkit.displayln(f"stderr: {result.stderr}")
            raise subprocess.SubprocessError("Installation failed")

    except Exception as e:
        SFIToolkit.errprintln(f"Error installing Python package: {e}")
        SFIToolkit.errprintln("")
        SFIToolkit.errprintln("Manual installation:")
        SFIToolkit.errprintln("python: import subprocess; subprocess.run(['pip', 'install', 'mint'])")
        SFIToolkit.errprintln("or")
        SFIToolkit.errprintln("python: import subprocess, os; ado_path = SFIToolkit.getStringLocal('c(sysdir_plus)'); mint_path = os.path.dirname(ado_path); subprocess.run(['pip', 'install', '-e', mint_path])")
        raise
end