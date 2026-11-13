"""`outs_matching` — declared metadata paths resolved against DVC truth.

The pure tier. The real-dvc oracle that licenses these fixtures against dvc
itself lives in `tests/test_harness_contract.py`
(`test_outs_matching_agrees_with_real_dvc_on_one_graph`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mintd._dvc_state import PointerJudgement, delivery, judge_pointers, outs_matching, push_targets
from mintd._fast_sync_ops import EMPTY_DIR_MD5, DvcFileEntry, dir_manifest_md5, ensure_dir_manifest
from tests._dvc_fixtures import REMOTE, write_dvc_config, write_pipeline, write_pointer

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
    same reason `_cache_ops._is_tracked` casefolds (its docstring states it).

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
        # remains a known, unfixed gap.
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
    pointers: a mixed argv trips dvc 3.67.1's `index_from_targets` defect,
    which is why
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


# ---------------------------------------------------------------------------
# `delivery` — would `mintd data import <declared>` land bytes?
#
# Still the pure tier: real pointer files, no `.dvc/config` (the CALLER reads
# the version_aware flag and passes it), no doubles. The shared writers come
# from `tests/_dvc_fixtures.py` because the gate, CLI and publish tests author
# the same shapes.
# ---------------------------------------------------------------------------


def _dir_pointer(project: Path, *, version_id: str | None) -> None:
    write_pointer(project, "data/final", is_dir=True, version_id=version_id)


def _two_pointers(project: Path, *, b_version_id: str | None) -> None:
    write_pointer(project, "data/final/a.csv")
    write_pointer(project, "data/final/b.csv", version_id=b_version_id)


# (id, setup, declared, version_aware, expected state)
_DELIVERY_ROWS = [
    ("untracked-va", lambda p: write_pointer(p, "data/raw.csv"), "data/final/", True, "untracked"),
    ("untracked-md5", lambda p: write_pointer(p, "data/raw.csv"), "data/final/", False, "untracked"),
    ("not-pullable-cache-false-va", lambda p: write_pipeline(p, "data/final/x.csv", cache=False),
     "data/final/x.csv", True, "not_pullable"),
    ("not-pullable-cache-false-md5", lambda p: write_pipeline(p, "data/final/x.csv", cache=False),
     "data/final/x.csv", False, "not_pullable"),
    ("not-pullable-import", lambda p: write_pointer(p, "data/final", is_dir=True, imported=True),
     "data/final/", True, "not_pullable"),
    ("unpinned-dir-no-files-va", lambda p: _dir_pointer(p, version_id=None), "data/final/", True, "unpinned"),
    ("dir-no-files-on-md5-remote", lambda p: _dir_pointer(p, version_id=None), "data/final/", False, "pinned"),
    ("dir-pinned", lambda p: _dir_pointer(p, version_id="v-1"), "data/final/", True, "pinned"),
    ("partial-per-file-pointers", lambda p: _two_pointers(p, b_version_id=None), "data/final/", True, "partial"),
    ("per-file-pointers-on-md5-remote", lambda p: _two_pointers(p, b_version_id=None), "data/final/", False, "pinned"),
    ("pinned-per-file-pointers", lambda p: _two_pointers(p, b_version_id="v-2"), "data/final/", True, "pinned"),
    ("missing-from-manifest-va", lambda p: write_pointer(p, "data/final", files={"a.csv": "v-1"}),
     "data/final/b.csv", True, "missing_from_manifest"),
    ("missing-from-manifest-md5", lambda p: write_pointer(p, "data/final", files={"a.csv": "v-1"}),
     "data/final/b.csv", False, "missing_from_manifest"),
    ("files-subpath-pinned", lambda p: write_pointer(p, "data/final", files={"a.csv": "v-1"}),
     "data/final/a.csv", True, "pinned"),
    ("files-subpath-unpinned-va", lambda p: write_pointer(p, "data/final", files={"a.csv": None}),
     "data/final/a.csv", True, "unpinned"),
    ("files-subpath-unpinned-md5", lambda p: write_pointer(p, "data/final", files={"a.csv": None}),
     "data/final/a.csv", False, "pinned"),
    ("via-parent-pinned-dir", lambda p: _dir_pointer(p, version_id="v-1"), "data/final/x.csv", True, "via_parent"),
    ("via-parent-unpinned-dir-va", lambda p: _dir_pointer(p, version_id=None), "data/final/x.csv", True, "unpinned"),
    ("via-parent-unpinned-dir-md5", lambda p: _dir_pointer(p, version_id=None), "data/final/x.csv", False, "via_parent"),
    ("via-parent-cache-false-lock-dir", lambda p: write_pipeline(p, "data/final", cache=False),
     "data/final/x.csv", True, "not_pullable"),
    ("empty-dir-files-format", lambda p: write_pointer(p, "data/final", files={}), "data/final/", True, "pinned"),
    ("empty-dir-md5", lambda p: write_pointer(p, "data/final", is_dir=True, version_id=None, md5=EMPTY_DIR_MD5[:-4]),
     "data/final/", True, "pinned"),
    # a files-format parent dvc never pushes: its manifest says nothing about delivery
    ("via-parent-import-files-parent", lambda p: write_pointer(p, "data/final", files={"x.csv": "v-1"}, imported=True),
     "data/final/x.csv", True, "not_pullable"),
    ("via-parent-cache-false-files-lock", lambda p: write_pipeline(p, "data/final", cache=False, files={"x.csv": "v-1"}),
     "data/final/x.csv", True, "not_pullable"),
    # a never-pushed out BESIDE pullable siblings is not part of the promise:
    # a pipeline's uncached metric commonly lives under data/final/
    ("mixed-cache-false-sibling-va", lambda p: (write_pointer(p, "data/final/a.csv"),
                                                write_pipeline(p, "data/final/b.csv", cache=False)),
     "data/final/", True, "pinned"),
    ("mixed-cache-false-sibling-md5", lambda p: (write_pointer(p, "data/final/a.csv"),
                                                 write_pipeline(p, "data/final/b.csv", cache=False)),
     "data/final/", False, "pinned"),
    ("mixed-import-sibling", lambda p: (write_pointer(p, "data/final/a.csv"),
                                        write_pointer(p, "data/final/b.csv", imported=True)),
     "data/final/", True, "pinned"),
    # the matcher folds case; delivery must not, because the consumer's filesystem may not
    ("case-mismatch", lambda p: _dir_pointer(p, version_id="v-1"), "Data/Final/", True, "untracked"),
    ("case-mismatch-subpath", lambda p: _dir_pointer(p, version_id="v-1"), "Data/Final/x.csv", True, "untracked"),
    ("case-mismatch-files-subpath", lambda p: write_pointer(p, "data/final", files={"b.csv": "v-1"}),
     "data/final/B.csv", True, "missing_from_manifest"),
    # a parent known to hold nothing cannot be hiding the subpath
    ("via-parent-empty-dir-md5", lambda p: write_pointer(p, "data/final", is_dir=True, md5=EMPTY_DIR_MD5[:-4]),
     "data/final/x.csv", True, "missing_from_manifest"),
    ("via-parent-file-out", lambda p: write_pointer(p, "data/final"),
     "data/final/x.csv", True, "missing_from_manifest"),
]


@pytest.mark.parametrize("row", _DELIVERY_ROWS, ids=[r[0] for r in _DELIVERY_ROWS])
def test_delivery_case_table(tmp_path: Path, row) -> None:
    """One row per shape the fleet showed, times both remote layouts where
    the layout changes the answer.

    `version_aware` is load-bearing only for the pin arms: on an md5-layout
    remote plain `dvc pull` serves any pushed out, so a missing version ID is
    not a defect there (`*-md5` rows) — which is also why a local-directory
    remote never trips the gate. UNTRACKED, NOT-PULLABLE and
    MISSING-FROM-MANIFEST fire on either layout. A subpath under a `.dir` out
    inherits that out's coverage (`via-parent-*`): an unpinned parent cannot
    land anything beneath it, a pinned one is the offline ceiling. An out that
    pins ZERO files is a deliverable empty directory, not a defect.

    Mutations, one per arm: drop `version_aware and` -> every `*-md5` pin row
    reddens; drop the `use_cache` / `is_import` arms of `_never_pushed` -> the
    `not-pullable-*` rows; drop the `out_pins_no_files` arm -> `empty-dir-*`;
    drop the `if not inside` scan -> `missing-from-manifest-*`; return
    `via_parent` for every strict-ancestor match before the tally ->
    `via-parent-unpinned-dir-va` and `via-parent-cache-false-lock-dir`; drop
    the `_never_pushed` screen before the manifest scan ->
    `via-parent-*-files-*`; make `_never_pushed` return None -> the
    `not-pullable-*` rows; count never-pushed siblings as files -> the
    `mixed-*` rows; set `exact = matched` -> `case-*`.
    """
    _, setup, declared, version_aware, expected = row
    setup(tmp_path)

    verdict = delivery(tmp_path, declared, REMOTE, version_aware=version_aware)

    assert verdict.state == expected, verdict
    assert verdict.deliverable is (expected in ("pinned", "via_parent"))


def test_delivery_names_what_a_repair_needs(tmp_path: Path) -> None:
    """`tracked` (untracked) and `unpinned` (partial) are what a hint prints;
    a verdict that only carried the state would leave the producer guessing
    which file.

    Mutation: return `()` for `tracked` or stop collecting `unpinned` -> reddens.
    """
    write_pointer(tmp_path, "data/raw.csv")
    untracked = delivery(tmp_path, "data/final/", REMOTE, version_aware=True)
    assert untracked.state == "untracked"
    assert untracked.tracked == ("data/raw.csv",)

    _two_pointers(tmp_path, b_version_id=None)
    partial = delivery(tmp_path, "data/final/", REMOTE, version_aware=True)
    assert partial.state == "partial"
    assert partial.unpinned == ("data/final/b.csv",)
    assert "data/final/b.csv" in partial.detail

    write_pipeline(tmp_path, "data/final/c.csv", cache=False)
    alone = delivery(tmp_path, "data/final/c.csv", REMOTE, version_aware=True)
    assert alone.state == "not_pullable"
    assert alone.unpullable == ("data/final/c.csv",)


def test_outs_matching_uses_a_passed_out_list(tmp_path: Path) -> None:
    """A caller resolving many declared paths enumerates the workspace once
    and passes the list in; `outs=` must be what is searched, not a hint.

    Mutation: ignore the kwarg and call `_all_dvc_outs` anyway -> reddens.
    """
    write_pointer(tmp_path, "data/final/a.csv")

    assert outs_matching(tmp_path, "data/final/", REMOTE, outs=[]) == []
    assert [o.target for o in outs_matching(tmp_path, "data/final/", REMOTE)] == ["data/final/a.csv"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need a privilege on Windows")
@pytest.mark.parametrize("tracked_by", ["pointer", "lock"])
def test_delivery_keeps_a_symlinked_checkout_on_its_workspace_path(tmp_path: Path, tracked_by: str) -> None:
    """When reflinks and hardlinks are unavailable (a cache on another
    filesystem), dvc checks a file out as a symlink into `.dvc/cache/`.
    Resolving that link made the out look untracked -- or vanish, with an
    external cache -- so a delivered product failed `check`, `publish` and
    `register`. Workspace paths are lexical now.

    Mutation: resolve the path in `_workspace_rel` (pointer) or in
    `parse_dvc_lock_outs` (lock) -> the matching row reddens.
    """
    if tracked_by == "pointer":
        write_pointer(tmp_path, "data/final/x.parquet")
    else:
        write_pipeline(tmp_path, "data/final/x.parquet", version_id="v-1")
    blob = tmp_path / ".dvc" / "cache" / "files" / "md5" / "aa" / ("a" * 30)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x")
    (tmp_path / "data" / "final").mkdir(parents=True, exist_ok=True)
    os.symlink(blob, tmp_path / "data" / "final" / "x.parquet")

    verdict = delivery(tmp_path, "data/final/x.parquet", REMOTE, version_aware=True)

    assert verdict.state == "pinned", verdict


# ---------------------------------------------------------------------------
# push_targets: what `data push` hands dvc
# ---------------------------------------------------------------------------


_FAN_YAML = (
    "stages:\n"
    "  build:\n    cmd: run\n    outs:\n    - data/out.csv\n"
    "  fan:\n    foreach: [a]\n    do:\n      cmd: run\n      outs:\n      - data/fan/${item}.csv\n"
)
_FAN_LOCK = (
    "schema: '2.0'\nstages:\n"
    "  build:\n    cmd: run\n    outs:\n    - path: data/out.csv\n      md5: " + "b" * 32 + "\n      size: 12\n"
    "  fan@a:\n    cmd: run\n    outs:\n    - path: data/fan/a.csv\n      md5: " + "b" * 32 + "\n      size: 12\n"
)


@pytest.fixture
def tracked(tmp_path: Path) -> Path:
    """A folder pointer, two per-file pointers, a stage out and a foreach out."""
    write_pointer(tmp_path, "data/final", is_dir=True)
    write_pointer(tmp_path, "data/per/a.csv")
    write_pointer(tmp_path, "data/per/b.csv")
    (tmp_path / "dvc.yaml").write_text(_FAN_YAML)
    (tmp_path / "dvc.lock").write_text(_FAN_LOCK)
    (tmp_path / "code").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "cwd, target, groups",
    [
        (".", "data/final/x.csv", [["data/final.dvc"]]),
        (".", "data/out.csv", [["dvc.yaml:build"]]),
        (".", "data/fan/a.csv", [["dvc.yaml:fan@a"]]),
        (".", "data/per", [["data/per/a.csv.dvc", "data/per/b.csv.dvc"]]),
        (".", "data", [
            ["data/final.dvc", "data/per/a.csv.dvc", "data/per/b.csv.dvc"],
            ["dvc.yaml:build", "dvc.yaml:fan@a"],
        ]),
        (".", "untracked.csv", [["untracked.csv"]]),
        (".", "data/typed.dvc", [["data/typed.dvc"]]),
        ("code", "../data/final/x.csv", [["data/final.dvc"]]),
        ("code", "nothing.csv", [["code/nothing.csv"]]),
    ],
    ids=[
        "file-in-a-folder-out", "lock-out", "foreach-out", "folder-of-pointers",
        "folder-of-everything", "untracked-passes-through", "typed-dvc-kept",
        "from-a-subdirectory", "unmatched-from-a-subdirectory",
    ],
)
def test_push_targets_push_whole_outputs_in_unmixed_groups(
    tracked: Path, cwd: str, target: str, groups: list[list[str]]
) -> None:
    """Every tracked target becomes the `.dvc` file or `dvc.yaml:<stage>` key
    of each out it reaches, spelled from the root; the rest pass through; an
    argv never mixes kinds. Mutation: push the target as typed -> the first
    five rows go red."""
    got, selection = push_targets(tracked, [target], REMOTE, cwd=tracked / cwd)
    assert sorted(map(sorted, got)) == sorted(map(sorted, groups))
    assert selection == frozenset(t for g in groups for t in g if t.endswith(".dvc") or t.startswith("dvc.yaml:"))


@pytest.mark.parametrize(
    "target, groups",
    [
        ("build", [["code/dvc.yaml:build"]]),
        ("dvc.yaml:build", [["code/dvc.yaml:build"]]),
        ("out.csv", [["code/out.csv"]]),
    ],
    ids=["bare-stage-name", "file-and-stage", "output-missing-from-disk"],
)
def test_push_targets_from_a_subdirectory_pipeline(tmp_path: Path, target: str, groups: list[list[str]]) -> None:
    """dvc runs from the root, so a stage name typed beside `code/dvc.yaml`
    needs the file named; anything else re-spelled from the root already
    resolves there. Mutation: pass the bare name through -> `code/build`,
    red; rewrite every unmatched name -> the other two rows red."""
    (tmp_path / "code").mkdir()
    (tmp_path / "code" / "dvc.yaml").write_text("stages:\n  build:\n    cmd: run\n    outs:\n    - out.csv\n")
    got, _ = push_targets(tmp_path, [target], REMOTE, cwd=tmp_path / "code")
    assert got == groups


def test_push_targets_passes_a_target_on_another_drive_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows' relpath raises for a path on another drive; that target is
    outside the project, so dvc gets it as typed rather than a traceback.
    Mutation: drop the ValueError arm -> raises, red."""
    def other_drive(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", other_drive)
    groups, selection = push_targets(tmp_path, ["x.csv"], REMOTE, cwd=tmp_path)
    assert groups == [[os.path.normpath(tmp_path / "x.csv")]] and selection == frozenset()


def test_lock_outs_carry_their_stage(tracked: Path) -> None:
    from mintd._fast_sync_ops import parse_dvc_lock_outs
    assert {o.target: o.stage for o in parse_dvc_lock_outs(tracked, REMOTE)} == {
        "data/out.csv": "build", "data/fan/a.csv": "fan@a",
    }


# ---------------------------------------------------------------------------
# judge_pointers: may `data push` commit these pointer files?
# ---------------------------------------------------------------------------

_XY = [DvcFileEntry("a" * 32, "x.csv"), DvcFileEntry("a" * 32, "y.csv")]


def _folder_project(tmp_path: Path, *, version_aware: bool = True, workspace: bool = True) -> tuple[Path, str]:
    """`.dvc/config`, the x/y tree saved in the cache, the folder on disk, and
    the HEAD text of `data/final.dvc` (a `.dir` pointer to that tree)."""
    write_dvc_config(tmp_path, version_aware=version_aware)
    tree = ensure_dir_manifest(tmp_path / ".dvc" / "cache", _XY)
    if workspace:
        (tmp_path / "data" / "final").mkdir(parents=True)
        for e in _XY:
            (tmp_path / "data" / "final" / e.relpath).write_text("x\n")
    head = write_pointer(tmp_path, "data/final", is_dir=True, md5=tree.removesuffix(".dir"), version_id=None).read_text()
    return tmp_path, head


def _judge(root: Path, pointers: list[str], head: dict, *, version_aware: bool = True, selection=None):
    return judge_pointers(
        root, pointers, head=head, remote_name=REMOTE, version_aware=version_aware, selection=selection,
    )


def test_judge_passes_a_folder_pinned_whole(tmp_path: Path) -> None:
    root, head = _folder_project(tmp_path)
    write_pointer(root, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})
    assert _judge(root, ["data/final.dvc"], {"data/final.dvc": head}) == PointerJudgement()


def test_judge_blocks_a_pointer_without_version_ids(tmp_path: Path) -> None:
    """Mutation: drop the delivery check -> passes, red."""
    root, head = _folder_project(tmp_path)
    write_pointer(root, "data/final", files={"x.csv": "v-1", "y.csv": None})
    j = _judge(root, ["data/final.dvc"], {"data/final.dvc": head})
    assert len(j.blocked) == 1 and j.blocked[0].startswith("data/final.dvc: data/final (")


def test_judge_blocks_a_cut_down_folder_listing(tmp_path: Path) -> None:
    """The list a one-file push leaves names a tree the cache never held.
    Mutation: drop the tree check -> passes, red."""
    root, head = _folder_project(tmp_path)
    write_pointer(root, "data/final", files={"x.csv": "v-1"})
    (j,) = _judge(root, ["data/final.dvc"], {"data/final.dvc": head}).blocked
    assert "lists 1 file(s), a tree DVC never saved" in j


@pytest.mark.parametrize(
    "committed, where", [(True, "the last commit's pointer"), (False, "the folder on disk")],
    ids=["against-head", "against-the-folder"],
)
def test_judge_blocks_an_emptied_folder(tmp_path: Path, committed: bool, where: str) -> None:
    """Against HEAD with the folder gone from disk too (a `dvc checkout` of
    the emptied pointer), so each check is the only one that can block.
    Mutation: drop either half of the empty-listing check -> its row passes, red."""
    root, head = _folder_project(tmp_path, workspace=not committed)
    write_pointer(root, "data/final", files={})
    (j,) = _judge(root, ["data/final.dvc"], {"data/final.dvc": head if committed else None}).blocked
    assert f"lists no files, but {where}" in j


def test_judge_passes_a_folder_empty_at_head_and_on_disk(tmp_path: Path) -> None:
    root, _ = _folder_project(tmp_path, workspace=False)
    write_pointer(root, "data/final", files={})
    assert _judge(root, ["data/final.dvc"], {"data/final.dvc": None}) == PointerJudgement()


def test_judge_names_the_lock_stage(tmp_path: Path) -> None:
    write_dvc_config(tmp_path)
    write_pipeline(tmp_path, "data/out.csv")
    (j,) = _judge(tmp_path, ["dvc.lock"], {"dvc.lock": None}).blocked
    assert j.startswith("dvc.lock stage 'build': data/out.csv (")


def test_judge_skips_an_unchanged_out_of_a_changed_stage(tmp_path: Path) -> None:
    """A commit that does not change an out does not change what importers
    get from it. Mutation: judge every out of a changed stage -> blocked."""
    write_dvc_config(tmp_path)
    (tmp_path / "dvc.yaml").write_text("stages:\n  s:\n    cmd: run\n    outs:\n    - data/a.csv\n    - data/b.csv\n")
    lock = (
        "schema: '2.0'\nstages:\n  s:\n    cmd: {cmd}\n    outs:\n"
        "    - path: data/a.csv\n      md5: " + "b" * 32 + "\n      size: 12\n{a_cloud}"
        "    - path: data/b.csv\n      md5: " + "b" * 32 + "\n      size: 12\n"
    )
    head = lock.format(cmd="run", a_cloud="")
    (tmp_path / "dvc.lock").write_text(
        lock.format(cmd="run2", a_cloud=f"      cloud:\n        {REMOTE}:\n          version_id: v-1\n")
    )
    assert _judge(tmp_path, ["dvc.lock"], {"dvc.lock": head}) == PointerJudgement()


def test_judge_blocks_a_dvc_out_with_no_hash(tmp_path: Path) -> None:
    write_dvc_config(tmp_path)
    (tmp_path / "x.csv.dvc").write_text("outs:\n- path: x.csv\n")
    (j,) = _judge(tmp_path, ["x.csv.dvc"], {"x.csv.dvc": None}).blocked
    assert "no hash" in j and "dvc commit x.csv.dvc" in j


@pytest.mark.parametrize(
    "build, pointer, reason",
    [
        (lambda r: None, "gone.dvc", "gone.dvc was deleted"),
        (lambda r: write_pointer(r, "data/imp", imported=True), "data/imp.dvc", "is a dvc import"),
        (lambda r: (r / "url.csv.dvc").write_text(
            f"deps:\n- path: s3://elsewhere/url.csv\nouts:\n- path: url.csv\n  md5: {'a' * 32}\n"),
         "url.csv.dvc", "is a dvc import"),
        (lambda r: ((r / "sub").mkdir(), (r / "sub" / "out.dvc").write_text(
            f"outs:\n- path: ../../outside.csv\n  md5: {'a' * 32}\n")),
         "sub/out.dvc", "records an output outside the project"),
        (lambda r: (r / "bad.dvc").write_text("outs: [\n"), "bad.dvc", "bad.dvc does not parse"),
        (lambda r: ((r / "sub").mkdir(), (r / "sub" / "dvc.lock").write_text("stages: {}\n")),
         "sub/dvc.lock", "not the project's root dvc.lock"),
        (lambda r: ((r / "dvc.yaml").write_text("stages:\n  other:\n    cmd: run\n"),
                    (r / "dvc.lock").write_text("schema: '2.0'\nstages:\n  gone:\n    cmd: run\n    outs:\n"
                                                "    - path: data/g.csv\n      md5: " + "b" * 32 + "\n")),
         "dvc.lock", "dvc.lock stage 'gone' has outs mintd cannot resolve"),
    ],
    ids=["deleted", "dvc-import", "import-url", "escaped-out", "unparseable", "nested-lock", "orphan-stage"],
)
def test_judge_leaves_what_it_cannot_judge(tmp_path: Path, build, pointer: str, reason: str) -> None:
    write_dvc_config(tmp_path)
    build(tmp_path)
    j = _judge(tmp_path, [pointer], {pointer: None})
    assert not j.blocked and any(reason in r for r in j.not_committed), j


def test_judge_skips_a_lock_stage_the_commit_does_not_change(tmp_path: Path) -> None:
    """On an md5 remote, a targeted push of stage a commits dvc.lock with stage
    b unchanged in it. Mutation: judge unchanged stages too -> b is left as
    'not uploaded by this push', red."""
    write_dvc_config(tmp_path, version_aware=False)
    (tmp_path / "dvc.yaml").write_text(
        "stages:\n  a:\n    cmd: run\n    outs:\n    - data/a.csv\n  b:\n    cmd: run\n    outs:\n    - data/b.csv\n"
    )
    lock = (
        "schema: '2.0'\nstages:\n  a:\n    cmd: run\n    outs:\n    - path: data/a.csv\n      md5: {a}\n      size: 12\n"
        "  b:\n    cmd: run\n    outs:\n    - path: data/b.csv\n      md5: " + "b" * 32 + "\n      size: 12\n"
    )
    (tmp_path / "dvc.lock").write_text(lock.format(a="c" * 32))  # `dvc repro a`
    j = _judge(tmp_path, ["dvc.lock"], {"dvc.lock": lock.format(a="b" * 32)}, version_aware=False,
               selection=frozenset({"dvc.yaml:a"}))
    assert j == PointerJudgement()


def test_judge_on_an_md5_remote_leaves_a_pointer_this_push_did_not_upload(tmp_path: Path) -> None:
    """Mutation: drop the selection check -> passes, red."""
    root, head = _folder_project(tmp_path, version_aware=False)
    write_pointer(root, "data/other", md5="c" * 32, version_id=None)
    j = _judge(root, ["data/other.dvc"], {"data/other.dvc": None}, version_aware=False,
               selection=frozenset({"data/final.dvc"}))
    assert j.not_committed and "data/other.dvc reached S3" in j.not_committed[0]


def test_judge_without_a_cache_leaves_the_folder(tmp_path: Path) -> None:
    root, head = _folder_project(tmp_path)
    write_pointer(root, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})
    shutil.rmtree(root / ".dvc" / "cache")
    j = _judge(root, ["data/final.dvc"], {"data/final.dvc": head})
    assert not j.blocked and "no local DVC cache" in j.not_committed[0]


@pytest.mark.parametrize("where", ["config-local", "dvc2-layout"])
def test_judge_finds_the_tree_where_dvc_keeps_it(tmp_path: Path, where: str) -> None:
    root, head = _folder_project(tmp_path)
    write_pointer(root, "data/final", files={"x.csv": "v-1", "y.csv": "v-2"})
    tree = dir_manifest_md5(_XY)
    shutil.rmtree(root / ".dvc" / "cache")
    if where == "config-local":
        (root / ".dvc" / "config.local").write_text("[cache]\n    dir = ../elsewhere\n")
        ensure_dir_manifest(root / "elsewhere", _XY)
    else:
        (root / ".dvc" / "cache" / tree[:2]).mkdir(parents=True)
        (root / ".dvc" / "cache" / tree[:2] / tree[2:]).write_text("[]")
    assert _judge(root, ["data/final.dvc"], {"data/final.dvc": head}) == PointerJudgement()


@pytest.mark.parametrize("shape", ["lock-cache-false", "lock-cache-false-folder", "dvc-push-false"])
def test_judge_exempts_outs_dvc_never_pushes(tmp_path: Path, shape: str) -> None:
    """A folder dvc never caches has no tree in the cache to check.
    Mutation: drop the exemption -> the folder row is left or blocked, red."""
    write_dvc_config(tmp_path)
    if shape == "lock-cache-false":
        write_pipeline(tmp_path, "data/metrics.json", cache=False)
        pointer = "dvc.lock"
    elif shape == "lock-cache-false-folder":
        (tmp_path / "dvc.yaml").write_text("stages:\n  r:\n    cmd: run\n    outs:\n    - data/reports:\n        cache: false\n")
        (tmp_path / "dvc.lock").write_text(
            "schema: '2.0'\nstages:\n  r:\n    cmd: run\n    outs:\n    - path: data/reports\n"
            "      md5: " + "b" * 32 + ".dir\n      size: 12\n      nfiles: 2\n"
        )
        pointer = "dvc.lock"
    else:
        (tmp_path / "x.csv.dvc").write_text(f"outs:\n- path: x.csv\n  md5: {'a' * 32}\n  push: false\n")
        pointer = "x.csv.dvc"
    assert _judge(tmp_path, [pointer], {pointer: None}) == PointerJudgement()
