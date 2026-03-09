# Configuration Guide

## Initial Setup

Run the interactive configuration:

```bash
mintd config setup
```

This will prompt for:
- **Storage Provider**: S3-compatible service (AWS, Wasabi, MinIO)
- **Endpoint**: Service endpoint URL (leave blank for AWS)
- **Region**: AWS region
- **Bucket Prefix**: Prefix for project bucket names
- **GitHub Organization**: GitHub org that owns your repos (required)
- **Author**: Your name
- **Organization**: Your lab/organization

## Configuration File

Settings are stored in `~/.mintd/config.yaml`:

```yaml
storage:
  provider: "s3"
  endpoint: ""           # For non-AWS services
  region: "us-east-1"
  bucket_prefix: "mylab"
  versioning: true

registry:
  url: "https://github.com/your-org/data-commons-registry"
  org: "your-org"                       # GitHub org (required)
  default_branch: "main"               # Registry default branch
  admin_team: "infrastructure-admins"   # Default admin team slug
  researcher_team: "all-researchers"    # Default researcher team slug

defaults:
  author: "Jane Researcher"
  organization: "Economics Lab"
```

## Configuration Reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `storage.provider` | No | `s3` | Storage provider type |
| `storage.endpoint` | No | `""` | Custom endpoint URL (non-AWS services) |
| `storage.region` | No | `us-east-1` | AWS region |
| `storage.bucket_prefix` | Yes | — | S3 bucket name prefix for DVC remotes |
| `storage.versioning` | No | `true` | Enable S3 version-aware DVC storage |
| `registry.url` | No | — | Data Commons Registry GitHub URL |
| `registry.org` | Yes | — | GitHub organization that owns project repos |
| `registry.default_branch` | No | `main` | Default branch for registry |
| `registry.admin_team` | No | `infrastructure-admins` | Admin team slug for new projects |
| `registry.researcher_team` | No | `all-researchers` | Researcher team slug for new projects |
| `defaults.author` | No | `""` | Default author name for new projects |
| `defaults.organization` | No | `""` | Default organization name |

## Manual Configuration

```bash
# Set individual values
mintd config setup --set storage.bucket_prefix mylab
mintd config setup --set defaults.author "Jane Doe"
mintd config setup --set registry.org "your-org"
mintd config setup --set registry.url "https://github.com/your-org/registry"

# Configure storage credentials
mintd config setup --set-credentials
```
