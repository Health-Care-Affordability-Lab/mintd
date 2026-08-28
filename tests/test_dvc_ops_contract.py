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
