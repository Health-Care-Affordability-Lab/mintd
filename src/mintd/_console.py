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

    def status(self, msg: str) -> Any:  # rich.Status or nullcontext
        if self.json_mode or self.level < 1:
            from contextlib import nullcontext
            return nullcontext()
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

    def passthrough_stdout(self, line: str) -> None:
        self._stdout.print(line, end="", soft_wrap=True, highlight=False, markup=False)

    def passthrough_stderr(self, line: str) -> None:
        self._stderr.print(line, end="", soft_wrap=True, highlight=False, markup=False)

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
