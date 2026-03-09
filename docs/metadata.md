# Metadata Reference

Every mintd project includes a `metadata.json` file that stores project information, governance settings, and discoverability metadata.

## Discoverability Fields

These optional fields help the registry answer questions like "which repos produce HHI measures?" or "which repos depend on the provider data service?"

### `metadata.description`

A brief description of what the data product does.

```json
{
  "metadata": {
    "description": "CMS IPPS reimbursement rates derived from provider and weight data"
  }
}
```

### `metadata.tags`

Searchable keywords for the data product.

```json
{
  "metadata": {
    "tags": ["cms", "medicare", "reimbursement", "hospital"]
  }
}
```

### `metadata.configurations`

Dataset configurations or variants supported by the pipeline.

```json
{
  "metadata": {
    "configurations": ["hospitals", "mergerpanel", "snf"]
  }
}
```

Use this when your pipeline produces multiple dataset variants based on configuration.

### `metadata.methods`

Methods or algorithms implemented in the data product.

```json
{
  "metadata": {
    "methods": ["hhi", "diversion-ratios", "semiparametric-demand", "willingness-to-pay"]
  }
}
```

Use this for data products that implement notable methodology.

### `metadata.data_dependencies`

Upstream data products this repo depends on.

```json
{
  "metadata": {
    "data_dependencies": ["data_cms-provider", "data_cms-ipps-weights"]
  }
}
```

This is populated automatically when using `mintd data import`, but you can also add entries manually.

## Example: Complex Data Product

A complex data product like `data_market-competition` might have:

```json
{
  "metadata": {
    "version": "1.0.0",
    "mint_version": "0.5.0",
    "description": "Market competition measures (HHI, diversion, WTP) for hospital markets",
    "tags": ["competition", "antitrust", "hospital", "markets"],
    "configurations": ["hospitals", "mergerpanel"],
    "methods": ["hhi", "diversion-ratios", "semiparametric-demand", "willingness-to-pay"],
    "data_dependencies": ["data_cms-provider", "data_aha-annual-survey", "data_choice-model-estimates"]
  }
}
```

## Full Schema Reference

| Section | Field | Type | Description |
|---------|-------|------|-------------|
| | `schema_version` | string | Metadata schema version (currently `1.0`) |
| `mint` | `version` | string | mintd version that created the project |
| `mint` | `commit_hash` | string | mintd git commit hash at creation time |
| `project` | `name` | string | Project name without prefix |
| `project` | `type` | string | `data`, `project`, `code`, or `enclave` |
| `project` | `full_name` | string | Full name with prefix (e.g., `data_my-project`) |
| `project` | `display_name` | string | Human-readable display name (auto-generated) |
| `project` | `created_at` | string | ISO 8601 creation timestamp |
| `project` | `created_by` | string | Author who created the project |
| `metadata` | `version` | string | Data product version |
| `metadata` | `mint_version` | string | mintd version at creation |
| `metadata` | `description` | string | Brief description |
| `metadata` | `tags` | array | Searchable keywords |
| `metadata` | `configurations` | array | Dataset variants |
| `metadata` | `methods` | array | Implemented methods |
| `metadata` | `data_dependencies` | array | Upstream data products |
| `ownership` | `team` | string | Owning team slug |
| `ownership` | `maintainers` | array | List of maintainer names |
| `access_control` | `teams` | array | Teams and their permission levels |
| `access_control.teams[]` | `name` | string | Team slug |
| `access_control.teams[]` | `permission` | string | `admin` or `read` |
| `status` | `state` | string | Project state (default: `active`) |
| `status` | `last_updated` | string | ISO 8601 timestamp of last update |
| `governance` | `classification` | string | `public`, `private`, or `contract` |
| `governance` | `contract_info` | string | Contract description or link (if applicable) |
| `storage` | `provider` | string | Storage provider (default: `s3`) |
| `storage` | `bucket` | string | S3 bucket name |
| `storage` | `prefix` | string | S3 path prefix (e.g., `lab/data_my-project/`) |
| `storage` | `endpoint` | string | Custom storage endpoint URL |
| `storage` | `versioning` | boolean | Whether S3 versioning is enabled |
| `storage.dvc` | `remote_name` | string | DVC remote name (matches `full_name`) |
| `storage.dvc` | `remote_url` | string | Full S3 URL for DVC remote |
| `schema` | `version` | string | Schema version |
| `schema` | `standard` | string | Schema standard (`frictionless-table-schema`) |
| `schema` | `location` | string | Path to schema file |
| `lifecycle` | `retention_policy` | string | Data retention policy (default: `permanent`) |
| `lifecycle` | `archival_status` | string | Archive status (default: `active`) |
| `repository` | `github_url` | string | GitHub repository URL |
| `repository` | `default_branch` | string | Default branch (default: `main`) |
| `repository` | `visibility` | string | Repository visibility (default: `private`) |
| `repository.mirror` | `url` | string | Mirror repository URL |
| `repository.mirror` | `purpose` | string | Purpose of mirror |

### Storage Path Patterns

The `storage.prefix` follows classification-specific patterns:

| Classification | Pattern | Example |
|---------------|---------|---------|
| `private` | `lab/{full_name}/` | `lab/data_my-project/` |
| `public` | `pub/{full_name}/` | `pub/data_my-project/` |
| `contract` | `contract/{slug}/{full_name}/` | `contract/cms-2024/data_my-project/` |

The `storage.dvc.remote_url` is always `s3://{bucket}/{prefix}`, ensuring `mintd data push` and `dvc push` write to the same S3 path.

## Updating Metadata

Edit `metadata.json` directly, or use the CLI:

```bash
# Update metadata fields interactively
mintd update metadata

# Update specific fields
mintd update metadata --sensitivity restricted
```
