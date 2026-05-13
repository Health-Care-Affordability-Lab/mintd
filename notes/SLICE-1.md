# Slice 1 — `mintd check` validates `metadata.json`

**Goal:** end-to-end working `check_project(path)` that loads a `metadata.json`, parses it through the Pydantic `Metadata` model, and returns a list of validation findings.

**Concepts introduced:** Pydantic v2 basics, Owner × Audience as `Annotated` metadata, the file-loading pattern, `CheckFinding` shape.

**Files in this slice:**
- `src/mintd/__init__.py` — package marker
- `src/mintd/model.py` — Pydantic `Metadata` + sub-models with Owner × Audience annotations
- `src/mintd/check.py` — `check_project(path) -> list[CheckFinding]`
- `tests/test_model.py` — Pydantic validation tests
- `tests/test_check.py` — `check_project` tests
- `tests/fixtures/metadata_v2_minimal.json` — a small valid fixture
- `pyproject.toml` — declares `pydantic ~= 2.12` as a dep, sets up pytest

**Acceptance criteria:**

1. `Metadata.from_json_file(Path("tests/fixtures/metadata_v2_minimal.json"))` returns a valid model instance.
2. Loading a `metadata.json` with the wrong `schema_version` raises a `ValidationError` with a clear field-by-field message (or via `try_at`-style soft path — see decision below).
3. `check_project(path)` returns a `list[CheckFinding]`:
   - Empty list when the file is valid.
   - A finding with `severity="error"`, `section="producer"`, and a useful message when Pydantic validation fails.
4. Every field in the model is annotated with `Owner` and `Audience`. A helper `field_metadata(Metadata, "ownership.team") -> (Owner, Audience)` returns the annotation for any field path.
5. All tests pass: `pytest tests/`

**Not in this slice (defer to later):**
- CLI command — `mintd check` as a Click command. Slice 1 ends with the Python API working.
- `imports.yaml` / `DataDependency` / consumer-side checks. Add in slice 4.
- Producer-fetch / network checks. Add in slice 4 (ProducerView).
- Catalog disagreement detection. Add in slice 3 (when CatalogClient exists).
- Environment hygiene checks (dvc, git, gh availability). Add in slice 6.
- `--upgrades` mode. Add in slice 6.
- Migration of 1.1 metadata. Slice 1 reads 2.0 only.

---

## What to build, file by file

### `src/mintd/model.py`

The Pydantic `Metadata` model. Single class (no discriminated union); `project.type` is informational. See `docs/plans/metadata-standard.md` → "The model" for the full field list.

For slice 1, you need the *full* model — every field defined — even though slice 1 only exercises validation. Reason: validation is what proves the model covers reality. If a field is missing, a real fixture will fail to parse, which is the bug you want to catch now, not later.

Sub-models to define:
- `Mint` (version, commit_hash)
- `Project` (name, type, full_name, created_at, created_by) — `type: Literal["data", "code", "project", "enclave"]`
- `ProjectMetadataBlock` (description, tags)
- `Ownership` (team, maintainers)
- `AccessTeam` (name, permission)
- `AccessControl` (teams: list[AccessTeam])
- `Governance` (classification, contract_info)
- `Storage` (provider, bucket, prefix, endpoint, versioning, dvc) and `DvcStorage` (remote_name)
- `DataProductOutput` (path, description, primary, last_published) — *no `pin_strategy` field* per the grilling
- `DataProducts` (primary, outputs: list[DataProductOutput])
- `Mirror` (url, purpose)
- `Repository` (github_url, default_branch, visibility, mirror)
- `Status` (state, last_updated, last_published_version)

Top-level `Metadata`:
- `schema_version: Literal["2.0"]`
- `mint: Mint`
- `project: Project`
- `metadata: ProjectMetadataBlock`
- `ownership: Ownership`
- `access_control: AccessControl`
- `governance: Governance`
- `storage: Storage | None = None`
- `data_products: DataProducts = Field(default_factory=...)`  — empty outputs by default
- `repository: Repository`
- `status: Status`

`model_config = ConfigDict(extra='allow')` during transition (tightened in slice 6).

A classmethod `from_json_file(cls, path: Path) -> Metadata`. Reads the file, parses JSON, validates via `cls.model_validate_json`. The single entry point readers use.

### Owner × Audience annotations

Define two enums:

```python
class Owner(StrEnum):
    USER = "user"
    MINTD = "mintd"
    PIPELINE = "pipeline"
    REGISTRY = "registry"

class Audience(StrEnum):
    LOCAL = "local"
    CATALOG = "catalog"
    PRODUCER_CONTRACT = "producer_contract"
    CONSUMER = "consumer"
```

Attach them to fields via Pydantic's `Annotated` + `Field(..., json_schema_extra=...)` *or* via a custom marker class you store in `metadata` and read back. Two patterns work; pick one:

**Option α — `Annotated` with marker dataclass:**
```python
@dataclass(frozen=True)
class FieldRole:
    owner: Owner
    audience: Audience

class Project(BaseModel):
    name: Annotated[str, FieldRole(Owner.MINTD, Audience.CATALOG)]
    type: Annotated[Literal["data","code","project","enclave"], FieldRole(Owner.USER, Audience.CATALOG)]
    ...
```

Read back with `Metadata.model_fields["project"].metadata` (returns the list of annotations attached).

**Option β — Pydantic `Field` with `json_schema_extra`:**
```python
class Project(BaseModel):
    name: str = Field(..., json_schema_extra={"owner": "mintd", "audience": "catalog"})
```

Read back via `Metadata.model_fields["project"].json_schema_extra`.

Option α is more typed and reads cleaner. My slight preference, but β is fine.

Either way, write the `field_metadata(model_class, field_path) -> tuple[Owner, Audience]` helper that walks a dotted path (`"project.name"`, `"ownership.team"`) and returns the annotation. This is what `check_project` will use in later slices to detect "USER-owned field looks tool-generated" warnings — for slice 1, just having the helper is enough.

### `src/mintd/check.py`

A small module. Public surface:

```python
@dataclass(frozen=True)
class CheckFinding:
    severity: Literal["error", "warning", "info"]
    section: Literal["producer", "consumer", "environment"]
    message: str
    field_path: str | None = None

def check_project(path: Path) -> list[CheckFinding]:
    """Validate metadata.json. Returns findings; empty list = clean."""
```

For slice 1, the producer section just runs Pydantic validation:

- File doesn't exist → one error finding ("metadata.json not found")
- File exists but JSON malformed → one error finding ("malformed JSON: <detail>")
- JSON valid but Pydantic refuses → one error finding per Pydantic error (use `model_validate_json` and catch `ValidationError`; iterate `e.errors()` to produce one finding per field)
- All valid → empty list

The consumer and environment sections return `[]` for now (added in slices 4 and 6).

### `tests/test_model.py`

At minimum:

- `test_minimal_fixture_parses` — loads `tests/fixtures/metadata_v2_minimal.json`, asserts the model returns the expected values.
- `test_wrong_schema_version_rejected` — constructs a dict with `schema_version: "1.1"`, asserts `ValidationError` is raised.
- `test_field_metadata_returns_owner_audience` — calls `field_metadata(Metadata, "project.name")` and asserts the returned `(Owner, Audience)` tuple.
- `test_data_products_outputs_default_empty` — constructs a minimal Metadata without `data_products`, asserts `m.data_products.outputs == []`.

### `tests/test_check.py`

- `test_check_clean_file_returns_empty` — runs `check_project` against the minimal fixture, asserts `[]`.
- `test_check_missing_file_returns_error` — runs against a nonexistent path, asserts one error finding.
- `test_check_malformed_json_returns_error` — passes a path to a file with invalid JSON, asserts one error finding.
- `test_check_invalid_schema_returns_error` — passes a path to a 1.1-shaped JSON, asserts findings.

### `tests/fixtures/metadata_v2_minimal.json`

The smallest valid `metadata.json` you can construct. Used by every model and check test. Will grow over time as more slices land.

Required structure (matches the model fields you define):

```json
{
  "schema_version": "2.0",
  "mint": { "version": "0.0.1", "commit_hash": "" },
  "project": {
    "name": "test_project",
    "type": "data",
    "full_name": "data_test_project",
    "created_at": "2026-05-11T00:00:00Z",
    "created_by": "tester"
  },
  "metadata": { "description": "", "tags": [] },
  "ownership": { "team": "test_team", "maintainers": ["tester"] },
  "access_control": { "teams": [{"name": "admins", "permission": "admin"}] },
  "governance": { "classification": "public", "contract_info": "" },
  "data_products": { "primary": null, "outputs": [] },
  "repository": {
    "github_url": "https://github.com/test-org/data_test_project",
    "default_branch": "main",
    "visibility": "private",
    "mirror": { "url": "", "purpose": "" }
  },
  "status": { "state": "active", "last_updated": "2026-05-11T00:00:00Z", "last_published_version": "" }
}
```

`storage` is omitted because it's optional (`None` allowed). That's intentional — the minimal fixture should exercise the "no storage" case.

### `pyproject.toml`

If you're starting a new repo, the minimum is:

```toml
[project]
name = "mintd"
version = "0.0.1"
dependencies = [
    "pydantic ~= 2.12",
]

[project.optional-dependencies]
dev = ["pytest>=7.0"]

[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

If you're starting in the existing repo, you already have `pyproject.toml`; just add the deps changes from PR 0.

---

## Decisions you'll make as you implement

These are small enough that you don't need to grill on them; making the call yourself is part of internalizing the design. If any feel non-obvious, ask.

1. **Owner × Audience annotation style: α (dataclass marker) or β (json_schema_extra)?** My slight rec: α.
2. **`Storage.bucket` empty-string vs. `None` vs. validator-rejected?** Today's code uses empty strings (`""`). My rec: keep empty string allowed at the Pydantic level (it's set later by `mintd storage init`), but `check_project` warns when `storage` is present but `storage.bucket` is empty. Slice 1 just allows it; the warning is added in slice 6.
3. **`Project.created_at` — `datetime` (parsed) or `str` (raw)?** Pydantic parses ISO-8601 strings to `datetime` automatically. My rec: `datetime`, let Pydantic handle.
4. **`Status.last_published_version` empty-string vs. `None`?** Today there's no such field; under the new model, projects pre-first-publish have nothing here. My rec: `str = ""` empty-default. `None` would also work; whichever feels right.
5. **`metadata` (the `ProjectMetadataBlock` field on `Metadata`) — keep that nested name, or rename?** Today's structure has `metadata.metadata.description` which is confusing. The grilling settled on keeping the structure but with typed access (`m.metadata.description` reads ok in code). My rec: keep the name for slice 1 to match today's file shape; reconsider if it grates during slice 2.

---

## Definition of done

- All four tests in `tests/test_model.py` pass
- All four tests in `tests/test_check.py` pass
- `check_project(project_dir)` against a directory containing a valid `metadata.json` prints `[]` (the input is the project directory, not the json file path — `check_project` looks for `metadata.json` inside it)
- `python -c "from mintd.model import Metadata, field_metadata; print(field_metadata(Metadata, 'project.name'))"` prints the `(Owner, Audience)` tuple
- `mypy src/mintd/model.py` is clean (Pydantic v2 has good type hints; should be no warnings)
- You can articulate, without re-reading the spec: what Pydantic gives you that dict access didn't, how Owner × Audience are stored on fields, what `from_json_file` does on each failure mode

If any of those last points is fuzzy after slice 1, slow down and re-read the section that covers it before starting slice 2. Slice 2 (CatalogClient) assumes these patterns are second nature.

---

## When you're done

Pause, write up a short note on what surprised you / what felt clunky / where the design didn't quite fit. Bring those to the slice 2 conversation — they're the inputs to whether the design needs adjustment before I build slice 3+.
