# vocab

The shared language of mintd. Read this first if a term in the code or commit messages is unfamiliar. New concepts go in here as they land.

Each entry is **definition → why it exists**. If a term has a stronger meaning than ordinary English would suggest, that's where the "why" lives. An entry with no "why" is one the code gives no reason for.

---

## Core roles: Producer and Consumer

Most other terms make sense only against one split: **who originated a piece of data, and who depends on it**. These are roles, not project types. A `data_` project that imports another `data_`'s outputs to derive its own is both, and `check_project` gives every project a producer section and a consumer section. In code the split is `CheckFinding.section` (`producer`, `consumer` or `environment`), set by which artifact a finding reads; `environment` is declared but nothing emits it.

### Producer

A project whose `metadata.json` and DVC-tracked outputs other projects depend on. Any project type can be registered and published. The producer owns:

- Its own `metadata.json` (validated by `Metadata`, below).
- The data products themselves: DVC-tracked files at the paths declared in `data_products.outputs[].path`. `data_products.primary` names one of them and is required only when `project.type` is `data`.
- Its catalog entry (see Catalog projection), sent to the registry as a PR by `mintd registry register`, `mintd registry update`, or the last step of `mintd publish`.

The catalog keys a producer by `project.name`; consumers use that name on the CLI and in an enclave's `approved_products[].repo`. `project.full_name` names the folder its imports land in. A consumer's `.dvc` pointer records the producer's git URL, not its name. Producer findings read the project's own `metadata.json`, `.dvc/config`, DVC pointers, `dvc.yaml` and `dvc.lock`.

Why: the producer section of `mintd check` is what a project owner is responsible for getting right before publishing.

### Consumer

A project that **depends on** another project's data products. Its dependency list is what DVC already records: `.dvc` files under `data/imports/` (written by `mintd import`, the same command as `mintd data import`, which runs `dvc import`) and `dvc.lock` stage deps that carry a `repo:` block. `scan_imports` reads both. An enclave project also records subscriptions in `enclave_manifest.yaml`. Under `--upgrades`, consumer findings compare these against the producer at the pin and at `HEAD`; plain `check` reports each import (and refuses an empty pin) without reading the producer. Producers can keep changing; consumers see a stable view until they explicitly bump the pin (`mintd import <name> --bump`, `mintd enclave bump`). `mintd check --upgrades` reads the producer at `HEAD`, but only to compare.

Why: the consumer section of `mintd check` is what a project owner is responsible for keeping current as upstreams move.

### Producer↔consumer contract (two layers of authority)

- **Catalog**: how a consumer turns a name into a producer. Consumers read `repository.github_url` (required), `project.type` (where `data clone` puts the repo), `project.full_name` (the import folder) and `data_products` (default path selection for `data import` and `data clone`). Consumer code never reads `ownership.*` or `governance.*`. `mintd data ls` reads the entry's `storage` block to list S3 objects.
- **Producer at a pinned commit**: `ProducerView`, read only for `data_products` (primary and outputs). Drift compares the producer's DVC pointer md5 at the pin and at `HEAD`, not its metadata. The storage location for fetching comes from the producer's `.dvc/config` at the pin, read by DVC itself and by the import-rescue lane; no consumer reads `storage.*` from metadata at the pin.

`data import`, `data clone`, `enclave add`/`pull`/`bump` and the enclave-manifest checks find the producer through the catalog. `data import --bump` and the `.dvc`-lane checks use the git URL recorded in the `.dvc` file, so an import on disk stays bumpable after its catalog entry is gone.

Why: the catalog is canonical for identity and the producer-at-pin for pipeline correctness. The catalog also carries the latest publish state (`status.*`); `mintd publish` reads its `status.last_published_version` to refuse replacing a newer entry.

---

## Metadata and validation

### `Metadata` ([src/mintd/model.py](src/mintd/model.py))

The typed Pydantic representation of a project's `metadata.json`. `Metadata` is **one class**, not a discriminated union by `project.type`: every field is shared across the four types (`data`, `code`, `project`, `enclave`), `schema_version` must be `"2.0"`, `storage` is optional and `data_products` defaults to empty. The optional `schema` key (`SchemaRef`, attribute `schema_`) names where the product's column schema is committed: `location` is a repo-relative path, and other keys (`standard`, `version`) pass through. `mintd init data` writes `schemas/v1/schema.json`, which `dvc.yaml` keeps `cache: false` so git holds it. Every dump writes the key as `schema` (`serialize_by_alias`), and leaves it out, never `null`, when it is unset. The top level is `extra="allow"`, so unknown top-level keys parse; sub-models silently drop unknown keys. `project.type` still changes behaviour: only `data` must declare `data_products.primary`, and the catalog files each entry under `catalog/<type>/`.

Reading has no single seam. `Metadata.from_json_file(path)` is used only by publish; `check`, the registry verbs and `ProducerView` call `model_validate_json` on bytes they read themselves, and some sites read the file as a plain dict. Writers emit `model_dump_json(by_alias=True, exclude_none=False, indent=2)`, except publish, which merges that dump onto the raw file before writing.

Why publish merges: because sub-models drop unknown keys, writing the bare dump back would permanently delete keys a user added (e.g. `ownership.slack`).

### `Owner` (enum) and `field_metadata`

Names the **role expected to write** a given field, attached to every `Metadata` field as `Annotated[T, Owner.X]`:

- `USER`: humans edit it (`metadata.*`, `ownership.*`, `access_control.*`, `governance.*`, `repository.mirror`, `status.state`).
- `MINTD`: written by `mintd init`, `mintd update metadata`, or `mintd publish` (which sets `mint.version`). Covers `schema_version`, `mint.*`, `project.*`, `storage.*`, `schema_.location` and the rest of `repository.*`.
- `PIPELINE`: `data_products.*` and `status.last_updated` / `last_published_version`. Publish writes the status fields and each `outputs[].last_published`; nothing derives `outputs[].path` or `primary` from DVC, so those are edited by hand.
- `REGISTRY`: reserved; no field uses it.

`field_metadata(model_class, "dotted.path")` → `Owner` walks a dotted path (e.g. `"storage.dvc.remote_name"`) through nested models, unwrapping `Optional[...]` and `list[...]`, and raises `KeyError` for an unknown segment, a non-model intermediate, or a leaf with no Owner. It is Owner's only reader, and only tests call it: Owner is documentation, not enforcement, and no `mintd check` finding looks at it.

Why: it tells someone reading the model what to expect of a field's lifecycle.

### `CheckFinding` and finding `kind` ([src/mintd/check.py](src/mintd/check.py))

The shape of every validation result: a frozen dataclass with `severity` (`error` / `warning` / `info`), `section`, `message`, `field_path`, `source` (the file it came from), `kind` and `hint` (a repair suggestion). `mintd check --json` prints all seven fields per finding. Any error makes `mintd check` exit 1 and blocks `mintd registry register`. `mintd publish` refuses any error except `data_products_output_unpinned`, which its re-check after `dvc push` still blocks on. `mintd registry update` does not run the check. Warnings and infos never change the exit code.

`kind` is the machine-readable discriminator. Producer kinds: `metadata_missing`, `metadata_invalid`, `storage_<state>`, `data_products_primary_missing` / `_mismatch`, `data_products_output_<state>`, `dvc_state_unreadable`, `repository_github_url_missing`. Consumer kinds: `drift`, `drift_unknown`, `up_to_date`, `unreachable`, `schema_too_old`, `pin_missing`, `metadata_missing`, `metadata_invalid`, `invalid_manifest`, `catalog_unresolved`, `dvc_state_unreadable`. Summary info findings have kind `None`. Both bump verbs act on kind alone: `up_to_date` does nothing, `drift` re-pins, anything else (including `None`) raises `BumpBlocked`, except that `data import --bump` also re-pins a `schema_too_old` import when the producer's `HEAD` reads.

Why `kind`: `message` is only human text. Hint and exit-code dispatch both read `kind`, so no caller parses messages.

### `check_project(path, *, upgrades=False, remote=False, all_outs=False, ...)` → `list[CheckFinding]`

The unified validation entry point, used by `mintd check`, `registry register`, `publish` (before and after its `dvc push`) and both bump verbs. Takes a **project directory**. An empty list means clean, but a healthy consumer still gets one info finding per import.

- Producer section: `metadata.json` missing or malformed; one `metadata_invalid` per Pydantic error; storage drift (runs even if validation failed); then, for valid metadata, the primary and `repository.github_url`; and, when `.dvc/` exists, unreadable DVC files, a root `dvc.lock` dep recorded with no hash (`dvc_lock_dep_unhashed`, a warning), a `dvc.yaml` dep holding files git ignores and DVC doesn't track (`pipeline_input_untracked`, a warning, also for a dep outside the project; `pipeline_input_unchecked`, a warning, for what it can't read: a nested `dvc.yaml` or `dvc.lock`, a templated dep, git failing), and the delivery gate.
- `remote=True` (`mintd check --remote`): `_check_remote` asks S3, read-only, whether every pinned file version the working tree and HEAD record is there. The remote is read through `_push_verify.pinned_remote`, push's own test, which reads `version_aware` as dvc does (`_storage_state.remote_is_version_aware`: quotes stripped, and a `worktree = true` remote is version-aware): not a version-aware S3 remote is `remote_not_checked` (info); a remote with its own keys, or an out with a remote of its own, is a `remote_unreadable` error, as push refuses both. HEAD covers every `.dvc` file it tracks outside a subrepo, including one deleted from the working tree; no `.gitignore` hides one, as git never ignores a tracked file (the hint for one `.gitignore` excludes says to stop ignoring it or `git rm` it, as push skips it). That covers the declared outputs' publishable outs, or all of them with `all_outs` (`--all`), as the identity DVC pushes with (the remote's profile, then mintd's `aws_profile`). It probes through `prove_pins`, with no memo, once per pin and recorded md5, so each tree is judged by its own bytes. The findings are `remote_pin_missing`, `remote_pin_wrong_bytes` and `remote_pin_unpinned` as errors; `remote_unchecked` as an error where S3 gave no clear answer (a pin with no verdict is unproven, never fine), and as a warning for a multipart upload the cache can't prove; and `remote_unreadable` as an error for access denied. An entry with no version ID is left to the offline unpinned finding where that covers it, a declared out in the working tree. Otherwise it is a `remote_pin_unpinned` error: at HEAD, which nothing offline reads, when the working tree's copy of the file has its ID (a committed `dvc commit -f`, the working tree since re-pinned; also for a folder out written back as a bare tree hash); and under `--all`, for an undeclared out in either tree. So nothing is reported twice. A HEAD pointer dvc couldn't read is a `dvc_state_unreadable` error. Only the root `dvc.lock` is read, as push verification reads it; a nested one, in either tree, is a `remote_unchecked` warning naming it. Git is read relative to the project (`HEAD:./<path>`, `ls-tree` from the project), so a project below its repository's root reads the same; before the first commit only the working tree is checked (`has_commit`: a branch with no ref at all), and any other git failure, a broken HEAD ref included, raises.
- `check_staged(path)` (`mintd check --staged`, offline): refuses (`staged_drops_version_ids`) a commit whose staged `.dvc` files or root `dvc.lock` drop S3 version IDs from files whose md5 it leaves unchanged, as `dvc commit -f <stage>` does to a stage's folder out. It compares against HEAD's copies of every pointer the commit changes, deleted ones included, and HEAD's root `dvc.lock` whenever a pointer changes (an out can move from an unchanged lock into a new `.dvc` file), each tree read with its own `dvc.yaml` (HEAD's only beside a lock HEAD has) through `publishable_scope`. A file is matched by its S3 identity, its path, so renaming the pointer or the stage, or moving an out between a `.dvc` file and a stage, still compares. Its ID is dropped only when no staged entry for the same bytes at that key still records one, since two pointers can record one key. A folder out written back as a bare `.dir` md5 whose tree isn't in the local cache is compared by the tree hash of HEAD's file list. A changed md5, added IDs, a removed out or pointer, a new file and the first commit pass, and so do this project's readable pointers on a remote that isn't version-aware (`pinned_remote`: nothing is pinned there). HEAD's copies are screened too: one dvc couldn't read is `staged_unchecked` when the commit records a file with no ID, since read as no outs it would hide every ID it records. The remote's own refusals (its own keys, an out with its own remote) are `staged_unchecked`. The index is read relative to the project (`git diff --cached --relative`, `:./<path>`), so a project below its repository's root reads the same and another project's staged pointers elsewhere in the repository are not its business. Staged pointers it doesn't read are `staged_unchecked`, naming them: a subrepo's (another DVC project inside this one, whatever this project's remote is), an error; and a nested `dvc.lock` the commit changes or deletes, a warning, as in `--remote`, since re-running a nested pipeline is everyday work. A nested lock's pins are named but not checked. When git can't read the repository the command fails rather than pass; and a staged pointer dvc couldn't read (`_unreadable_dvc_files`, which parses as dvc does, so a duplicated key counts, and runs before the remote is read) is `dvc_state_unreadable`. `read12` reads an impossible date (ruamel's bare `ValueError`) as unparseable, as it does any YAML error, for every caller. The path need not be the project's root: the project is found as dvc finds it, at the nearest folder up from the path that holds `.dvc/`, never past the repository's top level (found by its `.git`), so `proj/code` checks `proj`. With no DVC project there, the whole commit's staged list is read: a staged `.dvc` or `dvc.lock` is `staged_unchecked`, naming them; none means nothing to check (an enclave repo). A missing path, or one outside a git repository, is `staged_unchecked` before any git call. `--staged` takes neither `--all` nor `--upgrades`. The scratch trees are written by `_atomic.write_tree`, which writes git's bytes back as they were, so a pointer that isn't UTF-8 is named rather than a traceback. No hook runs it: run it before committing.
- Consumer section: every import from `scan_imports` plus `enclave_manifest.yaml` subscriptions. An empty pin is always a `pin_missing` error. Without `upgrades` no producer is read: the `.dvc` lane is fully offline, but the enclave-manifest lane still resolves each approved product through the catalog, which refreshes the registry clone. With `upgrades` it resolves each producer at the pin and at `HEAD` and reports drift or the producer error.

Why: other modules query `check_project` instead of re-implementing checks.

### Storage drift (`StorageState` / `inspect_storage`)

A pure classifier comparing the `metadata.json` `storage` block with the `.dvc/config` remote: `fresh` (neither present), `partial_meta_only`, `partial_dvc_only`, `bucket_empty`, `name_mismatch`, `url_mismatch`, `initialized`. Anything other than `fresh` or `initialized` is an error `storage drift detected: <state>` with a repair hint.

Why it runs even when validation failed: drift is independent of metadata-schema validity.

### Delivery gate (`Delivery` / `DeliveryState`)

`_dvc_state.delivery` answers, offline from a tree's pointers and `dvc.lock`, whether `mintd import <name> --path <path>` would land bytes. States: `pinned`, `via_parent` (under a pinned `.dir` out), `untracked`, `not_pullable` (only `cache: false` or `dvc import` outs), `unpinned` / `partial` (on a version-aware remote, no / some files carry an S3 version ID), `missing_from_manifest`. Only `pinned` and `via_parent` are deliverable. `check` runs it on the primary and every `outputs[].path` and emits one `data_products_output_<state>` error per undeliverable path (`partial` reports as `unpinned`). When an import with a recorded pin lands no file, `data import` runs the same function on the producer's tree at that pin; if the tree cannot deliver the path, or cannot be read, it fails (`UndeliverableImport`) instead of reporting success.

Why: `metadata.json` holds a human's claim about outputs and DVC holds the truth. Without the gate, a product could declare a path DVC tracks nowhere, or one with no S3 version IDs, pass `check`, publish, and hand every consumer an empty directory at exit 0.

---

## Catalog and registry

### Catalog projection (`CATALOG_EXCLUDED_PATHS` / `CatalogEntry`)

The catalog entry is `Metadata.model_dump(mode="json")` with the top-level keys in `CATALOG_EXCLUDED_PATHS` (`schema_version`, `mint`) removed. Everything else ships, including `storage`, `data_products`, `ownership`, `governance`, `status`, `schema` (where the column schema is committed), and any unknown top-level key (a strict-xfail test pins that leak). There is no per-field filter. `CatalogEntry` is a Pydantic model with `extra="allow"` and no declared fields; `to_catalog_entry()` builds it on write and `_catalog_serializer.deserialize()` builds it from registry YAML on read. Properties `name`, `project_type`, `description`, `repo_url` return `''` when a key is missing.

Why: the excluded fields describe the `metadata.json` file itself, not the project. Deriving the shape from `Metadata` instead of a hand-defined class keeps the model and the catalog from drifting apart.

### `CatalogClient` (`Protocol`) ([src/mintd/catalog.py](src/mintd/catalog.py))

The interface for every catalog read and write, six methods: `register(metadata, *, dry_run=False, reporter=None)` → `RegisterResult` (raises `CatalogAlreadyExists` on a duplicate name); `update(...)` → `UpdateResult` (raises `CatalogNotFound` if the name is not registered); `fetch(name)` → `CatalogEntry` (`CatalogNotFound` if missing, `CatalogEntryInvalid` if the file exists but is broken); `list(filter=None)`, whose only narrowing is `CatalogFilter(project_type=...)` (`mintd data list --type`); `status(name)` → `RegistrationStatus`; `sync()` → entry count, after a refresh.

`UpdateResult.changes: list[FieldChange]` is the field-by-field diff `mintd registry update` prints, computed against the registry's main copy: one `FieldChange(field_path, before, after)` per leaf, with dicts recursing and lists compared whole. `pr_reused` is true when the change was stacked onto an already-open PR. Publish uses the same type for its preview "Catalog diff".

Two implementations: `InMemoryCatalogClient` (a dict, used only in tests) and `GitCatalogClient` (production; the CLI always builds it, and `registry_url` must be set in config).

Why: callers depend only on the shape, and the shared contract tests in `tests/test_catalog.py` run against both implementations through one parametrized `client` fixture.

### `GitCatalogClient` and the catalog cache (`CatalogCache`)

The registry repo is cloned to `<cache_dir>/registry` (default `~/.cache/mintd/registry`) with layout `catalog/<type>/<name>.yaml`. Before every read and write, `ensure_fresh()` clones it, or runs `git checkout -f main`, `git fetch origin`, `git reset --hard origin/main`. `mintd registry sync` is that refresh plus a count. `register` branches `register/<name>` off main, writes the YAML, commits, pushes and opens a PR with `gh pr create`. It refuses when the entry is on main or a local pending record exists. `update` works on `update/<name>`.

**Catalog update onto an open PR**: an empty diff or `--dry-run` returns after the usual registry refresh, but before the branch and PR queries and before any commit, push or PR. Otherwise mintd asks the registry whether `update/<name>` exists (`git ls-remote`) and, only if it does, whether a PR is open on it (`gh pr list`). An existing branch is fetched and committed on top of; the commit is skipped when it already carries an identical entry, so retries are idempotent. An open PR is reused; a leftover branch with no open PR gets a new one. A rejected push raises `RegistryBranchExists`, which `registry update` reports with a `gh pr list --head` hint.

Why checkout before `reset --hard`: an interrupted register can leave the clone on `register/<name>`, and resetting that branch to `origin/main` would silently corrupt it. Why two questions: "does the branch exist" and "is a PR open" are separate. A leftover branch with no open PR would otherwise make every push from a fresh main-based branch a non-fast-forward. Both questions go to the registry, not to per-machine state.

### `RegistrationStatus` and pending registrations (`.mintd_pending.json`)

`RegistrationStatus` is a frozen dataclass: `state` is `"registered"`, `"pending"` or `"not_found"`, plus `pr_number`. `GitCatalogClient.status` returns `registered` if the entry is on main, `pending` if the local pending file has a record, otherwise `not_found`; it never queries `gh`. The pending file, `<registry work dir>/.mintd_pending.json`, holds one `PendingRegistration(name, pr_number, kind, created_at)` per name and is written atomically after every non-dry-run register, and after every non-dry-run update with a non-empty diff, whether that update opened a new PR or reused an open one. `mintd registry status` with no name lists it. Nothing removes entries once a PR merges; `status` still reports `registered` because main is checked first.

Why: lets `status` and `register` know about a PR that has not merged yet. `update` asks the registry instead, because this file is per-machine: a PR opened from a laptop is invisible on the cluster.

### `mintd registry` verbs

`register [path]` loads `metadata.json`, refuses on any `check_project` error, refuses a project with uncommitted changes, then opens the registration PR. `update [path] [--dry-run]` skips the check, prints each `FieldChange`, and opens or updates the PR. Both refuse a `metadata.json` whose `schema_version` is not `"2.0"` and point to `mintd update metadata`. `status [name]` and `sync` are described above. `CatalogEntryInvalid` (a registry YAML that does not parse or validate) is caught once for every verb, with a hint to fix the entry and run `mintd registry sync`.

Why register refuses uncommitted changes: importers read the commit on the git remote, not your disk.

`rebuild [--check] [--registry PATH]` runs in a checkout of the registry. Each entry under `catalog/` is rebuilt from its product: the repository the entry names is cloned at its default branch's HEAD (one commit, no DVC data), and only the product's own fields are copied (`catalog.COPIED_LEAVES`: description, tags, team, maintainers, the declared outputs and `status.state`). The entry records the commit as `source.commit` and its date as `status.last_updated`. A product whose `metadata.json` is missing or invalid, declares an output path a consumer can't import, or fails offline `mintd check` keeps its entry as it was, with `source.held` (since, commit, why); its next good commit clears it. Differences in name, storage, classification or schema, and a credential in the product's own URL, are reported, never copied. `--check` writes nothing and exits 1 when any entry would change or is held; `--json` gives each entry's verdict.

Why only those fields: the registry's own workflows act on the rest (repository permissions from `access_control`, bucket policies from `storage`, a public mirror from `repository.mirror.url`), so a product must not be able to set them by editing its own file. The entries are the join record: a product with no entry gets none.

---

## Producer side: publishing

### Publish transaction (`mintd publish`) ([src/mintd/publish.py](src/mintd/publish.py))

Six steps in order: write the bumped `metadata.json`, `dvc push`, commit, `git tag v<version>`, push commit and tags, update the catalog (a registry PR). Before step 1, `check_project` must report no errors other than `data_products_output_unpinned`, and the working tree must be clean. After `dvc push` the full check runs again; any error rolls back `metadata.json` and stops before commit, tag or catalog update. A failed `dvc push` also rolls back. Later failures leave a partial state with recovery hints, and a rerun at the same version is idempotent because publish adopts a tag it already created on `HEAD`. Without a TTY, `--yes` is required.

**Version semantics** (`mint.version`): it must be plain semver `MAJOR.MINOR.PATCH`; otherwise `InvalidCurrentVersion` names the release segment to write back. With no argument publish bumps PATCH; an explicit version must be valid semver and not lower than the current one (`VersionNotIncreasing`). Before any write, publish also refuses when the catalog entry's `status.last_published_version` is newer than this repo's `mint.version`, or equals the version being published, unless the run is a rerun (nothing to commit and the tag already on `HEAD`). Publish sets `mint.version`, `status.last_published_version`, `status.last_updated` and every `outputs[].last_published`; the two timestamps are left out of the diff.

Why the catalog update is last: it is the advertisement. Once the entry is out, consumers resolve `--rev v<version>` against the remote, so everything they need must already be there. The unpinned exemption exists because publish's own `dvc push` records the S3 version IDs; the re-check catches a push that reports success but pins nothing. Why the catalog guard: `client.update` replaces the entry wholesale, and this repo's `mint.version` knows nothing of a release made from another clone. Why the timestamps are excluded: they change on every run. Counting them would make every retry commit again, moving `HEAD` past the tag the failed attempt created, so the tag could never be adopted.

---

## Consumer side: imports, pins, producer view

### `DataDependency` / `scan_imports()` ([src/mintd/imports.py](src/mintd/imports.py))

`DataDependency` is a frozen record of one import: `source`, `kind` (`dvc_file` or `dvc_lock_stage`), `producer_repo` (`repo.url`), `contract_pin`, `output_path` (producer-side; empty for `dvc.lock` deps), `local_path`, optional `artifact_md5` and `stage_name`. `scan_imports(repo_root, under="data/imports")` reads every `*.dvc` under `data/imports/` that has a `repo` block (plain `dvc add` pointers are skipped) and every `dvc.lock` stage dep with `repo:`. It dedups on `(producer_repo, local_path, contract_pin)`, preferring the `.dvc` row. Read by `mintd check` and `mintd data list --imported`.

Why: it unifies the two ways DVC records imports on the consumer side.

### Import namespace (`data/imports/<full_name>/<producer path>`)

`mintd import` (or `mintd data import`) writes each output to `<dest_root>/<namespace>/<the producer's own output path>`, relative to the folder it runs in. `--dest-root` defaults to `data/imports`; the namespace is the producer's `project.full_name` (falling back to the catalog name) and must be a single plain folder name. The import writer and the bump reader share one definition. Import, `--bump` included, runs from any folder inside a project (a `.dvc/` there or above, found as dvc finds it, by `dvc_root`); outside one it stops before reading the catalog or running dvc, and points at `mintd clone` for the whole product or `mintd init` for a new project.

Why: without the namespace, two products that both publish `data/final/` would collide. Mirroring the full producer path, not just the basename, keeps `data/final/` and `archive/final/` apart under `--all`.

### Pin (`contract_pin` / `artifact_pin`)

A consumer's record of which producer commit it imported and, where recorded, which exact bytes it got. There is no `Pin` type; pins are plain strings, and the names differ by lane:

- `.dvc` import: `contract_pin` is the pointer's `repo.rev_lock` (the commit `dvc import --rev` resolved), next to an optional `artifact_md5` (`outs[0].md5`). A missing, null or non-string `rev_lock` becomes `""`, which `mintd check` reports as a `pin_missing` error even without `--upgrades`.
- Enclave: an `approved_products[]` row holds a single `pin` (a commit), shared by every row of the same repo. `downloaded[]`, `transferred[]` and the transfer manifest carry `contract_pin` + `artifact_pin` (the md5 from the imported `.dvc`).

`mintd check` never lets an empty pin mean `HEAD` (it reports `pin_missing`), and a bump records the resolved SHA, never the string `HEAD`. `enclave pull` does not check: a `source_path` row with an empty `pin` is imported without `--rev`, that is, at `HEAD`.

Why the non-string refusal: YAML 1.1 reads an unquoted `rev_lock: 0123456` as octal, so `str()` would mint a different, valid-looking pin. Refusing gives a loud `pin_missing` instead of a quiet wrong comparison.

### `ProducerView` / `ProducerError` / `Fetcher` ([src/mintd/producer.py](src/mintd/producer.py))

`ProducerView` is a frozen dataclass `(repo, pin, metadata)`: a producer's `metadata.json` at one commit. `ProducerView.at(repo, pin)` is strict and raises `ProducerError`; `at_head(repo)` resolves the remote `HEAD` and returns `(view, sha)`; `try_at(repo, pin)` returns the error instead of raising, with `pin == ""` meaning `HEAD`. Bytes are cached on disk under `~/.cache/mintd/producers/`, keyed by `(repo, pin)`, and re-validated on every read. Helpers: `outputs()`, `output_paths()`, `primary_or_raise()`. Used by `check --upgrades`, `data import --rev`, `data import --bump`, `enclave add`, `enclave bump` and `enclave pull`.

`ProducerError.Reason` has five values: `unreachable`, `pin_missing`, `metadata_missing` (translated from the fetcher's `FetchError`), and `metadata_invalid`, `schema_too_old` (from validation; any `schema_version` other than `"2.0"`). Consumer findings reuse these names as their `kind`. `Fetcher` ([src/mintd/_producer_git_ops.py](src/mintd/_producer_git_ops.py)) is the Protocol that reads one file from a producer repo at a commit; `GitArchiveFetcher` uses `git archive --remote` and falls back to a shallow clone.

Why: `ProducerView` is the runtime object that *is* "producer at pin", with a typed failure taxonomy at the construction boundary: fetchers raise transport errors and views raise typed view errors. `fetch_metadata_at_head` returns the resolved SHA because the consumer records it; the clone fallback exists because GitHub disables `upload-archive` by default.

### Resolver

How a consumer turns a product name into the paths to fetch. There are two:

- `data._resolve_paths`, shared by `data import` and `data clone`, reads the **catalog entry**: `--all` → every `data_products.outputs[].path`; else `--path` (repeatable) → exactly those; else `data_products.primary`. Exception: `data import --rev R` with neither `--path` nor `--all` takes the primary from `ProducerView.at(repo, R)`. Clone checks `--path` against the catalog at the default rev, or against the cloned `metadata.json` when `--rev` is given; a clone with no flags pulls everything DVC tracks.
- `enclave._resolve_outputs`, used by `enclave pull`, reads the **producer at the pin**: `source_path` → that path; `all` → every output at the pin; else the primary at the pin.

Why one resolver for both data verbs: their selection semantics cannot drift.

### Drift (`mintd check --upgrades`)

An import has drifted when the producer's DVC pointer md5 for the imported path (the recorded output path or enclave `source_path`, else the primary at the pin) differs between the pinned commit and `HEAD`; for an `all` subscription the whole path→md5 map is compared. Kinds: `drift` (warning; bump re-pins), `up_to_date` (info; bump is a no-op), `drift_unknown` (warning; the comparison could not be made: a pointer or `HEAD` was unreadable, an `all` producer lists no outputs, there is no recorded path and no primary, or the path is absent at `HEAD`; bump refuses). Producer problems at the pin surface as `unreachable` / `schema_too_old` (warning) or `pin_missing` / `metadata_missing` / `metadata_invalid` (error).

Why: producer metadata has no per-output content identity (`last_published` is a per-publish stamp), so bytes committed without a `mintd publish` still count and the pointer is the ground truth. An unreadable pointer is never `up_to_date`: silent degradation to "up to date" is the bug class this lane exists to kill.

### `mintd import --bump` (`bump_import`)

Re-pins one existing import to the producer's current `HEAD` SHA, selecting it by the same `(name, --path)` pair a plain import uses. It reads the import's consumer finding from `check_project(upgrades=True)`: `up_to_date` changes nothing, `drift` rewrites the same `.dvc` via `dvc import --rev <head_sha> --force` on the path the import recorded (or `HEAD`'s primary if it recorded none). `schema_too_old` re-pins the same way when `HEAD` reads (check judges the schema at the pin only); when `HEAD` is too old as well it raises `BumpBlocked` with that finding, and when `HEAD` cannot be read, with a `drift_unknown` one. Any other kind raises `BumpBlocked` with the finding attached.

**Payload backup** (`<dest>.mintd-bump-backup`): `--bump`, and `data import --force` when a `.dvc` already sits beside the destination, rename the existing payload aside before `dvc import`. It is restored if the transfer raises (including Ctrl-C) and deleted on success. If a backup is already there, the last transfer was interrupted: the command refuses with `StaleBackupExists` and names two ways back, `dvc checkout --force <pointer>` or restoring the backup.

Why: `check_project` is the one place that finds drift and `bump_import` the one place that acts on it, so detection is not duplicated. The concrete SHA, not the symbolic `HEAD`, keeps pin semantics. Why the backup: dvc treats an existing directory destination as a container, so the payload has to move first; a same-directory rename is atomic and costs no extra disk. Refusing on a stale backup keeps a retry from deleting what may be the last complete working copy.

---

## Moving data

### File lanes: data vs cache vs share

mintd moves files to S3 three separate ways. `mintd data push` / `publish`: DVC-tracked, versioned outputs that can be pinned and cited. `mintd cache push|pull|ls`: untracked working-tree files mirrored to the project's own prefix, durable but unpinned. `mintd share put|get`: a one-off handoff of a single file to `share/<user>/…`, not catalogued, not pinned, not tied to a repo. `cache push` refuses DVC-tracked paths and points one-offs at `mintd share put`.

Why: the CLI's own messages keep the lanes apart: "the repo file cache (S3) is durable but unpinned — for versioned, citable outputs use: mintd data push".

### Pointer and out

A **pointer** is a git-tracked DVC file that records data by hash: a `.dvc` file or a `dvc.lock` (`_POINTER_PATHSPEC`). DVC never reads a `.dvc` file that `.gitignore` rules exclude, tracked or not, so `data push`, `data pull`, `publish` and `check` leave it out too, `data push` names the ones it skipped, and a target that names one is refused. A pointer is **cut short** when it isn't whole (`check_dvc_file`, `check_lock`): no YAML; a null or wrong type where DVC writes a mapping or list (outs, an out, `files`, a file entry, `cloud`, `stages`, a stage, a stage's outs); a count that isn't a number, or a hash or version ID that is a mapping or list; or an out with no path or (outside a `dvc import`) no hash. A `dvc.lock` out with `hash` but no md5 isn't cut: a push to a version-aware remote writes a declared stage that never ran that way, and readers skip it. DVC reads many such files as tracking nothing, or less than they record, and exits 0. Both parsers raise `PointerCutShort`, a `MintdError` naming the file, so pull, clone, `check`, import and the delivery verdict stop on it; a `dvc.lock` is checked only beside a `dvc.yaml`. A folder pointer cut between two file entries reads as a smaller folder, since DVC writes no file count beside the list, and so does one recorded before files were added. The folder on disk is the only reference: `unlisted_on_disk` names the files it holds that a version-aware folder pointer doesn't list, as DVC would hash them (`.dvcignore` above it, its built-in ignores, subrepos), leaving OS junk (`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`) out of the comparison. `data push` refuses before upload and `check` reports an error (so publish refuses), whether or not the pointer is committed or its shorter tree cached, for `.dvc` files and every `dvc.lock` a push reads, nested pipelines included. A `.dvc` file or folder at the top of a tracked folder makes DVC record it as empty, and the message says so. With the folder not on disk, only the push's commit step's tree check is left. Before any upload, `data push` refuses a pointer in its scope that is cut short (`does_not_parse`); a push naming a path reads every pointer to find it, so any cut pointer stops it. It also refuses, on every remote type, a stage behind its `dvc.yaml` (`stages_behind`): a stage `dvc stage list` names (foreach and matrix instances, as dvc expands them) with no hashed lock entry, or no `dvc.lock` at all (the refusal then says the pipeline hasn't run yet), or one recording fewer hashed outs than it declares. The stage hasn't run, or the lock was cut. A `dvc.yaml` dvc can't read refuses the push with dvc's message. mintd reads pointers, `dvc.yaml` and `dvc.lock` as DVC does, as YAML 1.2 (`load12`), so a stage named `on` or `no` keeps its name. Only the stages a push includes count: a push naming other stages (a nested pipeline's by `sub/dvc.yaml:<stage>` or its out's path) or `.dvc` files goes ahead. A full push's refusal spells out, shell-quoted, the `dvc repro` command and the command that pushes the rest. The lockfiles in scope are those DVC reads: beside a `dvc.yaml` that `.gitignore` doesn't exclude, outside any subrepo. An **out** (`DvcOut`) is one output a pointer records: a file, a `.dir` directory, or a files-format directory with per-file entries; outs in `dvc.lock` are stage outs. On a version-aware remote, `dvc push` also writes S3 version IDs into the pointers. So after uploading, `mintd data push` lists every pointer that differs from `HEAD` (`uncommitted_pointers` under `--json`, `null` if git could not answer). It then commits those pointer files by path and pushes them to the branch's upstream of the same name (`commit_pushed_pointers`), and the result is under `git` in `--json`. It never pulls, rebases or forces. It leaves the commit to the user, says why and exits 1 in any of these cases:
- a changed pointer records something importers couldn't pull (`judge_pointers`): missing version IDs under the default remote, or a folder list that is empty or names a tree the cache never held;
- another tracked file also differs from `HEAD`;
- the push went to a remote importers don't read;
- the project isn't its git repository's top level.

A target inside a tracked output pushes the whole output. `--no-commit` uploads only.

Why: importers read the producer's pushed commit, never its working tree, so a pointer that is not committed and pushed is one no importer can see yet.

### Reading DVC's own files

The shared readers of the files DVC itself writes or reads live in one module, `_dvc_state`: `.dvc` pointers, `dvc.lock`, `dvc.yaml`, `.dvc/config` (with `config.local` on top) and the cache's layout, each read the way dvc reads it. They parse YAML with `load12` (YAML 1.2, so `on` and `no` stay names; a value its type can't hold is invalid YAML, so a pointer with one is cut short). A few older call sites still parse DVC files with their own `yaml.safe_load`, in `check`, import rescue, `enclave`, `imports` and one CLI handler; moving them onto the shared readers is a follow-up. `.dvc/config` is read once by every caller, with quoting and inline comments as dvc treats them. `_fast_sync_ops` keeps the S3 code and the cache writers and imports what it reads from `_dvc_state`; `_dvc_state` imports no other mintd module but `mintd.errors`. What a push records, and the folder comparison built on it (`unlisted_on_disk`), live in `_pin_proof`, which talks to S3.

Why: when five places parsed `.dvc/config` each its own way, a quoted value, an inline comment or a remote set in `config.local` meant different things to fast sync, the push check and publish.

### `mintd data remove`: stop tracking, keep the data

`mintd data remove <pointer>` runs `dvc remove` without `--outs`: the pointer and its `.gitignore` line go, and the data stays on disk. The command names what stayed (never its contents), for a `.dvc` pointer or a root stage, and warns about the outs whose files git now sees as untracked (neither tracked by HEAD nor ignored), so a `git add` would commit them: delete them, or add them to `.gitignore`. Under the scaffold's `/data/**` rule git still ignores files under `data/`.

Why: the name reads as "delete", so the command says plainly what it did and didn't do.

### Version-aware remote / out

A DVC remote with `version_aware = true`, which `mintd init` always sets. Its S3 key is the file's real path (`<prefix>/<dvc file's dir>/<out path>`), and pulling it needs the per-file S3 `version_id` the producer's push recorded in the pointer or `dvc.lock`. An md5-keyed out instead lives at `<prefix>/files/md5/XX/…`. Without version IDs, a pointer on a version-aware remote pulls nothing at exit 0.

Why `init` sets it: the S3 key is then the file's real path, which is mintd's mental model and matches `metadata.storage.versioning = True`.

### Pin repair and the push check ([src/mintd/_push_verify.py](src/mintd/_push_verify.py), [src/mintd/_pin_proof.py](src/mintd/_pin_proof.py))

On a version-aware remote, `mintd data push` and `mintd publish` don't trust `dvc push`'s report. An **entry** is one file version a pointer records (`publishable_scope`: what a `dvc push` uploads, so not repo imports, `cache: false` or `push: false` outs, nor a `.dvc` file DVC skips because of `.gitignore`). A pin **proves** its file (`prove`) when S3, asked for that version at the file's key, returns a single-part ETag equal to the md5, or a multipart ETag equal to the one recomputed from the cache file. A multipart pin with no cache file passes on size alone, but only when this command didn't set it and the commit leaves its record as the last commit has it.

- **Before the upload** (`prepare`), push refuses a `--remote` other than the default, a remote with its own access keys, and outs with their own remote. For each pin that doesn't prove its file (`plan_repair`):
  - **re-attach**: the object at the key proves it, so its ID goes back into the pointer and nothing is uploaded;
  - **strip**: the cache holds it, so the ID is removed and DVC uploads it;
  - otherwise push stops, writing nothing.

  `write_repair` changes only `cloud.<remote>`, with one exception: a folder in the `.dir` form (what `dvc commit -f` and `dvc repro` write) with a file re-attached is rewritten as the per-file list DVC writes after a push, and only if that list hashes to the folder's tree. It re-reads each file it wrote. Every `dvc push` of the command runs with one throwaway `DVC_SITE_CACHE_DIR`.
- **After the upload**, `restore` puts back records DVC wasn't asked to change. If the upload fails or is interrupted, `uploading` instead puts every pointer file back byte for byte as it was before `prepare` wrote anything: DVC rewrites a pushed out's pointer even then, listing only the files that reached S3, and the rerun re-attaches those and uploads the rest. `judge` then proves every pin this push set, or whose record differs from HEAD's. Only its verdict prints the ✓ line and the `verify` counts. The commit carries exactly the pointer files it judged, byte for byte.

`ProofMemo` (`proven-versions.sqlite` under `cache_dir`) remembers full proofs, so a later push HEADs only a sample of 16 of them.

Why: on a version-aware remote `dvc push` can print "Everything is up to date" and upload nothing (a stale site cache, or a version check that a denied ListObjectVersions empties). Its post-push rebuild also pins whatever object is at a path without checking its bytes. A ✓ from DVC's report shipped pins that pointed nowhere.

### Fast-sync (`try_fast_pull`) ([src/mintd/_fast_sync_ops.py](src/mintd/_fast_sync_ops.py))

mintd's own pull path for every non-import DVC out on an S3 remote: each object is downloaded straight into `.dvc/cache` (version-aware outs at their pinned S3 version, md5-keyed outs at `<prefix>/files/md5/…`) via temp file, md5 check, fsync, rename, without `dvc pull`. It needs bundled dvc ≥ 3.66 and < 4.0, an S3 remote, boto3, AWS credentials and bucket versioning; if any is missing every target is degraded with a named reason. A spot-check HEADs the pinned versions of up to 5 outs, and an out that has drifted is blocked. `--dvc-arg` is ignored on fast-sync paths.

Why: plain `dvc pull` is documented broken on version-aware outs (rehash-on-pull re-downloads, StorageKeyError tuple crashes), so those outs must be served directly or fail loudly.

### Pull lanes: `fallback` / `blocked` / `incomplete`, and `PullSummary`

`try_fast_pull` sorts every target it could not fully serve into one of three lists on `FastPullResult`; `data_pull` then routes each list. `dvc_pull_can_serve(out)` (true for an import or a non-version-aware out) decides fallback vs blocked.

- **fallback**: plain `dvc pull` *can* genuinely serve it: dvc-imports, md5-keyed outs fast-sync could not serve, targets with no `.dvc` file (including bare stage-out paths), and pointers that parse to no outs. Fully-cached fallback targets are checked out; the rest get a scoped `dvc pull <targets>`, never `targets=None`. Non-imports are pulled in batches per argv shape, each import alone with the import-rescue lane behind it.
- **blocked**: a version-aware out fast-sync could not serve (guard fired, spot-check drift, fetch failed). Never handed to plain `dvc pull`. Fully-cached ones are checked out and count as pulled; the rest are reported as `cannot pull <t>: <reason>` with a `mintd data pull <t>` retry hint and count as errors. `blocked_reasons` names why, per target.
- **incomplete**: a version-aware out whose per-file downloads failed after retries, so the cache holds partial blobs (md5-keyed outs with failures go to fallback instead). Never checked out, never pulled; reported with the failed-file count and counted as an error.

`data_pull` returns a `PullSummary`: `targets_pulled` counts targets that landed, not files; `error_count` and `failed_targets` cover every target left absent (hard-blocked, incomplete, still missing after the checkout retry, failed pulls, imports whose rescue failed), each reported individually first. A non-zero `error_count` makes `mintd data pull` exit 1; `data clone` reports it in its result.

Why: `dvc_pull_can_serve` is the single fallback-vs-blocked split, so the policy cannot diverge, and an out plain `dvc pull` cannot serve fails loudly instead of quietly landing nothing. `data_pull` never lets a per-target pull error escape, so `error_count` is the only signal that keeps scripts and CI from mistaking a partial pull for success.

### Import-rescue lane (`rescue_import_pull`) ([src/mintd/_import_rescue_ops.py](src/mintd/_import_rescue_ops.py))

A per-import fallback inside `data pull`, run only after the import's own `dvc pull` failed or exited 0 without materializing it. It reads the producer's `.dvc/config` at the pinned rev via git, downloads each object from the producer's bucket, verifies its md5 against the pinned manifest, seeds the consumer's DVC cache, then runs `dvc checkout` and confirms the out landed. It is not part of fast-sync.

Why: when a producer's version-aware bucket holds the objects but its committed lock recorded no `version_id`, no dvc invocation can materialize the import, even though the bytes on S3 are fine and md5-verifiable.

### Verify-after-checkout

`dvc checkout` exit 0 is not trusted: after every checkout, mintd stats each target's workspace outcome (shape-aware — file vs dir vs files-format dir vs provably-empty dir), retries missing outs once single-target, and reports still-missing ones as errors. The same check follows a zero-exit `dvc pull` and the rescue lane's checkout. Companion: `_shape_groups` / `_checkout_grouped` ([src/mintd/data_ops.py](src/mintd/data_ops.py)) never mix `.dvc` paths and bare paths (stage outs, suffix-less targets) in one argv; the same split applies to `dvc pull` and `dvc push`. The principle generalizes: **verify outcomes, don't trust exit codes.**

Why: in dvc 3.67.1, `index_from_targets` on a mixed argv keeps the last `.dvc` target's index when a later bare target raises StageNotFound, so the checkout exits 0 having materialized almost nothing. Homogeneous argvs are safe.

### Share lane (`mintd share put` / `get`) ([src/mintd/_share_ops.py](src/mintd/_share_ops.py))

`share put <file> [--as SUBPATH]` uploads one file to `share/<user>/<name>` in the bucket named by config `storage_bucket_prefix`; a `--as` ending in `/` is a folder, otherwise a rename. `share get <ref> [--out PATH]` downloads it, needs no sender identity and refuses to overwrite. A **share ref** is the key without its leading `share/` (`<user>/<sub>/<file>`); `parse_share_ref` refuses anything that could normalise outside `share/<user>/`. Folders are not supported. mintd sets no expiry on share objects.

**`share_user`** is the `<user>` segment: config `share_user` if set (a slug, not `.` or `..`), else the slugified config `author`. If neither works, `ShareError` points to `mintd config setup`.

**Stratum T / Stratum P** are the two layers of `_share_ops.py`. Stratum T is verb-agnostic S3 transport (`head_remote_object`, `upload_object`, `download_object`, `file_sha256`, `TransferError`), forbidden from referencing Config, Reporter, `SHARE_PREFIX` or the ref grammar; uploads always send `ChecksumAlgorithm="SHA256"` and downloads `ChecksumMode="ENABLED"`. Stratum P is the share policy on top. `_cache_ops` reuses Stratum T unchanged for all its bytes.

Why: `get` stays zero-identity because the author is already in the ref. `share_user` is never taken from the AWS identity, because the shared `[mintd]` profile is the same for every lab member. Keeping transport free of share policy lets the repo file cache be a second policy skin over the same bytes path.

### Repo file cache (S3) (`mintd cache push|pull|ls`) ([src/mintd/_cache_ops.py](src/mintd/_cache_ops.py))

Mirrors untracked working-tree files to `s3://<bucket>/<prefix>/cache/<repo-relative-path>`, with bucket and prefix read from the project's DVC remote in `.dvc/config`, never recomputed. `push` refuses DVC-tracked paths, anything under `.git/` or `.dvc/`, and paths outside the project. `pull [PATH …]` restores objects to their repo paths (`ls [PATH …]` lists them; a path means that file or folder, mapped as push maps it), never overwrites a DVC-tracked out, and skips a local file with different content unless `--force`. Both refuse to run if any DVC out's S3 key falls under `cache/`.

**Skip-compare** (`decide_push` / `decide_pull`): push skips a file only when the remote object exists, its size matches, and its `mintd-sha256` user metadata equals the local SHA256; a missing object, size difference, missing metadata (e.g. uploaded with `aws s3 cp`) or mismatch all upload. Pull skips only when a local file has the same size and SHA256.

**`mintd-lane=cache` tag**: every `cache push` upload carries this S3 object tag. After a real push to a versioned bucket, mintd reads (never writes) the bucket lifecycle configuration and warns if no enabled `NoncurrentVersionExpiration` rule filters on the tag.

Why the name: every user-facing string says "repo file cache (S3)" to avoid confusion with DVC's `.dvc/cache` and mintd's `config.cache_dir`. Pull refuses silent overwrites because a pull can land anywhere in the tree. Only a verified match skips; anything unverifiable transfers. The tag exists for an admin's lifecycle rule; without one, overwrites on a versioned bucket are billed forever.

---

## Enclave

### Enclave project: outside copy and inside copy

An `enclave` project is a consumer whose data must reach an air-gapped machine. The repo exists twice, each copy with its own `enclave_manifest.yaml`. The **outside** copy, on a networked machine, subscribes (`enclave add`), fetches (`enclave pull`) and bundles (`enclave package`). The **inside** copy, on the air-gapped box, receives the archive and lands it with `python3 land.py` (stdlib only, Python 3.8-compatible, no network, DVC or mintd) or `mintd enclave verify`. Only the transfer archive crosses, one way. CLI messages also say "inside the enclave" for the enclave repo directory: `enclave pull` must run from the manifest's directory.

Why: a shipped transfer cannot be recalled and the inside box cannot fetch anything, so everything about what to bundle is decided outside.

### `EnclaveManifest` ([src/mintd/enclave.py](src/mintd/enclave.py))

Typed Pydantic view over `enclave_manifest.yaml`, the default `--manifest` of every `mintd enclave` verb. Fields: `schema_version` (`"2.0"`), `enclave_name`, and three lists of row types:

- `approved_products` (`ApprovedProduct`: `repo` (the catalog name), `registry_entry`, `pin`, optional `source_path` or `all`): the active subscriptions.
- `downloaded` (`DownloadedItem`: `repo`, `output`, `contract_pin`, `artifact_pin`, `fetch_strategy` (only `dvc-import` is ever written), `downloaded_at`, `local_path`): outside-enclave staging, written by `enclave pull`.
- `transferred` (`TransferredItem`, `frozen=True`: `repo`, `contract_pin`, `artifact_pin`, `transfer_date`, `transfer_id`, `local_path`): the audit trail. `enclave package` appends to it on the outside manifest ("bundled"); `enclave verify` or the shipped `land.py` append to it inside the enclave ("landed").

When the file already exists, `EnclaveManifest.save()` diffs `transferred[]` against the on-disk version and raises `AppendOnlyViolation` (carrying `path` and `changed_indices`) if any entry was modified, removed, or reordered; appending is allowed. Saves are atomic, and keys the model does not declare are dropped. The guard covers only writes through `save()`: hand edits and `land.py`'s text append bypass it.

Why: `transferred[]` is provenance that cannot be re-derived, so a crashed write must never truncate it, and a wrong row can only be removed by hand-editing.

### Subscription

One `approved_products[]` row. `enclave add --source-path X` subscribes to one output, `--all` to every `data_products.outputs[]` (re-resolved on each pull), and a bare add to the primary. A repo holds **one pin**: a second add inherits it, a disagreeing `--pin` is refused, and `bump` moves every row of the repo at once. `enclave remove` takes `--source-path X`, `--primary`, or `--all` (every row of the repo; not the meaning `--all` has on `add`), and also drops the matching `downloaded[]` rows.

Why one pin per repo: `check` keys consumer findings on the repo alone and `enclave bump` takes a repo, not a row. Remove drops `downloaded[]` rows because `enclave package` selects from `downloaded[]`, and a revoked row left there would ship across a one-way transfer.

### `enclave_bump` (`mintd enclave bump <repo> [--force]`)

Re-pins every row of one repo to the producer's `HEAD` SHA (via `ProducerView.at_head`, validating the primary when a row needs it), using `EnclaveManifest.apply_pin_bump` and saving through the append-only seam. Without `--force` it reads the repo's findings from `check_project(upgrades=True)` and decides on `kind`, like `--bump`. `--force` skips that gate. Only pins change; `downloaded[]` rows at the old pin are pruned by the next `enclave pull`.

Why a blocked row blocks the whole repo: a repo holds one pin, so a partial bump is not possible.

### `enclave_pull` (`mintd enclave pull [repo] [--force]`)

Outside-enclave fetch. For each resolved output it runs `dvc import --rev <pin>` into staging, then moves the result to `downloads/<repo>/<artifact_pin[:7]>-<pull date>/` and appends a `DownloadedItem`. Outputs already recorded are skipped unless `--force`. When every subscription succeeds, `downloaded[]` rows whose pin is no longer approved are pruned; recorded downloads missing on disk are reported by name, never re-fetched unasked. The CLI must run from the manifest's directory.

Why: dvc runs with the enclave directory (the manifest's parent) as its working directory; run from an enclosing repo, it would cache the producer's restricted bytes into that repo and still exit 0. Superseded rows are pruned because `enclave package` would otherwise ship both old and new bytes.

### Transfer archive (`enclave package` → `land.py` / `enclave verify`)

`mintd enclave package [repo] [--resend]` bundles the selected `downloaded[]` folders into `transfers/<transfer_id>.tar.gz` (`transfer-YYYY-MM-DD-NNNNNN`) with `_transfer_manifest.yaml`/`.json`, a generated `README.md` and `land.py` ([src/mintd/files/land.py.txt](src/mintd/files/land.py.txt)). Rows whose `(repo, artifact_pin)` already appear in `transferred[]` are skipped unless `--resend`. Inside the enclave, `python3 land.py` (stdlib only) or `mintd enclave verify <extracted_dir>` moves each product to `data/<repo>/<version_folder>/` and appends `transferred[]` rows; verify refuses a wrong `enclave_name` or an existing destination and is idempotent on `(repo, contract_pin, artifact_pin)`.

Why: the archive is the only artifact guaranteed to cross the gap, so the lander and instructions travel inside it. Package keys on `(repo, artifact_pin)`, "have these bytes already crossed?", so a metadata-only pin bump does not re-ship identical bytes.

---

## Config and plumbing

### Config (`~/.config/mintd/config.yaml`) ([src/mintd/_config.py](src/mintd/_config.py))

A frozen Pydantic `Config` (`extra="forbid"`) loaded from `$MINTD_CONFIG_DIR/config.yaml` or `~/.config/mintd/config.yaml`; a missing file means all defaults. Fields include `registry_url`, `cache_dir` (default `~/.cache/mintd`), `timeouts`, `author`, `share_user`, `storage_endpoint`, `storage_bucket_prefix`. Managed with `mintd config show | setup | validate`. **Timeout tiers** are wall-clock caps for spawned dvc/git commands: `timeouts.fast` (default 30 s) for quick calls, `timeouts.transfer` (default unbounded) for add, status, remove, import, push, pull, checkout and git transfers.

The CLI loads config and passes values down. The exception is the scaffold renderer (`_templates/_render.py`), which calls `Config.load()` itself to fill template fields such as `author` and `organization`, falling back to defaults if loading fails.

**AWS profile (`[mintd]`)**: `Config.aws_profile_name` is `"mintd"` when the shared credentials file (`$AWS_SHARED_CREDENTIALS_FILE`, else `~/.aws/credentials`) has a `[mintd]` section, otherwise `None`. Every S3 client mintd opens (`mintd._s3.create_client`) picks its profile in the order dvc does: the remote's `profile` (`.dvc/config`, or a per-machine override in `.dvc/config.local`; every other remote key stays the committed one), else an exported `AWS_DEFAULT_PROFILE` or `AWS_PROFILE` (botocore's order), else `[mintd]`. The import-rescue lane reads a producer's bucket with the consumer's own credentials, because the producer's `profile` names a profile on the producer's machine. With none named it uses boto3's default chain, on purpose, so CI and sandboxes work. A named profile that doesn't exist stops the command with `error: AWS profile 'X' (named in .dvc/config) isn't in <credentials file>` and a hint, never a silent fallback to other credentials; plain `dvc` fails on it too. `data ls` and share take their storage from the catalog or the config, not from a DVC remote, so only the environment's profile and `[mintd]` apply there. dvc gets `[mintd]` as `AWS_PROFILE` via `setdefault`, so an exported `AWS_PROFILE` wins there too; `mintd init` writes it as the DVC remote's `profile`. `mintd config setup` offers to write the section (mode 0600, atomic, `[default]` untouched), and `mintd config validate` reports the profile it will use and where its name came from.

Why checkout is a transfer: it materializes cache blobs, which can mean minutes of copying on non-reflink filesystems, longer than the 30 s fast cap allows.

Why add, status and remove are transfers: add hashes every byte, status hashes every file it has not hashed yet, and remove copies hardlinked or symlinked files out of the cache, so on a large folder each can outlast the 30 s fast cap.

### Subprocess seams (`DvcOps`, `RegistryGitOps`, `Fetcher`, `InitOps`)

External commands are spawned behind Protocols that tests replace with fakes: `DvcOps` (dvc); `RegistryGitOps` ([src/mintd/_registry_git_ops.py](src/mintd/_registry_git_ops.py)), git and gh for the catalog client, the catalog cache, and the project-side git calls of `check`, `publish`, `registry register`, `data push`, `data remove` and `import`, plus the producer clone in `clone`; `Fetcher` (the other git reads against producers); `InitOps` (`git init`, `dvc init`, remote setup). `RegistryGitOps` failures become typed exceptions: `GitOpError` (with `GitTagError`, `GitTagAlreadyExists`, `RegistryBranchExists`), `GhAuthError`, `GhNotInstalled`, `PRConflictError`. Only five modules are exempt from the lint ban on spawning through `subprocess` directly (`_subprocess`, `_registry_git_ops`, `_init_ops`, `_fast_sync_ops`, `_producer_git_ops`), a list `tests/test_substrate_rules.py` pins. Every dvc spawn uses `dvc_cmd()` (`sys.executable -m dvc`, the dvc in mintd's own env) and `dvc_env()` (`DVC_NO_ANALYTICS=1`); every git spawn uses `git_env()`, which pins git's messages to English.

Why: one seam normalizes subprocess failures into typed exceptions and lets implementations be swapped without touching callers. mintd parses git's stderr, so a translated `fatal:` line would silently stop matching. dvc telemetry is off because an unannounced outbound request on project creation is a governance question for enclave and lab-only projects.

### `mintd init`: project types, classification tier, resume sentinel

**Project types and scaffold names**: `mintd init <type> <name>` takes `data`, `code`, `project` or `enclave`. The folder and `full_name` are `data_<name>`, `prj_<name>`, `enclave_<name>`, or the bare name for `code`. `dvc init` runs for every type except `enclave`.

**Classification tier** (`labonly` / `public` / `licensed`): chosen at an interactive prompt (non-enclave types; fails without a TTY). It sets the S3 prefix: `lab/<project>/`, `pub/<project>/`, or `<slug>/<project>/` for licensed data. The cache code reads that prefix from `.dvc/config` and never looks at the tier.

**Resume sentinel** (`.mintd-init-incomplete`): written before the first scaffold file and removed only after a fully successful init. If a later run without `--force` finds it next to a `metadata.json` with the same `full_name`, and every scaffold file is present, init resumes the storage steps instead of refusing on file collisions. A partially rendered tree is still refused (`--force` re-renders it).

Why only `data` must declare a primary: a code/project repo publishes no consumable data product, and an enclave resolves upstream producers' primaries rather than publishing its own. Why a prompt for the tier: the choice is governance-critical and must not be flag-driven. Why a sentinel: its presence makes "this directory is init's own half-finished output" provable rather than inferred.

### v1 → v2 migration (`mintd update metadata`, `mintd config setup --migrate-v1`)

`mintd update metadata [path] [--dry-run]` rewrites a schema-1.x `metadata.json` to `"2.0"` and writes the validated model dump; `--dry-run` lists the keys it strips. `mintd config setup --migrate-v1 FILE` maps a v1 config onto the flat v2 `Config`.

Why the validated dump: legacy fields the v2 model ignores are stripped, so a migrated file is byte-identical to what `mintd init` scaffolds.
