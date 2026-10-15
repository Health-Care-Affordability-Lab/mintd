"""Docs cannot point at things that aren't shipped.

``notes/`` is gitignored scratch — 0 tracked files — so a reference into it is
a dead link for everyone but the laptop that wrote it, and nothing (no diff, no
``git status``) ever says so. These two ratchets are what keeps that true.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = [REPO / "README.md", REPO / "vocab.md", REPO / "DECISIONS.md"]

# `notes/` followed by a path character — a citation. A bare mention of the
# directory (as in README's conventions section) is fine.
NOTES_PATH = re.compile(r"notes/[\w.-]")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _sources() -> list[Path]:
    return sorted(
        p
        for d in ("src", "tests")
        for p in (REPO / d).rglob("*.py")
    ) + [p for p in DOCS if p.exists()]


def test_nothing_tracked_cites_notes() -> None:
    """A docstring, hint, or doc link into `notes/` is a link to nothing."""
    offenders = [
        f"{p.relative_to(REPO)}:{i}: {line.strip()}"
        for p in _sources()
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if NOTES_PATH.search(line)
    ]
    assert not offenders, "notes/ is gitignored; state the fact instead:\n" + "\n".join(offenders)


def test_relative_doc_links_resolve() -> None:
    """Every relative link in a tracked doc points at a file that exists."""
    broken = [
        f"{doc.relative_to(REPO)}:{i}: {target}"
        for doc in DOCS
        if doc.exists()
        for i, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1)
        for target in MD_LINK.findall(line)
        if not target.startswith(("http://", "https://", "mailto:", "#"))
        and not (doc.parent / target.split("#", 1)[0]).exists()
    ]
    assert not broken, "broken relative links:\n" + "\n".join(broken)
