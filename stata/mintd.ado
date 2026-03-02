*! version 1.0.0
*! mintd - Lab Project Scaffolding Tool
*! Native Python integration for Stata 16+

program define mintd
    version 16.0
    syntax, Type(string) Name(string) [Path(string) NOGit NODvc Bucket(string)]

    * Validate type
    if !inlist("`type'", "data", "project", "prj") {
        display as error "Invalid type. Use: data or project"
        exit 198
    }

    * Normalize "prj" to "project"
    if "`type'" == "prj" {
        local type "project"
    }

    * Set default path to current directory
    if "`path'" == "" {
        local path "`c(pwd)'"
    }

    * Convert Stata options to Python values
    local py_nogit = cond("`nogit'" != "", "True", "False")
    local py_nodvc = cond("`nodvc'" != "", "True", "False")
    local py_bucket = cond("`bucket'" != "", "`bucket'", "None")

    display as text "Creating project..."

    * Call Python directly using native integration
    python: _prjsetup_create("`type'", "`name'", "`path'", `py_nogit', `py_nodvc', "`py_bucket'")

end

python:
def _prjsetup_create(project_type, name, path, no_git, no_dvc, bucket):
    """Create project using mintd Python package."""
    from sfi import Macro, SFIToolkit

    try:
        from mintd import create_project

        # Convert string booleans to Python booleans
        init_git = no_git != "True"
        init_dvc = no_dvc != "True"

        # Handle bucket parameter
        bucket_name = bucket if bucket != "None" else None

        result = create_project(
            project_type=project_type,
            name=name,
            path=path,
            init_git=init_git,
            init_dvc=init_dvc,
            bucket_name=bucket_name
        )

        SFIToolkit.displayln("{result}Project created: " + result.full_name + "{reset}")
        SFIToolkit.displayln("{text}Location: " + str(result.path) + "{reset}")

        # Store result path in Stata macro for programmatic use
        Macro.setLocal("project_path", str(result.path))

    except ImportError:
        SFIToolkit.displayln("{text}mintd package not found. Attempting automatic installation...{reset}")

        # Try to install mintd automatically
        try:
            import subprocess
            import sys
            import os

            # Try installing from GitHub or local source
            try:
                # First try GitHub installation
                SFIToolkit.displayln("{text}Trying to install mintd from GitHub...{reset}")
                result = subprocess.run([sys.executable, "-m", "pip", "install", "git+https://github.com/Cooper-lab/mint.git"],
                                      capture_output=True, text=True, timeout=120)

                if result.returncode == 0:
                    SFIToolkit.displayln("{result}Successfully installed mintd from GitHub!{reset}")
                    # Now try importing again
                    from mintd import create_project
                else:
                    raise subprocess.SubprocessError(f"GitHub installation failed: {result.stderr}")

            except (subprocess.SubprocessError, subprocess.TimeoutExpired):
                # If GitHub installation fails, try local installation
                # Get the path to the Stata ado directory to find the mint source
                ado_path = SFIToolkit.getStringLocal("c(sysdir_plus)")
                mint_path = os.path.join(os.path.dirname(ado_path), "mintd")

                if os.path.exists(os.path.join(mint_path, "pyproject.toml")):
                    SFIToolkit.displayln("{text}GitHub installation failed. Trying local source...{reset}")
                    result = subprocess.run([sys.executable, "-m", "pip", "install", "-e", mint_path],
                                          capture_output=True, text=True, timeout=120)

                    if result.returncode == 0:
                        SFIToolkit.displayln("{result}Successfully installed mintd from local source!{reset}")
                        from mintd import create_project
                    else:
                        raise subprocess.SubprocessError(f"Local installation failed: {result.stderr}")
                else:
                    raise ImportError("Could not find mintd package locally or on GitHub")

        except Exception as install_error:
            SFIToolkit.errprintln("Error: Failed to automatically install mintd package.")
            SFIToolkit.errprintln(f"Installation error: {install_error}")
            SFIToolkit.errprintln("")
            SFIToolkit.errprintln("Manual installation options:")
            SFIToolkit.errprintln("1. From GitHub: python: import subprocess; subprocess.run(['pip', 'install', 'git+https://github.com/Cooper-lab/mint.git'])")
            SFIToolkit.errprintln("2. From local source: python: import subprocess, os; ado_path = SFIToolkit.getStringLocal('c(sysdir_plus)'); mint_path = os.path.join(os.path.dirname(ado_path), 'mintd'); subprocess.run(['pip', 'install', '-e', mint_path])")
            SFIToolkit.exit(198)

    except Exception as e:
        SFIToolkit.errprintln(f"Error creating project: {e}")
        SFIToolkit.exit(198)
end