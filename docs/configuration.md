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
  url: "https://github.com/health-care-affordability-lab/data-commons-registry"

defaults:
  author: "Jane Researcher"
  organization: "Economics Lab"
```

## Manual Configuration

```bash
# Set individual values
mintd config setup --set storage.bucket_prefix mylab
mintd config setup --set defaults.author "Jane Doe"
mintd config setup --set registry.url "https://github.com/your-org/registry"

# Configure storage credentials
mintd config setup --set-credentials
```
