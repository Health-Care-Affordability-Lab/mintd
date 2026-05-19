from dataclasses import dataclass
from typing import Callable, List, Dict, Optional, Any
import subprocess
import threading
import time
import sys
from pathlib import Path

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
        # Always capture into stdout_lines so callers (e.g. SubprocessDvcOps.status)
        # can parse the output even in json_mode. json_mode only suppresses
        # FORWARDING stdout to the terminal — not capture.
        stdout_lines.append(line.rstrip("\n"))
        if json_mode:
            return
        if reporter is not None:
            reporter.passthrough_stdout(line)
        else:
            sys.stdout.write(line)
            sys.stdout.flush()

    def _default_stderr(line: str) -> None:
        stderr_lines.append(line.rstrip("\n"))
        if reporter is not None:
            reporter.passthrough_stderr(line)
        else:
            sys.stderr.write(line)
            sys.stderr.flush()

    cb_stdout = on_stdout or _default_stdout
    cb_stderr = on_stderr or _default_stderr

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

    last_line_at = [clock()]

    def _reader(stream, callback):
        for line in stream:
            last_line_at[0] = clock()
            callback(line)

    t1 = threading.Thread(target=_reader, args=(proc.stdout, cb_stdout), daemon=True)
    t2 = threading.Thread(target=_reader, args=(proc.stderr, cb_stderr), daemon=True)
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
