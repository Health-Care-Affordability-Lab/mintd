#!/usr/bin/env python3
"""
Installation verification script for mint.
Run this after installing mint to verify everything is working correctly.
"""

def verify_installation():
    """Verify that mint is properly installed and functional."""
    print("🔍 Verifying mint installation...")
    print()

    checks_passed = 0
    total_checks = 0

    def check(name, condition, details=""):
        nonlocal checks_passed, total_checks
        total_checks += 1
        if condition:
            print(f"✅ {name}")
            checks_passed += 1
            if details:
                print(f"   {details}")
        else:
            print(f"❌ {name}")
            if details:
                print(f"   {details}")
        print()

    # Check Python version
    import sys
    version_ok = sys.version_info >= (3, 9)
    check(
        "Python version >= 3.9",
        version_ok,
        f"Current version: {sys.version}"
    )

    # Check mint import (try local source first for development)
    import sys
    import os
    local_src = os.path.join(os.path.dirname(__file__), 'src')
    if local_src not in sys.path:
        sys.path.insert(0, local_src)

    try:
        import mint
        version = getattr(mint, '__version__', 'unknown')
        check("mint package import", True, f"Version: {version} (local source)")
    except ImportError:
        check("mint package import", False, "Package not found in local source")

    # Check CLI
    try:
        from mint.cli import main
        check("CLI module import", True)
    except ImportError:
        check("CLI module import", False)

    # Check API
    try:
        from mint.api import create_project
        check("API module import", True)
    except ImportError:
        check("API module import", False)

    # Check templates
    try:
        from mint.templates import DataTemplate, ProjectTemplate, InfraTemplate
        check("Template modules import", True)
    except ImportError:
        check("Template modules import", False)

    # Check initializers
    try:
        from mint.initializers.git import init_git
        from mint.initializers.storage import init_dvc
        check("Initializer modules import", True)
    except ImportError:
        check("Initializer modules import", False)

    # Check CLI command execution
    import subprocess
    try:
        # For development testing, ensure local src is available
        env = os.environ.copy()
        if local_src not in sys.path:
            # Add local src to Python path for subprocess
            python_path = local_src
            if 'PYTHONPATH' in env:
                env['PYTHONPATH'] = f"{python_path}:{env['PYTHONPATH']}"
            else:
                env['PYTHONPATH'] = python_path

        # First try the installed script entry point
        result = subprocess.run([
            'mint', '--version'
        ], capture_output=True, text=True, timeout=5, env=env)

        if result.returncode != 0:
            # If that fails, try running via python module (for development)
            result = subprocess.run([
                sys.executable, '-c', f'import sys; sys.path.insert(0, "{local_src}"); from mint.cli import main; main(["--version"])'
            ], capture_output=True, text=True, timeout=5, env=env)

        check(
            "CLI command execution",
            result.returncode == 0,
            f"Output: {result.stdout.strip() or result.stderr.strip()}"
        )
    except Exception as e:
        check("CLI command execution", False, str(e))

    # Summary
    print(f"📊 Installation verification: {checks_passed}/{total_checks} checks passed")
    print()

    if checks_passed == total_checks:
        print("🎉 mint is properly installed and ready to use!")
        print()
        print("Quick start:")
        print("  mint create data --name myproject")
        print("  mint create project --name analysis")
        print("  mint create infra --name tools")
        print()
        print("For Stata users, see stata/README.md for installation instructions.")
    else:
        print("⚠️  Some checks failed. Please check the installation.")
        print("You may need to reinstall or check your Python environment.")

    return checks_passed == total_checks


if __name__ == "__main__":
    success = verify_installation()
    exit(0 if success else 1)
