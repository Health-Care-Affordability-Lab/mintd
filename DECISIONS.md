# Decisions

A running log of design decisions for mintdv2. Newest at the top. Each entry: what was decided, when, and why.

---

## 2026-10-19 — `data push` reports uncommitted pointers instead of committing them; `registry register` refuses them

**Decision:** on a version-aware remote `dvc push` writes each object's S3 version ID into the
`.dvc` / `dvc.lock` files, and an importer reads the producer's pushed commit, not its disk. So:

1. `mintd data push` still uploads and exits 0, but when any `.dvc` / `dvc.lock` file in the repo
   differs from HEAD it prints no ✓; it names the files and says to commit and push them. `--json`
   carries them as `uncommitted_pointers`. It checks the whole repo from any subdirectory, as the
   push uploads it.
2. `mintd registry register`, after `check` passes, exits 1 while `git status` shows anything
   uncommitted: tracked changes or untracked files that are not ignored, whatever
   `status.showUntrackedFiles` says (`publish`'s clean-tree check honours that setting, so it
   can pass a tree register refuses). It names them; data goes to DVC or `.gitignore`. There is no list of "the
   files importers read": each one tried missed something (`metadata.json` and `dvc.yaml` on a
   never-committed project, then the `dvc_vars.yaml` every scaffolded `dvc.yaml` loads), and a
   miss passes register while every import fails. The cost: an unrelated untracked file blocks
   register until it is committed, ignored or stashed.
3. Pointers are judged against HEAD, not against what one push rewrote: importers see HEAD
   whichever command dirtied the file.
4. When git cannot answer, outside a git repository included, register fails closed and data push
   warns (`--json` reports `uncommitted_pointers: null`). There is no exception for a directory
   outside git: importers read a git history, so a project without one cannot be imported.
5. `check` keeps judging the working tree, because it is the step before the commit.

Rejected: `data push` commits and pushes the pointers itself.

**Why:** `publish` stays the only verb that writes the producer's git history, and it refuses a
dirty tree so its commit holds only what it wrote. `data push` runs mid-work in arbitrary dirty
trees; committing there would sweep unrelated edits into pushed history, and mintd would have to
decide which pointers a targeted push owns. Exit 0, because the upload succeeded. `register` is the
hard stop because it is the moment the product becomes discoverable, and a warning can be scrolled
past. The guard lives in the register handler, not in `check_project` or the catalog client,
because `publish` re-runs `check` between its own push and its commit, while the pointers are dirty
by design.

**Not covered:** a pointer committed but not pushed, or pushed only to a branch other than the
default branch importers read. Inferring "pushed" from local upstream and branch history broke on
every git workflow variant (repos with no commits yet, branches deleted after a merge, squash
merges, signed commits); the follow-up compares pointer contents with that default branch instead.
`registry update`, which runs no delivery gate at all.

**Reversal cost:** Low. Two git queries, two call sites.

---

## 2026-08-22 — Every fake gets a verdict: licensed by a contract test, or recorded as unlicensed

**Decision:** `tests/test_substrate_rules.py` now carries a census of `tests/_fakes/`. A fake is
either LICENSED (some test parametrizes `params=["fake", "real"]` and runs one body over both it and
the real implementation) or explicitly recorded as UNLICENSED. A new fake fails the ratchet until it
gets one or the other. A second ratchet re-derives the licensed set by scanning, so the literal
cannot drift from what is actually true.

**Why:** substrate rule 2 has been written down since unit 1 — *a fake earns the right to stand in
for a boundary only if some test runs unchanged over both it and the real thing* — but only
`dvc_ops` ever had such a test, and nothing measured the rest. A rule satisfied one time in seven
reads exactly like a rule satisfied.

The cost of not measuring it, from P5: `_fakes/reporter.py` called itself "a deterministic,
**rich-free** Reporter", and rich-free is precisely what made it unable to witness a rich defect.
`enclave pull`'s per-subscription label `[<source_path>]` was being DELETED by rich's markup parser
— the feature was silently non-functional for its primary input — and an absolute `--source-path`
raised `MarkupError`, which is not a `ValueError`, straight out of a data-custody verb. The test
guarding that label injected the fake and was green throughout.

**Mutation testing did not catch it and could not.** Reverting the label reddened that test, so the
mutation table reported the guard as pinned while the feature was broken. Mutation testing proves a
test is sensitive to your code; it says nothing about whether your code survives contact with its
real collaborator. Those are different failures and they need different tools.

`tests/test_reporter_contract.py` licenses ONE property — *the text a caller passes is the text the
user sees, verbatim* — and says so in a BOUNDARY section, because an unstated boundary is how a
licensed fake starts lying again. It is not a claim about styling, spinner frames, wrapping, or
ordering against subprocess output; a test needing those needs the real Reporter directly.

**Reversal cost:** Low. Two literals and two test functions; deleting them loses the measurement,
not any behaviour.

---

## 2026-08-21 — A subscription is a `(repo, source_path|all)` pair, and a repo has ONE pin

**Decision (P5 / issue33, user-ratified):**

1. **`enclave_add`'s duplicate guard keys on the SUBSCRIPTION, not the repo.** An enclave can hold
   several subscriptions to one producer. `subscription_label(ap)` is the rendered form
   (`<path>` / `<all>` / `<primary>`), lifted into `enclave.py` and shared by `enclave list`,
   `check` and the pull status line.
2. **D1 — one pin per repo.** A second `add` inherits the pin already recorded for that repo; an
   explicit `--pin` that disagrees is refused, naming the recorded pin. Rejected: per-row pins,
   which need a row-addressable `field_path` in `check` AND a row-addressable `enclave bump`.
3. **D2 — bare `enclave remove <repo>` refuses when more than one row matches**, demanding
   `--source-path`, `--primary` or `--all`. `--all` on `remove` means "every row" (what bare
   `remove` did before); on `add` it still means the all-outputs subscription.

**Why:** `ApprovedProduct.source_path`, `enclave_remove --source-path` and `enclave_pull`'s per-row
loop were all written for many rows per repo — only the guard disagreed, so a researcher could not
subscribe to a second product from a producer and the CLI offered no way to. D1 is the cheapest
reading that keeps `check` (repo-keyed findings) and `enclave bump` (takes a repo) coherent with no
change to either. D2 exists because bare `remove` was unambiguous only while a repo held one row;
`--primary` exists because without it a repo holding `[path, primary]` has no way to drop just the
primary, and the only workaround is the hand-edit this unit exists to eliminate.

**Consequences that shipped with it, because opening the guard made them reachable:**

- `_all_already_downloaded`'s primary fast-skip cited the deleted guard as its own correctness
  proof. It now refuses to fast-skip any repo holding more than one approved row.
- `apply_pin_bump` moves every row of a repo, not the first.
- `_validated_head_sha` gates the primary check on ANY primary row, not row 0.
- `enclave_bump` decides from EVERY finding for the repo, not the first (blocked > drift > up to
  date) — reading the first made a WRITE verb depend on YAML row order.
- The in-run dedup reads the in-flight list, so overlapping rows import each output once.
- `enclave add` warns at add time when it inherited a pin, naming `bump --force` as the remedy.

**Reversal cost:** Medium. `ApprovedProduct.pin` is already a per-row field, so a D1-written
manifest loads and row-bumps under per-row-pin code with no migration and `schema_version`
unchanged at `'2.0'`. `field_path` is never persisted — it lives in memory and in `check --json`
stdout — so re-keying it later is an output-contract change, not a migration. Reverting D2 would
change a destructive verb's behaviour a second time.

---

## 2026-07-27 — One open catalog PR per product; `registry update` stacks onto the existing branch

**Decision:** When `update/<name>` already exists on the registry, `mintd publish` /
`mintd registry update` fetch that branch, stack the regenerated entry onto its tip,
and reuse the open PR instead of opening a second one. The branching decision is keyed
on whether the REMOTE BRANCH exists (`git ls-remote`), not on whether a PR is open —
those are different questions, and conflating them wedges publish permanently.

**Why:** The lab publishes several versions before a registry admin merges. Previously
the second publish built a branch off a freshly-reset `origin/main`, which can never
fast-forward the remote branch, so `git push` was rejected — as a raw traceback, at
step 5 of 5, after the DVC push, the version commit, and the tag had all landed.

**Why branch-existence and not the PR:** a squash-merged PR leaves its branch behind
with no open PR. Keying on the PR sends that case back down the main-based path and it
fails identically on every subsequent publish, recoverable only by an admin deleting
the branch out of band.

**Accepted cost:** a PR a reviewer has already looked at can gain commits under them.
Judged better than making publishing wait on review latency. `register` is unchanged
— it keeps its local-only `.mintd_pending.json` refusal.

**Reversal cost:** low. One call site (`GitCatalogClient.update`) and one git op.

---

## 2026-05-14 — Drop the `Audience` filter; catalog = `metadata.json` minus a small exclude list

**Decision:** No `Audience` enum, no `FieldRole`, no per-field audience annotations, no two-tier serializer, no advisory `last_synced_at` block. The catalog yaml is `metadata.json` verbatim except for `CATALOG_EXCLUDED_PATHS = {"mint", "schema_version"}` (mint-internal provenance fields that describe the file, not the project).

`Metadata.to_catalog_entry()` becomes one ~5-line method: dump the model, drop the excluded paths, validate into `CatalogEntry`. `_catalog_serializer.py` becomes two `yaml.safe_dump`/`yaml.safe_load` wrappers (~30 LOC). `field_metadata(Metadata, path)` returns `Owner` only.

`Owner` annotations stay on every field — they drive `mintd check`'s "USER field looks tool-generated" warnings and reviewer signal at field-add time. The audience axis was decorative; the owner axis isn't.

**Why:** The audience axis was encoding in code something that's actually a *human contract*: "consumers re-read producer-at-pin for `data_products` and `storage`; everything else they trust from the catalog." Walking through end-state mintd:

1. **No runtime code reads the audience annotation to make decisions.** It filters once at serialize-time and never reads back. Consumers don't introspect field metadata to decide whether to trust a field; they follow the spec.
2. **The on-disk advisory tier (`advisory:` block, `last_synced_at`) wasn't read by anything.** No mintd command branched on whether a field was in the canonical vs. advisory section. Browse UI doesn't exist yet to render the freshness badge.
3. **The publish-flow blocking/non-blocking 10a/10b distinction** was the one place audience drove behavior. Replaced with all-or-nothing: if the catalog write fails, the publish rolls back. The git tag + producer metadata.json are the actual data publication; the catalog is an eventually-consistent index. A subsequent `mintd registry update` repairs any registry-side staleness.
4. **Latency** (the slice-2 retro's load-bearing argument for keeping PRODUCER_CONTRACT advisory tier): still fine. Catalog still has one file per project; browse is still fast.
5. **"Catalog is internal"** removes PII concerns about, e.g., `project.created_by`. Coupling concerns are real but managed by a small explicit exclude list (which is plain English, not a per-field tag system).

**The carve-out worth naming:** if mintd ever gets a public catalog (external consumers, not just lab researchers), "what's in the catalog" becomes a contract with the outside world and PII/sensitive leakage matters. At that point, the audience filter can be reintroduced — it'd be additive work, not a teardown.

**What gets deleted:**

- `Audience` enum + `FieldRole` dataclass + every `_USER_CATALOG` / `_MINTD_LOCAL` / `_MINTD_CATALOG` / `_MINTD_PRODUCER` / `_PIPELINE_PRODUCER` / `_PIPELINE_CATALOG` / `_PIPELINE_LOCAL` alias.
- `project_by_audience()` function.
- `_catalog_serializer.py`'s `canonical_view`, `advisory_view`, `_json_safe`, `ADVISORY_KEY`, `LAST_SYNCED_KEY`, two-tier emission logic.
- All slice-2/slice-3 tests pinning audience-filter and advisory-tier behavior (replaced with a thinner exclude-list + round-trip suite).

**What stays:**

- `Owner` annotations on every field.
- `field_metadata(model_class, path) -> Owner` introspection.
- All registry plumbing — `_registry_git_ops.py`, `_catalog_cache.py`, `pending_registrations.py`, `GitCatalogClient`, `InMemoryCatalogClient`, `CatalogClient` Protocol, `RegistrationStatus`.
- `Metadata.to_catalog_entry()` (simplified — uses the exclude list).
- The pinning model (just `contract_pin`).
- The producer-at-pin contract for consumer-side reads, documented in vocab.md + plan rather than encoded in field annotations.

**Reversal cost:** Medium-high. Reversal would require re-introducing the audience enum + per-field annotations + walker + two-tier serializer + advisory-tier tests. The trigger that would justify the cost is the public-catalog use case noted above.

**Net effect on code size:** [`model.py`](src/mintd/model.py) drops from 263 LOC to ~225 LOC; [`_catalog_serializer.py`](src/mintd/_catalog_serializer.py) drops from 99 LOC to ~30 LOC; total test LOC drops by ~150 (slice-2/3 audience-tier suites collapse). The slice-3 retro question ("does the Protocol seam hold?") still holds — the seam is untouched by this change.

---

## 2026-05-14 — Drop `imports.yaml`; `.dvc` files are the dependency source of truth

**Decision:** No `imports.yaml` file. Consumer-side dependencies are recorded exclusively in the `.dvc` files that DVC writes during `dvc import`. A new `src/mintd/imports.py` provides a typed Pydantic view: `DataDependency.from_dvc_file(path)` parses a single file, `scan_imports(repo_root, *, under="data/imports")` walks the tree. PR 5 (originally "imports.yaml + introduce Pin") collapses to this typed scanner (~60 LOC) — no yaml schema, no migration, no parallel writes.

**Why:** Field-by-field, `imports.yaml` would have duplicated everything DVC's `.dvc` file already records:

| `imports.yaml` field | Already in `.dvc`? |
|---|---|
| `producer_repo` | ✓ `deps[0].repo.url` |
| `output_path` | ✓ `deps[0].repo.path` |
| `contract_pin` | ✓ `deps[0].repo.rev_lock` |
| `local_path` | ✓ `outs[0].path` |
| `artifact_md5` | ✓ `outs[0].md5` |
| `imported_at` | derivable via `git log -- <dvc_file>` |
| `product_name` | resolvable via catalog lookup from URL |

Two sources of truth invite drift — the same failure mode that motivated slice 2's structural register/update fix. The `.dvc` file is written by `dvc import` as part of an atomic-ish operation; a parallel `imports.yaml` mintd would write alongside is exactly the parallel-paths-disagree shape we just spent design effort eliminating.

UX is preserved:

- `mintd data import <name>` → `catalog.fetch(name)` for repo URL + `data_products.primary` → `dvc import <url> <primary>`
- `mintd data import <name> --path <p>` → catalog lookup for URL only → `dvc import <url> <p>`
- `mintd data import <name> --rev <c> [--path <p>]` → `ProducerView.at(url, c)` (PR 6) resolves primary if `--path` omitted → `dvc import --rev <c> <url> <path>`
- `mintd data import <name> --all` → loop over producer's `data_products.outputs[]`
- `mintd data list --imported` → `scan_imports(repo_root)` + render

**Side effects:**

- **PR 5 collapses materially.** Becomes `src/mintd/imports.py` (~60 LOC) + tests. No yaml schema, no migration.
- **PR 7's `mintd check --upgrades`** enumerates dependencies via `scan_imports()` instead of parsing a yaml.
- **PR 10's resolver** "manifest override" step becomes a `.dvc`-file lookup matching the producer + path.
- **The `data_dependencies` drift bug** (plan:13) is structurally impossible — there's no second writer to drift from.

**Reversal cost:** Medium. If `imports.yaml` is ever needed (a non-DVC dependency type, or an external tool that introspects deps without scanning files), it can be added as a denormalized summary written by mintd alongside the `.dvc` files. `DataDependency` would gain a `from_imports_yaml` constructor.

---

## 2026-05-13 — `status.last_updated` and `status.last_published_version` re-annotated to `_PIPELINE_CATALOG`

**Decision:** `Status.last_updated` and `Status.last_published_version` move from `_PIPELINE_LOCAL` to `_PIPELINE_CATALOG`. Owner stays `PIPELINE` (both are auto-stamped by `mintd publish` step 5); audience flips from `LOCAL` to `CATALOG`. The `_PIPELINE_CATALOG` shorthand alias (briefly retired earlier today after the `data_products` reversion) is restored for these two fields.

**Why:** Surfaced during slice-2 retro: `plans/metadata-standard.md:222-223` lists `status.{state, last_updated, last_published_version}` as canonical CATALOG fields. The model had `state` correctly annotated but `last_updated` / `last_published_version` were `_PIPELINE_LOCAL`. Latent inconsistency between model and plan.

The mismatch is load-bearing: consumers answer "is this project stale?" by reading `last_updated`, and "what version do I pin in `imports.yaml`?" by reading `last_published_version`. Both questions are catalog-resolved (the catalog is the only browse-time channel for project-level state) — annotating them LOCAL would have excluded them from the catalog yaml emitted by `_catalog_serializer.py`, breaking those workflows.

Same shape as the `data_products` reversion: realigning the model with the plan's two-layer authority design.

**Reversal cost:** Low. Two field annotations + one alias. No test changes required (the structural `test_catalog_entry_contains_only_catalog_audience_fields` invariant already covers any new CATALOG field that surfaces).

---

## 2026-05-13 — Audience taxonomy for `Metadata`: 3 states (LOCAL / CATALOG / PRODUCER_CONTRACT); CONSUMER does not apply

**Decision:** The slice-2 retro grilling resolved the open audience-taxonomy question flagged on 2026-05-13 (`data_products` re-annotation entry, below). The audience axis on `Metadata` fields is three states: `LOCAL`, `CATALOG`, `PRODUCER_CONTRACT`. `Audience.CONSUMER` is removed from `Metadata`'s taxonomy — it applies to `DataDependency` in `imports.yaml` (slice 5), per `plans/metadata-standard.md:84`. The `_PIPELINE_CONSUMER` shorthand alias retires; the `Audience.CONSUMER` enum value stays as a forward-guard for slice 5.

**Why:** The grilling tested whether the 3-state distinction does load-bearing work or is decoration. Three behaviors fall out of one annotation:

1. **Catalog inclusion** — LOCAL excluded; CATALOG and PRODUCER_CONTRACT both written to the catalog yaml.
2. **Canonical vs. advisory authority** — CATALOG canonical in the registry yaml; PRODUCER_CONTRACT canonical at producer-at-pin, advisory in the registry (with a `last_synced_at` timestamp per `plans/metadata-standard.md:412`).
3. **Publish-failure semantics** — CATALOG writeback is blocking (rollback the tag on failure); PRODUCER_CONTRACT writeback is non-blocking (log a warning) per `plans/metadata-standard.md:337-353`.

The latency analysis is the load-bearing argument for keeping PRODUCER_CONTRACT as a distinct state rather than collapsing to LOCAL/CATALOG. Batch browse ("show all projects with their outputs") needs the advisory copy in the catalog because per-project producer-HEAD fetches don't admit a disk cache (HEAD is mutable) and run N × ~300ms. With 50 projects, that's 50ms (catalog read) vs. ~15s (per-producer HEAD fetch). The advisory tier is what makes pre-pinning browse fast.

The alternative considered — collapse to {LOCAL, CATALOG} and encode "canonical vs advisory" elsewhere — required either hand-maintained field lists (the β style flagged as a warning sign in slice 1's retro) or a parallel orthogonal annotation. Neither was cleaner.

**Reversal cost:** Medium. Reversal to a 2-state design would require the hand-maintained "which fields are blocking-canonical vs non-blocking-advisory" lists in the publish flow and a separate channel for the advisory copies. Reversal to a 4-state design (re-adding CONSUMER to Metadata) is contraindicated by the plan — CONSUMER applies to `DataDependency`, not `Metadata`.

---

## 2026-05-13 — Revert `data_products.*` annotation back to PRODUCER_CONTRACT

**Decision:** Reverses the earlier 2026-05-13 entry in this file. `Metadata.data_products`, `DataProducts.primary`, `DataProducts.outputs`, and all four `DataProductOutput.*` leaves go back to `Audience.PRODUCER_CONTRACT` (owner stays `PIPELINE`). The `_PIPELINE_CATALOG` shorthand alias retires. Storage fields stay `PRODUCER_CONTRACT` (unchanged).

**Why:** The earlier entry argued "the actual design has only one channel" — i.e., the catalog as the resolver. That argument was made without consulting `plans/metadata-standard.md:210-238`, which explicitly describes a two-layer authority model: the catalog is canonical for identity (name, ownership, governance, repository, status); the producer's metadata.json at a pinned commit is canonical for pipeline correctness (`data_products.outputs[]`, storage). The plan emits PRODUCER_CONTRACT fields to the catalog yaml as advisory copies (with a `last_synced_at` timestamp) for browse-time display, but consumers do not read them as authoritative — they re-read producer-at-pin.

The slice-2 retro grilling confirmed the plan's two-layer design earns its weight on (a) latency for batch browse and (b) publish-failure semantics (advisory writeback is non-blocking). The earlier "single channel" framing would force advisory writeback to be blocking, regressing the `registry update` data_products bug from "non-fatal staleness" to "publish must succeed at writing every field."

**Side effect:** `test_update_data_products_roundtrips` needs reframing. The test's premise was "data_products is in the catalog; verify roundtrip." Under the reverted design, the catalog still carries an advisory copy of data_products that should round-trip on `register` / `update` — the structural fix for the writeback bug (single serializer path) still holds. What changes is the failure-mode test: a writeback failure for data_products is no longer a `register`/`update` error; it's a logged warning.

**Reversal cost:** Low. Touches one shorthand alias (`_PIPELINE_CATALOG` retires; `_PIPELINE_PRODUCER` resumes use) and seven field annotations. The model migration is annotation-only — no stored data changes shape.

---

## 2026-05-13 — Drop `artifact_pin`; pinning is just the producer git SHA

**Decision:** `imports.yaml` (slice 5, PR 5) carries `contract_pin` only — the producer git commit SHA. The `artifact_pin` (DVC md5) is removed. The `Pin` typed pair from `plans/metadata-standard.md:132-136` collapses: either `src/mintd/pin.py` retires entirely and `imports.yaml` carries `contract_pin: str` (or `NewType("ContractPin", str)`), or `Pin` shrinks to a one-field frozen type. PR 5's spec (`plans/metadata-standard.md:488-496`) simplifies accordingly.

**Why:** Git is content-addressed. A commit SHA uniquely determines the bytes of every file at that commit, including the `.dvc` file. The `.dvc` file contains `outs[].md5`. The chain is:

```
contract_pin (git SHA)
  → .dvc file at that commit (deterministic via git)
    → outs[].md5 (deterministic via the .dvc file)
      → bytes of the artifact (verified by `dvc pull`)
```

Storing the md5 in `imports.yaml` adds zero information beyond what's already pinned by the SHA. The failure modes the `artifact_pin` was supposed to defend against either can't happen (producer "rewriting history at the same SHA" is impossible — git SHAs are content-addressed) or are already caught by DVC's own md5 verification at `pull` time (remote serving wrong bytes). The remaining benefits (audit-trail-at-a-glance, offline verification without `git fetch`) are convenience, not capability, and derivable on demand.

**Side effect:** The "multi-file logical product" wrinkle dissolves. With no `artifact_pin` shape to commit to, there's no design tension between "scalar md5" and "list of md5s" and "manifest hash" for products that span multiple `.dvc` files. The contract_pin pins every file in the producer's tree at that commit, regardless of how DVC tracks them.

**Reversal cost:** Low. `imports.yaml`'s `pin:` block shrinks to a single SHA field; if needs change later, adding `artifact_pin` back is additive. PR 5's spec gets simpler, not more complex, by this change.

---

## 2026-05-13 — `data_products.*` re-annotated from CONSUMER / PRODUCER_CONTRACT to CATALOG

**Decision:** `Metadata.data_products`, `DataProducts.primary`, `DataProducts.outputs`, and all four `DataProductOutput.*` leaves now carry `Audience.CATALOG` (owner stays `PIPELINE`). Added a `_PIPELINE_CATALOG` shorthand alongside the existing audience aliases.

**Why:** Surfaced during slice 2 implementation. The original split between `Audience.CATALOG` ("discovery index") and `Audience.CONSUMER` ("for downstream consumers when they import me") assumed two channels: a catalog browse and a separate import-time fetch. The actual design has only one channel — downstream consumers discover and resolve via the same catalog. So fields tagged CONSUMER were behaving identically to fields tagged CATALOG-not-included (i.e., never visible to anyone but the local project). `data_products` is the obvious example: it *is* discovery information. Re-annotating fixes the immediate bug and makes `test_update_data_products_roundtrips` true to its name.

Side effect: `Audience.CONSUMER` currently has zero callers. Left in the enum as a forward-guard for slice 4's `imports.yaml` resolver, with the open question of whether the 4-audience taxonomy (LOCAL / CATALOG / PRODUCER_CONTRACT / CONSUMER) collapses to 2 states (LOCAL / CATALOG) since the latter three currently produce identical filter behavior. Flag for slice-2 retro.

**Reversal cost:** Low. Touches one shorthand alias and seven field annotations. The model migration is annotation-only — no stored data changes shape.

---

## 2026-05-13 — Extend `_unwrap_container` instead of adding `walk_fields`

**Decision:** `_unwrap_optional` was renamed to `_unwrap_container` and made iterative — it now unwraps both `Optional[T]` and `list[T]` (and nestings like `Optional[list[T]]`). `field_metadata` uses it to descend through paths like `access_control.teams.name`. No `walk_fields` sibling function was added.

**Why:** SLICE-2.md decision 2 leaned toward adding a `walk_fields(model, callback)` sibling to keep `field_metadata` "simple, separated from walk." On building it out, the only consumer that needed traversal was the inclusion-invariant test in `test_catalog_serialization.py`, and what it needed was *targeted lookup* on each path the dump-walker already produced — not enumeration. Extending the unwrap helper to descend through `list[T]` was three added lines and preserved `field_metadata`'s role as "dotted-path lookup." `walk_fields` becomes warranted when something genuinely needs to enumerate every leaf path on the class (slice-4 catalog-drift warnings, possibly).

**Reversal cost:** Low. If a future consumer needs class-level enumeration, add `walk_fields` as a free function alongside the existing helpers. No change to `field_metadata` required.

---

## 2026-05-13 — α (dynamic projection) confirmed for `CatalogEntry`; strict per-leaf audience

**Decision:** `CatalogEntry` is a Pydantic `BaseModel` with `model_config = ConfigDict(extra="allow")` and no declared fields. `Metadata.to_catalog_entry()` builds a dict via `_project_catalog()` — a recursive walk that, at every level, checks each leaf's own `FieldRole`. A CATALOG container does *not* cascade to its children; each leaf carries its own annotation and is filtered independently. The shape of a `CatalogEntry` is *derived* from `Metadata`, not declared.

**Why:** SLICE-2.md flagged this as the slice-1 payoff test. α won concretely: `to_catalog_entry()` is one line wrapping a 12-line recursive helper, with zero hand-maintained list of CATALOG paths. Per-leaf semantics surfaced during TDD when `project.created_at` (Audience.LOCAL) leaked through inside the CATALOG `project` container — the recursion fix and the per-leaf rule fell out of the same failure.

**Reversal cost:** Medium if reversed to β (hand-defined CatalogEntry). Every CATALOG-audience field would need to be respelled on a parallel Pydantic class, and `to_catalog_entry()` would become a series of `setattr` calls. Worth doing only if Pydantic type-checking breaks badly on the `extra="allow"` shape.

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
