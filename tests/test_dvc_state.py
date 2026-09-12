"""`outs_matching` — declared metadata paths resolved against DVC truth.

The pure tier. The real-dvc oracle that licenses these fixtures against dvc
itself lives in `tests/test_harness_contract.py`
(`test_outs_matching_agrees_with_real_dvc_on_one_graph`).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mintd._dvc_state import outs_matching

# ---------------------------------------------------------------------------
# builders
#
# Module-local on purpose: one slice does not earn a shared helper, and a module
# under `tests/_fakes/` would owe a LICENSED/UNLICENSED verdict at
# `tests/test_substrate_rules.py:504-521`.
# ---------------------------------------------------------------------------


def _pointer(project: Path, out_rel: str, md5: str = "a" * 32, *, is_dir: bool = False) -> None:
    """Write `<out_rel>.dvc` the way `dvc add` does.

    The recorded `path` is the BASENAME, not the project-relative path: a
    `.dvc` file anchors its out to its own directory
    (`_fast_sync_ops.workspace_path_for`). That is what makes the anchoring
    load-bearing rather than decorative, so the fixture must reproduce it.
    """
    p = project / f"{out_rel}.dvc"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "outs:\n"
        f"- path: {Path(out_rel).name}\n"
        f"  md5: {md5}{'.dir' if is_dir else ''}\n"
        "  size: 12\n"
        "  cloud:\n"
        "    storage:\n"
        "      version_id: v-1\n",
        encoding="utf-8",
    )


def _pipeline(project: Path, stages: dict[str, dict]) -> None:
    """Write `dvc.yaml` AND `dvc.lock` together.

    The dvc.yaml is NOT optional. S1's orphan guard
    (`src/mintd/_fast_sync_ops.py:553-555`) drops any lock stage whose name and
    `@`-base are both absent from dvc.yaml, so a lock-only fixture enumerates
    to `[]` and every direction-3 assertion would pass vacuously against a
    resolver that does nothing.

    `stages` maps a LOCK stage name (`fan@a` for a foreach instance) to
    `{"wdir": str | None, "outs": [lock-relative path, ...]}`. The declared
    `@`-base is derived and written into dvc.yaml.
    """
    yaml_stages: dict[str, str | None] = {}
    for lock_name, spec in stages.items():
        yaml_stages.setdefault(lock_name.split("@", 1)[0], spec.get("wdir"))

    y = ["stages:"]
    for name, wdir in yaml_stages.items():
        y.append(f"  {name}:")
        if wdir:
            y.append(f"    wdir: {wdir}")
        y.append("    cmd: run")
    (project / "dvc.yaml").write_text("\n".join(y) + "\n", encoding="utf-8")

    lk = ["schema: '2.0'", "stages:"]
    for lock_name, spec in stages.items():
        lk.append(f"  {lock_name}:")
        lk.append("    cmd: run")
        lk.append("    outs:")
        for i, o in enumerate(spec["outs"]):
            lk.append(f"    - path: {o}")
            lk.append(f"      md5: {chr(ord('a') + i) * 32}")
            lk.append("      size: 12")
    (project / "dvc.lock").write_text("\n".join(lk) + "\n", encoding="utf-8")


def _targets(project: Path, declared: str) -> list[str]:
    return sorted(o.target for o in outs_matching(project, declared, "storage"))


# ---------------------------------------------------------------------------
# the three resolution directions
# ---------------------------------------------------------------------------


def test_outs_matching_declared_equals_a_tracked_out(tmp_path: Path) -> None:
    """Direction 1, over both out kinds."""
    _pointer(tmp_path, "data/final/a.parquet")
    _pipeline(tmp_path, {"build": {"outs": ["data/staged/b.csv"]}})

    assert _targets(tmp_path, "data/final/a.parquet") == ["data/final/a.parquet"]
    assert _targets(tmp_path, "data/staged/b.csv") == ["data/staged/b.csv"]


def test_outs_matching_declared_under_a_tracked_directory_out(tmp_path: Path) -> None:
    """Direction 2: a declared subpath resolves to its ENCLOSING directory out.

    The expansion is therefore wider than the declared path — pulling it
    delivers the subpath's siblings too. Correct for this contract (it returns
    OUTS, and the out is the directory), but a caller must not report
    "delivered exactly what you asked for" off the back of it.
    """
    _pointer(tmp_path, "data/parts", is_dir=True)

    assert _targets(tmp_path, "data/parts/p1.csv") == ["data/parts"]


@pytest.mark.parametrize(
    "shape",
    ["per-file-pointers", "foreach-lock-outs", "mixed", "wdir-anchored"],
)
def test_outs_matching_directory_over_per_file_outs(tmp_path: Path, shape: str) -> None:
    """**Direction 3 — issue04's must-pass case**, over four live shapes.

    A directory declared over per-file outs is the ordinary lab shape, and a
    two-direction predicate rejects it: `_is_tracked('data/final', …)` is
    False while DVC tracks everything beneath it. The `mixed` row is the one
    that exposed the stamp defect — a prefix holding BOTH a lock out and a
    pointer, which an either/or "lock outs, else pointers" matcher fails.

    Mutation: drop the `path_covers(declared_rel, rel)` term in
    `outs_matching` -> all four rows return [] and this reddens.
    """
    if shape == "per-file-pointers":
        _pointer(tmp_path, "data/final/a.parquet")
        _pointer(tmp_path, "data/final/b.parquet")
        declared, expect = "data/final/", ["data/final/a.parquet", "data/final/b.parquet"]
    elif shape == "foreach-lock-outs":
        _pipeline(tmp_path, {
            "hhi@t1": {"outs": ["data/final/hhi/t1.csv"]},
            "hhi@t2": {"outs": ["data/final/hhi/t2.csv"]},
        })
        declared, expect = "data/final/hhi/", ["data/final/hhi/t1.csv", "data/final/hhi/t2.csv"]
    elif shape == "mixed":
        _pointer(tmp_path, "data/final/ptr.csv")
        _pipeline(tmp_path, {"lockstage": {"outs": ["data/final/lock.csv"]}})
        declared, expect = "data/final/", ["data/final/lock.csv", "data/final/ptr.csv"]
    else:  # wdir-anchored
        _pipeline(tmp_path, {"wd": {"wdir": "code", "outs": ["out/p.csv"]}})
        declared, expect = "code/out/", ["code/out/p.csv"]

    assert _targets(tmp_path, declared) == expect


def test_outs_matching_is_casefolded(tmp_path: Path) -> None:
    """On a case-insensitive filesystem `data/FINAL/A.parquet` IS the tracked
    `data/final/a.parquet`, so a case-sensitive compare would miss it — the
    same reason `_is_tracked` casefolds (`_cache_ops.py:301-306`).

    Mutation: drop `.casefold()` in `path_covers` -> this reddens, and so does
    `tests/test_cache_ops.py::test_is_tracked_is_case_insensitive`.
    """
    _pointer(tmp_path, "data/final/a.parquet")

    assert _targets(tmp_path, "data/FINAL/A.parquet") == ["data/final/a.parquet"]
    assert _targets(tmp_path, "data/FINAL/") == ["data/final/a.parquet"]


def test_outs_matching_normalizes_a_lock_out_through_wdir(tmp_path: Path) -> None:
    """A lock out is recorded relative to its stage's `wdir`, so it is
    addressable at `code/out/p.csv` and NOT at the lock's own `out/p.csv`.

    Covers the plain, foreach-under-`do:`, and climbing-above-the-wdir shapes
    in one graph.

    Mutation: swap `workspace_path_for(project_path, out)` for
    `project_path / out.path` -> the pointer row of
    `test_outs_matching_stamps_the_pull_target_on_every_out` reddens (a
    pointer out's `path` is a bare basename).
    """
    _pipeline(tmp_path, {
        "wd": {"wdir": "code", "outs": ["out/p.csv"]},
        "fan@a": {"wdir": "code", "outs": ["out/a.csv"]},
        "climb@x": {"wdir": "code", "outs": ["../data/final/x.csv"]},
    })

    assert _targets(tmp_path, "code/out/") == ["code/out/a.csv", "code/out/p.csv"]
    assert _targets(tmp_path, "out/") == []          # the lock's own spelling is NOT a target
    assert _targets(tmp_path, "data/final/") == ["data/final/x.csv"]


@pytest.mark.parametrize(
    "declared,expect_match",
    [
        ("data/final/", True),
        ("./data/final/", True),
        ("data\\final", True),
        (".\\data\\final\\", True),
        ("data/final", True),
        # Known-bad spellings. Both fail CLOSED, which is the only direction
        # that matters here -- over-matching would turn a selector into a
        # whole-repo download. `normalize_target` has three production callers
        # outside this slice, so it is not changed here; the rooted-path escape
        # is filed at notes/mintd-check/FOLLOWUP-normalize-target-root-escape.md.
        (".//data/final", False),   # -> '/data/final', a ROOTED path
        ("data//final/", False),    # -> 'data//final', doubled separator
    ],
    ids=["trailing", "dot-slash", "backslash", "win-full", "bare", "double-dot-slash", "double-sep"],
)
def test_outs_matching_normalizes_declared_spellings(
    tmp_path: Path, declared: str, expect_match: bool,
) -> None:
    """One spelling normalizer (`normalize_target`), called on `declared`
    rather than re-derived here."""
    _pointer(tmp_path, "data/final/a.parquet")

    got = _targets(tmp_path, declared)
    assert got == (["data/final/a.parquet"] if expect_match else [])


@pytest.mark.parametrize("declared", ["data/nothing/", "data/final/a.parquet2", "", "/"])
def test_outs_matching_unmatched_prefix_returns_empty(tmp_path: Path, declared: str) -> None:
    """The negative. `data/final/a.parquet2` is a STRING prefix of nothing
    tracked but not a PATH prefix, and an empty declared must never expand to
    the whole repo — `dvc pull ''` means every out, at exit 0.
    """
    _pointer(tmp_path, "data/final/a.parquet")

    assert outs_matching(tmp_path, declared, "storage") == []


def test_outs_matching_stamps_the_pull_target_on_every_out(tmp_path: Path) -> None:
    """Every returned out carries a NON-EMPTY `target` equal to its
    project-relative workspace path — pointer outs included.

    `parse_dvc_outs` hard-codes `target=""` (`_fast_sync_ops.py:429`); only
    `classify_targets` stamps it. So a resolver that FILTERS `_all_dvc_outs`
    hands `dvc pull` an empty string, and `dvc pull ''` does not fail — it
    pulls the whole repo at exit 0, turning a selector into a silent
    full-product download reported as success.

    The spelling is project-relative for BOTH kinds, never `X.dvc` for
    pointers: a mixed argv trips dvc 3.67.1's `index_from_targets` defect
    (`notes/issues/issue-dvc-checkout-mixed-argv.md`), which is why
    `data_ops._checkout_grouped` already keeps every argv homogeneous.

    Mutation: return `out` instead of `dataclasses.replace(out, target=rel)`
    -> the pointer rows come back `target=''` and this reddens.
    """
    _pointer(tmp_path, "data/final/a.parquet")
    _pipeline(tmp_path, {"fan@t1": {"outs": ["data/final/hhi/t1.csv"]}})

    outs = outs_matching(tmp_path, "data/final/", "storage")

    assert len(outs) == 2
    assert all(o.target for o in outs), [o.target for o in outs]
    assert sorted(o.target for o in outs) == [
        "data/final/a.parquet", "data/final/hhi/t1.csv",
    ]
    # Not the .dvc spelling, and not the pointer-relative basename.
    assert not any(o.target.endswith(".dvc") for o in outs)


def test_dvc_state_has_no_import_cycle() -> None:
    """`_dvc_state` sits BELOW `_fast_sync_ops` and must never import upward.

    `_cache_ops` imports `_fast_sync_ops`, and `_fast_sync_ops` imports
    `_cache_ops` nowhere, so `_cache_ops -> _dvc_state -> _fast_sync_ops` is a
    chain. Putting the resolver in `_fast_sync_ops` while it called
    `_cache_ops._all_dvc_outs` would close a cycle — which is why placement
    was a decision (Option A, 2026-08-14) and not a preference.

    A fresh subprocess per order: an in-process import proves nothing once
    pytest has already loaded the world.

    Mutation: add `from mintd._dvc_state import _all_dvc_outs` at the TOP of
    `_fast_sync_ops`' import block -> the orders fail with "cannot import name
    ... from partially initialized module". Placement in the block matters;
    appended at the bottom it reddens fewer orders.
    """
    for order in (
        "import mintd._cache_ops, mintd._fast_sync_ops",
        "import mintd._fast_sync_ops, mintd._cache_ops",
        "import mintd._dvc_state, mintd._cache_ops, mintd._fast_sync_ops",
    ):
        r = subprocess.run(
            [sys.executable, "-c", order],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{order!r} failed:\n{r.stderr}"
