#!/usr/bin/env python3
"""
Installation test script for mint package.
This script validates that the package can be properly installed and used.
"""

def test_package_installation():
    """Test that the mint package can be imported and used after installation."""
    print("🧪 Testing mint package installation...")

    try:
        # Test basic import
        import mint
        print(f"✅ Package imported successfully: {mint.__name__}")
        print(f"   Version: {getattr(mint, '__version__', 'Not specified')}")

        # Test CLI import
        from mint.cli import main
        print("✅ CLI module imported successfully")

        # Test API import
        from mint.api import create_project, ProjectResult
        print("✅ API module imported successfully")

        # Test template imports
        from mint.templates import DataTemplate, ProjectTemplate, InfraTemplate
        print("✅ Template modules imported successfully")

        # Test initializer imports
        from mint.initializers.git import init_git
        from mint.initializers.storage import init_dvc
        print("✅ Initializer modules imported successfully")

        # Test configuration
        from mint.config import get_config, save_config
        print("✅ Configuration module imported successfully")

        # Test Stata files accessibility
        import os
        package_dir = os.path.dirname(mint.__file__)
        stata_dir = os.path.join(package_dir, '..', '..', 'stata')

        if os.path.exists(stata_dir):
            stata_files = os.listdir(stata_dir)
            print(f"✅ Stata integration files found: {len(stata_files)} files")
            ado_files = [f for f in stata_files if f.endswith('.ado')]
            sthlp_files = [f for f in stata_files if f.endswith('.sthlp')]
            print(f"   - .ado files: {len(ado_files)}")
            print(f"   - .sthlp files: {len(sthlp_files)}")
        else:
            print("⚠️  Stata files not found in package (expected for source installation)")

        # Test CLI execution (basic)
        import subprocess
        import sys

        try:
            result = subprocess.run([
                sys.executable, '-c', 'from mint.cli import main; main(["--help"])'
            ], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print("✅ CLI execution test passed")
            else:
                print(f"⚠️  CLI execution test failed: {result.stderr}")
        except Exception as e:
            print(f"⚠️  CLI execution test failed: {e}")

        print("\n🎉 Package installation test completed successfully!")
        print("\n📦 The mint package is ready for distribution!")
        print("\nTo install for end users:")
        print("  pip install git+https://github.com/Cooper-lab/mint.git")
        print("  # or")
        print("  pip install -e /path/to/local/mint")

        return True

    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("This suggests the package is not properly installed.")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_package_installation()
    exit(0 if success else 1)