"""No function in `src/mintd` grows more complex, and a new one stays at 15 or under.

Complexity is ruff's C901 count, the one CI already caps at the largest function's. That cap lets every function
grow to it; this test holds each one where it is. `complexity_ratchet.txt` lists each function over `LIMIT` as
`<module>::<function> <complexity>`. A function may not rise above its line, and one not listed may not pass
`LIMIT`. A listed function that falls, or goes, gets a warning to lower or drop its line, so the gain stays. Raising
a line is an edit to a file the maintainers own, justified in the PR; moving a function to another module moves its
line.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RATCHET = Path(__file__).with_name("complexity_ratchet.txt")
LIMIT = 15


def over_limit() -> dict[str, int]:
    """`<module>::<function>` -> complexity, for each function over `LIMIT`, as ruff counts it."""
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src/mintd", "--isolated", "--select", "C901",
         "--config", f"lint.mccabe.max-complexity = {LIMIT}", "--output-format", "json", "--exit-zero"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    found: dict[str, int] = {}
    for row in json.loads(out):
        m = re.fullmatch(r"`(.+)` is too complex \((\d+) > \d+\)", row["message"])
        assert m, row["message"]
        module = Path(row["filename"]).as_posix().split("/src/mintd/", 1)[1]
        key = f"{module}::{m.group(1)}"
        found[key] = max(found.get(key, 0), int(m.group(2)))
    return found


def recorded() -> dict[str, int]:
    lines = (ln.split() for ln in RATCHET.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#"))
    return {key: int(n) for key, n in lines}


def _check(now: dict[str, int], kept: dict[str, int]) -> None:
    worse = {key: (kept.get(key), n) for key, n in now.items() if n > kept.get(key, LIMIT)}
    assert not worse, (
        f"more complex than tests/{RATCHET.name} allows (a function not listed may reach {LIMIT}):\n"
        + "\n".join(f"  {key}: {was or 'new'} -> {n}" for key, (was, n) in sorted(worse.items()))
        + "\nsplit it; or, if the growth is worth it, raise its line and say why in the PR"
    )
    lower = {key: now.get(key) for key, n in kept.items() if now.get(key, 0) < n}
    if lower:
        warnings.warn(f"lock the gain in tests/{RATCHET.name}: "
                      + ", ".join(f"{key} -> {n or 'drop the line'}" for key, n in sorted(lower.items())),
                      stacklevel=2)


def test_no_function_grows_more_complex() -> None:
    now, kept = over_limit(), recorded()
    assert now or not kept, "ruff found no function over the limit though the list names some: did it run on src/mintd?"
    _check(now, kept)


def test_growth_and_a_new_function_over_the_limit_fail_and_a_fall_only_warns() -> None:
    """Mutations: count an unlisted function against no limit -> the new one passes, red; drop the warning -> red."""
    kept = {"a.py::f": 20}
    with pytest.raises(AssertionError, match=re.escape("a.py::f: 20 -> 21")):
        _check({"a.py::f": 21}, kept)
    with pytest.raises(AssertionError, match=re.escape("b.py::g: new -> 16")):
        _check({"a.py::f": 20, "b.py::g": 16}, kept)
    with pytest.warns(UserWarning, match=re.escape("a.py::f -> 18")):
        _check({"a.py::f": 18}, kept)
    with pytest.warns(UserWarning, match=re.escape("a.py::f -> drop the line")):
        _check({}, kept)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _check({"a.py::f": 20}, kept)  # held: silent
