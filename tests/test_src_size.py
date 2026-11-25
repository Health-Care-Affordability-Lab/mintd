"""`src/mintd` may not grow past its budget unless a PR says why.

The count is code lines in every `.py` file under `src/mintd`, not counting blank lines, comments or docstrings. The scaffold templates (the `.j2` files in `src/mintd/files/`) aren't Python and
aren't counted. Nor is `src/mintd/files/land.py.txt`: it is Python that the enclave packaging copies
into every transfer archive as `land.py`, but it is stored as `.txt` and the rule counts `.py` files only.

The budget is the number in `src_size_budget/base.txt` (`17118: <when it was last folded>`) plus the number in
every other `.txt` file there. A PR that adds code adds
its own file, one line such as `+28: why mintd needs the lines`, so two growing PRs never edit the same line. A PR
that cuts more than `MARGIN` lines gets a warning to lock the cut in with a `-N` file; a cut never fails. Now and then
one PR folds the files into `base.txt`. Packaging ships every folder of Python
under `src/`, with or without an `__init__.py`, so Python there outside mintd fails the suite rather than ship
uncounted.
"""

from __future__ import annotations

import ast
import functools
import io
import os
import re
import tokenize
import warnings
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mintd"
BUDGET_DIR = Path(__file__).with_name("src_size_budget")
BASE = "base.txt"
MARGIN = 100
_BASE_LINE = re.compile(r"(\d+): *(\S.*)")
_PR_LINE = re.compile(r"([+-]\d+): *(\S.*)")

_NOT_CODE = {
    tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
    tokenize.ENCODING, tokenize.ENDMARKER,
}


def code_lines(text: str) -> int:
    """Lines holding part of a real token. A docstring's own string token isn't one, but code sharing its line is."""
    docstrings: set[tuple[int, int]] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add((first.lineno, first.end_lineno or first.lineno))
    lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in _NOT_CODE or (tok.type == tokenize.STRING and (tok.start[0], tok.end[0]) in docstrings):
            continue
        lines.update(range(tok.start[0], tok.end[0] + 1))
    return len(lines)


@functools.cache
def src_code_lines() -> int:
    return sum(code_lines(p.read_text(encoding="utf-8")) for p in SRC.rglob("*.py"))


def python_outside_mintd(src: Path) -> list[str]:
    """What under ``src`` holds a ``.py`` file besides mintd: packaging would ship it uncounted."""
    return sorted({p.relative_to(src).parts[0] for p in src.rglob("*.py")} - {"mintd"})


def _number(path: Path, line: re.Pattern[str], example: str) -> int:
    """The number on the file's one line; a file that isn't UTF-8 or doesn't match fails, named."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        text = ""
    found = line.fullmatch(text)
    assert found, f"tests/{path.parent.name}/{path.name} must be UTF-8 and hold one line such as '{example}'."
    return int(found.group(1))


def budget(folder: Path = BUDGET_DIR) -> int:
    """`base.txt`'s number plus each other `.txt` file's signed number."""
    total = _number(folder / BASE, _BASE_LINE, "17118: count at the last fold")
    for path in sorted(folder.glob("*.txt")):
        if path.name != BASE:
            total += _number(path, _PR_LINE, "+28: why mintd needs the lines")
    return total


def _pr_file() -> str:
    """The file a PR adds: its branch name in CI, a placeholder elsewhere."""
    return f"tests/{BUDGET_DIR.name}/{(os.environ.get('GITHUB_HEAD_REF') or '<your-branch>').replace('/', '-')}.txt"


def _check(count: int, budget: int) -> None:
    assert count > 0, "src/mintd has no code lines: the tree moved, or the counter found no .py files there."
    assert count <= budget, (
        f"src/mintd has {count} code lines, {count - budget} over its budget of {budget}. If the growth is worth it, "
        f"add {_pr_file()} holding '+{count - budget}: <why mintd needs the lines>', or raise its number by that much."
    )
    if count < budget - MARGIN:
        warnings.warn(
            f"src/mintd has {count} code lines, {budget - count} under its budget of {budget}. "
            f"Lock the cut in: add {_pr_file()} holding '-{budget - count}: <what was cut>'.",
            stacklevel=2,
        )


def _check_src(folder: Path = BUDGET_DIR) -> None:
    _check(src_code_lines(), budget(folder))


def test_src_mintd_is_within_its_budget() -> None:
    _check_src()


def test_the_real_check_counts_every_file_in_the_folder(tmp_path: Path) -> None:
    """Main's folder may hold only `base.txt`, so this pins that the real check sums the files: a base one under the
    count fails alone and passes with a `+1` file. Mutation: the real check reading `base.txt` only -> red."""
    (tmp_path / BASE).write_text(f"{src_code_lines() - 1}: one under\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="1 over its budget"):
        _check_src(tmp_path)
    (tmp_path / "grow.txt").write_text("+1: one more\n", encoding="utf-8")
    _check_src(tmp_path)


def test_the_budget_is_the_base_plus_each_pr_file(tmp_path: Path) -> None:
    """A merged PR's file stays until a fold, and still counts. Mutations: PR files ignored, a file's sign
    dropped, or the base ignored -> red."""
    (tmp_path / BASE).write_text("1000: the last fold\n")
    (tmp_path / "grow-a.txt").write_text("+28: a new verb\n")
    (tmp_path / "cut-b.txt").write_text("-5: two helpers merged\n")
    (tmp_path / "notes.md").write_text("not counted\n")
    assert budget(tmp_path) == 1023


@pytest.mark.parametrize("text", ["28: no sign", "+28:", "+28 no colon", "", "+2.5: not whole", "+28: one\n+3: two"])
def test_a_pr_file_that_is_not_one_signed_line_with_a_reason_fails(tmp_path: Path, text: str) -> None:
    """Mutation: the reason made optional -> the `+28:` row red."""
    (tmp_path / BASE).write_text("1000: the last fold\n")
    (tmp_path / "bad.txt").write_text(text, encoding="utf-8")
    with pytest.raises(AssertionError, match="bad.txt must be UTF-8 and hold one line"):
        budget(tmp_path)


@pytest.mark.parametrize("name, data", [
    (BASE, b"1000\n"),                                        # the old bare form
    (BASE, b"junk\n"),
    (BASE, "\ufeff1000: a BOM\n".encode("utf-8")),
    ("pr.txt", "+28: from Windows PowerShell\n".encode("utf-16")),
])
def test_a_budget_file_that_is_not_utf8_or_not_its_form_fails_named(tmp_path: Path, name: str, data: bytes) -> None:
    (tmp_path / BASE).write_text("1000: the last fold\n", encoding="utf-8")
    (tmp_path / name).write_bytes(data)
    with pytest.raises(AssertionError, match=f"{re.escape(name)} must be UTF-8 and hold one line"):
        budget(tmp_path)


def test_src_holds_no_python_but_mintd() -> None:
    assert python_outside_mintd(SRC.parent) == [], "src/ holds Python besides mintd: count it, or move it out of src/."


def test_python_outside_mintd_is_found(tmp_path: Path) -> None:
    """A package, and a folder of Python with no `__init__.py` (packaging finds namespace folders too)."""
    for path in ("mintd/__init__.py", "mintd/sub/x.py", "extra/__init__.py", "loose/deep/mod.py", "mintd.egg-info/PKG-INFO"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).touch()
    assert python_outside_mintd(tmp_path) == ["extra", "loose"]


def test_growth_fails_and_a_cut_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    with pytest.raises(AssertionError, match="no code lines"):
        _check(0, 1000)
    with pytest.raises(AssertionError, match=re.escape("add tests/src_size_budget/<your-branch>.txt holding '+1: <why")):
        _check(1001, 1000)
    monkeypatch.setenv("GITHUB_HEAD_REF", "")  # set but empty, as on a push: still the placeholder
    with pytest.raises(AssertionError, match=re.escape("add tests/src_size_budget/<your-branch>.txt holding '+2: <why")):
        _check(1002, 1000)
    monkeypatch.setenv("GITHUB_HEAD_REF", "feat/x")
    with pytest.raises(AssertionError, match=re.escape("add tests/src_size_budget/feat-x.txt holding '+3: <why")):
        _check(1003, 1000)
    with pytest.warns(UserWarning, match=re.escape("add tests/src_size_budget/feat-x.txt holding '-101: <what")):
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
    assert code_lines('def f(): """d"""; x = 1; return x\n') == 1  # code sharing a docstring's line counts
