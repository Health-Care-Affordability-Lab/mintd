"""`src/mintd` may not grow past its budget unless a PR says why.

The budget is the number in `src_size_budget.txt`: code lines in every `.py` file under `src/mintd`, not counting
blank lines, comments or docstrings. The scaffold templates (the `.j2` files in `src/mintd/files/`) aren't Python and
aren't counted. A PR that adds code raises the budget and says why. A PR that cuts more than `MARGIN` lines gets a
warning to lower it, so the cut stays cut; a cut never fails, so two parallel cuts can't turn main red.
"""

from __future__ import annotations

import ast
import functools
import io
import tokenize
import warnings
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mintd"
BUDGET_FILE = Path(__file__).with_name("src_size_budget.txt")
MARGIN = 100

_NOT_CODE = {
    tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
    tokenize.ENCODING, tokenize.ENDMARKER,
}


def code_lines(text: str) -> int:
    """Lines holding part of a real token, minus docstring lines."""
    lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type not in _NOT_CODE:
            lines.update(range(tok.start[0], tok.end[0] + 1))
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                lines.difference_update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return len(lines)


@functools.cache
def src_code_lines() -> int:
    return sum(code_lines(p.read_text(encoding="utf-8")) for p in SRC.rglob("*.py"))


def _check(count: int, budget: int) -> None:
    assert count <= budget, (
        f"src/mintd has {count} code lines, {count - budget} over its budget of {budget}. "
        f"If the growth is worth it, set tests/{BUDGET_FILE.name} to {count} "
        "and say in the PR why mintd needs the extra lines."
    )
    if count < budget - MARGIN:
        warnings.warn(
            f"src/mintd has {count} code lines, {budget - count} under its budget of {budget}. "
            f"Lower tests/{BUDGET_FILE.name} to {count} so the cut stays cut.",
            stacklevel=2,
        )


def test_src_mintd_is_within_its_budget() -> None:
    _check(src_code_lines(), int(BUDGET_FILE.read_text(encoding="utf-8").strip()))


def test_growth_fails_and_a_cut_only_warns() -> None:
    with pytest.raises(AssertionError, match="say in the PR why"):
        _check(1001, 1000)
    with pytest.warns(UserWarning, match="Lower tests/src_size_budget.txt to 1000"):
        _check(1000, 1000 + MARGIN + 1)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check(1000, 1000 + MARGIN)  # within the margin: silent


def test_the_counter_skips_blanks_comments_and_docstrings() -> None:
    sample = (
        '"""module doc"""\n'
        "\n"
        "# a comment\n"
        "x = 1  # trailing\n"
        "\n"
        "def f():\n"
        '    """doc,\n'
        '    two lines"""\n'
        '    return """a\n'
        'b"""\n'
    )
    # x = 1, def f(), and both lines of the returned string
    assert code_lines(sample) == 4
