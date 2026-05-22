from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional
import sys
import logging
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.status import Status

class Reporter:
    def __init__(self, *, verbose: int = 0, quiet: int = 0,
                 json_mode: bool = False, no_color: bool = False) -> None:
        self.json_mode = json_mode
        self.level = 1 + verbose - quiet
        self._stderr = Console(file=sys.stderr, no_color=no_color, force_terminal=None)
        self._stdout = Console(file=sys.stdout, no_color=no_color, force_terminal=None)
        self._active_status: Optional[Status] = None
        self._active_progress: Optional[Progress] = None
        self._progress_task_id: Optional[TaskID] = None
        self._status_base: str = ""
        # Per-stream byte buffer for chunk-boundary safety: subprocess
        # output arrives in 256-byte chunks that may split a \r-terminated
        # progress tick in half. Without buffering, the spinner would
        # display the partial start of the next tick ("Updating files:"
        # instead of "Updating files: 23% (700/3090)"). We coalesce chunks
        # until we see a \r or \n boundary.
        self._stderr_buf: str = ""

    def status(self, msg: str) -> Any:  # context manager
        if self.json_mode or self.level < 1:
            from contextlib import nullcontext
            return nullcontext()
        # Clear any residual partial from the previous subprocess (rare,
        # but possible if a child exited mid-tick or reader thread was
        # slow to drain). Prevents cross-block bleed into the new spinner.
        self._stderr_buf = ""
        self._status_base = msg
        rich_status = self._stderr.status(msg)
        self._active_status = rich_status
        # Wrap rich.Status in a context manager that ALSO resets
        # ``self._active_status`` on exit. Without the reset, a later
        # ``reporter.progress(...)`` block sees a stale ``_active_status``
        # reference, treats the (already-stopped) spinner as "active",
        # and re-opens it on progress exit — leaving the old label
        # stuck on screen during subsequent subprocess phases.
        outer_self = self

        class _StatusCM:
            def __enter__(self) -> Any:
                rich_status.__enter__()
                return rich_status

            def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
                try:
                    rich_status.__exit__(exc_type, exc, tb)
                finally:
                    outer_self._active_status = None
                    outer_self._status_base = ""

        return _StatusCM()

    def update_status(self, msg: str) -> None:
        """Refresh the active status spinner's label. No-op when status is a
        nullcontext (json_mode / quiet level). Used to phase a multi-step
        operation under one outer ``with reporter.status(...)`` block."""
        if self.json_mode or self._active_status is None:
            return
        self._status_base = msg
        self._active_status.update(msg)

    def update_progress_desc(self, msg: str) -> None:
        """Update the active progress widget's description prefix. No-op when
        no progress is active (e.g. json_mode). Lets the fast-sync loop surface
        per-output progress like 'Pulling data/final/carrier.parquet (3/9)...'."""
        if self.json_mode or self._active_progress is None or self._progress_task_id is None:
            return
        self._active_progress.update(self._progress_task_id, description=msg)

    def info(self, msg: str) -> None:
        if self.level >= 1 and not self.json_mode:
            self._stderr.print(msg)

    def success(self, msg: str, *, elapsed_s: Optional[float] = None) -> None:
        if self.json_mode:
            return
        if elapsed_s is not None:
            msg += f" (elapsed: {elapsed_s:.2f}s)"
        self._stderr.print(msg)

    def warn(self, msg: str) -> None:
        if not self.json_mode:
            self._stderr.print(f"warning: {msg}")

    def error(self, msg: str, *, hint: Optional[str] = None) -> None:
        self._stderr.print(f"error: {msg}")
        if hint:
            for line in hint.splitlines():
                self._stderr.print(f"  hint: {line}")

    def result(self, payload: Any, *, pretty: Optional[Callable[[Any], str]] = None) -> None:
        if self.json_mode:
            import json
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        else:
            # Pretty mode: write to stdout directly (no rich wrapping). The
            # pretty callable has already formatted its output; rich.Console
            # would terminal-width-wrap long lines, breaking --detailed mode.
            text = pretty(payload) if pretty else str(payload)
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

    def debug(self, msg: str) -> None:
        if self.level >= 2 and not self.json_mode:
            self._stderr.print(f"debug: {msg}")

    def trace(self, msg: str) -> None:
        if self.level >= 3 and not self.json_mode:
            self._stderr.print(f"trace: {msg}")

    def passthrough_stdout(self, chunk: str) -> None:
        # Stdout from streamed subprocesses is rare and usually structured
        # (e.g. dvc status --json). Route plain through the stdout console.
        if not chunk or self.json_mode:
            return
        self._stdout.print(chunk, end="", soft_wrap=True, highlight=False, markup=False)

    def passthrough_stderr(self, chunk: str) -> None:
        """Route a raw stderr chunk from a child subprocess.

        Accumulates chunks into ``self._stderr_buf`` so we never display
        a partial tick whose tail is still in the next chunk:

        1. Drain complete \\n-terminated lines: for each, drop the
           \\r-overwritten history and print only the post-last-\\r final
           state to scrollback. (str.splitlines splits on \\r too, which
           is why we split("\\n") explicitly.)
        2. After draining, the buffer holds only the in-flight (no-\\n)
           tail. Update the spinner with the LAST COMPLETE tick — that's
           the text between the second-to-last and the last \\r. Text
           after the last \\r is partial; it sits in the buffer until the
           next chunk closes it with another \\r or \\n.

        Net UX: spinner reflects a complete, intelligible tick at every
        update (no truncated "Updating files:" without the count) and
        scrollback has one final-state line per phase."""
        if not chunk or self.json_mode:
            return
        self._stderr_buf += chunk
        # Drain complete \n-terminated lines first.
        while "\n" in self._stderr_buf:
            line, _, self._stderr_buf = self._stderr_buf.partition("\n")
            visible = line[line.rfind("\r") + 1:] if "\r" in line else line
            visible = visible.rstrip()
            if visible:
                self._stderr.print(visible)
        # Now buffer has the in-flight tail (no \n). Find the last
        # complete tick — text between the second-to-last \r and the last
        # \r. Anything after the last \r is partial and stays buffered.
        if self._active_status is None or "\r" not in self._stderr_buf:
            return
        last_r = self._stderr_buf.rfind("\r")
        before_last_r = self._stderr_buf[:last_r]
        if "\r" in before_last_r:
            second_last_r = before_last_r.rfind("\r")
            complete_tick = before_last_r[second_last_r + 1:]
        else:
            # Only one \r in buffer — text before it is a complete tick.
            complete_tick = before_last_r
        complete_tick = complete_tick.rstrip()
        if not complete_tick:
            return
        if self._status_base:
            self._active_status.update(f"{self._status_base}  {complete_tick}")
        else:
            self._active_status.update(complete_tick)

    @contextmanager
    def progress(self, total: int, *, desc: str) -> Iterator[Callable[[int], None]]:
        """Determinate progress bar via ``rich.Progress``. Yields an
        ``advance(n_bytes)`` callable.

        Suspends the active spinner (``self._active_status``) for the
        duration so the bar and the spinner don't fight for the terminal
        line; resumes the spinner on exit.

        Returns a nullcontext-style no-op (yielding a callable that does
        nothing) when ``json_mode``, quiet mode (``level < 1``), or
        ``total <= 0`` (empty/no-data repo).

        Single-active per Reporter — nesting is not supported (no current
        caller nests; would corrupt ``_active_status`` tracking)."""
        if self.json_mode or self.level < 1 or total <= 0:
            yield lambda _n: None
            return
        had_status = self._active_status is not None
        base = self._status_base
        if had_status and self._active_status is not None:
            self._active_status.stop()
            self._active_status = None
        prog = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self._stderr,
            transient=True,  # bar erased on exit; scrollback stays clean
        )
        task_id = prog.add_task(desc, total=total)
        self._active_progress = prog
        self._progress_task_id = task_id

        def advance(n: int) -> None:
            prog.update(task_id, advance=n)

        try:
            with prog:
                yield advance
        finally:
            self._active_progress = None
            self._progress_task_id = None
            if had_status:
                # Reset the chunk buffer (matches status() entry behavior)
                # so any in-flight tail from pre-progress doesn't bleed in.
                self._stderr_buf = ""
                self._status_base = base
                self._active_status = self._stderr.status(base)
                self._active_status.start()

    def install_log_bridge(self) -> None:
        lg = logging.getLogger("mintd")
        self._prev_propagate = lg.propagate
        self._prev_level = lg.level
        self._log_handler = _ReporterLogHandler(self)
        lg.addHandler(self._log_handler)
        lg.propagate = False
        lg.setLevel(logging.DEBUG)

    def uninstall_log_bridge(self) -> None:
        lg = logging.getLogger("mintd")
        if hasattr(self, "_log_handler"):
            lg.removeHandler(self._log_handler)
            # Restore prior state so subsequent uses (e.g. pytest caplog) work.
            if hasattr(self, "_prev_propagate"):
                lg.propagate = self._prev_propagate
            if hasattr(self, "_prev_level"):
                lg.setLevel(self._prev_level)

class _ReporterLogHandler(logging.Handler):
    def __init__(self, reporter: Reporter) -> None:
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        if record.levelno >= logging.ERROR:
            self.reporter.error(msg)
        elif record.levelno >= logging.WARNING:
            self.reporter.warn(msg)
        elif record.levelno >= logging.INFO:
            self.reporter.info(msg)
        else:
            self.reporter.debug(msg)
