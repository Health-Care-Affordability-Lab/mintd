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
  or without a cache), so dependencies are pinned by version, not by hash. reflink has no wheel for
  Python 3.13 and is not in the lock, so it is built on the user's machine and not pinned.
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
- Users on vBAD are told when they next run `mintd --version` or bare `mintd`: "mintd X was
  withdrawn or is not released yet", with the install line. Each install looks this up at most once
  a day, so someone who already checked that day hears of the rollback up to a day later.
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

## A failed run

Use "Re-run failed jobs" in the same run: it reuses the build's files. If the prerelease already
exists, the rerun uploads only the files it lacks and stops if one it has differs. A release's files
are never replaced, and a release that is no longer a prerelease is never changed.

- **An incomplete upload.** If the run stops with "has an incomplete upload of `<file>`", an
  interrupted upload left that file half there. The maintainer deletes that one file from the
  prerelease (`gh release delete-asset vX.Y.Z <file>`), then re-runs the failed jobs.
- **The tag job's `queue: max`.** `tag.yml` keeps every waiting tag run with
  `concurrency: queue: max`, which GitHub documents but actionlint does not know yet. After the
  first merge that uses it, check that the tag run succeeded and the merge got its tag. If GitHub
  rejects the key, remove that one line from `tag.yml` in a PR. That PR's merge gets the next tag,
  and its release includes the merge that went untagged.
- **Starting a release by hand.** If the tag job could not start it (for example, a branch has the
  tag's name), run `gh workflow run release.yml --ref vX.Y.Z`, or Actions → release → Run workflow,
  with "Use workflow from" set to the tag.
