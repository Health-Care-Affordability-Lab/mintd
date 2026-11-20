"""Scaffold contract suite (C1-C7) over the full rendered matrix.

One module-scoped fixture renders the 8 unique scaffold trees once
(data x {py, r, stata}, project x {py, r, stata}, code, enclave) via the
public ``render_scaffold`` seam, exactly as ``test_templates.py`` does. Each
check guards a *class* of defect; see ``tests/scaffold_contract.py`` for the
helper contracts.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mintd._templates import render_scaffold, scaffold_targets
from mintd._templates._render import project_full_name
from mintd._templates.scaffolds import dispatch
from mintd.cli import _build_parser

from tests.scaffold_contract import (
    RenderedTree,
    check_dvc_out_closure,
    check_embedded_mintd_commands,
    check_referenced_dirs,
    check_requirements,
    check_stage_outs,
    is_ignored,
)

_COMBOS = [
    ("data", "python"),
    ("data", "r"),
    ("data", "stata"),
    ("project", "python"),
    ("project", "r"),
    ("project", "stata"),
    ("code", "python"),
    ("enclave", "python"),
]

_DATA_LANGS = ["python", "r", "stata"]


@pytest.fixture(scope="module")
def rendered_matrix(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Render every unique scaffold tree once.

    WARNING to future editors: ``test_c4_gitignore_policy`` runs ``git init``
    *inside* the project/python tree below. Every check reads files from the
    captured ``RenderedTree.files`` map (populated here, at render time), NOT
    via ``os.walk`` of the on-disk tree — so C4's ``.git/`` never leaks into
    C1/C3/C5. Do not switch any check to directory traversal without excluding
    ``.git/``.
    """
    base = tmp_path_factory.mktemp("matrix")
    trees: dict[tuple[str, str], RenderedTree] = {}
    for ptype, lang in _COMBOS:
        root = base / f"{ptype}_{lang}"
        root.mkdir()
        written = render_scaffold(
            project_type=ptype, name="foo", language=lang, target_dir=root
        )
        files = {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in written
        }
        full_name = project_full_name(ptype, "foo")
        dirs, _ = dispatch(ptype)(lang, "foo", full_name)
        trees[(ptype, lang)] = RenderedTree(ptype, lang, root, list(dirs), files)
    return trees


# --- C1 — dvc.yaml out closure --------------------------------------------

@pytest.mark.parametrize("lang", _DATA_LANGS)
def test_c1_dvc_out_closure(rendered_matrix: dict, lang: str) -> None:
    """Every file out in the rendered data dvc.yaml is produced by a script in
    the tree, or (for mintd-cmd stages) matches the tool's known default out."""
    tree = rendered_matrix[("data", lang)]
    violations = check_dvc_out_closure(tree)
    assert not violations, "C1 (dvc out closure) violations:\n" + "\n".join(
        violations
    )


def test_c1_helper_rejects_phantom_mintd_out() -> None:
    """Teeth proof for the mintd-tool-outs branch: a phantom out on a
    ``mintd``-cmd stage fails; the table-matching out passes."""
    phantom = {
        "schema": {
            "cmd": "mintd data schema generate --project-dir ..",
            "wdir": "code",
            "outs": ["../data/phantom.json"],
        }
    }
    assert check_stage_outs(phantom, []), "phantom mintd-tool out must fail C1"

    # The mapping form an uncached out takes is checked too, not skipped.
    phantom_mapping = {
        "schema": {
            "cmd": "mintd data schema generate --project-dir ..",
            "wdir": "code",
            "outs": [{"../data/phantom.json": {"cache": False}}],
        }
    }
    assert check_stage_outs(phantom_mapping, []), "phantom mapping-form out must fail C1"

    good = {
        "schema": {
            "cmd": "mintd data schema generate --project-dir ..",
            "wdir": "code",
            "outs": ["../schemas/v1/schema.json"],
        }
    }
    assert not check_stage_outs(good, []), (
        "table-matching mintd-tool out must pass C1"
    )


# --- C2 — embedded mintd commands parse -----------------------------------

@pytest.mark.parametrize("combo", _COMBOS, ids=lambda c: f"{c[0]}-{c[1]}")
def test_c2_embedded_mintd_commands_parse(
    rendered_matrix: dict, combo: tuple[str, str]
) -> None:
    """Every embedded ``mintd`` command names a real CLI verb path."""
    tree = rendered_matrix[combo]
    parser = _build_parser()
    violations = check_embedded_mintd_commands(tree, parser)
    assert not violations, "C2 (embedded command) violations:\n" + "\n".join(
        violations
    )


# --- C3 — referenced directories resolve ----------------------------------

@pytest.mark.parametrize("combo", _COMBOS, ids=lambda c: f"{c[0]}-{c[1]}")
def test_c3_referenced_dirs_resolve(
    rendered_matrix: dict, combo: tuple[str, str]
) -> None:
    """Every referenced directory exists in the scaffold, is a file's parent,
    or is created in the same file."""
    tree = rendered_matrix[combo]
    violations = check_referenced_dirs(tree)
    assert not violations, "C3 (referenced dir) violations:\n" + "\n".join(
        violations
    )


# --- C4 — gitignore policy via real git -----------------------------------

@pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)
def test_c4_gitignore_policy(rendered_matrix: dict) -> None:
    """The project scaffold's .gitignore keeps results/ artifacts ignored while
    leaving .gitkeep / .dvc pointers trackable, ignores raw data, and never
    ignores source code."""
    tree = rendered_matrix[("project", "python")]
    subprocess.run(["git", "init", "-q", str(tree.root)], check=True)

    assert is_ignored(tree.root, "results/figures/plot.png")
    assert not is_ignored(tree.root, "results/figures/.gitkeep")
    assert not is_ignored(tree.root, "results/tables/model.dvc")
    assert is_ignored(tree.root, "data/raw/big.csv")
    assert not is_ignored(tree.root, "code/analysis.py")
    # init's crash marker must never be committed.
    assert is_ignored(tree.root, ".mintd-init-incomplete")


# --- C5 — requirements sanity ---------------------------------------------

@pytest.mark.parametrize("combo", _COMBOS, ids=lambda c: f"{c[0]}-{c[1]}")
def test_c5_requirements_sanity(
    rendered_matrix: dict, combo: tuple[str, str]
) -> None:
    """Every rendered requirements*.txt line parses and names a PyPI package."""
    tree = rendered_matrix[combo]
    violations: list[str] = []
    for rel, text in tree.files.items():
        name = Path(rel).name
        if name.startswith("requirements") and name.endswith(".txt"):
            violations.extend(f"{rel}: {v}" for v in check_requirements(text))
    assert not violations, "C5 (requirements) violations:\n" + "\n".join(
        violations
    )


@pytest.mark.skipif(
    not os.environ.get("MINTD_NETWORK_TESTS"),
    reason="network test; set MINTD_NETWORK_TESTS=1 to run uv pip compile",
)
def test_c5_requirements_resolvable_network(rendered_matrix: dict) -> None:
    """Opt-in: the rendered project requirements actually resolve on PyPI.

    The only *true* guard against an unsatisfiable pin. Skipped by default."""
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")
    tree = rendered_matrix[("project", "python")]
    req = tree.root / "requirements.txt"
    result = subprocess.run(
        ["uv", "pip", "compile", str(req), "-o", "/dev/null"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --- C7 — the rendered scaffold's Python is ruff-clean ---------------------
#
# The only check that a fresh scaffold's Python is valid: it runs ruff over the
# rendered files at ruff's defaults, with the ruff pinned in pyproject.toml.

_LONG_NAME = "a" + "b" * 66 + "c"  # 68 chars; see the long-name cell below

_no_ruff = pytest.mark.skipif(
    importlib.util.find_spec("ruff") is None, reason="ruff not importable"
)


def _ruff(*args: str) -> subprocess.CompletedProcess:
    # `sys.executable -m ruff`, never shutil.which("ruff"): windows-test
    # invokes .venv\Scripts\pytest.exe by full path without activating the
    # venv, so which() finds nothing and a which()-gated test would skip on
    # all three Windows cells while looking green.
    return subprocess.run(
        [sys.executable, "-m", "ruff", *args],
        capture_output=True,
        text=True,
    )


@_no_ruff
# The data and project scaffolds, the ones this has always held to ruff; the
# code and enclave scaffolds never were.
@pytest.mark.parametrize(("ptype", "lang"), [c for c in _COMBOS if c[0] in ("data", "project")])
@pytest.mark.parametrize("name", ["foo", _LONG_NAME], ids=["short-name", "long-name"])
def test_c7_rendered_python_is_ruff_clean(
    tmp_path: Path, ptype: str, lang: str, name: str
) -> None:
    """A freshly rendered scaffold's Python passes ruff check and ruff format.

    Rendered into a function-scoped ``tmp_path`` rather than reusing
    ``rendered_matrix``, whose project/python tree gets a ``.git/`` from C4.

    The long-name cell is load-bearing, not padding: the templates' log lines
    embed the project name, so they cross ruff's default 88 columns once the
    name passes ~43 characters and ruff-format rewraps the *rendered* file.
    A suite pinned at ``name="foo"`` would never see that.
    """
    render_scaffold(
        project_type=ptype, name=name, language=lang, target_dir=tmp_path
    )

    # --isolated everywhere: pyproject.toml sets line-length = 120, but a
    # rendered scaffold ships no ruff config, so a user's ruff runs at the
    # default 88. Checking at 120 would pass here and still fail for them.
    check = _ruff("check", "--isolated", str(tmp_path))
    assert check.returncode == 0, f"ruff check:\n{check.stdout}{check.stderr}"

    fmt = _ruff("format", "--isolated", "--check", str(tmp_path))
    assert fmt.returncode == 0, f"ruff format:\n{fmt.stdout}{fmt.stderr}"


# --- C6 — the collision guard sees exactly what the renderer writes -------

@pytest.mark.parametrize(("ptype", "lang"), _COMBOS)
def test_c6_scaffold_targets_match_renderer(
    rendered_matrix: dict, ptype: str, lang: str
) -> None:
    """``scaffold_targets`` is what ``init`` refuses to overwrite, so it must
    equal what ``render_scaffold`` actually writes -- in the same order.

    If these drift, init either refuses on a file it would not have written
    (a false refusal the user cannot clear) or silently overwrites one it
    would (the defect the guard exists to stop).
    """
    tree = rendered_matrix[(ptype, lang)]
    assert scaffold_targets(
        project_type=ptype, name="foo", language=lang
    ) == list(tree.files)
