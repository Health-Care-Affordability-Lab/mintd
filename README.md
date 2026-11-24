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
- **Registry / catalog** — a git repo that holds every producer's
  `metadata.json` (minus the `schema_version` and `mint` fields), so
  consumers can discover what's available without scraping S3.
- **Pin** — the producer git commit a consumer imported at
  (`contract_pin`), recorded in the consumer's own `.dvc` files. Consumers
  never silently re-resolve; `mintd data import --bump` moves it.
- **Project types** — `data`, `code`, `project`, `enclave`. See
  `mintd init --help`.

See [`vocab.md`](vocab.md) for the long version.

## Install

`mintd` is a pure-Python CLI. Linux, macOS, and Windows.

### One-liner (recommended)

macOS / Linux:

```bash
curl -fsSL https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.sh | bash
```

Windows PowerShell:

```powershell
irm https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.ps1 | iex
```

These install the newest release the maintainer has promoted, with `dvc`:
there is no separate DVC install. Both scripts install `uv` 0.13.9 if it is
missing, and stop if your `uv` is older or doesn't report its version, naming
the command that upgrades it.
They download the release's wheel, check it against the release's SHA-256, and
run `uv tool install --python 3.13 --with-executables-from dvc` with the
release's `constraints.txt`, which pins every dependency (DVC included) to the
versions that release was tested with. `uv tool install` does not check the
hashes in that file, so the dependencies are pinned by version, not by hash.
Then the scripts add `cffi` and `reflink` to the tool's environment, so DVC's
reflink cache mode works on APFS / XFS / Btrfs. That step installs only files
whose hashes are pinned (reflink's in the scripts, cffi's in `constraints.txt`),
builds reflink on your machine from its pinned source, and ignores uv settings
files (`uv.toml`, `pyproject.toml`) in the folder you run the line from or
above it. Their last line says whether `mintd` and `dvc`
are both on your PATH, or which one is missing or shadowed by another copy,
and whether reflink was refused because a download didn't match its hash.

- To update, run the same line again. `uv tool upgrade mintd` does not apply.
- For the `[schema]` extra, add `-s -- --with-schema` after `bash`, or on
  Windows run
  `& ([scriptblock]::Create((irm https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.ps1))) -WithSchema`.
- For a given release, use `releases/download/vX.Y.Z/` in place of
  `releases/latest/download/`.

Verify:

```bash
mintd --version
dvc --version
mintd --help
```

> Windows note: the scaffolded `scripts/*.sh` are bash scripts. Run them
> under WSL2, Git Bash, or MSYS2 — there are no `.ps1` siblings yet.

## First-time setup

mintd reaches GitHub through [gh](https://cli.github.com) only. Log in, and let git
use gh for GitHub:

```bash
gh auth login
gh auth setup-git
```

If your clones use SSH (`git@github.com:` or `ssh://git@github.com/`), point them at
https once, so git goes through gh too:

```bash
git config --global --add url."https://github.com/".insteadOf git@github.com:
git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/
```

When GitHub refuses access, mintd names the repo's org and your active gh account,
and the account to switch to with `gh auth switch -u <account>`.

Then point `mintd` at your lab's registry and your S3 storage:

```bash
mintd config setup            # interactive walkthrough
mintd config show             # see what got written
mintd config validate         # schema + AWS profile; --bucket NAME adds an S3 head_bucket check
```

The config lives at `~/.config/mintd/config.yaml`. Knobs include
`registry_url`, `storage_bucket_prefix`, `storage_endpoint` and a
`timeouts:` block:

```yaml
timeouts:
  fast: 30        # quick calls (dvc init, the registry's git queries)
  transfer: null  # unbounded; set to e.g. 7200 for a 2h cap on dvc add / pull / git clone
```

`fast` caps quick calls such as `dvc init` and the registry's git queries;
`transfer` caps DVC add, status, remove, import, push, pull and checkout, and git clone,
and is `null` — disabled — by default. Per-command, `--timeout 600` opts into a
10-minute wall clock and `--timeout 0` disables it. `NO_COLOR` in the environment
turns off all color.

The AWS profile isn't a config key. mintd signs with the profile the remote's
`profile` names in `.dvc/config` (or `.dvc/config.local`); else the one
`AWS_DEFAULT_PROFILE` or `AWS_PROFILE` names; else `mintd`, when your shared
credentials file (`~/.aws/credentials`, or `$AWS_SHARED_CREDENTIALS_FILE`) has a
`[mintd]` section; else boto3's default credential chain.

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

# Edit metadata.json: `metadata.description`, `ownership.team` and
# `ownership.maintainers`, then one `data_products.outputs[]` entry per output
# and `data_products.primary` (see "What metadata.json needs" below).

# Produce the outputs. The scaffold's pipeline owns data/final/: `dvc repro`
# rebuilds it from data/raw/ and deletes anything placed there by hand, so
# keep source files in data/raw/:
dvc repro
# Nothing uploads data/raw/ on its own: git ignores it, and `dvc repro`
# records it as an input without caching it, so a teammate's clone can't
# rerun the pipeline. `mintd data add data/raw` tracks it if it should be
# shared; `mintd check` warns while a pipeline input is tracked by neither.

# Commit the project; `mintd data push` pushes it. Data belongs in DVC, not
# git: the scaffold's .gitignore covers data/, and any data file git status
# lists elsewhere goes to `mintd data add` or .gitignore before you commit.
# The exception is schemas/v1/schema.json: it lists column names and types,
# not data, and git keeps it so the lab catalog can show them. Commit it once
# you have checked its column names are real ones, not a first row of data.
# First time only: create the GitHub repository named in metadata.json
# (repository.github_url), `git remote add origin <that url>`, and push
# with `git push -u origin main`.
git status
git add -A
git commit -m "Produce the cleaned survey"

# Push the bytes, then the commits. On a version-aware remote this writes
# each object's S3 version ID into the .dvc / dvc.lock files, checks every
# output they record is on S3, then commits those files, and only those, and
# pushes them with your commit to the branch's upstream: importers read your
# pushed commit, not your disk. Skip this step and GitHub keeps your last
# importable version rather than a commit whose data is not on S3. It never
# pulls, rebases or forces. If any other tracked file differs from the last
# commit, it uploads, commits nothing and names the files for you to commit.
# With no pointer to commit it pushes nothing, and says when commits are
# waiting: run `git push` then. --no-commit uploads only.
mintd data push

# Validate. check also errors on any declared output DVC cannot deliver to
# importers (untracked, or pushed without S3 version IDs):
mintd check

# Announce the project to the lab registry (opens a PR). register refuses
# while git has any uncommitted change or untracked file that is not ignored
# (whatever status.showUntrackedFiles says):
mintd registry register

# Cut a versioned release. Tags, pushes, and updates the catalog
# entry. Shows a preview and asks before doing anything destructive.
# With no version it bumps the patch. A new product starts at the release
# number of the mintd that scaffolded it, so its first release is the next
# patch after that; an explicit version lower than the current one is refused.
mintd publish
```

### What metadata.json needs

You write the human fields: `metadata.description`, `metadata.tags`,
`ownership.team`, `ownership.maintainers`, `access_control.teams`,
`governance.classification`, `governance.contract_info`, `repository.mirror`
and `status.state`. mintd writes the rest (`mintd init`, `mintd update metadata`,
`mintd publish`), except `data_products`, which you fill in by hand: nothing
derives it from DVC. A data product needs at least one output and a primary:

```json
{
  "data_products": {
    "primary": "data/final/",
    "outputs": [
      {"path": "data/final/", "description": "County income, one row per county and year", "primary": true}
    ]
  }
}
```

- Each `data_products.outputs[]` entry needs `path`, `description` and
  `primary` (true or false). Leave `last_published` out: `mintd publish`
  writes it.
- `data_products.primary` must equal one entry's `path` character for
  character, so `data/final` doesn't match `data/final/`. It is what
  `mintd data import <name>` brings, and a `data` product must have one.
- `mintd check` errors on each declared path that DVC doesn't track, or that
  was pushed without S3 version IDs.
- mintd reads nothing from `outputs[].primary`, though it is required:
  `data_products.primary` decides.

## Example: consume someone else's dataset

Two commands, for two jobs.

**Bring a product's data into your project** with `mintd data import`. The data
lands under `data/imports/<full_name>/`, beside a `.dvc` file that pins the
producer commit it came from (`--rev`; default: the producer's HEAD).

| Command | Brings |
|---|---|
| `mintd data import <name>` | the primary output |
| `mintd data import <name> --path <path>` | only that dataset or folder, declared as an output or not (repeatable) |
| `mintd data import <name> --all` | every output in the product's `data_products.outputs`; a tracked folder no output declares is left out |

**Get a product's whole project, code and data, to read or work on** with
`mintd data clone`. A data product's clone lands in `./data_<name>/`.

| Command | Brings |
|---|---|
| `mintd data clone <name>` | a full git clone of the producer's repo, with every DVC-tracked output pulled |
| `mintd data clone <name> --primary` | the same clone, with only the primary output pulled; `--path` pulls only declared outputs |

```bash
# See what's published in the lab catalog:
mintd registry sync
mintd data list

# Peek at the S3 layout of a specific product before pulling:
mintd data ls my-cleaned-survey

# Clone the whole producer repo + pull every tracked output:
mintd data clone my-cleaned-survey
# or: only the primary output, pinned to a tag
mintd data clone my-cleaned-survey --rev v0.1.0 --primary
# or: only specific declared outputs (repeatable; an unknown path lists the
# valid outputs):
mintd data clone my-cleaned-survey --path data/final/survey.parquet

# Inside an existing analysis project, import one or more outputs
# (--path is repeatable) and record the pin in this repo's .dvc files:
mintd data import my-cleaned-survey --path data/final/survey.parquet

# Later, check whether anything you depend on has moved:
mintd check --upgrades
# And bump a pin when you're ready — same name, same --path selector
# (--path picks the row once you have imported more than one output):
mintd data import my-cleaned-survey --bump --path data/final/survey.parquet
```

## Example: enclave workflow

For air-gapped / governed-access environments where data leaves a secure
machine via a manifest + archive instead of S3:

On the networked machine:

```bash
mintd init enclave my-restricted-analysis
cd enclave_my-restricted-analysis

mintd enclave add producer-repo --pin <git-sha>
mintd enclave pull                          # fetch outside the enclave
mintd enclave package                       # bundle into a transfer archive
```

**Bundles are incremental.** `package` skips products already recorded in
`transferred[]`, so each archive carries only what hasn't crossed the gap yet:

```
$ mintd enclave package
skipped 2 already-transferred products: claims, prices (pass --resend to ship them again)
packaged: transfers/transfer-2026-07-28-000001.tar.gz
```

When nothing is new it says so and exits 0 without building an archive:

```
$ mintd enclave package
nothing new to package: all 3 downloaded product(s) have already crossed the gap
a bundle that was built is recorded as transferred even if it never arrived — to re-ship:
  mintd enclave package --resend
```

A bundle counts as transferred when it's *built*, not when it arrives. If an
archive is lost in transit, re-ship it with `mintd enclave package --resend
<repo>`.

Inside the enclave, after the archive is delivered:

```bash
mkdir -p incoming && tar -xzf transfer-2026-07-28-000001.tar.gz -C incoming
python3 incoming/land.py
```

Every archive ships with its own generated `README.md` — listing exactly which
products, pins and version folders it carries, and where they go — plus
`land.py`, which does the placement. `land.py` uses **only the Python standard
library**: no pip, no network, no DVC, no mintd, no PyYAML. It moves each
product into `data/<repo>/<version-folder>/`, appends the audit row, refuses to
run against the wrong enclave, and does nothing on a second run. `--dry-run`
previews; `--repo <path>` points it at the repo if you extracted elsewhere.

This matters because mintd itself generally *can't* be installed inside the gap
— it needs Python 3.11+, network access and DVC. If the enclave does happen to
have mintd, `mintd enclave verify incoming` does the same job, and running it
after `land.py` correctly reports nothing left to do.

The manifest's `transferred[]` section is append-only by construction —
`EnclaveManifest.save()` refuses to write if an existing entry was
mutated, so the audit trail can't be silently rewritten.

> **Not yet verified:** neither landing path checksums the delivered files
> against the recorded `artifact_pin`, so in-flight corruption is not currently
> detected. Treat the audit trail as a record of provenance, not of integrity.

## Example: files that aren't data products

Not everything is a versioned, citable output. For **untracked** files —
scratch intermediates, a large input you don't want in git, a one-off
handoff to a teammate — `mintd` has two lighter lanes that reuse the same
S3 storage but skip the pin/registry machinery.

**`cache`** — a durable file cache on your project's own S3 prefix. Push
*any* working-tree file that isn't DVC-tracked; a teammate on the same repo
pulls it back to the same path. Use it for expensive intermediates you'd
rather recompute-once-and-share than commit.

```bash
# Push any untracked path (a file or a whole directory):
mintd cache push data/scratch/isochrones
mintd cache push results/model.pkl scratch/notes.csv

# See what's cached, then pull it back on another clone:
mintd cache ls                         # or one path: mintd cache ls data/scratch
mintd cache pull                       # everything
mintd cache pull data/scratch/areas_v2 results/model.pkl   # just these paths
```

`cache pull` and `cache ls` take paths as `cache push` does: `data/scratch/area`
is that file or folder, never `data/scratch/area.csv` or `data/scratch/areas_v2`.
A path that matches nothing is an error. (`--prefix` was removed; pass the path.)

A DVC-tracked path is refused (it belongs to `mintd data push`), as is
anything under `.git/` or `.dvc/`. Pull reconstructs each file at its repo
path and won't clobber a local file that differs unless you pass `--force`.
Transfers are size-first, hash-verified: an unchanged file is skipped.

**`share`** — an ephemeral drop-zone keyed by user, for a quick handoff that
isn't tied to any repo:

```bash
# Producer drops a file; the command prints the ref to send:
mintd share put ./quarterly.xlsx
mintd share put ./plot.png --as figures/     # into a sub-folder

# Recipient fetches it by ref:
mintd share get alice/quarterly.xlsx
mintd share get alice/figures/plot.png --out ./downloads/
```

Rule of thumb: **`data push`** for versioned, citable outputs; **`cache`**
for durable-but-unpinned repo files; **`share`** for a one-off handoff.

## Command reference

```
mintd init     {data|code|project|enclave} NAME   Scaffold a new project
mintd check    [path] [--upgrades] [--remote [--all] | --staged]
                                                  Validate metadata, output delivery + (optionally) pins;
                                                  --remote asks S3 for every pinned version; --staged checks a commit for dropped IDs
mintd data     import|clone|pull|push|add|verify|remove|schema|list|ls
mintd cache    push|pull|ls                        Durable file cache for untracked repo files
mintd share    put|get                             Ephemeral per-user file handoff
mintd enclave  add|remove|bump|pull|package|verify|list   package: incremental, --resend re-ships
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

### `--json` output contract

Unversioned — payloads carry no `schema_version`, and a breaking change needs
a documented deprecation. Numbers are JSON numbers (`elapsed_s: 14.3`, not
`"14.3"`), missing values are explicit `null` rather than `""` or an omitted
key, and arrays are always present, possibly empty, never null.

- `data list` — array of entries sorted by `project_type`, then `name`;
  each has `name`, `project_type`, `description` (str or null).
- `data list --imported` — array of entries; each has `local_path`,
  `producer_repo`, `contract_pin`, `output_path`.
- `data clone` — object: `dest` (absolute), `primary` (str or null),
  `elapsed_s` (two decimals), `files`, `bytes`, `rev` (str or null),
  `remote_bucket` (str or null).

## Where to look next

- [`vocab.md`](vocab.md) — domain language (producer/consumer, audience,
  pins, resolver).
- `mintd <command> --help` — every flag, always current.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Conventions:

- A design decision and its reasons live in the pull request that makes it,
  and in the docstring of the code it shapes — not in a separate log.
- `README.md` and `vocab.md` describe the code as it is today. A change that
  adds, renames or removes a command, flag or term updates them in the same
  PR; `tests/test_docs.py` checks both against the code.
- A known, unfixed defect is a `pytest.mark.xfail(strict=True)` test whose
  docstring is the spec. `grep -rn 'strict=True' tests/` lists them, and one
  that starts passing turns CI red, so the list cannot go stale.
- Plans live in pull request descriptions.
- `notes/` is gitignored scratch. Nothing tracked may reference it —
  `tests/test_docs.py` enforces that.
