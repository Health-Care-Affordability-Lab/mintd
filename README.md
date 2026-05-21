# mintd

Lightweight data product framework for research labs.

`mintd` is a Python CLI that wraps **git** (for code + small metadata) and
**DVC** (for the actual data bytes) into a single workflow built around
**data products** — versioned, citable, fetchable datasets that one project
publishes and other projects depend on at a pinned commit.

It targets the workflow of a research lab where:

- one project produces a clean dataset (e.g. survey responses, cleaned
  admin records),
- several downstream projects (papers, analyses, dashboards) consume that
  dataset and need to lock in *exactly* the version they ran against,
- and everything needs to be findable from a single catalog without
  hand-rolling S3 paths or ad-hoc folder conventions.

> Status: under active development. The CLI surface is stable enough to use
> day-to-day inside the lab; some commands (notably Windows `.ps1` wrappers
> and a few v1 parity bits) are still being filled in.

## Concepts in one minute

- **Producer** — a project that *publishes* a data product. Owns its
  `metadata.json`, owns the bytes in S3.
- **Consumer** — a project that *imports* another project's data product
  at a specific git commit. Records what it depended on, so an analysis is
  reproducible.
- **Registry / catalog** — a git repo that holds the
  CATALOG-audience subset of every producer's metadata, so consumers can
  discover what's available without scraping S3.
- **Pin** — the pair `(contract_pin, artifact_pin)` = `(producer git commit,
  DVC md5)`. Always recorded together. Consumers never silently re-resolve.
- **Project types** — `data`, `code`, `project`, `enclave`. See
  `mintd init --help`.

See [`vocab.md`](vocab.md) for the long version.

## Install

`mintd` is a pure-Python CLI. Linux, macOS, and Windows.

### One-liner (recommended)

Prerequisite: an SSH key on your GitHub account that has access to this
repo (`ssh -T git@github.com` should greet you).

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/main/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/main/install.ps1 | iex
```

Both scripts install `uv` if missing, run `uv tool install
git+ssh://git@github.com/Health-Care-Affordability-Lab/mintdv2.git`
(isolated venv, `mintd` on PATH), then layer `cffi + reflink` into the
tool venv so DVC's reflink cache mode works on APFS / XFS / Btrfs. Pin
to a branch with `bash -s -- --branch <name>` or `-Branch <name>`.

> While the repo is private, the `curl … | bash` fetch step itself only
> works if you already cloned the repo locally and ran `bash install.sh`,
> or if you have a `gh` token wired into `git`. Anonymous fetch of
> `raw.githubusercontent.com/.../install.sh` will 404 on a private repo.

### Manual install

```bash
# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh    # macOS / Linux
# irm https://astral.sh/uv/install.ps1 | iex       # Windows

uv tool install git+ssh://git@github.com/Health-Care-Affordability-Lab/mintdv2.git
```

Or `pipx install git+…` / `pip install --user git+…` if you prefer.

You'll also need **DVC** with the S3 backend, since `mintd` shells out to
it for every data operation:

```bash
pip install 'dvc[s3]'
# or: brew install dvc / winget install iterative.dvc
```

Verify:

```bash
mintd --version
mintd --help
```

> Windows note: the scaffolded `scripts/*.sh` are bash scripts. Run them
> under WSL2, Git Bash, or MSYS2 — there are no `.ps1` siblings yet.
> See [`notes/INSTALL.md`](notes/INSTALL.md).

## First-time setup

Point `mintd` at your lab's registry and your S3 storage:

```bash
mintd config setup            # interactive walkthrough
mintd config show             # see what got written
mintd config validate         # schema + AWS profile + S3 head_bucket check
```

The config lives at `~/.config/mintd/config.yaml`. Knobs include
`registry_url`, `storage_bucket_prefix`, `storage_endpoint`,
`aws_profile_name`, and a `timeouts:` block — see
[`notes/CONFIG.md`](notes/CONFIG.md).

If you have a v1 mintd config kicking around:

```bash
mintd config setup --migrate-v1 ~/.config/mintd/config.yaml.v1
```

## Example: produce a dataset

```bash
# Scaffold a new data-producing project. The wizard asks for a
# governance classification (labonly / public / licensed).
mintd init data my-cleaned-survey
cd data_my-cleaned-survey

# Edit metadata.json (description, contact, data_products.outputs, …)
# Then validate before you commit:
mintd check

# Track outputs with DVC, push the bytes:
mintd data add data/final/survey.parquet
mintd data push

# Announce the project to the lab registry (opens a PR):
mintd registry register

# Cut a versioned release. Tags, pushes, and updates the catalog
# entry. Shows a preview and asks before doing anything destructive.
mintd publish 0.1.0
```

## Example: consume someone else's dataset

```bash
# See what's published in the lab catalog:
mintd registry sync
mintd data list

# Peek at the S3 layout of a specific product before pulling:
mintd data ls my-cleaned-survey

# Clone the whole producer repo + pull the primary data product:
mintd data clone my-cleaned-survey
# or: only the primary output, pinned to a tag
mintd data clone my-cleaned-survey --rev v0.1.0 --primary

# Inside an existing analysis project, import a single output and
# record the pin in this repo's .dvc files:
mintd data import my-cleaned-survey --path data/final/survey.parquet

# Later, check whether anything you depend on has moved:
mintd check --upgrades
# And bump a pin when you're ready:
mintd data import my-cleaned-survey --bump
```

## Example: enclave workflow

For air-gapped / governed-access environments where data leaves a secure
machine via a manifest + archive instead of S3:

```bash
mintd init enclave my-restricted-analysis
cd enclave_my-restricted-analysis

mintd enclave add producer-repo --pin <git-sha>
mintd enclave pull                          # fetch outside the enclave
mintd enclave package                       # bundle into a transfer archive

# Inside the enclave, after the archive is delivered:
mintd enclave verify ./extracted/
mintd enclave list
```

The manifest's `transferred[]` section is append-only by construction —
`EnclaveManifest.save()` refuses to write if an existing entry was
mutated, so the audit trail can't be silently rewritten.

## Command reference

```
mintd init     {data|code|project|enclave} NAME   Scaffold a new project
mintd check    [path] [--upgrades]                Validate metadata + (optionally) pins
mintd data     import|clone|pull|push|add|verify|remove|list|ls
mintd enclave  add|remove|bump|pull|package|verify|list
mintd registry register|update|status|sync        Catalog operations
mintd publish  [version] [--dry-run] [-y]         Cut a versioned release
mintd config   show|setup|validate
mintd update   metadata [path]                    Migrate v1 metadata.json → v2
```

Global flags worth knowing:

- `-v` / `-vv` / `-vvv` — bump log verbosity (info → debug → trace).
- `-q` — errors only.
- `--json` — machine-readable output on read-side commands.
- `--no-color` — disable color (also respects `NO_COLOR`).

Every subcommand has `--help`.

## Where to look next

- [`vocab.md`](vocab.md) — domain language (producer/consumer, audience,
  pins, resolver).
- [`notes/INSTALL.md`](notes/INSTALL.md) — install paths per OS.
- [`notes/CONFIG.md`](notes/CONFIG.md) — config file reference.
- [`notes/JSON-SCHEMA.md`](notes/JSON-SCHEMA.md) — the `metadata.json` v2
  schema.
- [`notes/plans/metadata-standard.md`](notes/plans/metadata-standard.md)
  — design rationale for the metadata model.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Slice-based development; specs live in `notes/SLICE-*.md`.
