from typing import Any, Callable, Optional
import sys
import logging
from rich.console import Console
from rich.status import Status

class Reporter:
    def __init__(self, *, verbose: int = 0, quiet: int = 0,
                 json_mode: bool = False, no_color: bool = False) -> None:
        self.json_mode = json_mode
        self.level = 1 + verbose - quiet
        self._stderr = Console(file=sys.stderr, no_color=no_color, force_terminal=None)
        self._stdout = Console(file=sys.stdout, no_color=no_color, force_terminal=None)
        self._active_status: Optional[Status] = None
        self._status_base: str = ""
        # Per-stream byte buffer for chunk-boundary safety: subprocess
        # output arrives in 256-byte chunks that may split a \r-terminated
        # progress tick in half. Without buffering, the spinner would
        # display the partial start of the next tick ("Updating files:"
        # instead of "Updating files: 23% (700/3090)"). We coalesce chunks
        # until we see a \r or \n boundary.
        self._stderr_buf: str = ""

    def status(self, msg: str) -> Any:  # rich.Status or nullcontext
        if self.json_mode or self.level < 1:
            from contextlib import nullcontext
            return nullcontext()
        # Clear any residual partial from the previous subprocess (rare,
        # but possible if a child exited mid-tick or reader thread was
        # slow to drain). Prevents cross-block bleed into the new spinner.
        self._stderr_buf = ""
        self._status_base = msg
        self._active_status = self._stderr.status(msg)
        return self._active_status

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
