"""Scaffold contract suite (C1-C5) over the full rendered matrix.

One module-scoped fixture renders the 8 unique scaffold trees once
(data x {py, r, stata}, project x {py, r, stata}, code, enclave) via the
public ``render_scaffold`` seam, exactly as ``test_templates.py`` does. Each
check guards a *class* of defect; see ``tests/scaffold_contract.py`` for the
helper contracts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mintd._templates import render_scaffold
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


# --- Bounded execution: the rendered lockfile hook ------------------------

def _write_hook(tree: RenderedTree, work: Path) -> Path:
    hook = work / "check-env-lockfiles.sh"
    # Normalize CRLF -> LF: on Windows CI, git's autocrlf checks the .j2
    # template out with \r\n and bash chokes on the \r (exit 1 everywhere).
    # We are testing the hook's LOGIC, not the checkout's line-ending
    # accident; real scaffolds .gitattributes-pin *.sh to LF is a tracked
    # Windows-GA follow-up (project_windows_support_followup).
    body = tree.files["scripts/check-env-lockfiles.sh"].replace("\r\n", "\n")
    hook.write_text(body, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return hook


def _run_hook(hook: Path, work: Path) -> int:
    return subprocess.run(
        ["bash", str(hook)], cwd=work, capture_output=True, text=True
    ).returncode


@pytest.mark.skipif(
    os.name == "nt",
    reason="the hook is a POSIX pre-commit script, fully exercised on the six "
    "POSIX cells; on windows-latest subprocess bash resolution is unreliable "
    "(System32's WSL-stub bash.exe can shadow Git Bash and exits 1 for any "
    "script — observed as constant exit 1 across all scenarios), so running "
    "it there tests the runner, not the hook",
)
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize(
    ("scenario", "lock_content", "expected"),
    [
        ("no_requirements", None, 0),
        ("lock_missing", "__omit__", 1),
        ("lock_empty", "", 1),
        ("lock_whitespace", "   \n\t\n", 1),
        ("lock_real", "pandas==2.0.0\n", 0),
    ],
)
def test_lockfile_hook_scenarios(
    rendered_matrix: dict,
    tmp_path: Path,
    scenario: str,
    lock_content: str | None,
    expected: int,
) -> None:
    """The lockfile hook fails on a missing/empty/whitespace-only lock and
    passes on no-requirements or a real lock."""
    tree = rendered_matrix[("project", "python")]
    hook = _write_hook(tree, tmp_path)

    if scenario != "no_requirements":
        (tmp_path / "requirements.txt").write_text("pandas>=1.5.0\n")
    if lock_content not in (None, "__omit__"):
        (tmp_path / "requirements-lock.txt").write_text(lock_content)

    assert _run_hook(hook, tmp_path) == expected
