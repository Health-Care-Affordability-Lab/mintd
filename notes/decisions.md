# Decisions

A running log of design decisions for mintdv2. Newest at the top. Each entry: what was decided, when, and why.

---

## 2026-05-12 — `check_project(path)` takes a project directory, not a file path

**Decision:** `check_project(path: Path)` expects `path` to be the project directory; it looks for `metadata.json` inside. Passing the `metadata.json` file directly is not supported.

**Why:** SLICE-1 was internally inconsistent — the `check.py` and `test_check.py` stubs both assume a project directory, but the Definition of Done line passed the JSON file directly. Resolved in favor of project-directory because: (a) slice 4 will add `imports.yaml` checks that need a project root anyway; (b) the natural CLI shape is `mintd check .`; (c) "checking a project" is the user-level concept, not "checking a file". Updated the slice's Definition of Done line to match.

**Reversal cost:** Low while there are no callers. Once the CLI ships, reversal would require a deprecation cycle.

---

## 2026-05-12 — Drop `configurations` and `methods` from `ProjectMetadataBlock`

**Decision:** `ProjectMetadataBlock` carries only `description` and `tags`. The `configurations: list[str]` and `methods: list[str]` fields listed in the original SLICE-1 spec are removed from the model, the minimal fixture, and the slice notes.

**Why:** No concrete use case. They were carried forward from the 1.1 shape without a clear consumer in the 2.0 design. Easier to add them back later (with a real shape, e.g. structured objects instead of `list[str]`) than to maintain dead fields that every fixture has to fill in.

**Reversal cost:** Low. Adding fields back means re-adding the annotations, regenerating fixtures, and bumping any code that reads them — but no migrations of stored metadata since nothing has been written yet.

---

## 2026-05-12 — Option α (Annotated + FieldRole dataclass) for Owner × Audience

**Decision:** Owner × Audience are attached to fields via `Annotated[T, FieldRole(Owner.X, Audience.Y)]` using a frozen `FieldRole` dataclass, read back through Pydantic's `model_fields[name].metadata`. Option β (`Field(json_schema_extra={...})`) was the alternative.

**Why:** α is typed end-to-end — `field_metadata` returns a `tuple[Owner, Audience]` with no string-key lookups or stringly-typed enum values floating in JSON-schema dicts. The slice notes flagged α as the slight preference for exactly this reason. β would have made it easier to leak the annotation into generated JSON Schema (free side-effect), but we don't have a consumer for that yet, and α keeps the annotation private to the Python layer where it belongs.

**Reversal cost:** Medium. Switching to β means rewriting every field annotation and the `field_metadata` walker. Worth doing only if we need the annotations to show up in generated JSON Schema for an external tool.

---

## Template for new entries

```
## YYYY-MM-DD — Short title

**Decision:** What was decided. State it as a concrete rule, not a discussion.

**Why:** The constraint or motivation. If it came from a specific incident, conversation, or spec section, name it.

**Reversal cost:** Low / Medium / High, and what reversing would touch. Helps future-you decide whether to revisit.
```
