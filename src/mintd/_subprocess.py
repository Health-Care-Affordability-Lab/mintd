from dataclasses import dataclass
from typing import Callable, List, Dict, Optional, Any
import logging
import shlex
import subprocess
import threading
import time
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class StreamResult:
    returncode: int
    stdout_lines: List[str]
    stderr_lines: List[str]

class WallTimeoutExceeded(Exception):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"command exceeded wall timeout of {seconds}s")

def run_streaming(
    argv: List[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    stall_timeout: Optional[float] = None,
    wall_timeout: Optional[float] = None,
    reporter: Optional[Any] = None,
    on_stdout: Optional[Callable[[str], None]] = None,
    on_stderr: Optional[Callable[[str], None]] = None,
    json_mode: bool = False,
    clock: Callable[[], float] = time.monotonic,
    popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> StreamResult:
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    
    def _default_stdout(line: str) -> None:
        # Reader hands us complete \n-terminated lines (post-\r-tick cleanup).
        # json_mode suppresses the forward to terminal so JSON consumers
        # don't see child output on stdout; line capture still happens.
        if json_mode:
            return
        if reporter is not None:
            reporter.passthrough_stdout(line)
        else:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _default_stderr(line: str) -> None:
        if reporter is not None:
            reporter.passthrough_stderr(line)
        else:
            sys.stderr.write(line)
            sys.stderr.flush()

    cb_stdout = on_stdout or _default_stdout
    cb_stderr = on_stderr or _default_stderr

    # Observability: -vv must show exactly which subprocess ran (the silent
    # dvc-checkout hunt was blind without this). One line, debug level.
    logger.debug(
        "subprocess argv: %s (cwd=%s)", shlex.join(argv), cwd or Path.cwd(),
    )

    proc = popen_factory(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
    )
    # Subprocess.Popen's default TextIOWrapper uses universal-newline mode
    # (newline=None), which translates every \r and \r\n into \n BEFORE our
    # reader sees the chunks. git/dvc progress uses \r to overwrite the
    # same line on a TTY — losing the \r makes every tick look like a
    # completed line and produces a wall of scrollback output. Re-wrap the
    # underlying buffered streams with newline="" so \r is preserved as-is.
    # Skip the re-wrap on test fakes whose stdout/stderr aren't real
    # TextIOWrapper instances (no .detach()).
    import io
    if proc.stdout is not None and hasattr(proc.stdout, "detach"):
        proc.stdout = io.TextIOWrapper(
            proc.stdout.detach(), encoding="utf-8", newline="", line_buffering=True,
        )
    if proc.stderr is not None and hasattr(proc.stderr, "detach"):
        proc.stderr = io.TextIOWrapper(
            proc.stderr.detach(), encoding="utf-8", newline="", line_buffering=True,
        )

    last_line_at = [clock()]

    def _reader(stream, forward, captured_lines):
        """Read chunks. Forward each chunk RAW to ``forward`` (so live
        \\r-based progress ticks reach the spinner update path with
        sub-second latency). Separately accumulate \\n-terminated, post-
        \\r-cleaned lines into ``captured_lines`` for caller-side parsing
        (StreamResult.stdout_lines / stderr_lines)."""
        line_buf = ""
        while True:
            try:
                chunk = stream.read(256)
            except ValueError:
                break  # stream closed
            if not chunk:
                if line_buf:
                    captured_lines.append(line_buf.rstrip("\r\n"))
                break
            last_line_at[0] = clock()
            # Capture path: line-by-line, post-\r-clean.
            line_buf += chunk
            while "\n" in line_buf:
                line, _, line_buf = line_buf.partition("\n")
                display = line[line.rfind("\r") + 1:] if "\r" in line else line
                captured_lines.append(display)
            # Display path: raw chunk to the forwarder.
            forward(chunk)

    t1 = threading.Thread(target=_reader, args=(proc.stdout, cb_stdout, stdout_lines), daemon=True)
    t2 = threading.Thread(target=_reader, args=(proc.stderr, cb_stderr, stderr_lines), daemon=True)
    t1.start()
    t2.start()

    def _kill_ladder():
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    stall_thread = None
    if stall_timeout is not None:
        def _stall_watchdog():
            while proc.poll() is None:
                if clock() - last_line_at[0] > stall_timeout:
                    _kill_ladder()
                    break
                time.sleep(min(stall_timeout, 1.0))
        stall_thread = threading.Thread(target=_stall_watchdog, daemon=True)
        stall_thread.start()

    try:
        proc.wait(timeout=wall_timeout)
    except subprocess.TimeoutExpired:
        _kill_ladder()
        # wall_timeout cannot be None here — TimeoutExpired only fires when set.
        raise WallTimeoutExceeded(wall_timeout if wall_timeout is not None else 0.0)
    except KeyboardInterrupt:
        _kill_ladder()
        raise

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    return StreamResult(
        returncode=proc.returncode,
        stdout_lines=stdout_lines,
        stderr_lines=stderr_lines,
    )
