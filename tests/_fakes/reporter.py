"""RecordingReporter — a deterministic, rich-free Reporter for tests.

Records every feedback call as a tuple in ``self.events`` instead of
rendering to a terminal. Spinners (`status`) become nullcontexts and
the determinate `progress` bar yields a no-op advance — so tests can
assert *which* feedback fired without depending on rich's transient
terminal frames (which capsys can't reliably catch).

Inject via ``monkeypatch.setattr("mintd.cli._build_reporter", lambda args: rep)``.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

from mintd._console import Reporter


class RecordingReporter(Reporter):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []

    def status(self, msg: str) -> Any:
        self.events.append(("status", msg))
        return contextlib.nullcontext()

    def update_status(self, msg: str) -> None:
        self.events.append(("update_status", msg))

    @contextlib.contextmanager
    def progress(self, total: int, *, desc: str) -> Iterator[Any]:
        self.events.append(("progress", desc, total))
        yield (lambda _n: None)

    def update_progress_desc(self, msg: str) -> None:
        self.events.append(("update_progress_desc", msg))

    def error(self, msg: str, *, hint: str | None = None) -> None:
        self.events.append(("error", msg, hint))

    def info(self, msg: str) -> None:
        self.events.append(("info", msg))

    def success(self, msg: str, *, elapsed_s: float | None = None) -> None:
        self.events.append(("success", msg))

    def result(self, payload: Any, *, pretty: Any = None) -> None:
        self.events.append(("result", payload))

    def events_of(self, kind: str) -> list[tuple]:
        """Return every recorded event of ``kind`` (status, update_status,
        error, ...) as the full tuples, in order."""
        return [e for e in self.events if e and e[0] == kind]
