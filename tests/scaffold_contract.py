"""Pure contract-check helpers for the rendered mintd scaffolds.

Test-side only: nothing here imports from ``mintd`` production code except
the CLI parser (read-only, for the C2 verb walk). The checks guard *classes*
of scaffold defect, not instance strings — this repo has already lost the
"instance assertions + review catches the next one" bet once (SLICE-37 ->
SLICE-42), so the guards live at the class level.

Checks:

* ``C1`` — :func:`check_dvc_out_closure`: every *file* out in a rendered data
  ``dvc.yaml`` is either produced by a script in the same tree, or (for stages
  whose ``cmd`` starts with ``mintd``) matches the tool's known default output
  in :data:`MINTD_STAGE_OUTS`.
* ``C2`` — :func:`check_embedded_mintd_commands`: every embedded ``mintd``
  command names a real CLI verb path (walked from the live argparse tree).
* ``C3`` — :func:`check_referenced_dirs`: every referenced directory under a
  known prefix exists in the scaffold, is a rendered file's parent, or is
  created in the same file.
* ``C4`` — :func:`is_ignored`: gitignore policy, exercised through real git.
* ``C5`` — :func:`check_requirements`: every requirements line parses and names
  a PyPI-installable package.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# C1 — dvc.yaml out closure
# ---------------------------------------------------------------------------

# Maps a ``mintd`` verb path (as it appears in a dvc.yaml stage ``cmd``) to the
# project-root-relative file the tool writes *by default*. Grounded against the
# ``schema generate`` handler in ``src/mintd/cli.py`` (``_handle_data_schema_
# generate``), which defaults its output to
# ``project_dir / "schemas" / "v1" / "schema.json"``.
#
# INTENTIONAL COUPLING: if that handler ever changes its default output path,
# THIS TABLE must change in the same commit — the contract suite is designed to
# break here so the template that declares the stage's ``outs`` gets updated
# too. Adding a new ``mintd``-driven dvc stage means adding its entry here
# deliberately.
MINTD_STAGE_OUTS: dict[tuple[str, ...], str] = {
    ("data", "schema", "generate"): "schemas/v1/schema.json",
}


def mintd_verb_path(cmd: str) -> list[str]:
    """Extract the verb path from a shell command containing ``mintd``.

    Returns the tokens after ``mintd`` up to (but not including) the first
    flag-like token (one starting with ``-``). ``mintd data schema generate
    --project-dir ..`` -> ``["data", "schema", "generate"]``. Returns ``[]``
    when the command does not invoke ``mintd``.
    """
    toks = cmd.split()
    if "mintd" not in toks:
        return []
    verbs: list[str] = []
    for tok in toks[toks.index("mintd") + 1:]:
        if tok.startswith("-"):
            break
        verbs.append(tok)
    return verbs


def _resolve_out(wdir: str, out: str) -> str:
    """Resolve a stage ``out`` (relative to ``wdir``) to a project-root-relative
    POSIX path. ``wdir="code"``, ``out="../schemas/v1/schema.json"`` ->
    ``"schemas/v1/schema.json"``."""
    return posixpath.normpath(posixpath.join(wdir or ".", out))


def check_stage_outs(stages: dict, script_texts: list[str]) -> list[str]:
    """Core of C1, operating on an already-parsed ``stages`` dict.

    Exposed separately so a unit test can feed a synthetic ``mintd``-cmd stage
    and prove the tool-outs branch bites.
    """
    violations: list[str] = []
    for stage_name, stage in stages.items():
        cmd = str(stage.get("cmd", "")).strip()
        wdir = str(stage.get("wdir", "."))
        outs = stage.get("outs") or []
        file_outs = [str(o) for o in outs if not str(o).endswith("/")]

        if cmd.startswith("mintd"):
            verbs = mintd_verb_path(cmd)
            if any(
                t == "--output" or t.startswith("--output=")
                for t in cmd.split()
            ):
                violations.append(
                    f"stage {stage_name!r}: cmd passes --output, so the tool's "
                    f"default output path is no longer authoritative "
                    f"(MINTD_STAGE_OUTS cannot verify it)"
                )
                continue
            expected = MINTD_STAGE_OUTS.get(tuple(verbs))
            if expected is None:
                violations.append(
                    f"stage {stage_name!r}: unknown mintd verb path {verbs} in "
                    f"cmd {cmd!r} — not in MINTD_STAGE_OUTS"
                )
                continue
            for out in file_outs:
                resolved = _resolve_out(wdir, out)
                if resolved != expected:
                    violations.append(
                        f"stage {stage_name!r}: mintd-tool out {out!r} resolves "
                        f"to {resolved!r}, expected {expected!r}"
                    )
        else:
            for out in file_outs:
                base = posixpath.basename(out)
                if not any(base in txt for txt in script_texts):
                    violations.append(
                        f"stage {stage_name!r}: file out {out!r} (basename "
                        f"{base!r}) is not produced by any script in the tree"
                    )
    return violations


def check_dvc_out_closure(tree: RenderedTree) -> list[str]:
    """C1 — every file out in the rendered data ``dvc.yaml`` is accounted for."""
    dvc_text = tree.files.get("dvc.yaml")
    if dvc_text is None:
        return []
    stages = yaml.safe_load(dvc_text).get("stages", {}) or {}
    script_texts = [
        txt for rel, txt in tree.files.items() if rel.startswith("code/")
    ]
    return check_stage_outs(stages, script_texts)


# ---------------------------------------------------------------------------
# C2 — embedded mintd commands parse
# ---------------------------------------------------------------------------

_VERB_RE = re.compile(r"mintd((?:\s+[a-z][a-z-]*)+)")
_SINGLE_LINE_QUOTE_RE = re.compile(r"""(["'])([^"'\n]*)\1""")


def _subparser_choices(parser: argparse.ArgumentParser) -> dict | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return None


def verb_path_valid(parser: argparse.ArgumentParser, verbs: list[str]) -> bool:
    """Walk ``verbs`` down the subparser ``.choices`` tree from ``parser``.

    A verb path is valid if each verb selects a real subparser until we reach a
    parser with no further subparsers, at which point remaining tokens are
    treated as positional args (also valid). Never calls ``parse_args`` (avoids
    argparse's exit-on-error).
    """
    current = parser
    for verb in verbs:
        choices = _subparser_choices(current)
        if choices is None:
            # No more subcommands here; remaining tokens are arguments.
            return True
        if verb not in choices:
            return False
        current = choices[verb]
    return True


def extract_mintd_commands(rel_path: str, text: str) -> list[list[str]]:
    """Extract candidate ``mintd`` verb sequences from one rendered file.

    Sources (chosen to catch real commands while excluding flowing prose that
    merely mentions "mintd"):

    1. dvc.yaml stage ``cmd`` values (parsed).
    2. Lines that begin with ``mintd`` after stripping leading whitespace
       (fenced markdown command lines).
    3. Single-line quoted string spans whose content, stripped, starts with
       ``mintd`` (e.g. a command embedded in a Python error string).
    """
    candidates: list[str] = []

    if posixpath.basename(rel_path) == "dvc.yaml":
        stages = yaml.safe_load(text).get("stages", {}) or {}
        for stage in stages.values():
            cmd = str(stage.get("cmd", ""))
            if "mintd" in cmd:
                candidates.append(cmd)

    for line in text.splitlines():
        if line.lstrip().startswith("mintd "):
            candidates.append(line.strip())

    for _, span in _SINGLE_LINE_QUOTE_RE.findall(text):
        if span.strip().startswith("mintd "):
            candidates.append(span.strip())

    commands: list[list[str]] = []
    for cand in candidates:
        m = _VERB_RE.search(cand)
        if m:
            commands.append(m.group(1).split())
    return commands


def check_embedded_mintd_commands(
    tree: RenderedTree, parser: argparse.ArgumentParser
) -> list[str]:
    """C2 — every embedded ``mintd`` command names a real CLI verb path."""
    violations: list[str] = []
    for rel, text in tree.files.items():
        for verbs in extract_mintd_commands(rel, text):
            if not verb_path_valid(parser, verbs):
                violations.append(
                    f"{rel}: `mintd {' '.join(verbs)}` is not a valid CLI verb path"
                )
    return violations


# ---------------------------------------------------------------------------
# C3 — referenced directories resolve
# ---------------------------------------------------------------------------

# Top-level scaffold directories a path reference may point under. Matching on
# the first path segment (rather than a ``startswith`` prefix) lets a bare
# ``logs`` created by `capture mkdir` match the same category as a
# ``logs/run_all.log`` reference, without treating ``codebase`` as ``code``.
#
# CALIBRATED (per the plan's "narrow the prefix set instead of growing an
# allowlist" rule) to ``code`` and ``logs`` only. Both observed live defects
# (run_all's phantom ``code/00_setup`` and un-created ``logs/``) live under
# these two roots, and they are the roots a *script* navigates to reach a
# file. The ``data``/``schemas`` roots are deliberately excluded: they are
# runtime-created by the pipeline (``_mintd_utils`` defines ``DATA_DIR`` /
# ``LOGS_DIR`` path constants that ``ingest``/``validate`` ``mkdir`` on the fly,
# and the shared template's ``../data/analysis`` constant legitimately points
# outside the *data* scaffold's dir list), so treating them as static-dir
# references produces only cross-scaffold false positives. Data-out closure is
# C1's job, not C3's.
_KNOWN_TOP_DIRS = frozenset({"code", "logs"})

# A candidate that carries regex / glob metacharacters is a pattern string
# (e.g. check-dvc-sync's ``code/.*\.(py|do)$`` PIPELINE_PATTERNS, or a
# ``data/**`` gitignore glob), not a real path reference — never a directory.
_PATTERN_CHARS = frozenset("*$()\\?[]|{}")

# Stata local-macro interpolation, e.g. `project_root' or `datetime'. These
# embed single quotes, so they must be stripped from the text *before* quote
# spans are extracted, or the `'` closes a span mid-path.
_STATA_MACRO_RE = re.compile(r"`[^`'\n]*'")
_DQUOTE_RE = re.compile(r'"([^"\n]*)"')
_SQUOTE_RE = re.compile(r"'([^'\n]*)'")
_FILE_PATH_CODEDIR_RE = re.compile(r"file\.path\(\s*CODE_DIR\s*,\s*([^)]*)\)")
_CREATE_DIR_RE = re.compile(
    r"(?:capture\s+mkdir|mkdir\s+-p|dir\.create|os\.makedirs)\s*\(?\s*"
    r"""["']?([^"'\n)]*)"""
)


def _demacro(text: str) -> str:
    return _STATA_MACRO_RE.sub("", text)


def _quoted_spans(text: str) -> list[str]:
    """Single- and double-quoted spans, after removing Stata macros."""
    clean = _demacro(text)
    return [m.group(1) for m in _DQUOTE_RE.finditer(clean)] + [
        m.group(1) for m in _SQUOTE_RE.finditer(clean)
    ]


def _normalize_path(raw: str) -> str:
    """Strip Stata macro interpolation and leading ``./`` / ``../`` / ``/``."""
    cleaned = _demacro(raw).strip()
    while cleaned.startswith(("./", "../", "/")):
        cleaned = cleaned.split("/", 1)[1] if "/" in cleaned else ""
    return cleaned


def _has_known_prefix(norm: str) -> bool:
    return bool(norm) and norm.split("/", 1)[0] in _KNOWN_TOP_DIRS


def _dir_of(path: str) -> str:
    """Directory implied by a path reference (dropping a trailing filename)."""
    path = path.rstrip("/")
    last = posixpath.basename(path)
    if "." in last:  # looks like a filename
        return posixpath.dirname(path)
    return path


def _prefixed_dir(raw: str) -> str | None:
    if _PATTERN_CHARS & set(raw):
        return None
    norm = _normalize_path(raw)
    # A directory *reference* is a multi-segment path (a script navigating into
    # a subdir); a bare single segment is a config value / type name, not a dir.
    if "/" not in norm:
        return None
    if _has_known_prefix(norm):
        d = _dir_of(norm)
        return d or None
    return None


def extract_referenced_dirs(text: str) -> set[str]:
    """Directories referenced by a rendered file, under the known prefix set."""
    refs: set[str] = set()
    for content in _quoted_spans(text):
        d = _prefixed_dir(content)
        if d:
            refs.add(d)
    for group in _FILE_PATH_CODEDIR_RE.findall(text):
        parts = re.findall(r'"([^"]*)"', group)
        if parts:
            joined = "code/" + "/".join(parts)
            d = _dir_of(joined)
            if d:
                refs.add(d)
    return refs


def extract_created_dirs(text: str) -> set[str]:
    """Directories a file creates itself (mkdir / dir.create / makedirs).

    The mkdir target *is* the directory, so (unlike a reference) we keep its
    last segment instead of dropping it as a filename."""
    created: set[str] = set()
    for raw in _CREATE_DIR_RE.findall(_demacro(text)):
        norm = _normalize_path(raw)
        if _has_known_prefix(norm):
            created.add(norm.rstrip("/"))
    return created


def check_referenced_dirs(tree: RenderedTree) -> list[str]:
    """C3 — every referenced directory resolves against the scaffold."""
    scaffold_dirs = set(tree.dirs)
    file_parents = {posixpath.dirname(rel) for rel in tree.files if "/" in rel}
    resolvable = scaffold_dirs | file_parents

    violations: list[str] = []
    for rel, text in tree.files.items():
        created = extract_created_dirs(text)
        for ref in extract_referenced_dirs(text):
            if ref in resolvable or ref in created:
                continue
            # A referenced dir also resolves if one of its ancestors is created
            # in the same file (mkdir of a parent covers children on repro).
            if any(ref == c or ref.startswith(c + "/") for c in created):
                continue
            violations.append(
                f"{rel}: references directory {ref!r} which does not exist in "
                f"the scaffold and is not created in the same file"
            )
    return violations


# ---------------------------------------------------------------------------
# C4 — gitignore policy via real git
# ---------------------------------------------------------------------------

def is_ignored(repo_dir: Path, rel: str) -> bool:
    """True iff ``rel`` is git-ignored inside ``repo_dir`` (a real git repo).

    ``git check-ignore -q`` exits 0 when the path is ignored, 1 when not.
    Promoted out of ``tests/test_templates.py`` so the downloads-gitignore test
    and the C4 contract check share one definition.
    """
    return (
        subprocess.run(
            ["git", "-C", str(repo_dir), "check-ignore", "-q", rel]
        ).returncode
        == 0
    )


# ---------------------------------------------------------------------------
# C5 — requirements sanity
# ---------------------------------------------------------------------------

# Names that are not installable from PyPI (shipped via their own installers).
NON_PYPI_PACKAGES = {"quarto"}


def check_requirements(text: str) -> list[str]:
    """C5 — every requirements line parses and names a PyPI package."""
    from packaging.requirements import InvalidRequirement, Requirement

    violations: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        try:
            req = Requirement(stripped)
        except InvalidRequirement as exc:
            violations.append(f"unparseable requirement {stripped!r}: {exc}")
            continue
        if req.name.lower() in NON_PYPI_PACKAGES:
            violations.append(
                f"requirement {req.name!r} is not installable from PyPI "
                f"(ships via its own installer)"
            )
    return violations


# ---------------------------------------------------------------------------
# Rendered-tree container
# ---------------------------------------------------------------------------

class RenderedTree:
    """One rendered scaffold: its type, language, on-disk root, dir list, and a
    ``{relpath: text}`` map of every rendered file (captured at render time, so
    later ``git init`` inside ``root`` never leaks a ``.git/`` into the map)."""

    def __init__(
        self,
        project_type: str,
        language: str,
        root: Path,
        dirs: list[str],
        files: dict[str, str],
    ) -> None:
        self.type = project_type
        self.language = language
        self.root = root
        self.dirs = dirs
        self.files = files
