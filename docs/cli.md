# CLI Reference

## Main Commands

```bash
mintd --help                    # Show help
mintd --version                 # Show version
```

## Project Creation

### Create Data Product

```bash
mintd create data --name <name> --lang <language> [OPTIONS]
```

Creates a data product repository (`data_{name}`).

### Create Project

```bash
mintd create project --name <name> --lang <language> [OPTIONS]
```

Creates a project repository (`prj__{name}`).

### Track Code Repository

```bash
mintd create code --name <name> --lang <language> [OPTIONS]
```

Tracks a code-only repository (library, package, tool) by dropping a `metadata.json` for governance, ownership, and mirroring. No directory scaffold is created.

### Create Enclave Workspace

```bash
mintd create enclave --name <name> [OPTIONS]
```

Creates a secure data enclave workspace (`enclave_{name}`).

Enclave-specific options:

| Option | Description |
|--------|-------------|
| `--registry-url TEXT` | Data Commons Registry GitHub URL |

### Create from Custom Template

```bash
mintd create custom <template_name> --name <name> [OPTIONS]
```

### Common Options

| Option | Description |
|--------|-------------|
| `-n, --name TEXT` | Project name (required) |
| `-p, --path PATH` | Output directory (default: current) |
| `--lang TEXT` | Primary programming language (`python\|r\|stata`), required for data/project |
| `--no-git` | Skip Git initialization |
| `--no-dvc` | Skip DVC initialization |
| `--bucket TEXT` | Custom DVC bucket name |
| `--register` | Register project with Data Commons Registry |
| `--use-current-repo` | Use current directory as project root |

### Governance Options

These options are available for `data`, `project`, and `code` commands:

| Option | Description |
|--------|-------------|
| `--public` | Mark as public data |
| `--private` | Mark as private/lab data (default) |
| `--contract TEXT` | Mark as contract data (provide contract slug) |
| `--contract-info TEXT` | Description or link to contract |
| `--team TEXT` | Owning team slug |
| `--admin-team TEXT` | Override default admin team |
| `--researcher-team TEXT` | Override default researcher team |

## Configuration

```bash
mintd config show                     # Show current config
mintd config setup                    # Interactive setup
mintd config setup --set KEY VALUE    # Set specific value
mintd config setup --set-credentials  # Set storage credentials
```

## Data Management

```bash
mintd data list                       # List available data products
mintd data list --imported            # List imported dependencies
mintd data get <product>              # Download data/final/ to ./<product>/
mintd data get <product> --path data  # Download entire data/ directory
mintd data get <product> --rev v2.0   # Download a specific version
mintd data import <product>           # Import data/final/ from product (default)
mintd data import <product> --all     # Import entire data/ directory
mintd data import <product> --stage raw  # Import specific stage
mintd data pull <product>             # Pull/download data from registry
mintd data push                       # Push all DVC-tracked data to project remote
mintd data push <targets>             # Push specific .dvc files or stages
mintd data update                     # Update all DVC imports to latest version
mintd data update <path>              # Update specific .dvc file
mintd data remove <import>            # Remove a data import from the project
```

### Data Get Options

`mintd data get` downloads data product files directly without requiring a mintd project context. No git clone, no `.dvc` tracking files, no pipeline metadata -- just the data files. Use this to explore a dataset before deciding to import it.

| Option | Description |
|--------|-------------|
| `--dest TEXT` | Target directory (default: `./<product-name>/`) |
| `--rev TEXT` | Version tag or git ref (default: latest) |
| `--path TEXT` | Path inside source repo (default: `data/final/`) |
| `--with-schema / --no-schema` | Include `schemas/v1/schema.json` (default: on) |
| `--dry-run` | Show what would be downloaded without downloading |

Examples:

```bash
# Quick exploration -- download final data and schema
mintd data get aha-annual-survey

# Download to a specific directory
mintd data get aha-annual-survey --dest ~/Desktop/aha-data

# Download raw data instead of final
mintd data get aha-annual-survey --path data/raw

# Download everything under data/
mintd data get aha-annual-survey --path data

# Preview without downloading
mintd data get aha-annual-survey --dry-run
```

### Data Import Options

By default, `mintd data import` imports only `data/final/` (the validated output) from the source data product. If `data/final/` is not found, you are prompted to choose from available directories.

| Option | Description |
|--------|-------------|
| `--stage TEXT` | Pipeline stage to import (`raw`, `intermediate`, `final`) |
| `--source-path TEXT` | Specific path to import from the product |
| `--all` | Import the entire `data/` directory |
| `--dest TEXT` | Local destination path (default: `data/imports/<product>/`) |
| `--rev TEXT` | Specific git revision to import from |
| `-p, --project-path PATH` | Path to project directory |

`--stage`, `--source-path`, and `--all` are mutually exclusive.

### Data Push Options

| Option | Description |
|--------|-------------|
| `TARGETS` | Specific .dvc files or pipeline stages to push (optional) |
| `-j, --jobs INT` | Number of parallel upload jobs |
| `-p, --project-path PATH` | Path to project directory |

The push command reads the DVC remote name from `metadata.json` so data is always pushed to the correct S3 location. There is no need to specify the remote manually.

### Data Remove Options

| Option | Description |
|--------|-------------|
| `-f, --force` | Remove even if dvc.yaml still has references |
| `-p, --project-path PATH` | Path to project directory |

### Data Update Options

| Option | Description |
|--------|-------------|
| `--rev TEXT` | Specific git revision to update to |
| `--dry-run` | Show what would be updated without making changes |
| `-p, --project-path PATH` | Path to project directory |

## Registry Management

```bash
mintd registry register --path <path>                # Register existing project
mintd registry status <project_name>                 # Check registration status
mintd registry sync                                  # Process pending registrations
mintd registry update [project_name] [OPTIONS]       # Update project metadata in registry
```

### Registry Update Options

| Option | Description |
|--------|-------------|
| `-p, --path PATH` | Path to project directory |
| `--dry-run` | Show changes without creating PR |

## Enclave Management

```bash
mintd enclave add <product>           # Add data product to approved list
mintd enclave list                    # List approved/transferred products
mintd enclave pull                    # Pull data products from registry
mintd enclave package                 # Package data for transfer
mintd enclave unpack <archive>        # Unpack a transfer archive
mintd enclave verify                  # Verify transfer and update manifest
mintd enclave clean                   # Prune old versions, clean staging
```

## Manifest Management

```bash
mintd manifest create <path>          # Create/update file manifest
mintd manifest check <file>           # Check if file changed vs manifest
mintd manifest status <dir>           # Show status of files in directory
```

## Template Management

```bash
mintd templates list                  # List available project templates
```

## Update Commands

```bash
mintd update utils                    # Update mintd utility scripts
mintd update metadata                 # Update metadata.json to latest schema
mintd update storage                  # Update DVC storage configuration
mintd update schema                   # Add Frictionless Table Schema support
mintd update schema --generate        # Auto-generate schema from data files
mintd update schema --force           # Overwrite existing schema.json
```

### Update Metadata Options

| Option | Description |
|--------|-------------|
| `-p, --path PATH` | Path to project directory |
| `--sensitivity TEXT` | Storage sensitivity level (`public\|restricted\|confidential`) |
| `--mirror-url TEXT` | Mirror repository URL |

### Update Storage Options

| Option | Description |
|--------|-------------|
| `-p, --path PATH` | Path to project directory |
| `-y, --yes` | Skip confirmation |

### Update Schema Options

| Option | Description |
|--------|-------------|
| `-p, --path PATH` | Path to project directory |
| `-g, --generate` | Auto-generate schema from data files |
| `-f, --force` | Overwrite existing schema.json |
