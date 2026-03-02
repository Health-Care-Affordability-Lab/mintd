#!/usr/bin/env python3
"""
Installation test script for mint package.
This script validates that the package can be properly installed and used.
"""
import pytest
import sys
import os
import subprocess

def test_package_installation():
    """Test that the mint package can be imported and used after installation."""
    print("🧪 Testing mint package installation...")

    # Test basic import
    import mintd
    assert mintd.__name__ == "mintd"
    
    # Test CLI import
    from mintd.cli import main
    assert main is not None

    # Test API import
    from mintd.api import create_project
    assert create_project is not None

    # Test template imports
    from mintd.templates import DataTemplate, ProjectTemplate
    assert DataTemplate is not None
    assert ProjectTemplate is not None

    # Test initializer imports
    from mintd.initializers.git import init_git
    from mintd.initializers.storage import init_dvc
    assert init_git is not None
    assert init_dvc is not None

    # Test configuration
    from mintd.config import get_config
    assert get_config is not None

    # Test Stata files accessibility
    package_dir = os.path.dirname(mintd.__file__)
    stata_dir = os.path.join(package_dir, '..', '..', 'stata')

    if os.path.exists(stata_dir):
        stata_files = os.listdir(stata_dir)
        ado_files = [f for f in stata_files if f.endswith('.ado')]
        sthlp_files = [f for f in stata_files if f.endswith('.sthlp')]
        # Optional: assert len(ado_files) > 0
    
    # Test CLI execution (basic)
    result = subprocess.run([
        sys.executable, '-c', 'from mintd.cli import main; main(["--help"])'
    ], capture_output=True, text=True, timeout=10)
    
    assert result.returncode == 0, f"CLI execution failed: {result.stderr}"

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))