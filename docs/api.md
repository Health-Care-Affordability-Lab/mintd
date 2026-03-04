# Python API

Use mintd programmatically in Python:

```python
from mintd import create_project

# Create a project (language is now required)
result = create_project(
    project_type="data",
    name="my_analysis",
    language="python",            # Required: "python", "r", or "stata"
    path="/projects",
    init_git=True,
    init_dvc=True,
    bucket_name="custom-bucket",  # Optional
    register_project=True         # Register with Data Commons Registry
)

print(f"Created: {result.full_name}")
print(f"Location: {result.path}")
if result.registration_url:
    print(f"Registration PR: {result.registration_url}")

# Track a code-only repository (metadata only, no scaffold)
result = create_project(
    project_type="code",
    name="mylib",
    language="python",
    init_git=True,
    init_dvc=False,              # No DVC for code repos
)
```

## Data Push

Push DVC-tracked data to a project's configured remote:

```python
from pathlib import Path
from mintd.data_import import get_project_remote, push_data

project = Path("/projects/data_my_analysis")

# Look up the remote name from metadata.json
remote = get_project_remote(project)
print(f"Remote: {remote}")

# Push all DVC-tracked data to the project remote
push_data(project_path=project)

# Push specific targets with parallel jobs
push_data(
    project_path=project,
    targets=["data/raw.dvc", "data/final.dvc"],
    jobs=4,
)
```

### `get_project_remote(project_path)`

Returns the DVC remote name configured in the project's `metadata.json` (under `storage.dvc.remote_name`). Raises `DataImportError` if the metadata file is missing or no remote is configured.

### `push_data(project_path, targets=None, jobs=None)`

Runs `dvc push -r <remote>` using the remote from `get_project_remote()`. Accepts an optional list of `.dvc` file paths or stage names to push, and an optional `jobs` count for parallel uploads. Returns `True` on success.
