# vocab

The shared language of mintd. Read this first if a term in the code or commit messages is unfamiliar. New concepts go in here as they land.

Each entry is **definition → why it exists**. If a term has a stronger meaning than ordinary English would suggest, that's where the "why" lives.

---

## Core roles: Producer and Consumer

The whole design splits on **who originated a piece of data and who depends on it**. Most other terms make sense only against this split.

### Producer

A project that **publishes** data products others can depend on. Anything that has output paths declared in `data_products.outputs[]` is acting as a producer.

The producer owns:
- Its own `metadata.json` (the local typed view — see `Metadata` below).
- The CATALOG-audience subset of its metadata, which it announces to the registry via `register` / `update` (slice 2).
- The data products themselves (DVC-tracked files at paths declared in `data_products.outputs[]`).
- The PRODUCER_CONTRACT fields (storage config, output paths) at every commit it has tagged — these are authoritative for consumers reading them at a pinned commit.

A producer is identified by its `project.name` (and `project.full_name`). Consumers refer to it by name.

### Consumer

A project that **depends on** another project's data products. A consumer can also be a producer — these are roles, not project types. A `data_` project that imports another `data_`'s outputs to derive its own is both.

The consumer owns:
- Its `imports.yaml` (slice 4) — the typed list of which producers it depends on, at which commits.
- The pins it records (`contract_pin` = producer commit, `artifact_pin` = DVC md5) — together the "what did I depend on, exactly" snapshot.

A consumer never reads producer metadata at `HEAD` after first import — only at the pinned commit. This is the producer↔consumer contract: producers can keep changing; consumers see a stable view until they explicitly bump the pin.

### Why the split matters

Most of the design enforces this split structurally:

- `Audience` annotations (slice 1) name which fields are canonical for which side: `CATALOG` (consumers find producers), `PRODUCER_CONTRACT` (consumers fetch from producers), `LOCAL` (only the producer cares).
- The catalog stores only CATALOG-audience fields; producer-at-pin is the source of truth for PRODUCER_CONTRACT-audience fields.
- `check_project` (slice 1) splits findings into `producer` (derivable from metadata.json), `consumer` (from imports.yaml, slice 4), and `environment` (local tools, slice 6).

If you find code conflating these — e.g. a consumer-side fetch that reads the catalog's `storage.bucket` instead of the producer's at-pin metadata — that's a contract violation worth flagging.

---

## Slice 1 vocabulary (in the repo today)

### `Metadata` ([src/mintd/model.py](src/mintd/model.py))

The typed Pydantic representation of a project's `metadata.json`. The single entry point for reading metadata is `Metadata.from_json_file(path)`; the single entry point for writing is `model.model_dump_json()`. Every other read or write site is a bug.

`Metadata` is intentionally **one class**, not a discriminated union by `project.type`. Every field is shared across all project types. `project.type` is informational.

### `Owner` (enum)

Names the **role allowed to write** a given field. Four values:

- `USER` — humans edit this field manually.
- `MINTD` — the mintd CLI writes this on `create`, `storage init`, etc.
- `PIPELINE` — DVC / publish flow writes this (e.g. `last_published_version`).
- `REGISTRY` — the registry server writes this back (status changes during PR review, etc.).

Owner is policy, not enforcement. `mintd check` warns when a USER-owned field looks tool-generated, etc.

### `Audience` (enum)

Names **who reads a given field as canonical**. Four values:

- `LOCAL` — only the producer themselves; never published.
- `CATALOG` — the registry catalog; consumers reading "where is this project?" / "who owns it?".
- `PRODUCER_CONTRACT` — consumers at the producer's pinned commit, reading "where do I fetch the bytes?".
- `CONSUMER` — only meaningful inside the consumer's own `imports.yaml` (slice 4).

The Audience filter drives slice 2's `Metadata.to_catalog_entry()`: only CATALOG fields go to the registry. Audience is **the** seam between producer-local state and what consumers see.

### `FieldRole` ([src/mintd/model.py](src/mintd/model.py))

Frozen dataclass bundling `(owner, audience)`. Attached to every Pydantic field via `Annotated[T, FieldRole(...)]`. Named wrapper instead of a bare tuple so introspection code can scan with `isinstance(m, FieldRole)`.

### `field_metadata(model_class, "dotted.path")` → `(Owner, Audience)`

Helper that walks a dotted field path and returns the `(Owner, Audience)` tuple. Slice 1's main consumer is testing; slice 2's `to_catalog_entry()` will use it to drive the audience filter.

### `CheckFinding` ([src/mintd/check.py](src/mintd/check.py))

The shape of every validation result. Carries `severity` (`error` / `warning` / `info`), `section` (`producer` / `consumer` / `environment`), `message`, and an optional `field_path`. See the module docstring for severity semantics.

### `check_project(project_dir)` → `list[CheckFinding]`

The unified validation entry point. Takes a **project directory** and returns one finding per problem. Empty list means clean.

Slice 1 implements only the producer section; consumer and environment land in slices 4 and 6.

---

## Slice 2 vocabulary (designed, not yet built)

### `CatalogClient` (`Protocol`)

The four-method interface for the registry catalog:

- `register(metadata)` — announce a new project. Raises `CatalogAlreadyExists` on duplicate name.
- `update(metadata)` — sync changes to an existing project. Returns the field-by-field diff.
- `fetch(name)` — look up a single project. Raises `CatalogNotFound` if missing.
- `list(filter=None)` — browse all entries, optionally narrowed.

Two implementations:

- `InMemoryCatalogClient` (slice 2) — backed by a dict; used in tests and as the in-process store before flush.
- `GitCatalogClient` (slice 3) — production adapter; writes to a registry repo via `git` + `gh pr`.

### `CatalogEntry`

The CATALOG-audience subset of a `Metadata`. Produced by `Metadata.to_catalog_entry()`. Consumers reading the catalog see exactly this — never the full `Metadata`.

### `FieldChange` / `UpdateResult` / `RegisterResult`

Result types from the write methods. `UpdateResult.changes: list[FieldChange]` is the field-by-field diff the CLI displays after `mintd registry update`.

### Audience filter

Informal name for the slice-1-driven projection from `Metadata` → `CatalogEntry`. Walks fields via their `Audience` annotation; CATALOG fields go through, others are dropped. Slice 2 is the first place this pays off — if the projection feels forced or requires hand-maintained lists alongside the annotations, the slice-1 design hasn't earned its weight.

---

## Architectural patterns

### Producer↔consumer contract (two layers of authority)

- **Catalog** is canonical for **identity** fields: `project.name`, `project.type`, `ownership.*`, `governance.*`, `repository.*`. Consumers must find producers via the catalog; failure to round-trip these fields on publish is a blocking error.
- **Producer-at-pinned-commit** is canonical for **pipeline correctness** fields: `data_products.outputs[].path`, `storage.bucket`, `storage.prefix`, `storage.dvc.remote_name`. Consumers re-read the producer at the pin; catalog drift on these is a freshness issue, not a correctness bug.

This split is the structural fix for today's `registry update` data_products writeback bug.

### Pin (slice 5+)

A consumer's record of which producer commit they imported, and which exact bytes they got. Two halves: `contract_pin` (git commit) and `artifact_pin` (DVC md5). Always recorded as a pair, never re-resolved silently.

### Resolver (slice 10)

How a consumer turns a producer reference into a fetchable path. Two-step under the new design:

1. Manifest override (`source_path` or `all: true` in the consumer's import entry).
2. Producer's `data_products.outputs[]` at the pinned commit.

The old four-step resolver (manifest → catalog → producer-metadata → convention `data/<stage>/`) collapses to two. The catalog fallback and convention fallback go away.

---

## What's deliberately not in this vocab yet

Listed here so they're not "missing" — they'll get definitions in the slice where they land:

- `Pin`, `DataDependency`, `Imports` — slice 5 (`imports.yaml`).
- `ProducerView`, `Fetcher`, `ProducerError` — slice 6 (`--upgrades` mode).
- `EnclaveManifest`, `ApprovedProduct`, `TransferredItem` — slice 9.
- `RegistrationStatus`, pending registrations — slice 3.
