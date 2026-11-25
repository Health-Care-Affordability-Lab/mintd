# Releasing mintd

A release happens when you ask for one; merging a PR makes none. Two rules, which nothing enforces (anyone
with write access can edit a release):

- **Promote only through the `release` environment**, never by editing a release's page.
- **Never upload, replace or delete a release's files**, and never delete a published release.

## Release

1. Actions → `release` → Run workflow on `main`, with `patch`, or `minor` when users see a new or changed
   command, flag or default.
2. Wait until every job before `promote` is green: it tests and builds that commit, publishes it as a
   prerelease, installs it from its own URL on Linux, macOS and Windows, and runs the stories.
3. Preview the catalog. Promoting a release also makes it the mintd the catalog job runs, so before approving,
   run the prerelease's `registry rebuild --check` on a fresh clone of the catalog repo's main (your
   `registry_url`), cloned with read-only credentials. It writes nothing:

   ```bash
   uvx --from git+https://github.com/Health-Care-Affordability-Lab/mintd@vX.Y.Z mintd registry rebuild --check --registry path/to/catalog
   ```

   Look for entries that would change though their product didn't, holds that the catalog job's open issue
   doesn't list, and any entry losing fields. It exits 1 whenever an entry is held or would change, so read the
   output, not the exit code. On a Mac, run `export TMPDIR=$(cd "${TMPDIR:-/tmp}" && pwd -P)` first: until mintd
   resolves the temp folder itself, its path through /var gives false holds. If this release carries
   `clone --all` (#158), set `CLONE_ALL_RELEASE` in the catalog repo's site generator to it before promoting, since
   the catalog pages' install line fetches `releases/latest`.
4. Review deployments → tick `release` → Approve and deploy. Or Reject: nothing reaches users.

One release runs at a time: a second press waits for the first's yes or no. A read-only run of the live tier on the lab Linux box before promoting is still worth doing, as
practice, until a nightly clone of every product runs.

The notes' "What changed" lists the PRs merged since the last promoted release, by title, and a PR's own
"## Release notes" section when it has one (its "### Do this once" and "### Known limits" go to the top and
bottom). A hotfix lists its own commits since the release it was cut from. `promote` drops the notes' "A
prerelease" line as it makes the release latest.

## Roll back

```bash
gh release view vPREV --json assets --jq '[.assets[].name] | (index("install.sh") != null) and (index("install.ps1") != null)'   # must print true
gh release edit vPREV --latest      # first: the last good release is latest again
gh release edit vBAD --prerelease   # then: the withdrawn one
```

Users on vBAD are told within a day, the next time they run `mintd --version` or bare `mintd`. If vBAD wrote
catalog entries or metadata vPREV can't read, repair them with PRs. Never delete anything on S3.

## Hotfix

```bash
git switch -c hotfix/vX.Y.Z vX.Y.Z   # fix, with a test that fails without it, and commit; push the branch
```

Then Actions → `release` → Run workflow on `hotfix/vX.Y.Z`: it makes vX.Y.Z.postN. Approve it as above, and
land the fix on `main` in a normal PR. Only a hotfix for the latest release's line can become latest: when a
newer release is latest, `promote` refuses after the approval, and the hotfix stays a prerelease, installed
only by its tag. A line cut before releases ran on demand (v0.16.1 and older) first takes main's release
workflow, in one commit before the fix: `git checkout origin/main -- .github/workflows/release.yml
.github/workflows/test.yml scripts/ tests/test_release_workflow.py tests/test_release_notes.py
tests/test_release_version.py RELEASING.md`, then `git rm .github/workflows/tag.yml tests/test_tag_workflow.py`.

## A failed run

Use "Re-run failed jobs" in the same run: it keeps the run's version. Once the release is published, never
"Re-run all jobs": its first job refuses a commit that is already tagged.

- **Stopped on "draft <id>"**, or **"several releases for vX.Y.Z"**: delete each draft by the id the run
  printed, `gh api -X DELETE "repos/Health-Care-Affordability-Lab/mintd/releases/<id>"`, then re-run. Only
  ever a draft: never `gh release delete vX.Y.Z`, which picks the published release when there is one.
- **Stopped on "published release"**: it is never changed. If it is missing a file, roll it back and ship the
  next version.
- **"landed on <sha>, not <sha>"**: the tag is permanent, so the release stays an unpromoted prerelease and
  its number is spent. Run the workflow again: it takes the next number.
- **The notes say the changelog couldn't be written:** in that run, `evidence` → Re-run this job. It writes the
  changelog again and leaves the rest of the notes as they are.

## How it works

`release.yml` runs only when started by hand. Its first job picks the version (on `main`, the latest vX.Y.Z
plus a patch or a minor; on `hotfix/vX.Y.Z`, the next `.postN`) and stops unless the commit comes after that
line's latest tag and the tag is new. It reruns CI on that commit and builds the wheel once, telling
setuptools-scm the version, since no tag exists yet. `scripts/stamp_install_scripts.py` writes the tag into
both install scripts, the one stamper CI's install checks will use too. The files go to a draft aimed at that commit,
which is published as a prerelease once every file is up; publishing makes the `v` tag, which is permanent,
and the run checks it landed on that commit. It installs that release on Linux, macOS and Windows and runs
the stories, writing the results, a link to the run and the changelog into the notes. `promote` refuses
unless the environment has a required reviewer, the tag is still the built commit, both install scripts are
there, and no newer release is latest. The install line checks the wheel's SHA-256 and pins dependencies by
version (uv doesn't check a constraints file's hashes); reflink's hashes are checked, and `REFLINK_REQ` /
`$ReflinkReq` come from `uv pip compile --no-config --universal --python-version 3.13 --generate-hashes`.
