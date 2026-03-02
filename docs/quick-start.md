# Quick Start

## Configuration Setup

Before creating projects, initialize your configuration:

```bash
# Interactive setup
mintd config setup
```

This ensures your storage buckets and registry settings are correctly configured. See the [Configuration Guide](configuration.md) for more details.

## Basic Usage

```bash
# Create a data processing project (language required)
mintd create data --name hospital_project --lang python

# Create a research analysis project
mintd create project --name cost_study --lang r
```

> **Note:** For reusable code packages, use standard language tooling (e.g., `uv init`, `poetry init`, `usethis::create_package()`) instead of mintd. See [Project Types](project-types.md#standalone-packages-no-mintd-scaffolding) for details.

## With Registry Integration

```bash
# Create projects with automatic registration to Data Commons Registry
mintd create data --name hospital_project --lang python --register
mintd create project --name cost_study --lang stata --register

# Check registration status
mintd registry status hospital_project

# Register existing projects
mintd registry register --path /path/to/existing/project
```

## With Custom Options

```bash
# Create in specific directory with custom settings
mintd create data --name mydata --path /projects --bucket my-custom-bucket

# Create projects with specific programming languages (now required)
mintd create data --name hospital_project --lang r
mintd create project --name analysis --lang stata

# Skip version control initialization
mintd create project --name analysis --no-git --no-dvc

# Register with Data Commons Registry
mintd create data --name hospital_project --register

# Use current directory (when in existing git repo)
cd existing-git-repo
mintd create data --name mydata --use-current-repo
```

## Using Existing Git Repositories

mintd supports scaffolding projects directly in existing git repositories using the `--use-current-repo` flag. This is useful when you want to add mintd's standardized project structure to an existing codebase.

### Requirements
- You must be in a git repository
- Only works with git-initialized directories

### Example Usage

```bash
# Navigate to existing git repository
cd my-existing-project

# Scaffold mintd project structure in current directory
mintd create data --name hospital_project --use-current-repo

# Result: Project files created directly in my-existing-project/
# ├── README.md (mintd-generated)
# ├── metadata.json
# ├── data/
# ├── src/
# └── .gitignore
```

### What Happens
- **No subdirectory created**: Unlike normal usage, no `data_hospital_project/` folder is created
- **Git integration**: Uses existing git repository, adds and commits new files
- **File conflicts**: Warning displayed if existing files would be overwritten
- **Same functionality**: All other mintd features work normally (DVC, templates, etc.)

### When to Use
- Adding mintd structure to existing research projects
- Converting legacy projects to standardized format
- Working within established repository conventions
- Collaborating on projects with existing git history
