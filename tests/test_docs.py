"""Docs cannot point at things that aren't shipped.

``notes/`` is gitignored scratch — 0 tracked files — so a reference into it is
a dead link for everyone but the laptop that wrote it, and nothing (no diff, no
``git status``) ever says so. These two ratchets are what keeps that true.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from mintd.cli import _build_parser

REPO = Path(__file__).resolve().parent.parent
DOCS = [REPO / "README.md", REPO / "vocab.md"]

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


def _verbs(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_readme_command_reference_matches_cli() -> None:
    """README's command table lists exactly the groups and verbs the parser has.

    It once omitted `data schema` and nothing noticed. A group with no verbs
    (`init`, `check`, `publish`) only has to be present.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    table = readme.split("## Command reference", 1)[1].split("```")[1]
    listed = {
        line.split()[1]: line.split()[2]
        for line in table.splitlines()
        if line.startswith("mintd ")
    }
    groups = _verbs(_build_parser())
    assert set(listed) == set(groups)
    for group, cell in listed.items():
        verbs = _verbs(groups[group])
        if verbs:
            assert set(cell.split("|")) == set(verbs), group


CODE_NAME = re.compile(r"`([A-Za-z_]\w*)(?:\(.*?\))?`")


def test_vocab_headings_name_code_that_exists() -> None:
    """A code name in a vocab.md heading is defined or imported somewhere in src/.

    vocab.md once kept whole entries for `Audience` and `FieldRole` long after
    both were deleted. A lowercase word with no underscore (`fallback`) is prose,
    not a name, so it is skipped.
    """
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "src").rglob("*.py"))
    names = {
        name
        for line in (REPO / "vocab.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
        for name in CODE_NAME.findall(line)
        if not name.islower() or "_" in name
    }
    missing = sorted(
        n for n in names
        if not re.search(rf"(?m)^\s*(?:class|def)\s+{n}\b|^\s*{n}\s*[:=]|\bimport\b.*\b{n}\b", src)
    )
    assert not missing, "vocab.md headings name code that is not in src/: " + ", ".join(missing)
