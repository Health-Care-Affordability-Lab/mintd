"""The contract `RecordingReporter` is licensed by — one body, two implementations.

Substrate rule 2: a fake earns the right to stand in for a boundary only if some
test runs *unchanged* over both it and the real thing. `tests/_fakes/dvc_ops.py`
had such a test (`test_dvc_ops_contract.py`); `tests/_fakes/reporter.py` did not,
and its own docstring says why that mattered — it calls itself "a deterministic,
**rich-free** Reporter". Rich-free is exactly the property that makes it unable to
witness a rich defect.

**What this file cost to learn.** `enclave pull` labels each subscription
`Fetching <repo> [<source_path>]...`. Rich reads `[data/final/x]` as a style tag
and DELETES it, so the label never reached the user — the feature was silently
non-functional for its primary input — and `[/mnt/x]` (an absolute
`--source-path`, which nothing validates) parses as a *closing* tag and raises
`MarkupError`, which is not a `ValueError` and so escapes every CLI handler as a
traceback. The test guarding that label injected `RecordingReporter`, which
records the string it was handed and never renders it, so it was green
throughout. Mutation testing did not help either: reverting the label reddened
that test, so the mutation table reported the guard as pinned while the feature
was broken in production.

**THE BOUNDARY.** The property below is the only one this file licenses:
*the text a caller passes is the text the user sees, verbatim*. That is what
`RecordingReporter`'s `events` tuples are read as everywhere in the suite. It is
deliberately NOT a claim about styling, spinner frames, wrapping, or ordering
against subprocess output — no fake witnesses those, and a test that needs them
needs the real Reporter directly.

Each case runs twice, `["fake", "real"]`. The two arms observe different
surfaces — the fake exposes `events`, the real one renders to stderr and into a
rich `Status` — so each probe normalizes to "what the user would see".
"""

from __future__ import annotations

import io
import sys

import pytest

from mintd._console import Reporter
from tests._fakes.reporter import RecordingReporter

# Tokens that rich would eat or choke on. `[s3]` and `[error]` are real strings
# this codebase already passes to a Reporter; `[data/final/x]` is a subscription
# label; `[/mnt/x]` is the crash.
MARKUPISH = ["[s3]", "[error]", "[data/final/x]", "[/mnt/x]", "[bold]"]


class _FakeProbe:
    """RecordingReporter: the user-visible text IS the recorded argument."""

    def __init__(self) -> None:
        self.reporter: Reporter = RecordingReporter()

    def shown(self, kind: str) -> list[str]:
        # Join every string part of the tuple: `error` records (kind, msg, hint)
        # and a hint is user-visible text too.
        return [
            " ".join(str(part) for part in event[1:] if part)
            for event in self.reporter.events
            if event and event[0] == kind
        ]

    def shown_status(self) -> str:
        return self.shown("update_status")[-1]


class _RealProbe:
    """Real Reporter: the user-visible text is whatever rich renders.

    Owns its own stderr rather than using capsys: `Console(file=sys.stderr)`
    binds the stream object at construction, and capsys closes its replacement
    between phases, so a Console built in a fixture writes to a closed file.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", self.buf)
        self.reporter = Reporter(no_color=True)

    def shown(self, kind: str) -> list[str]:
        del kind  # every print path lands on the same stream
        return [line for line in self.buf.getvalue().splitlines() if line]

    def shown_status(self) -> str:
        # What the spinner would draw. Reaching into rich is the point: this is
        # the layer the fake cannot see, and the layer the defect lived in.
        return self.reporter._active_status.renderable.text.plain


@pytest.fixture(params=["fake", "real"])
def probe(request, monkeypatch):
    return _FakeProbe() if request.param == "fake" else _RealProbe(monkeypatch)


@pytest.mark.parametrize("token", MARKUPISH)
def test_error_text_reaches_the_user_verbatim(probe, token):
    probe.reporter.error(f"cannot resolve {token} for provider-xw")
    assert any(token in line for line in probe.shown("error"))


@pytest.mark.parametrize("token", MARKUPISH)
def test_info_text_reaches_the_user_verbatim(probe, token):
    probe.reporter.info(f"uploaded {token}")
    assert any(token in line for line in probe.shown("info"))


@pytest.mark.parametrize("token", MARKUPISH)
def test_warn_text_reaches_the_user_verbatim(probe, token):
    probe.reporter.warn(f"subscription {token} is stale")
    assert any(token in line for line in probe.shown("warn"))


@pytest.mark.parametrize("token", MARKUPISH)
def test_status_label_reaches_the_user_verbatim(probe, token):
    """The case that was broken in production. `status`/`update_status` do NOT
    go through Console.print — rich.Status builds a Spinner, which calls
    Text.from_markup unconditionally and ignores the console's markup setting,
    so silencing markup on the Console is not sufficient here."""
    with probe.reporter.status("start"):
        probe.reporter.update_status(f"Fetching provider-xw {token}... (1/2)")
        assert token in probe.shown_status()


def test_a_hint_reaches_the_user_verbatim(probe):
    probe.reporter.error("no such subscription", hint="pass --source-path [data/x]")
    assert any("[data/x]" in line for line in probe.shown("error"))


@pytest.mark.parametrize("token", MARKUPISH)
def test_subprocess_passthrough_keeps_the_status_base_verbatim(probe, token):
    """`passthrough_stderr` re-renders the spinner as
    `f"{status_base}  {tick}"` while a child process streams progress, so the
    subscription label goes through markup a SECOND time, on a code path the
    caller never sees. The first markup fix patched `update_status` and missed
    this one; review found it.
    """
    with probe.reporter.status("start"):
        probe.reporter.update_status(f"Fetching provider-xw {token}...")
        probe.reporter.passthrough_stderr("Updating files:  40%\rUpdating files:  80%\r")
        assert token in probe.shown_status()


@pytest.mark.parametrize("token", MARKUPISH)
def test_status_label_survives_a_progress_block(probe, token):
    """`progress()` REBUILDS the spinner on exit, so the label goes through
    Spinner construction a second time. That site was missed by the first
    markup fix and invisible to the first version of the ratchet, which
    scanned `.update` calls only."""
    with probe.reporter.status("start"):
        probe.reporter.update_status(f"Fetching provider-xw {token}...")
        with probe.reporter.progress(100, desc="Pulling something") as advance:
            advance(10)
        assert token in probe.shown_status()
