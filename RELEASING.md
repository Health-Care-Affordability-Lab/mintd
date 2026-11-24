# Releasing mintd

Every merge to `main` makes a prerelease `vX.Y.Z`; you promote it or let it lapse. Two rules, which
nothing enforces (anyone with write access can edit a release):

- **Promote only through the `release` environment**, never by editing a release's page.
- **Never upload, replace or delete a release's files**, and never delete a published release.

## Promote

Only the newest release run waits for approval: each newer one cancels the older ones still waiting.

1. Actions → `release` → the run waiting for approval. Every job before `promote` is green.
2. Review deployments → tick `release` → Approve and deploy.

A read-only run of the live tier on the lab Linux box before promoting is still worth doing, as
practice, until a nightly clone of every product runs.

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
git switch -c hotfix/vX.Y.Z vX.Y.Z   # fix, with a test that fails without it, and commit
git tag -a vX.Y.Z.post1 -m vX.Y.Z.post1 && git push origin hotfix/vX.Y.Z vX.Y.Z.post1
gh workflow run release.yml --ref vX.Y.Z.post1
```

Promote it as above (a hotfix run is never cancelled by newer ones: approve or Reject it), and land
the fix on `main` in a normal PR. A branch cut before releases were made as drafts must cherry-pick
that change first.

## A failed run

Re-run failed jobs in the same run. A release stays a draft, seen by no one else, until its files are up.

- **Stopped on a draft:** `gh release delete vX.Y.Z --yes` (the tag stays), then re-run.
- **"several releases for vX.Y.Z":** delete each stray draft by the id the run printed,
  `gh api -X DELETE "repos/Health-Care-Affordability-Lab/mintd/releases/<id>"`, then re-run. Never
  `gh release delete vX.Y.Z` here: it would delete the published one.
- **A published prerelease is missing a file:** it can't be fixed. Roll back and ship the next version.
- **The release never started** (say, a branch has the tag's name): `gh workflow run release.yml --ref vX.Y.Z`.

## How it works

`release.yml` reruns CI on the tag, builds the wheel once, and publishes a draft with the wheel, its
SHA-256, `constraints.txt` and both install scripts once every file is up. It installs that release
on Linux, macOS and Windows and runs the stories, writing the results and a link to the run into the
notes. Then `supersede` cancels the older runs waiting for approval, or this one if a newer run is
waiting; a run already promoting is never touched, and hotfix runs are left alone. `promote`
refuses unless the environment has a required reviewer, the tag is still the built commit, both
install scripts are there, and no newer release is latest. The install line checks the wheel's
SHA-256 and pins dependencies by version (uv doesn't check a constraints file's hashes); reflink's
hashes are checked, and `REFLINK_REQ` / `$ReflinkReq` come from
`uv pip compile --no-config --universal --python-version 3.13 --generate-hashes`.
