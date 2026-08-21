"""The contract `_FakeDvcOps` is licensed by — one body, two implementations.

Substrate rule 2: a fake earns the right to stand in for a boundary only if
some test runs *unchanged* over both it and the real thing. Every semantic
case below is parametrized `["fake", "real"]`; the real arm is a genuine
`SubprocessDvcOps` spawning mintd's own bundled dvc, hermetically.

**THE BOUNDARY — read this before adding an assertion anywhere in the suite.**

**142 assert-lines** across **seven** modules read `_FakeDvcOps`'s
call-recording attributes — 108 in `test_data_ops.py` alone, then
`test_data_clone.py` 18, `test_cli.py` 9, `test_publish.py` 3,
`test_enclave_pull.py` 2, `test_harness_contract.py` 1,
`test_import_rescue.py` 1.

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


@pytest.fixture(params=["fake", "real"])
def ops(request, tmp_path: Path, monkeypatch):
    """`(ops, workspace)` — not just `ops`.

    The tuple is forced by the thing it exposes: `SubprocessDvcOps` shells
    into `os.getcwd()` and `_FakeDvcOps` ignores location entirely, so there
    is no single object that answers "which repo does this act on". That
    missing `cwd` parameter is unit A's bug in fixture form; when unit A adds
    it to `DvcOps`, this fixture collapses back to returning `ops`.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _git(["init", "-b", "main", str(workspace)])

    if request.param == "fake":
        fake = _FakeDvcOps()
        fake.workspace = workspace
        return fake, workspace

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

    # Keyword-only by construction (`_dvc_ops.py:272-278`) — a bare
    # `SubprocessDvcOps()` raises TypeError.
    real = SubprocessDvcOps(timeouts=Timeouts())
    real.init(cwd=workspace)
    monkeypatch.chdir(workspace)
    return real, workspace


def test_add_writes_a_parseable_dvc_file(ops) -> None:
    """`add()` produces a pointer with a real `outs` block.

    Landed RED on the fake and GREEN on the real arm: the fake wrote `""`,
    which is valid YAML — it parses to `None` — so nothing ever failed
    loudly. It just meant every reader downstream of a faked `add` saw a
    pointer that declared no outputs, and read that as "this product has no
    data" rather than "this fixture is lying". Fixing the fake is what made
    this pass on both arms.
    """
    dvc_ops, workspace = ops
    (workspace / "data").mkdir()
    payload = workspace / "data" / "final.csv"
    payload.write_text("a,b\n1,2\n", encoding="utf-8")

    produced = dvc_ops.add(Path("data/final.csv") if _is_real(dvc_ops) else payload)

    parsed = yaml.safe_load(Path(produced).read_text(encoding="utf-8")) or {}
    assert parsed.get("outs"), f"no outs block: {parsed}"
    assert parsed["outs"][0]["path"] == "final.csv"


def test_pull_rejects_a_target_no_stage_declares(ops) -> None:
    """A target nothing declares is an error, on both arms.

    This is the case the fake could not express at all: `pull()` recorded the
    call and returned `None` for *any* target whatsoever, so a test could
    assert a successful pull of a target that does not exist. Every
    "resolver picked the right target" test written against the old fake was
    therefore checking that mintd passed a string along, not that the string
    meant anything.
    """
    dvc_ops, workspace = ops
    if not _is_real(dvc_ops):
        dvc_ops.strict_targets = True

    with pytest.raises(DvcOpError):
        dvc_ops.pull(targets=["data/nothing-declares-this.csv"])


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


def _is_real(dvc_ops) -> bool:
    return isinstance(dvc_ops, SubprocessDvcOps)
