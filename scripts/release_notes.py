#!/usr/bin/env python3
"""The "What changed" section of a release's notes: the PRs merged since the last promoted release.

release.yml's `evidence` job runs `python3 scripts/release_notes.py vX.Y.Z` on a checkout of the tag with its
history, with GH_TOKEN and GH_REPO set. The last promoted release is GitHub's `releases/latest`: every merge's tag
is a prerelease, and only a promoted release is ever latest. Each first-parent commit from that tag to this one is
a squash merge whose subject ends "(#N)", and a PR's title reads "<area>: <what>".

- Entries are grouped by area, in merge order. PRs whose area is tests, stories, ci or lint are counted on one
  "Internal" line.
- A PR body may hold a "## Release notes" section, written by a person. Its text replaces the PR's line, and what
  it puts under "### Do this once" or "### Known limits" goes to those sections, at the top and the bottom.

Only the standard library, plus the git and gh the runner has.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: TID251  (a release-time script, not mintd: it runs git and gh itself)
import sys

INTERNAL = ("tests", "stories", "ci", "lint")
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


def merges(previous: str, tag: str) -> list[tuple[int, str]]:
    """(PR number, title) of each squash merge after `previous` up to `tag`, oldest first."""
    result = _run("git", "log", "--first-parent", "--reverse", "--format=%s", f"{previous}..{tag}")
    if result.returncode != 0:
        raise SystemExit(f"release_notes: git log {previous}..{tag} failed: {result.stderr.strip()}")
    found = (_SUBJECT.match(subject) for subject in result.stdout.splitlines())
    return [(int(m["number"]), m["title"]) for m in found if m]


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
    texts = {name: "\n".join(lines).strip() for name, lines in parts.items()}
    return {name: text for name, text in texts.items() if text}


def _cite(text: str, number: int) -> str:
    """`text` with its PR number at the end, on a line of its own after a code block."""
    return f"{text}\n(#{number})" if text.endswith("```") else f"{text} (#{number})"


def render(previous: str, entries: list[tuple[int, str, dict[str, str]]]) -> str:
    """The section's markdown, from (number, title, release notes) in merge order."""
    hoisted: dict[str, list[str]] = {name: [] for name in HOISTED}
    groups: dict[str, list[str]] = {}
    internal: list[int] = []
    for number, title, notes in entries:
        area, _, what = title.partition(": ")
        if not what:
            area, what = "other", title
        for name in HOISTED:
            if name in notes:
                hoisted[name].append(_cite(notes[name], number))
        if "" in notes:
            text = notes[""]
            groups.setdefault(area, []).append(_cite(text if text.startswith("- ") else f"- {text}", number))
        elif area in INTERNAL:
            internal.append(number)
        else:
            groups.setdefault(area, []).append(f"- {what} (#{number})")
    out = [f"## What changed since {previous}"]
    if not entries:
        out.append(f"Nothing was merged since {previous}.")
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
        print("usage: release_notes.py vX.Y.Z  (GH_REPO set)", file=sys.stderr)
        return 2
    tag, repo = argv[1], os.environ["GH_REPO"]
    previous = last_promoted(repo)
    if previous is None:
        print("No release has been promoted yet, so there is nothing to compare this one with.")
        return 0
    entries = [(number, title, release_notes(pr_body(repo, number))) for number, title in merges(previous, tag)]
    print(render(previous, entries))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
