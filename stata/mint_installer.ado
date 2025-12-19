*! version 1.0.0
*! mint - Automated Installation Script
*! Handles both Stata package and Python package installation

program define mint_installer
    version 16.0
    syntax [anything] [, FORCE REPLACE FROM(string) PYTHONPATH(string) NOVENV GITHUB]

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

        # Installation logic: PyPI first, then local source if available
        installed_successfully = False

        # Determine installation path (only needed for local installation)
        mint_path = None
        if "`github'" != "":
            # Clone from GitHub
            import tempfile
            temp_dir = tempfile.mkdtemp()
            mint_path = os.path.join(temp_dir, "mint")
            SFIToolkit.displayln("{text}Cloning mint from GitHub...{reset}")
            SFIToolkit.displayln(f"{{text}}Clone destination: {mint_path}{{reset}}")

            clone_result = subprocess.run(["git", "clone", "https://github.com/Cooper-lab/mint.git", mint_path],
                                        capture_output=True, text=True, timeout=120)

            if clone_result.returncode != 0:
                SFIToolkit.displayln(f"{{error}}Git clone failed: {clone_result.stderr}{{reset}}")
                SFIToolkit.displayln("{text}Continuing with PyPI installation only...{reset}")
                mint_path = None
            elif not os.path.exists(os.path.join(mint_path, "pyproject.toml")):
                SFIToolkit.displayln(f"{{error}}Cloned repository missing pyproject.toml{reset}")
                SFIToolkit.displayln("{text}Continuing with PyPI installation only...{reset}")
                mint_path = None
            else:
                SFIToolkit.displayln("{result}✓ Successfully cloned mint from GitHub{reset}")
        elif pythonpath:
            mint_path = pythonpath
            if not os.path.exists(os.path.join(mint_path, "pyproject.toml")):
                SFIToolkit.displayln(f"{{error}}pyproject.toml not found in specified path: {mint_path}{{reset}}")
                SFIToolkit.displayln("{text}Continuing with PyPI installation only...{reset}")
                mint_path = None
        else:
            # Try to find local mint source based on common locations
            try:
                # Try common locations where mint might be installed
                alt_paths = [
                    os.path.join(os.path.expanduser("~"), "mint"),  # ~/mint
                    os.path.join(os.path.expanduser("~"), "projects", "mint"),  # ~/projects/mint
                    os.path.join(os.path.expanduser("~"), "git", "mint"),  # ~/git/mint
                    "/opt/mint",  # System location
                    "/usr/local/mint",  # Another system location
                ]

                potential_mint_path = None
                for path in alt_paths:
                    if os.path.exists(os.path.join(path, "pyproject.toml")):
                        potential_mint_path = path
                        break

                if potential_mint_path:

                if os.path.exists(os.path.join(potential_mint_path, "pyproject.toml")):
                    mint_path = potential_mint_path
                    SFIToolkit.displayln(f"{{text}}Found local mint source at: {mint_path}{{reset}}")
                else:
                    SFIToolkit.displayln(f"{{text}}Local mint source not found at: {potential_mint_path}{{reset}}")
                    SFIToolkit.displayln("{text}Continuing with PyPI installation only...{reset}")
            except Exception as e:
                SFIToolkit.displayln(f"{{text}}Could not determine Stata paths: {e}{{reset}}")
                SFIToolkit.displayln("{text}Continuing with PyPI installation only...{reset}")

        # Check if virtual environment should be used (only if we have local source)
        use_venv = False
        if use_venv and not mint_path:
            SFIToolkit.displayln("{text}Virtual environment requested but local source not available. Using direct installation.{reset}")
            use_venv = False 

        if use_venv and mint_path:
            # Create virtual environment for mint
            venv_path = os.path.join(mint_path, ".mint_venv")
            SFIToolkit.displayln(f"{{text}}Creating virtual environment at: {venv_path}{{reset}}")

            # Create virtual environment
            venv_result = subprocess.run([sys.executable, "-m", "venv", venv_path],
                                       capture_output=True, text=True, timeout=30)

            if venv_result.returncode != 0:
                SFIToolkit.displayln(f"{{error}}Failed to create virtual environment: {venv_result.stderr}{{reset}}")
                raise RuntimeError(f"Virtual environment creation failed. Try using 'mint_installer, novenv' to install directly. Error: {venv_result.stderr}")
            else:
                # Install into virtual environment
                pip_exe = os.path.join(venv_path, "bin", "pip") if os.name != 'nt' else os.path.join(venv_path, "Scripts", "pip.exe")

                SFIToolkit.displayln("{text}Installing mint into virtual environment...{reset}")
                result = subprocess.run([pip_exe, "install", "-e", mint_path],
                                      capture_output=True, text=True, timeout=120)
        else:
            # Direct installation without virtual environment
            # Try PyPI first
            SFIToolkit.displayln("{text}Trying PyPI installation...{reset}")
            result = subprocess.run([sys.executable, "-m", "pip", "install", "mint"],
                                  capture_output=True, text=True, timeout=60)

            # If PyPI fails and we have local source, try local installation
            if result.returncode != 0 and mint_path:
                SFIToolkit.displayln("{text}PyPI installation failed, trying local installation...{reset}")
                SFIToolkit.displayln(f"{{text}}Installing from local path: {mint_path}{{reset}}")
                result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", mint_path],
                                      capture_output=True, text=True, timeout=120)
            elif result.returncode != 0 and not mint_path:
                SFIToolkit.displayln("{error}PyPI installation failed and no local source found.{reset}")
                SFIToolkit.displayln("{text}To install from the development version:{reset}")
                SFIToolkit.displayln("{text}1. Clone the repository: git clone https://github.com/Cooper-lab/mint.git{reset}")
                SFIToolkit.displayln("{text}2. Run: mint_installer, pythonpath(\"/path/to/cloned/mint\"){reset}")
                SFIToolkit.displayln("{text}{reset}")
                SFIToolkit.displayln("{text}Or install the stable version from PyPI when available.{reset}")
                raise RuntimeError("Installation failed: No local source available and PyPI installation failed")

        # Test import - handle both virtual environment and direct installation
        try:
            if use_venv:
                # Add virtual environment to Python path for import testing
                venv_site_packages = os.path.join(venv_path, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
                if venv_site_packages not in sys.path:
                    sys.path.insert(0, venv_site_packages)

            # Also add the mint source directory to path (if available)
            if mint_path and mint_path not in sys.path:
                sys.path.insert(0, mint_path)

            import mint
            if use_venv:
                SFIToolkit.displayln("{result}✓ Python package installed successfully in virtual environment{reset}")
                SFIToolkit.displayln(f"{{text}}Virtual environment: {venv_path}{{reset}}")
            else:
                SFIToolkit.displayln("{result}✓ Python package installed successfully (direct installation){reset}")
            SFIToolkit.displayln(f"{{text}}Version: {mint.__version__}{{reset}}")
            installed_successfully = True

        except ImportError as e:
            install_type = "virtual environment" if use_venv else "direct installation"
            SFIToolkit.displayln(f"{{error}}{install_type.title()} installation completed but import failed: {e}{{reset}}")
            raise ImportError(f"Installation succeeded but mint module cannot be imported. Install type: {install_type}")

        if not installed_successfully:
            raise RuntimeError("Failed to install mint Python package")

    except Exception as e:
        SFIToolkit.errprintln(f"Error installing Python package: {e}")
        SFIToolkit.errprintln("")
        SFIToolkit.errprintln("Manual installation:")
        SFIToolkit.errprintln("python: import subprocess; subprocess.run(['pip', 'install', 'mint'])")
        SFIToolkit.errprintln("or")
        SFIToolkit.errprintln("python: import subprocess, os; ado_path = SFIToolkit.getStringLocal('c(sysdir_plus)'); mint_path = os.path.dirname(ado_path); subprocess.run(['pip', 'install', '-e', mint_path])")
        raise
end