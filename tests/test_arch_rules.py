"""Architecture rules for `src/mintd`: which modules exist, who runs git, which way imports point, and no cycles.

The maintainers team owns this file, so each pinned list below changes only with their approval:
- a new module is added to `MODULES`;
- a layer crossing, a CLI import of a boundary module, or a lazy import that hides a cycle may only be removed, and
  removing one means deleting its line here too, so the list stays the true backlog.

Git and module-level cycles are hard gates: today there are none to pin.

Imports are read in both forms (`from .x import`, `from mintd.x import`), skipping `if TYPE_CHECKING:` blocks, which
never run. `_templates/` counts as one boundary module.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "mintd"


def _owned(name: str) -> str:
    return f"edit {name} in tests/test_arch_rules.py; the maintainers team approves the change (CODEOWNERS)"


@cache
def _trees() -> dict[str, ast.Module]:
    return {p.relative_to(SRC).as_posix(): ast.parse(p.read_text(encoding="utf-8")) for p in sorted(SRC.rglob("*.py"))}


#: Every module in `src/mintd`. A new one is a design choice the maintainer approves, as a new flag is.
MODULES = {
    "__init__.py", "__main__.py", "_archive_ops.py", "_atomic.py", "_aws_credentials.py", "_cache_ops.py",
    "_catalog_cache.py", "_catalog_serializer.py", "_config.py", "_console.py", "_dvc_invoke.py", "_dvc_ops.py",
    "_dvc_state.py", "_fast_sync_ops.py", "_git_invoke.py", "_import_rescue_ops.py", "_init_ops.py", "_paths.py",
    "_pin_proof.py", "_producer_git_ops.py", "_push_verify.py", "_registry_git_ops.py", "_release_check.py",
    "_s3.py", "_s3_listing_ops.py", "_share_ops.py", "_storage_state.py", "_subprocess.py",
    "_templates/__init__.py", "_templates/_render.py", "_templates/engine.py", "_templates/languages.py",
    "_templates/scaffolds.py", "catalog.py", "check.py", "cli.py", "config_ops.py", "data.py", "data_ops.py",
    "enclave.py", "errors.py", "imports.py", "init.py", "metadata_migrate.py", "model.py",
    "pending_registrations.py", "producer.py", "publish.py", "schema_ops.py",
}


def test_src_mintd_holds_only_the_pinned_modules() -> None:
    """Mutation: a new `src/mintd/*.py` -> red until it is added here."""
    assert set(_trees()) == MODULES, _owned("MODULES")


#: The modules that may build a `git` argv. Everything else reaches git through them.
GIT_OPS = {"_init_ops.py", "_producer_git_ops.py", "_registry_git_ops.py"}


def test_git_argv_is_built_only_in_the_git_ops_modules() -> None:
    """`_subprocess.run_streaming` lets any module spawn a process, so the argv is what's pinned: a list or tuple
    literal whose first item is `"git"`. Not seen: `"git status".split()`, `[GIT, ...]`, `["env", "git", ...]` or a
    shell string. Read as an argv by mistake: a tuple of tool names that starts with `"git"`; order it another way.
    Mutation: `["git", "status"]` in any other module -> red."""
    def builds_git(tree: ast.Module) -> bool:
        return any(isinstance(n, (ast.List, ast.Tuple)) and n.elts and isinstance(n.elts[0], ast.Constant)
                   and n.elts[0].value == "git" for n in ast.walk(tree))
    assert {m for m, t in _trees().items() if builds_git(t)} == GIT_OPS, _owned("GIT_OPS")


def _imported(n: ast.AST, depth: int) -> list[str]:
    """The `src/mintd` modules an import statement names, relative or absolute; a package counts as one module."""
    if isinstance(n, ast.ImportFrom):
        if n.level == depth + 1:  # relative to src/mintd (`.x` from a top-level module, `..x` from _templates/)
            return [n.module.split(".")[0]] if n.module else [x.name for x in n.names]
        if n.level == 0 and n.module == "mintd":
            return [x.name for x in n.names]
        if n.level == 0 and n.module and n.module.startswith("mintd."):
            return [n.module.split(".")[1]]
    if isinstance(n, ast.Import):
        return [x.name.split(".")[1] for x in n.names if x.name.startswith("mintd.")]
    return []


def _type_checking_only(tree: ast.Module) -> set[int]:
    """Every node inside an `if TYPE_CHECKING:` block: those imports never run."""
    return {id(inner) for n in ast.walk(tree) if isinstance(n, ast.If) and (
        (isinstance(n.test, ast.Name) and n.test.id == "TYPE_CHECKING")
        or (isinstance(n.test, ast.Attribute) and n.test.attr == "TYPE_CHECKING"))
        for s in n.body for inner in ast.walk(s)}


@cache
def _imports() -> dict[tuple[str, str], bool]:
    """(importer, imported) between modules -> True if any such import sits at module level."""
    names = {m.split("/")[0].removesuffix(".py") for m in _trees()}
    edges: dict[tuple[str, str], bool] = {}
    for m, tree in _trees().items():
        a, depth = m.split("/")[0].removesuffix(".py"), m.count("/")
        top, skip = {id(n) for n in tree.body}, _type_checking_only(tree)
        for n in ast.walk(tree):
            if id(n) in skip:
                continue
            for b in _imported(n, depth):
                if b in names and b != a:
                    edges[(a, b)] = edges.get((a, b), False) or id(n) in top
    return edges


#: Types and errors every layer may import.
CORE = {"errors", "model", "catalog"}
#: Imports that point up a layer: the CLI imported by anything but `__main__`, or a boundary (underscore) module
#: importing an operations module.
LAYER_CROSSINGS = {("_cache_ops", "cli"), ("_dvc_ops", "imports"), ("_import_rescue_ops", "imports")}


def test_imports_point_down_the_layers() -> None:
    """CLI, then operations, then boundary modules. Mutation: `_s3` importing `publish` -> red."""
    edges = _imports()
    ops = {b for _, b in edges if not b.startswith("_")} - CORE - {"cli"}
    crossings = {(a, b) for a, b in edges
                 if (b == "cli" and a != "__main__") or (a.startswith("_") and a != "__main__" and b in ops)}
    assert crossings == LAYER_CROSSINGS, _owned("LAYER_CROSSINGS")


#: Boundary modules the CLI imports directly. The CLI should call operations, so this list only shrinks.
CLI_BOUNDARY_IMPORTS = {"_dvc_state", "_s3"}


def test_the_cli_imports_no_new_boundary_module() -> None:
    """Mutation: `cli.py` importing `_atomic` -> red."""
    assert {b for a, b in _imports() if a == "cli" and b in {"_dvc_state", "_s3", "_atomic"}} == CLI_BOUNDARY_IMPORTS, (
        _owned("CLI_BOUNDARY_IMPORTS"))


#: Imports inside a function that close an import cycle. Each hides a cycle a module-level import would raise on.
CYCLE_CLOSERS = {
    ("_cache_ops", "cli"), ("_producer_git_ops", "_registry_git_ops"), ("_registry_git_ops", "_producer_git_ops"),
    ("_registry_git_ops", "_subprocess"), ("catalog", "_catalog_cache"), ("catalog", "_catalog_serializer"),
    ("catalog", "check"), ("catalog", "model"), ("check", "data"), ("check", "enclave"),
    ("errors", "_registry_git_ops"), ("publish", "data_ops"), ("_templates", "model"),
}


def _reaches(edges: set[tuple[str, str]], start: str, goal: str) -> bool:
    seen, todo = {start}, [start]
    while todo:
        here = todo.pop()
        if here == goal:
            return True
        for a, b in edges:
            if a == here and b not in seen:
                seen.add(b)
                todo.append(b)
    return False


def test_no_import_cycle_at_module_level() -> None:
    """Mutation: `model.py` importing `check` at module level -> red."""
    top = {e for e, at_top in _imports().items() if at_top}
    assert [e for e in top if _reaches(top, e[1], e[0])] == [], "a module-level import cycle: break it, never pin it"


def test_no_new_import_cycle_hidden_in_a_function() -> None:
    """Mutation: `_paths.py` importing `check` inside a function -> red."""
    edges = _imports()
    every = set(edges)
    assert {e for e, at_top in edges.items() if not at_top and _reaches(every, e[1], e[0])} == CYCLE_CLOSERS, (
        _owned("CYCLE_CLOSERS"))
