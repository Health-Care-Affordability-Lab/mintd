# Implementation Plan: Simplify mintd Project Taxonomy

**Date**: 2026-03-02
**Branch**: `feature/simplify-project-taxonomy`
**Status**: Implemented
**Complexity**: MEDIUM

## Requirements Restatement

Remove the `infra` project type from mintd, simplifying to two scaffolded types:
- **`data`** - All repos that produce datasets (simple, derived, parameterized)
- **`project`** - Research analysis, papers, tables/figures

Pure code packages (Python/R/Stata) should use standard language tooling without mintd scaffolding.

### Success Criteria
- [ ] `mintd create infra` command removed
- [ ] All `infra` references removed from codebase
- [ ] Documentation updated with new taxonomy guidance
- [ ] Tests updated (infra tests removed, data tests expanded)
- [ ] Optional metadata fields added for discoverability
- [ ] Migration guide for existing `infra_*` repos
- [ ] Skill documentation updated

---

## Phase 1: Remove `infra` Template and CLI Command

### 1.1 Delete InfraTemplate Class
**File**: `src/mintd/templates/infra.py`
- Delete entire file (109 lines)

### 1.2 Remove Template Import
**File**: `src/mintd/templates/__init__.py`
- Remove `from .infra import InfraTemplate`
- Remove `InfraTemplate` from `__all__`

### 1.3 Remove Language Strategy Methods
**File**: `src/mintd/templates/languages.py`
- Remove `get_infra_structure()` from `PythonStrategy` (lines 102-120)
- Remove `get_infra_files()` from `PythonStrategy`
- Remove `get_infra_structure()` from `RStrategy` (lines 182-199)
- Remove `get_infra_files()` from `RStrategy`
- Remove `get_infra_structure()` from `StataStrategy` (lines 257-269)
- Remove `get_infra_files()` from `StataStrategy`

### 1.4 Delete Infra Template Files
**Directory**: `src/mintd/files/`
- Delete `README_infra.md.j2`
- Delete `pyproject_infra.toml.j2`
- Delete `DESCRIPTION_infra.j2`
- Delete `package.R.j2`
- Delete `package.ado.j2`
- Delete `package.sthlp.j2`

### 1.5 Remove CLI Command
**File**: `src/mintd/cli/create.py`
- Delete `infra()` command function (lines 178-239)

### 1.6 Remove from API
**File**: `src/mintd/api.py`
- Remove `infra` from type validation in `create_project()`
- Remove `InfraTemplate` import
- Remove infra case from template selection

### 1.7 Remove from Utils
**File**: `src/mintd/utils/__init__.py`
- Remove `elif project_type == "infra"` case from `format_project_name()`

### 1.8 Remove from Registry
**File**: `src/mintd/registry.py`
- Remove `'infra': 'infra'` from `type_dir` mappings (line 79, 212, 346)
- Remove `"infra"` from project type iterations (line 211)

### 1.9 Remove from Update Utils
**File**: `src/mintd/cli/update.py`
- Remove `"infra": InfraTemplate` from template_map (line 210)

### 1.10 Update Module Docstring
**File**: `src/mintd/__init__.py`
- Change "(data_, prj__, infra_)" to "(data_, prj_)"

---

## Phase 2: Update Documentation

### 2.1 Update Project Types Documentation
**File**: `docs/project-types.md`
- Remove entire "Infrastructure Projects (`infra_*`)" section (lines 203-256)
- Add new section: "When to Create a Standalone Package"
- Expand `data` section to cover derived/parameterized pipelines

### 2.2 Update CLI Reference
**File**: `docs/cli.md`
- Remove `mintd create infra` command documentation
- Update governance flag documentation
- Update examples

### 2.3 Update Quick Start
**File**: `docs/quick-start.md`
- Remove infra example (line 23-24)
- Add note about packages using standard tooling

### 2.4 Update Stata Documentation
**File**: `docs/features/stata.md`
- Remove infra type from examples
- Update type parameter documentation

### 2.5 Update README
**File**: `README.md`
- Remove infra example (line 44)
- Update project type overview

### 2.6 Create Migration Guide
**File**: `docs/migration/infra-to-data.md` (NEW)
- Document how to migrate existing `infra_*` repos
- Include checklist for reclassification
- Package extraction guidance

---

## Phase 3: Add Optional Metadata Fields

### 3.1 Update Metadata Schema
**File**: `src/mintd/utils/__init__.py` or new `src/mintd/schemas.py`
- Add optional fields to metadata validation:
  - `configurations: list[str]` - Dataset configurations supported
  - `methods: list[str]` - Methods implemented
  - `data_dependencies: list[str]` - Upstream data products

### 3.2 Update Data Template
**File**: `src/mintd/templates/data.py`
- Add commented-out optional fields to generated `metadata.json`

### 3.3 Document New Fields
**File**: `docs/metadata.md` (update or create)
- Document optional discoverability fields
- Provide examples for complex data products

---

## Phase 4: Update Tests

### 4.1 Remove Infra Tests
**File**: `tests/test_api.py`
- Delete `test_create_project_infra()` (lines 67-84)

**File**: `tests/test_templates.py`
- Delete `test_infra_template()` (lines 59-76)

**File**: `tests/test_cli.py`
- Delete `test_create_infra_help()` (lines 36-41)

**File**: `tests/test_utils.py`
- Delete `test_infra_project()` (lines 279-282)

**File**: `tests/test_stata_integration.py`
- Remove infra test case (lines 57-60)

### 4.2 Add Derived Data Product Tests
**File**: `tests/test_api.py`
- Add test for data product with `data_dependencies`
- Add test for data product with `configurations`
- Add test for data product with `methods`

### 4.3 Update Remaining Tests
- Verify no infra references in parameterized tests
- Update any fixture data that mentions infra

---

## Phase 5: Update Skills

### 5.1 Update mintd-guide Skill
**File**: `~/.claude/skills/mintd-guide.md`
- Remove infra references
- Update project type guidance
- Add package extraction guidance

### 5.2 Update mintd-data-migration Skill
**File**: `~/.claude/skills/mintd-data-migration.md`
- Add section on migrating infra repos to data
- Update examples

---

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Breaking existing `infra_*` repos | HIGH | Migration guide + repo-specific migration PRs before release |
| Registry expects infra catalog dir | MEDIUM | Remove infra from type iterations, leave catalog dir structure alone |
| Users confused by removal | LOW | Clear documentation, changelog entry, skill updates |
| Existing scripts reference `mintd create infra` | MEDIUM | Error message pointing to migration guide |

---

## Dependency Graph

```
Phase 1.1-1.4 (Template cleanup)
      ↓
Phase 1.5-1.9 (CLI/API cleanup)
      ↓
Phase 2 (Documentation) ←──── Can run in parallel after Phase 1
      ↓
Phase 3 (Metadata fields) ←── Can run in parallel after Phase 1
      ↓
Phase 4 (Tests) ←───────────── Depends on Phase 1-3
      ↓
Phase 5 (Skills)
```

---

## File Change Summary

| Action | Files |
|--------|-------|
| **DELETE** | 7 (1 Python module, 6 templates) |
| **MODIFY** | 15 (9 Python, 5 docs, 1 README) |
| **CREATE** | 1 (migration guide) |

---

## Testing Strategy (TDD)

Before implementing each phase:

1. **Write failing tests first** that verify infra removal:
   - `test_create_infra_raises_error()` - CLI should reject infra type
   - `test_api_rejects_infra_type()` - API should raise ValueError
   - `test_format_project_name_no_infra()` - Utils should not handle infra

2. **Write tests for new features**:
   - `test_data_with_configurations_metadata()`
   - `test_data_with_methods_metadata()`
   - `test_data_with_dependencies_metadata()`

3. **Run existing test suite** to catch regressions

---

## Estimated Effort

| Phase | Hours |
|-------|-------|
| Phase 1: Template/CLI removal | 2-3 |
| Phase 2: Documentation | 2-3 |
| Phase 3: Metadata fields | 1-2 |
| Phase 4: Tests | 1-2 |
| Phase 5: Skills | 1 |
| **Total** | **7-11 hours** |

---

**WAITING FOR CONFIRMATION**: Proceed with this plan? (yes/no/modify)
