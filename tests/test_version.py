"""Single-source-of-truth wiring for the package version.

``test_dunder_version_matches_metadata`` requires an installed (editable)
tree, since ``importlib.metadata.version`` reads ``.dist-info``/``.egg-info``
metadata.
"""

from __future__ import annotations

import ast
import inspect
from importlib.metadata import version as pkg_version

import mintd


def test_dunder_version_matches_metadata() -> None:
    assert mintd.__version__ == pkg_version("mintd")


def test_version_not_static_literal() -> None:
    """Guards against re-introducing a hardcoded version literal in
    __init__.py. Checked against the source AST rather than the runtime
    value: in an environment whose dist metadata also reads 0.0.1 (stale
    editable install, no-.git fallback build), a reintroduced literal
    would slip past the metadata-equality test above.

    The only permitted string assignment to __version__ is the
    PackageNotFoundError fallback inside the try/except.
    """
    tree = ast.parse(inspect.getsource(mintd))
    literal_assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    handler_lines = {
        node.lineno
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        for node in ast.walk(handler)
        if isinstance(node, ast.Assign)
    }
    offenders = [a for a in literal_assigns if a.lineno not in handler_lines]
    assert not offenders, (
        f"__version__ assigned a string literal at line(s) "
        f"{[a.lineno for a in offenders]} of mintd/__init__.py — the version "
        "must come from importlib.metadata (setuptools-scm), with a literal "
        "allowed only in the PackageNotFoundError fallback"
    )
