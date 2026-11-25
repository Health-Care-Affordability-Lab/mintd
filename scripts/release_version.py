#!/usr/bin/env python3
"""The version a release run makes, or why it makes none.

release.yml's `version` job runs `python3 scripts/release_version.py "$GITHUB_REF" "$GITHUB_SHA" <patch|minor>` on a
checkout with every tag. It prints the tag, or stops with the reason:

- On `refs/heads/main`: the latest vX.Y.Z tag, plus one patch (or one minor, with the patch reset). No tag yet
  gives v0.1.0. Hotfix tags (vX.Y.Z.postN) don't count here.
- On `refs/heads/hotfix/vX.Y.Z`: the next vX.Y.Z.postN after that line's latest tag; `bump` is ignored.
- Any other ref stops: the API can start a workflow on any branch or tag.
- The latest tag on the line must be a strict ancestor of the commit. That refuses a commit already released, and a
  "Re-run all jobs" on an old run, which would give an older commit a higher number.

Only the standard library, plus git.
"""

from __future__ import annotations

import re
import subprocess  # noqa: TID251  (a release-time script, not mintd: it runs git itself)
import sys

_RELEASE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_HOTFIX_BRANCH = re.compile(r"refs/heads/hotfix/(v\d+\.\d+\.\d+)")


class Refused(Exception):
    """Why this run makes no release."""


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _commit(tag: str) -> str:
    return _git("rev-list", "-n1", tag).strip()


def _check_after(latest: str, sha: str) -> None:
    """`latest`'s commit must be a strict ancestor of `sha`."""
    if _commit(latest) == _commit(sha):
        raise Refused(f"{sha} is already released as {latest}: nothing new to release")
    is_ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", latest, sha], check=False).returncode == 0
    if not is_ancestor:
        raise Refused(f"{sha} doesn't come after {latest}: start the release on the branch's newest commit")


def next_version(ref: str, sha: str, bump: str) -> str:
    tags = _git("tag", "--list", "v*").split()
    if ref == "refs/heads/main":
        if bump not in ("patch", "minor"):
            raise Refused(f"bump must be patch or minor, not {bump!r}")
        releases = sorted((tuple(map(int, m.groups())), t) for t in tags if (m := _RELEASE.fullmatch(t)))
        if not releases:
            return "v0.1.0"
        (major, minor, patch), latest = releases[-1]
        _check_after(latest, sha)
        return f"v{major}.{minor + 1}.0" if bump == "minor" else f"v{major}.{minor}.{patch + 1}"
    branch = _HOTFIX_BRANCH.fullmatch(ref)
    if not branch:
        raise Refused(f"a release runs on main or a hotfix/vX.Y.Z branch, not {ref}")
    base = branch[1]
    if base not in tags:
        raise Refused(f"{ref} is for {base}, which isn't a tag")
    posts = [int(t.rsplit("post", 1)[1]) for t in tags if re.fullmatch(re.escape(base) + r"\.post\d+", t)]
    latest = f"{base}.post{max(posts)}" if posts else base
    _check_after(latest, sha)
    return f"{base}.post{max(posts, default=0) + 1}"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        sys.stderr.write("usage: release_version.py <ref> <sha> <patch|minor>\n")
        return 2
    try:
        sys.stdout.write(next_version(argv[1], argv[2], argv[3]) + "\n")
    except Refused as why:
        sys.stderr.write(f"::error::{why}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
