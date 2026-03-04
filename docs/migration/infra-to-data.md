# Migration Guide: Infra Repos to Data Products

**Version**: mintd 0.5.0+
**Date**: 2026-03-02

## Overview

Starting with mintd 0.5.0, the `infra` project type has been removed. All repositories that produce datasets should use the `data` type. Pure code packages (Python, R, Stata) should use standard language tooling without mintd scaffolding.

## Why This Change?

The `infra` type was originally intended for "shared utility packages" but in practice was used for:
- Derived data products (inputs from other data products)
- Complex parameterized pipelines
- Legacy research code
- Pure code packages

These are fundamentally different things. The new taxonomy clarifies:

| What you're building | New approach |
|---------------------|--------------|
| Dataset (any complexity) | `mintd create data` |
| Research analysis/paper | `mintd create project` |
| Reusable code package | Standard language tooling (no mintd) |

---

## Migration Paths

### Path A: Infra Repo → Data Product

**When**: Your repo produces reusable datasets (even if derived from other data products)

**Examples**:
- `infra_cms-ipps-reimbursement` → `data_cms-ipps-reimbursement`
- `infra_market-competition` → `data_market-competition`

**Steps**:

1. **Update `metadata.json`**
   ```json
   {
     "project_type": "data",
     "name": "cms-ipps-reimbursement",
     "full_name": "data_cms-ipps-reimbursement",
     ...
   }
   ```

2. **Add optional discoverability fields** (if applicable)
   ```json
   {
     "configurations": ["hospitals", "mergerpanel"],
     "methods": ["hhi", "diversion-ratios"],
     "data_dependencies": ["data_cms-provider", "data_cms-ipps-weights"]
   }
   ```

3. **Update DVC remote name** (if S3 bucket uses old naming)
   ```bash
   dvc remote rename infra-cms-ipps-reimbursement data-cms-ipps-reimbursement
   ```

4. **Update README.md**
   - Change project type references
   - Update any installation/usage examples

5. **Register with new type** (if using registry)
   ```bash
   mintd update register --force
   ```

### Path B: Infra Repo → Standalone Package

**When**: Your repo produces reusable code (not datasets)

**Examples**:
- `geokit` (Python isochrone utilities)
- `statatools` (Stata ado packages)

**Steps**:

1. **Remove mintd scaffolding**
   ```bash
   rm -rf .dvc .dvcignore dvc.yaml dvc.lock
   rm metadata.json
   ```

2. **Keep standard package structure**
   - Python: `pyproject.toml`, `src/`, `tests/`
   - R: `DESCRIPTION`, `NAMESPACE`, `R/`, `man/`
   - Stata: `.ado`, `.sthlp` files

3. **Set up CI/CD** for package publishing
   - PyPI for Python
   - CRAN or r-universe for R
   - SSC or GitHub releases for Stata

4. **Remove from mintd registry** (if registered)

### Path C: Infra Repo → Absorbed Into Consumer

**When**: The code is only used by one project and isn't a separable product

**Examples**:
- Methodology code that's tightly coupled to a specific analysis
- Helper scripts that don't have clean APIs

**Steps**:

1. **Move code into the consuming repo**
2. **Archive the original infra repo**
3. **Update any DVC imports to use local paths**

---

## Checklist: Is This a Data Product?

Answer YES to most of these → Use `mintd create data`:

- [ ] Does it produce one or more datasets as output?
- [ ] Would other projects import its outputs via `mintd data import`?
- [ ] Does it have a DVC pipeline for reproducibility?
- [ ] Does it need data governance metadata (PHI, PII, contracts)?
- [ ] Does it have schema validation for outputs?

## Checklist: Is This a Package?

Answer YES to most of these → Use standard language tooling:

- [ ] Does it produce reusable code (functions, classes, commands)?
- [ ] Would other projects install it via pip/renv/ssc?
- [ ] Does it have a versioned API that consumers depend on?
- [ ] Does it have tests that run without any data files?
- [ ] Could it be published to PyPI/CRAN/SSC?

---

## Updating Existing Imports

If other projects import from your renamed repo:

### In the consuming project's `metadata.json`:
```json
{
  "data_dependencies": [
    {
      "source": "data_cms-ipps-reimbursement",  // Updated name
      "path": "data/raw/ipps",
      "version": "v1.2.0"
    }
  ]
}
```

### Update DVC imports:
```bash
# Remove old import
mintd data remove ipps

# Re-import with new source name
mintd data import data_cms-ipps-reimbursement --path data/raw/ipps
```

---

## Frequently Asked Questions

### Q: My infra repo has complex multi-stage pipelines. Is it still a data product?

**Yes.** Complexity doesn't change the type. If the output is datasets that other projects consume, it's a data product. You'll customize `dvc.yaml` beyond the default three stages — that's expected.

### Q: My infra repo takes other data products as inputs. Is that okay for a data type?

**Yes.** That's exactly what `data_dependencies` tracks. Many data products are derived — combining, transforming, or analyzing upstream data products.

### Q: Can I have Python/R code in a data repo?

**Yes.** Data repos regularly contain substantial code for data processing. The distinction is: does the repo's *purpose* is to produce datasets (data product) or to produce reusable code (package)?

### Q: When should I extract a package from a data repo?

When there's a **second consumer** that needs the code but not the specific outputs. See [Package Extraction Guide](#package-extraction-guide) below.

---

## Package Extraction Guide

### When to Extract

Extract code into a separate package when:

1. **A second project needs the code** (not hypothetical — actual)
2. **The code has a clear API boundary** (inputs/outputs well-defined)
3. **The code can be versioned independently** of the data pipeline
4. **The code has tests** that run without the full data pipeline

### How to Extract

1. **Create new package repo** (no mintd scaffolding)
   ```bash
   mkdir hhi-calculator && cd hhi-calculator
   poetry init  # or uv init, etc.
   ```

2. **Move code with clear boundaries**
   - Functions that take generic inputs and produce generic outputs
   - Classes with well-defined interfaces
   - No hardcoded paths or dataset-specific logic

3. **Write tests for the package**
   - Should run with synthetic/fixture data
   - No dependency on full data pipeline

4. **Update original data repo**
   ```bash
   # In data_market-competition
   pip install hhi-calculator  # or add to pyproject.toml
   ```

5. **Update pipeline to use package**
   ```python
   # Before: from code.hhi import calculate_hhi
   # After:
   from hhi_calculator import calculate_hhi
   ```

### What NOT to Extract

- Code that only one repo uses — leave it in the data repo
- Code tightly coupled to specific data structures — it's not reusable yet
- Code where the "API" is "run this script with these globals" — that's a script, not a package

---

## Support

For questions about migration:
- Open an issue in the mintd repo
- Ask in the lab Slack channel
- Check the [mintd documentation](../README.md)
