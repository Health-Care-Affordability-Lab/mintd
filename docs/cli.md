# CLI Reference

## Main Commands

```bash
mintd --help                    # Show help
mintd --version                 # Show version
```

## Project Creation

### Create Data Product

```bash
mintd create data --name <name> [OPTIONS]
```

Creates a data product repository (`data_{name}`).

### Create Project

```bash
mintd create project --name <name> [OPTIONS]
```

Creates a project repository (`prj__{name}`).

### Create Infrastructure

```bash
mintd create infra --name <name> [OPTIONS]
```

Creates an infrastructure repository (`infra_{name}`).

### Create Enclave Workspace

```bash
mintd create enclave --name <name> [OPTIONS]
```

Creates a secure data enclave workspace (`enclave_{name}`).

### Create from Custom Template

```bash
mintd create custom --name <name> --template <path> [OPTIONS]
```

### Common Options

| Option | Description |
|--------|-------------|
| `-n, --name TEXT` | Project name (required) |
| `-p, --path PATH` | Output directory (default: current) |
| `--lang TEXT` | Primary programming language (`python\|r\|stata`) |
| `--no-git` | Skip Git initialization |
| `--no-dvc` | Skip DVC initialization |
| `--bucket TEXT` | Custom DVC bucket name |
| `--register` | Register project with Data Commons Registry |
| `--use-current-repo` | Use current directory as project root |

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
mintd data import <product>           # Import data product as DVC dependency
mintd data pull <product>             # Pull/download data from registry
```

## Registry Management

```bash
mintd registry register --path <path> # Register existing project
mintd registry status <project_name>  # Check registration status
mintd registry sync                   # Process pending registrations
mintd registry update                 # Update project metadata in registry
```

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
```
