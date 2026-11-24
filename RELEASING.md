# Releasing mintd

## From merge to release

- Merging a PR into `main` tags its merge commit `vX.Y.Z` (`.github/workflows/tag.yml`) and starts
  `.github/workflows/release.yml` on that tag.
- `release.yml` reruns CI (`test.yml`) on the tag, builds the wheel once, and publishes a GitHub
  prerelease with the wheel, its SHA-256 (`<wheel>.sha256`), `constraints.txt` (the locked
  dependencies, with hashes), and `install.sh` and `install.ps1` with the tag written in. It then
  installs that release with its own scripts, from its URL, on Linux, macOS and Windows, and runs
  the story tests against the Linux install.
- The install line, `…/releases/latest/download/install.sh` (or `install.ps1`), runs the latest
  release's own script, and `releases/latest` never points at a prerelease.
- The run's `promote` job then waits for the maintainer's approval in the `release` environment.
  Approved, it checks the release (see Promote) and marks that same release, built from that
  same commit, as the latest release.

## What the install line checks

- The release's wheel is checked against the release's SHA-256 before anything is installed. On a
  mismatch, nothing is installed.
- Every dependency is pinned to the version in the release's `constraints.txt`. That file also holds
  their hashes, but `uv tool install` does not check hashes in a constraints file (uv 0.13.9, with
  or without a cache), so dependencies are pinned by version, not by hash.
- The reflink step does check hashes: `uv pip install --require-hashes` takes reflink's from the
  scripts (it is not in the lock) and cffi's and pycparser's from `constraints.txt`. reflink has
  no wheel for Python 3.13, so its pinned sdist is built on the user's machine; the build's own
  tools (setuptools) come from PyPI by version, unchecked. The step runs with `--no-config`, so a
  `uv.toml` or `pyproject.toml` where the line runs, or above it, changes nothing. That also skips
  the user's own uv settings file; uv's environment variables still apply.
- To bump reflink, set `REFLINK_REQ` in `install.sh` and `$ReflinkReq` in `install.ps1` from
  `uv pip compile --no-config --universal --python-version 3.13 --generate-hashes` on
  `reflink==<version>`; a test checks the two match.
- Checking the dependencies' hashes would mean installing with `uv pip sync --require-hashes` into
  a virtual environment the scripts manage themselves. That gives up what `uv tool` provides
  (`uv tool list`, `uv tool uninstall`, the `mintd` command on PATH), so the scripts would have to
  create the environment, the PATH entry and an uninstall path on each system.

## The gate's evidence

It is on the prerelease's notes. The workflow writes the first two lines; people fill in the rest.

- **CI on this tag:** written by the workflow.
- **Clean install:** written by the workflow: the release's own `install.sh` on Linux (with
  `--with-schema`) and macOS, its `install.ps1` on Windows PowerShell 5.1 (plain, then
  `-WithSchema` over it), and the story tests against the Linux install. The stories run on Linux
  only.
- **Live tier:** run by hand, read-only, on the lab Linux box, when the change touches download or
  push paths. Never in CI. Write the result on the live-tier line: `passed N of N checks on N
  products`, or `not run: <reason>`. Release notes are public: counts only, never the name of a
  non-public product.
- **Canary:** the lab's own workflows, run by hand by the people the maintainer names. Write the
  result on the canary line.
- **Pre-release sweep, for the first two releases:** every lab data product is cloned and imported
  with the candidate's mintd on the lab Linux box, through the live tier, read-only. Add a line:
  `pre-release sweep on the lab box: N of N data products cloned and imported`, or the failing count
  and the issues filed for it. Counts only, never a product's name. For the release that first ships
  the install scripts, the sweep runs on that PR's wheel before it merges, so the merge and the
  promotion happen the same day.

To edit the notes: `gh release view vX.Y.Z --json body --jq .body > notes.md`, edit `notes.md`,
then `gh release edit vX.Y.Z --notes-file notes.md`. CI holds no storage credentials, so every
check against real storage is one of the by-hand lines.

## Promote

1. Read the prerelease's notes. CI passed, and the live-tier and canary lines are filled in.
2. Actions → the `release` run for that tag → Review deployments → tick `release` → Approve and
   deploy.
3. `promote` checks that the environment has a required reviewer, that the tag still points at the
   commit that was built, that the release carries `install.sh` and `install.ps1`, and that no newer
   release is already latest. Then it marks the release latest.

Reject the runs you won't promote (Review deployments → Reject); otherwise each waits 30 days.

Promote only through the environment: never untick "Set as a pre-release" or tick "Set as the latest
release" on the web page, and never upload, replace or delete a release's files. The only direct edits
are the live-tier and canary lines in the notes, and the rollback's two commands. The environment holds the `promote` job; it does not lock releases. Anyone
with write access, and any workflow token with `contents: write`, can still edit one.

## Roll back

```bash
# vPREV must carry the install scripts: the install line runs the latest release's own.
gh release view vPREV --json assets --jq '[.assets[].name] | (index("install.sh") != null) and (index("install.ps1") != null)'   # must print true
gh release edit vPREV --latest      # the last good release is latest again
gh release edit vBAD --prerelease   # the withdrawn one goes back to prerelease
```

- Never roll back to a release without `install.sh` and `install.ps1`, such as one made before the
  install scripts shipped: the install line would then fail with "no mintd release is out yet".
- In that order, so there is always an explicit latest release.
- Never delete a published release. Once releases are immutable its tag can't be used again, so a
  fix ships as the next version or a `.postN`.
- An emergency rollback skips the canary: vPREV already passed it.
- When there is time, prefer a revert PR. It ships as the next patch, through the gate.
- Nothing checks that older versions can read what a newer one wrote. Check whether vBAD wrote
  catalog entries or metadata that vPREV can't read, and repair them with ordinary PRs. Never delete
  anything on S3.

## Hotfix

For a fix to a promoted release when `main` has moved on:

```bash
git switch -c hotfix/vX.Y.Z vX.Y.Z
# fix it, with a test that fails without the fix, and commit
git tag -a vX.Y.Z.post1 -m vX.Y.Z.post1
git push origin hotfix/vX.Y.Z vX.Y.Z.post1
gh workflow run release.yml --ref vX.Y.Z.post1
```

The maintainer pushes the tag; if only the tag job may create `v*` tags, an admin pushes it. The
dispatch runs the `release.yml` in that tag's own tree. Then come the gate and promotion, as above.
Land the same fix on `main` with a normal PR: `tag.yml` never creates or counts `.postN` tags.
A hotfix branch cut from a commit before `prerelease` made its release as a draft must carry that
change (cherry-pick it), or its release fails once releases are immutable: the older job uploads to
a release it has already published.

## A failed run

Use "Re-run failed jobs" in the same run: it reuses the build's files. The `prerelease` job makes
the release as a draft, and publishes it only once it holds exactly the build's files, each uploaded
whole. A rerun uploads only the files a draft lacks, then publishes it. A published prerelease is
checked, never changed. A release's files are never replaced, and a release that is no longer a
prerelease is never changed. A draft is invisible to users and to the install line.

- **A draft the run can't finish.** If the run stops on a draft (the Releases page marks it Draft)
  with "has an incomplete upload of `<file>`", "already has a different `<file>`", "has `<file>`,
  which this build didn't make" or "has a draft that is not a prerelease", delete the draft
  (`gh release delete vX.Y.Z --yes`; the tag stays, since `--cleanup-tag` isn't given) and re-run the
  failed jobs.
- **A published prerelease short of a file**, or with an incomplete, different or extra one. Only
  the job before drafts could leave one, since it published first. It can't be fixed in place once
  releases are immutable: withdraw it as in Roll back, and ship the next version.
- **"several releases for `vX.Y.Z`".** The step printed each one's id, draft flag and URL. Delete
  each stray draft by its id, `gh api -X DELETE "repos/Health-Care-Affordability-Lab/mintd/releases/<id>"`
  (or from the Releases page), keep at most one, and re-run. Never `gh release delete vX.Y.Z` here:
  with a published release for the tag, it deletes that one.
- **The tag job's `queue: max`.** `tag.yml` keeps every waiting tag run with
  `concurrency: queue: max`, which GitHub documents but actionlint does not know yet. After the
  first merge that uses it, check that the tag run succeeded and the merge got its tag. If GitHub
  rejects the key, remove that one line from `tag.yml` in a PR. That PR's merge gets the next tag,
  and its release includes the merge that went untagged.
- **Starting a release by hand.** If the tag job could not start it (for example, a branch has the
  tag's name), run `gh workflow run release.yml --ref vX.Y.Z`, or Actions → release → Run workflow,
  with "Use workflow from" set to the tag.
