# Skill Update: mintd-guide

**Purpose**: Document changes needed for the `mintd-guide` skill after taxonomy simplification.

## Current State

The `mintd-guide` skill currently references three project types:
- `data` - Data products
- `project` - Research projects
- `infra` - Infrastructure packages

## Required Updates

### 1. Remove Infra References

**Section**: Project Types

Before:
```markdown
mintd supports three project types:
- `data` (data_*) - Cleaned datasets with governance metadata
- `project` (prj_*) - Research analyses producing papers/tables
- `infra` (infra_*) - Reusable packages and infrastructure code
```

After:
```markdown
mintd supports two project types:
- `data` (data_*) - Reusable datasets with governance metadata
- `project` (prj_*) - Research analyses producing papers/tables

For reusable code packages (Python, R, Stata), use standard language tooling without mintd scaffolding.
```

### 2. Update Project Type Decision Tree

**Section**: Choosing a Project Type

Before:
```markdown
Q: What does your repo produce?
├── A dataset others will import → data
├── Tables/figures for a paper → project
└── Reusable code/package → infra
```

After:
```markdown
Q: What does your repo produce?
├── A dataset others will import → `mintd create data`
│   (Simple OR complex, primary OR derived, single OR parameterized)
├── Tables/figures for a paper → `mintd create project`
└── Reusable code package → Standard tooling (no mintd)
    - Python: `uv init` or `poetry init`
    - R: `usethis::create_package()`
    - Stata: Manual ado/sthlp structure
```

### 3. Add Expanded Data Guidance

**New Section**: Complex Data Products

```markdown
## When Data Products Get Complex

The `data` type handles more than simple cleaned datasets:

### Derived Data Products
If your inputs come from other lab data products:
- Use `mintd data import` to declare dependencies
- Track provenance in `data_dependencies` metadata
- This is normal — many data products build on others

### Parameterized Pipelines
If your pipeline runs on multiple configurations:
- Add a `configs/` directory
- Use `dvc_vars.yaml` for parameterization
- Example: market competition measures for different hospital/market definitions

### Multi-Stage Processing
The default ingest/clean/validate stages are a starting point:
- Add stages as needed: `estimate`, `aggregate`, `export`
- Rename stages to match your workflow
- Complex pipelines are still data products

### Optional Discoverability Metadata
For complex data products, add to `metadata.json`:
```json
{
  "configurations": ["hospitals", "mergerpanel"],
  "methods": ["hhi", "diversion-ratios", "semiparametric-demand"],
  "data_dependencies": ["data_cms-provider", "data_cms-ipps-weights"]
}
```
```

### 4. Add Package Extraction Guidance

**New Section**: When to Create a Standalone Package

```markdown
## When to Create a Standalone Package

Code lives inside a `data` repo until there's a reason to pull it out.

### Trigger: A Second Consumer
1. You build `data_market-competition` with HHI calculation code inside
2. Six months later, another project needs HHI on different data
3. They need the *code*, not just the output → Extract to a package

### Extraction Checklist
- [ ] Second consumer exists (not hypothetical)
- [ ] Code has clear API boundary
- [ ] Can be versioned independently
- [ ] Has tests without full data pipeline
- [ ] Original repo updated to import package

### What NOT to Extract
- Code only one repo uses
- Code tightly coupled to specific data structures
- Scripts without clean APIs
```

### 5. Update Command Reference

**Section**: `mintd create` Commands

Before:
```markdown
mintd create data --name <name> --lang <language>
mintd create project --name <name> --lang <language>
mintd create infra --name <name> --lang <language>
```

After:
```markdown
mintd create data --name <name> --lang <language>
mintd create project --name <name> --lang <language>

# For packages, use standard language tooling instead:
uv init my-package          # Python
poetry init                 # Python (alternative)
usethis::create_package()   # R
```

### 6. Add Migration Note

**New Section**: Migrating Legacy Repos

```markdown
## Migrating `infra_*` Repos

If you have existing `infra_*` repositories:

| What it actually produces | Action |
|--------------------------|--------|
| Reusable datasets | Rename to `data_*`, update metadata.json |
| Pure code package | Remove mintd scaffolding, use language tooling |
| Legacy unstructured code | Evaluate: restructure as data or absorb into consumer |

See [Migration Guide](../docs/migration/infra-to-data.md) for detailed steps.
```

---

## Full Skill Template After Updates

See `/Users/mad265/.claude/skills/mintd-guide.md` for the updated skill file.

---

## Related Skill Updates

### mintd-data-migration

Add new section:
```markdown
## Migrating Infra Repos to Data Products

When migrating `infra_*` repos that produce datasets:

1. Update `metadata.json`:
   - Change `project_type` to `"data"`
   - Update `full_name` from `infra_*` to `data_*`

2. Update DVC remote naming (if applicable)

3. Re-register with catalog (if using registry)

4. Update consumers' import references
```

---

## Testing the Updated Skill

After updating, verify with test prompts:

1. "What project type should I use for a derived dataset?"
   - Expected: `data` with mention of `data_dependencies`

2. "How do I create a shared utility package?"
   - Expected: Standard tooling recommendation, NOT `mintd create infra`

3. "My infra repo produces HHI measures. What should I do?"
   - Expected: Migrate to `data` type, mention optional metadata fields

4. "When should I extract code into a separate package?"
   - Expected: Second consumer trigger, extraction checklist
