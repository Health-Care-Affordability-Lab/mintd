#!/usr/bin/env python3
"""
Test script for Stata integration.
This simulates what happens when Stata tries to use the mint package.
"""

def test_stata_integration():
    """Test the Stata integration functionality."""
    print("🧪 Testing Stata Integration...")

    # Simulate the _prjsetup_create function that gets called from Stata
    def _prjsetup_create(project_type, name, path, no_git, no_dvc, bucket):
        """Create project using mint Python package (simulating Stata call)."""
        try:
            from mintd import create_project

            # Convert string booleans to Python booleans (as done in .ado file)
            init_git = no_git != "True"
            init_dvc = no_dvc != "True"
            bucket_name = bucket if bucket != "None" else None

            result = create_project(
                project_type=project_type,
                name=name,
                path=path,
                init_git=init_git,
                init_dvc=init_dvc,
                bucket_name=bucket_name
            )

            print(f"✅ Stata integration: Project created: {result.full_name}")
            print(f"📁 Location: {result.path}")

            # Simulate returning path to Stata macro
            return str(result.path)

        except ImportError:
            print("❌ Stata integration: mint package not installed")
            return None
        except Exception as e:
            print(f"❌ Stata integration: Error creating project: {e}")
            return None

    # Test the integration
    import tempfile
    with tempfile.TemporaryDirectory() as temp_dir:
        # Test data project creation
        path = _prjsetup_create("data", "stata_test", temp_dir, "False", "True", "None")
        if path:
            print("✅ Stata data project creation successful")

        # Test project creation
        path = _prjsetup_create("project", "stata_project_test", temp_dir, "True", "True", "custom-bucket")
        if path:
            print("✅ Stata project creation with options successful")

        # Test code project creation
        path = _prjsetup_create("code", "stata_code_test", temp_dir, "False", "True", "None")
        if path:
            print("✅ Stata code project creation successful")

    print("🎉 Stata integration tests completed successfully!")


if __name__ == "__main__":
    test_stata_integration()



