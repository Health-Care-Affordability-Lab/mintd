"""The contract `_FakeDvcOps` is licensed by — one body, two implementations.

Substrate rule 2: a fake earns the right to stand in for a boundary only if
some test runs *unchanged* over both it and the real thing. Every semantic
case below is parametrized `["fake", "real"]`; the real arm is a genuine
`SubprocessDvcOps` spawning mintd's own bundled dvc, hermetically.

**THE BOUNDARY — read this before adding an assertion anywhere in the suite.**

**148 assert-lines** across **eight** modules read `_FakeDvcOps`'s
call-recording attributes — 108 in `test_data_ops.py` alone, then
`test_data_clone.py` 19, `test_cli.py` 9, `test_publish.py` 5,
`test_enclave_pull.py` 4, `test_dvc_ops_contract.py` 1,
`test_harness_contract.py` 1, `test_import_rescue.py` 1.

*Re-measured at unit A, which added `cwd` to every recorded call and six
assertions that read it: 143 across seven modules before, 148 across eight
after.*

The number is only meaningful with the query that produced it, so here it is
rather than a bare figure — re-run it instead of trusting this paragraph:

    grep -rnE '\\b(init_calls|push_calls|pull_calls|add_calls|status_calls\\
    |remove_calls|checkout_calls)\\b' tests/ | grep -c assert

Word boundaries matter: without them the count picks up `remote_add_calls` and
`git_add_calls`, which belong to `_FakeInitOps` and have nothing to do with
this contract.

*This paragraph previously claimed 151 matching lines, six modules, and 113 in
`test_data_ops.py`, and "corrected" the plan's 141 as stale. Three of those
four numbers were wrong (168 / seven / 108), the correction was itself wrong,
and it disagreed with `_fakes/dvc_ops.py`'s own figure in the same commit. Kept
as a note because a docstring that asserts measurements is worth exactly as
much as the last time someone re-ran them.*

This contract licenses the **semantics** of `add` / `pull` / `push`: what ends
up on disk, what is refused. It does not and cannot license a recorded argv.

So those assert-lines are legal as *"the handler called the seam with X"* and
never as *"X had effect Y"*. The first is a statement about mintd's code, which
the fake is a fair witness to. The second is a statement about dvc, which only
the real arm can make. An unwritten boundary is precisely how a licensed fake
starts getting used unlicensed — the licence is narrow, and it is written down
here so nobody has to guess how narrow.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mintd._config import Timeouts
from mintd._dvc_ops import DvcOpError, SubprocessDvcOps
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._harness.git import _git


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """The repo under test. A plain fixture, because "which repo" is now a
    parameter of every call rather than something an `ops` object carries."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-b", "main", str(ws)])
    return ws


@pytest.fixture(params=["fake", "real"])
def ops(request, workspace: Path, tmp_path: Path, monkeypatch):
    """One object, and it is the `DvcOps` under test.

    This fixture used to return `(ops, workspace)`, and said so: the tuple
    was forced by `SubprocessDvcOps` shelling into `os.getcwd()` while
    `_FakeDvcOps` ignored location entirely, so no single object could answer
    "which repo does this act on". Unit A made `cwd` a required parameter of
    every verb, so the question is answered at the call and the tuple is
    gone — as is the `_is_real()` branching the two bodies below needed to
    paper over the same gap.
    """
    if request.param == "fake":
        fake = _FakeDvcOps()
        fake.workspace = workspace  # switch: let fake checkout materialize
        fake.strict_targets = True  # about strictness, not location
        return fake

    # Hermetic real dvc. The config knobs are dvc's own (`dvc/dirs.py`);
    # redirecting `HOME` alone fails OPEN on Windows and under XDG, and a
    # fixture that silently reads the developer's real config is worse than
    # no fixture. Same reasoning as `tests/_harness/dvc.py`.
    home = tmp_path / "dvc-home"
    home.mkdir()
    for key, value in {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DVC_GLOBAL_CONFIG_DIR": str(home / "global"),
        "DVC_SYSTEM_CONFIG_DIR": str(home / "system"),
        "DVC_SITE_CACHE_DIR": str(tmp_path / "dvc-site"),
    }.items():
        monkeypatch.setenv(key, value)

    # Keyword-only by construction — a bare `SubprocessDvcOps()` raises
    # TypeError. No `monkeypatch.chdir` here: `cwd=` is what aims it now.
    real = SubprocessDvcOps(timeouts=Timeouts())
    real.init(cwd=workspace)
    return real


def test_add_writes_a_parseable_dvc_file(ops, workspace: Path) -> None:
    """`add()` produces a pointer with a real `outs` block.

    Landed RED on the fake and GREEN on the real arm: the fake wrote `""`,
    which is valid YAML — it parses to `None` — so nothing ever failed
    loudly. It just meant every reader downstream of a faked `add` saw a
    pointer that declared no outputs, and read that as "this product has no
    data" rather than "this fixture is lying". Fixing the fake is what made
    this pass on both arms.
    """
    (workspace / "data").mkdir()
    payload = workspace / "data" / "final.csv"
    payload.write_text("a,b\n1,2\n", encoding="utf-8")

    produced = ops.add(payload, cwd=workspace)

    parsed = yaml.safe_load(Path(produced).read_text(encoding="utf-8")) or {}
    assert parsed.get("outs"), f"no outs block: {parsed}"
    assert parsed["outs"][0]["path"] == "final.csv"


def test_add_on_a_directory_writes_a_dir_pointer(ops, workspace: Path) -> None:
    """`add()` on a DIRECTORY writes an md5 with the `.dir` suffix plus
    `nfiles`; on a file it writes neither.

    The suffix is the only dir marker in a `.dvc` file — there is no
    `is_dir` YAML field — and production dispatches on exactly it
    (`_fast_sync_ops` sets `is_dir=md5.endswith(".dir")`). A fake that
    writes a plain md5 for a directory add is not laxer, it is
    WRONG-SHAPED: every downstream reader sees a file where the caller
    added a directory. `nfiles` counts FILES, recursively — the nested
    layout below (3 files, but 2 top-level entries) pins that it is not
    an `iterdir()` count.
    """
    bundle = workspace / "data" / "bundle"
    (bundle / "sub").mkdir(parents=True)
    (bundle / "a.csv").write_text("a\n", encoding="utf-8")
    (bundle / "sub" / "b.csv").write_text("b\n", encoding="utf-8")
    (bundle / "sub" / "c.csv").write_text("c\n", encoding="utf-8")

    produced = ops.add(bundle, cwd=workspace)

    out = yaml.safe_load(Path(produced).read_text(encoding="utf-8"))["outs"][0]
    assert out["md5"].endswith(".dir"), f"directory add wrote a file-shaped md5: {out}"
    assert out["nfiles"] == 3, f"nfiles must count files recursively: {out}"
    assert out["path"] == "bundle"

    # And the file arm of the dispatch: a plain file gets neither marker.
    payload = workspace / "data" / "plain.csv"
    payload.write_text("x\n", encoding="utf-8")
    fout = yaml.safe_load(
        Path(ops.add(payload, cwd=workspace)).read_text(encoding="utf-8")
    )["outs"][0]
    assert not fout["md5"].endswith(".dir"), f"file add wrote a dir-shaped md5: {fout}"
    assert "nfiles" not in fout


def test_pull_rejects_a_target_no_stage_declares(ops, workspace: Path) -> None:
    """A target nothing declares is an error, on both arms.

    This is the case the fake could not express at all: `pull()` recorded the
    call and returned `None` for *any* target whatsoever, so a test could
    assert a successful pull of a target that does not exist. Every
    "resolver picked the right target" test written against the old fake was
    therefore checking that mintd passed a string along, not that the string
    meant anything.
    """
    with pytest.raises(DvcOpError):
        ops.pull(targets=["data/nothing-declares-this.csv"], cwd=workspace)


def test_module_docstring_states_the_calls_boundary() -> None:
    """The licence is narrow; the narrowness has to be written down.

    Deliberately NOT parametrized over `["fake", "real"]`: it asserts a
    property of this module, not of an implementation, so a second arm would
    re-read the same string while paying the real fixture's `dvc init`
    (~0.70s measured). Stated rather than silently skipped — the rest of this
    file exists because unstated scope is how a fake drifts.

    Reds if someone trims the docstring, which is the point.
    """
    assert __doc__ is not None
    doc = __doc__
    # The two halves of the boundary, asserted verbatim. The COUNT is
    # deliberately not asserted: it moves whenever an unrelated module gains a
    # `*_calls` assertion, and a ratchet that reddens on someone else's test is
    # a ratchet people delete. The count is in the prose with the sha it was
    # measured at, which is what stops it rotting silently.
    assert "assert-lines" in doc
    assert "the handler called the seam with X" in doc
    assert "X had effect Y" in doc


def test_the_ops_fixture_returns_one_object(ops) -> None:
    """The fixture hands back a `DvcOps`, not a `(ops, workspace)` pair.

    This is unit A's binding acceptance criterion, kept as a test rather than
    a note. The tuple existed because `DvcOps` had no `cwd`: `SubprocessDvcOps`
    shelled into `os.getcwd()` and `_FakeDvcOps` ignored location entirely, so
    the fixture had to hand out the workspace separately for the bodies to
    branch on. Both are gone, and re-introducing either should redden a NAMED
    test rather than surface as an unpack error three files away.

    `_is_real` is asserted absent for the same reason — it is the other half
    of the same workaround.
    """
    assert not isinstance(ops, tuple), "the `ops` fixture re-grew its tuple"
    # Asked of the module NAMESPACE, not of its source text: a grep for the
    # helper's name matches this test's own docstring and assertion, so a
    # source-substring version of this check can never pass.
    import sys

    assert not hasattr(sys.modules[__name__], "_is_real"), (
        "the real/fake branching helper is back; the two arms have diverged again"
    )


def test_import_into_an_existing_directory_dest_is_refused(
    ops, workspace: Path, tmp_path: Path
) -> None:
    """issue09 fixes 3/4, licensed on both arms: `dvc import -o` treats an
    existing directory as a *container*, nests the source basename inside
    it, and refuses the overlap — `--force` does not help (it only
    overwrites the stage file). The fake raises on an existing dir dest so
    the callers' clear-the-destination guard is testable at all; this case
    is what keeps that fake behavior honest against real dvc.
    """
    from mintd._dvc_ops import DvcImportDestinationExists
    from tests._harness.producer import build_local_producer

    producer = build_local_producer(tmp_path / "prod")
    producer.publish({"data/final": {"a.csv": b"v1\n"}})
    dest = workspace / "final"

    ops.import_(
        repo_url=producer.url, path="data/final", dest=dest, cwd=workspace
    )
    # Real dvc materialized the payload; the fake records pointers only, so
    # stage the directory it would have left behind.
    dest.mkdir(exist_ok=True)

    with pytest.raises(DvcImportDestinationExists):
        ops.import_(
            repo_url=producer.url, path="data/final", dest=dest,
            cwd=workspace, force=True,
        )


def test_import_into_an_untracked_directory_dest_nests_inside(
    ops, workspace: Path, tmp_path: Path
) -> None:
    """The other half of the destination rule: a directory dvc does NOT
    track is a *container*, not a conflict. `dvc import -o <existing-dir>`
    nests the source basename inside it and exits 0 (measured:
    `Importing 'data/final (...)' -> 'final/final'`, pointer at
    `final/final.dvc`, prior contents untouched). Only the tracked shape
    above — pointer beside the directory — is refused.

    The fake used to raise `DvcImportDestinationExists` on ANY existing dir
    dest: stricter than dvc, the `GraphAwareDvcOps` failure class, and it
    licensed a caller-level test for a refusal dvc never makes.

    The dest is deliberately NOT named like the source (`landing` vs
    `final`): the nested entry takes the SOURCE basename — measured:
    `landing/final` + `landing/final.dvc` — which a same-named dest would
    leave indistinguishable from nesting by dest name.

    Also pinned: the seam's RETURN value is computed from the original
    `dest`, so after nesting it names `<dest>.dvc` — a file dvc never
    wrote. Both arms agree, which is exactly why no caller may trust the
    returned path into existence without looking.
    """
    from tests._harness.producer import build_local_producer

    producer = build_local_producer(tmp_path / "prod")
    producer.publish({"data/final": {"a.csv": b"v1\n"}})
    dest = workspace / "landing"
    dest.mkdir()
    (dest / "precious.csv").write_text("already here", encoding="utf-8")

    produced = ops.import_(
        repo_url=producer.url, path="data/final", dest=dest, cwd=workspace
    )

    # Nested: the pointer lands INSIDE the container, named for the source
    # basename — and nothing appears at the un-nested spot.
    nested = dest / "final.dvc"
    assert nested.is_file(), "import into an untracked dir must nest, not refuse"
    parsed = yaml.safe_load(nested.read_text(encoding="utf-8"))
    assert parsed["outs"][0]["path"] == "final"
    assert not (workspace / "landing.dvc").exists()
    # The computed return is the un-nested path, which dvc never wrote.
    assert produced == workspace / "landing.dvc"
    assert not produced.exists()
    # The container's prior contents survive.
    assert (dest / "precious.csv").read_text(encoding="utf-8") == "already here"


def test_pull_accepts_a_dot_segment_inside_the_target(ops, workspace: Path) -> None:
    """A `.` or `..` segment *inside* a target is dvc's business, not a typo.

    The fake normalized only a LEADING `./`, so any inner segment missed the
    lookup and the strict fake refused a target real dvc pulls — the fake
    being STRICTER than the thing it stands in for, which substrate rule 2
    forbids in that direction specifically.

    Measured, dvc 3.67.1, one `dvc add`ed out and no remote configured:

        data/final.csv           rc=0
        data/./final.csv         rc=0
        ./data/final.csv         rc=0   (the leading case, already handled)
        data/sub/../final.csv    rc=0   (`data/sub` need not exist)
        data/final.csv/..        rc=1
        data/nothing.csv         rc=1

    Both halves are asserted: dvc resolves `..` LEXICALLY, so collapsing it
    must not turn the rejected fifth row into an accept. A fix that widens
    the guard far enough to swallow `data/final.csv/..` has stopped modelling
    dvc and started ignoring the argument.
    """
    (workspace / "data").mkdir()
    payload = workspace / "data" / "final.csv"
    payload.write_text("a,b\n1,2\n", encoding="utf-8")
    ops.add(payload, cwd=workspace)

    ops.pull(targets=["data/./final.csv"], cwd=workspace)
    # An inner `..` too, collapsed lexically: `data/sub` never existed and
    # dvc still returns rc=0. Rejecting this row -- the obvious "block path
    # traversal" hardening -- is the stricter-than-dvc direction, and without
    # this call nothing in the suite notices.
    ops.pull(targets=["data/sub/../final.csv"], cwd=workspace)

    with pytest.raises(DvcOpError):
        ops.pull(targets=["data/final.csv/.."], cwd=workspace)


def test_pull_accepts_a_backslash_separated_target(ops, workspace: Path) -> None:
    """A backslash-separated target is a separator, not a filename.

    Measured, dvc 3.67.1, one `dvc add`ed out and no remote configured:

        data\\final.csv           rc=0, and `data/final.csv` materializes
        data\\sub\\..\\final.csv   rc=0, `data\\sub` need not exist
        data\\nothing.csv         rc=1, same workspace state

    So dvc splits on the backslash even on posix, where it is a legal
    filename character, and it still collapses dot segments across the
    mixed separators. The guard has to do both, in that order, and each
    row below pins one half:

    * drop `normalize_target` and keep only `posixpath.normpath`, which
      leaves a backslash alone, and the first row starts failing;
    * run them the other way round -- normalize the separators *after*
      collapsing dots, so the dots never get collapsed -- and the second
      row starts failing.
    """
    (workspace / "data").mkdir()
    payload = workspace / "data" / "final.csv"
    payload.write_text("a,b\n1,2\n", encoding="utf-8")
    ops.add(payload, cwd=workspace)

    ops.pull(targets=["data\\final.csv"], cwd=workspace)
    ops.pull(targets=["data\\sub\\..\\final.csv"], cwd=workspace)


def test_pull_accepts_a_target_that_reenters_the_workspace(ops, workspace: Path) -> None:
    """A leading `..` that lexically re-enters through the workspace's own
    directory name is still the declared out.

    Measured, dvc 3.67.1, one `dvc add`ed out, run from inside the workspace:

        ../<ws>/data/final.csv   rc=0, file materializes

    dvc anchors the target against cwd and compares lexically, so leaving and
    re-entering is a no-op to it. The fake's guard anchored only ABSOLUTE
    targets; a relative `../` one fell through to the membership checks as
    written and was refused -- stricter than dvc, on a dot-segment target,
    the exact subject and the exact forbidden direction of the test above.

    A `../` target that lands OUTSIDE the workspace is deliberately not
    asserted either way: what dvc does with those is unmeasured, and pinning
    a guess would license behaviour nobody checked.

    Mutation: drop the `elif nt.startswith("../")` anchor branch in
    `_reject_unknown_targets` -> the fake arm of this test raises.
    """
    (workspace / "data").mkdir()
    payload = workspace / "data" / "final.csv"
    payload.write_text("a,b\n1,2\n", encoding="utf-8")
    ops.add(payload, cwd=workspace)

    ops.pull(targets=[f"../{workspace.name}/data/final.csv"], cwd=workspace)


@pytest.mark.parametrize(
    "shape",
    [
        "dvc_yaml_list",
        "dvc_lock_list",
        "lock_outs_strings",
        "lock_stage_body_string",
        "yaml_stage_body_string",
        "yaml_stages_list",
        "lock_stages_list",
        "yaml_wdir_list",
        "yaml_wdir_escape",
        "lock_int_stage_key",
        "lock_out_int_path",
        "dvcfile_out_int_path",
    ],
)
def test_pull_rejects_cleanly_when_a_pipeline_file_is_the_wrong_shape(
    ops, workspace: Path, shape: str
) -> None:
    """Valid YAML that is not pipeline-shaped is a clean reject, not a crash.

    Real dvc exits 1 on each of these files (measured, 3.67.1); review r2
    found the fake escaping `pull()` with `AttributeError` instead — its
    `dvc.yaml`/`dvc.lock` scan called `.get` on unchecked `safe_load` results
    while the sibling `*.dvc` loop guarded `isinstance(body, dict)`.
    """
    stage_yaml = (
        "stages:\n"
        "  build:\n"
        "    cmd: python -c pass\n"
        "    outs:\n"
        "      - data/x.csv\n"
    )
    # A well-formed lock for `build`, outs as the mappings real dvc writes.
    good_lock = (
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: python -c pass\n"
        "    outs:\n"
        "    - path: data/x.csv\n"
        "      md5: " + "a" * 32 + "\n"
        "      size: 3\n"
    )
    files = {
        # The well-formed lock matters: without a sibling `dvc.lock` the
        # scan bails on OSError before it ever parses `dvc.yaml`, and the
        # list shape goes untested (a mutation dropping the guard survived
        # until this lock was added).
        "dvc_yaml_list": {
            "dvc.yaml": "- not\n- a\n- mapping\n",
            "dvc.lock": good_lock,
        },
        "dvc_lock_list": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": "- not\n- a\n- mapping\n",
        },
        # Lock outs as bare strings where real dvc writes mappings.
        "lock_outs_strings": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": (
                "schema: '2.0'\n"
                "stages:\n"
                "  build:\n"
                "    cmd: python -c pass\n"
                "    outs:\n"
                "    - data/x.csv\n"
            ),
        },
        # A lock stage whose body is a bare string, next to a valid dvc.yaml.
        "lock_stage_body_string": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": "schema: '2.0'\nstages:\n  build: oops\n",
        },
        # A dvc.yaml stage whose body is a bare string, next to a valid lock.
        "yaml_stage_body_string": {
            "dvc.yaml": "stages:\n  build: oops\n",
            "dvc.lock": good_lock,
        },
        # `stages:` itself as a list, in either file. Same class, one level
        # up; each shipped with a guard but NO arm, so deleting the guard
        # left every gate green -- the vacuity finding of review r2.
        "yaml_stages_list": {
            "dvc.yaml": "stages:\n- build\n",
            "dvc.lock": good_lock,
        },
        "lock_stages_list": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": "schema: '2.0'\nstages:\n- build\n",
        },
        # Non-string SCALARS where the scan expects text: each of these is a
        # clean rc=1 validation error in real dvc (measured, 3.67.1) and was
        # a TypeError/AttributeError escaping `pull()` in the fake. YAML
        # hands an unquoted `7` over as an int, so a hand-edited file is one
        # keystroke from every one of these.
        "yaml_wdir_list": {
            "dvc.yaml": "stages:\n  build:\n    wdir: [a, b]\n    cmd: python -c pass\n",
            "dvc.lock": good_lock,
        },
        # A wdir that walks OUT of the workspace: dvc refuses to collect the
        # stage (rc=1, measured) rather than resolving outs above the root.
        "yaml_wdir_escape": {
            "dvc.yaml": "stages:\n  build:\n    wdir: ../../..\n    cmd: python -c pass\n",
            "dvc.lock": good_lock,
        },
        "lock_int_stage_key": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": "schema: '2.0'\nstages:\n  7:\n    cmd: python -c pass\n",
        },
        "lock_out_int_path": {
            "dvc.yaml": stage_yaml,
            "dvc.lock": (
                "schema: '2.0'\n"
                "stages:\n"
                "  build:\n"
                "    cmd: python -c pass\n"
                "    outs:\n"
                "    - path: 7\n"
                "      md5: " + "a" * 32 + "\n"
            ),
        },
        "dvcfile_out_int_path": {
            "x.dvc": "outs:\n- path: 7\n  md5: " + "a" * 32 + "\n",
        },
    }[shape]
    for name, text in files.items():
        (workspace / name).write_text(text, encoding="utf-8")

    # An UNDECLARED target on purpose: the scan crashed before the target was
    # even considered, and an undeclared name is the one verdict both arms
    # share across all five shapes (the fake stays free to be LAXER than dvc
    # about declared outs next to a malformed file).
    with pytest.raises(DvcOpError):
        ops.pull(targets=["data/nothing.csv"], cwd=workspace)
