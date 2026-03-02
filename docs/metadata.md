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
| `project` | `name` | string | Project name without prefix |
| `project` | `type` | string | `data` or `project` |
| `project` | `full_name` | string | Full name with prefix (e.g., `data_my-project`) |
| `metadata` | `version` | string | Data product version |
| `metadata` | `description` | string | Brief description |
| `metadata` | `tags` | array | Searchable keywords |
| `metadata` | `configurations` | array | Dataset variants |
| `metadata` | `methods` | array | Implemented methods |
| `metadata` | `data_dependencies` | array | Upstream data products |
| `ownership` | `team` | string | Owning team slug |
| `governance` | `classification` | string | `public`, `private`, or `contract` |
| `storage` | `provider` | string | Storage provider (default: `s3`) |
| `storage` | `prefix` | string | S3 path prefix |

## Updating Metadata

Edit `metadata.json` directly, or use the CLI:

```bash
# Update metadata fields interactively
mintd update metadata

# Update specific fields
mintd update metadata --sensitivity restricted
```
