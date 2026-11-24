#!/usr/bin/env python3
"""The "What changed" section of a release's notes: the PRs merged since the last promoted release.

release.yml's `evidence` job runs `python3 scripts/release_notes.py vX.Y.Z` on a checkout of the tag with its
history, with GH_TOKEN and GH_REPO set. The last promoted release is GitHub's `releases/latest`: a release run's tag
is a prerelease until it is promoted, and only a promoted release is ever latest. Each first-parent commit from that tag to this one is
a squash merge whose subject ends "(#N)", and a PR's title reads "<area>: <what>".

- Entries are grouped by area, in merge order. PRs whose area is tests, stories, ci or lint are counted on one
  "Internal" line. A commit with no "(#N)" is listed by its subject.
- A tag that isn't ahead of the latest release (a hotfix, vX.Y.Z.postN, cut from an older one) lists its own
  commits since the release it was cut from, under "Hotfix for vX.Y.Z".
- A PR body may hold a "## Release notes" section, written by a person. Its text replaces the PR's line, and what
  it puts under "### Do this once" or "### Known limits" goes to those sections, at the top and the bottom.
- A PR's text can be edited after it merges, so it is never trusted as is: control and format characters (an
  escape, a right-to-left override) are dropped, each part is cut at a length, the whole section too, and a line
  that starts like one the release run writes itself is indented, so the run never takes it for its own.

Only the standard library, plus the git and gh the runner has.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: TID251  (a release-time script, not mintd: it runs git and gh itself)
import sys
import unicodedata

INTERNAL = ("tests", "stories", "ci", "lint")
MAX_PART = 2_000  # characters of one PR's text, per part
MAX_SECTION = 60_000  # characters of the whole section; GitHub refuses notes past 125,000
# Lines the release run finds and rewrites in the notes (release.yml's evidence and promote).
RUN_LINES = ("- clean install with ", "A prerelease: ")
# data._unprintable's rule: a control, format, surrogate or separator character; here newline and tab stay.
_UNPRINTABLE = ("Cc", "Cf", "Cs", "Zl", "Zp")
HOISTED = ("Do this once", "Known limits")
_SUBJECT = re.compile(r"^(?P<title>.+) \(#(?P<number>\d+)\)$")


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def last_promoted(repo: str) -> str | None:
    """The latest release's tag, or None when none has been promoted (GitHub answers 404)."""
    result = _run("gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name")
    if result.returncode == 0:
        return result.stdout.strip()
    if "HTTP 404" in result.stderr:
        return None
    raise SystemExit(f"release_notes: could not read the latest release: {result.stderr.strip()}")


def _git(*args: str) -> str:
    result = _run("git", *args)
    if result.returncode != 0:
        raise SystemExit(f"release_notes: git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def base(previous: str, tag: str) -> tuple[str, bool]:
    """Where the list starts, and whether `tag` is a hotfix: the last promoted release when `tag` is
    ahead of it, else the release `tag` was cut from (its nearest earlier tag)."""
    if _run("git", "merge-base", "--is-ancestor", previous, tag).returncode == 0:
        return previous, False
    return _git("describe", "--tags", "--abbrev=0", f"{tag}^").strip(), True


def merges(start: str, tag: str) -> list[tuple[int | None, str]]:
    """(PR number, or None for a commit with none; title) of each first-parent commit after `start`
    up to `tag`, oldest first."""
    found: list[tuple[int | None, str]] = []
    for subject in _git("log", "--first-parent", "--reverse", "--format=%s", f"{start}..{tag}").splitlines():
        m = _SUBJECT.match(subject)
        found.append((int(m["number"]), m["title"]) if m else (None, subject))
    return found


def pr_body(repo: str, number: int) -> str:
    result = _run("gh", "api", f"repos/{repo}/pulls/{number}", "--jq", '.body // ""')
    if result.returncode != 0:
        raise SystemExit(f"release_notes: could not read PR #{number}: {result.stderr.strip()}")
    return result.stdout


def release_notes(body: str) -> dict[str, str]:
    """A PR body's "## Release notes" section: its own text under "", each hoisted sub-section under its name."""
    parts: dict[str, list[str]] = {}
    current: str | None = None  # None: outside the section
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            current = "" if line[3:].strip().lower() == "release notes" else None
        elif current is not None and line.startswith("### ") and line[4:].strip() in HOISTED:
            current = line[4:].strip()
        elif current is not None:
            parts.setdefault(current, []).append(line)
    texts = {name: clean("\n".join(lines), MAX_PART).strip() for name, lines in parts.items()}
    return {name: text for name, text in texts.items() if text}


def clean(text: str, limit: int) -> str:
    """Text from a PR, safe in the notes: no control or format characters (newlines and tabs kept), at most
    `limit` characters."""
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in _UNPRINTABLE)
    return text if len(text) <= limit else text[:limit].rstrip() + " … (cut)"


def guard(section: str) -> str:
    """`section` with every line that starts like one the release run rewrites indented two spaces."""
    return "\n".join("  " + ln if ln.startswith(RUN_LINES) else ln for ln in section.split("\n"))


def _cite(text: str, number: int | None) -> str:
    """`text` with its PR number at the end, on a line of its own after a code block."""
    if number is None:
        return text
    return f"{text}\n(#{number})" if text.endswith("```") else f"{text} (#{number})"


def render(heading: str, entries: list[tuple[int | None, str, dict[str, str]]]) -> str:
    """The section's markdown, from (number, title, release notes) in merge order."""
    hoisted: dict[str, list[str]] = {name: [] for name in HOISTED}
    groups: dict[str, list[str]] = {}
    internal: list[int] = []
    for number, title, notes in entries:
        area, _, what = clean(title, MAX_PART).partition(": ")
        if not what:
            area, what = "other", title
        for name in HOISTED:
            if name in notes:
                hoisted[name].append(_cite(notes[name], number))
        if "" in notes:
            text = notes[""]
            groups.setdefault(area, []).append(_cite(text if text.startswith("- ") else f"- {text}", number))
        elif area in INTERNAL and number is not None:
            internal.append(number)
        else:
            groups.setdefault(area, []).append(_cite(f"- {what}", number))
    out = [heading]
    if not entries:
        out.append("No commits.")
    if hoisted["Do this once"]:
        out += ["### Do this once", *hoisted["Do this once"]]
    for area, lines in groups.items():
        out += [f"### {area}", "\n".join(lines)]
    if hoisted["Known limits"]:
        out += ["### Known limits", *hoisted["Known limits"]]
    if internal:
        numbers = ", ".join(f"#{n}" for n in internal)
        out.append(f"Internal: {len(internal)} change{'s' if len(internal) > 1 else ''} to tests, stories, CI and lint ({numbers}).")
    return "\n\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: release_notes.py vX.Y.Z  (GH_REPO set)\n")
        return 2
    tag, repo = argv[1], os.environ["GH_REPO"]
    previous = last_promoted(repo)
    if previous is None:
        sys.stdout.write("No release has been promoted yet, so there is nothing to compare this one with.\n")
        return 0
    start, hotfix = base(previous, tag)
    heading = f"## Hotfix for {re.sub(r'[.]post[0-9]+$', '', start)}" if hotfix else f"## What changed since {start}"
    entries = [(number, title, release_notes(pr_body(repo, number)) if number is not None else {})
               for number, title in merges(start, tag)]
    sys.stdout.write(guard(clean(render(heading, entries), MAX_SECTION)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
