import subprocess

import pytest

from mintd._subprocess import run_streaming


class _StreamFromString:
    """File-like read(n) over a fixed string; one read returns everything, the
    next returns ''."""

    def __init__(self, text: str) -> None:
        self._buf = text
        self._done = False

    def read(self, n: int) -> str:
        if self._done:
            return ""
        chunk, self._buf = self._buf[:n], self._buf[n:]
        if not self._buf:
            self._done = True
        return chunk


class FakeProcess:
    def __init__(self, stdout_text: str = "", stderr_text: str = "", returncode: int = 0):
        self.stdout = _StreamFromString(stdout_text)
        self.stderr = _StreamFromString(stderr_text)
        self.returncode = returncode

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self): pass
    def kill(self): pass


def test_streaming_stdout_forwarding():
    """Chunk-based reader still recovers complete \\n-terminated lines for
    StreamResult.stdout_lines."""
    mock_proc = FakeProcess(stdout_text="line1\nline2\n")

    def popen_factory(*args, **kwargs):
        return mock_proc

    result = run_streaming(["echo"], popen_factory=popen_factory)
    assert result.stdout_lines == ["line1", "line2"]


def test_a_childs_last_line_without_a_newline_is_still_printed(capsys):
    """The reporter prints only whole lines, so the end of the child's output
    flushes what's left. Mutation: drop that flush -> "tail" is lost, red."""
    from mintd._console import Reporter

    run_streaming(["dvc"], reporter=Reporter(no_color=True),
                  popen_factory=lambda *a, **k: FakeProcess(stdout_text="first\ntail"))
    assert capsys.readouterr().out == "first\ntail"


def test_streaming_captures_post_cr_lines_for_parsing():
    """captured_lines (StreamResult.stderr_lines) gets the post-\\r-clean
    final state of each \\n-terminated line — useful for JSON parsing
    of dvc status etc."""
    progress = "tick1\rtick2\rtick3\rdone\n"
    mock_proc = FakeProcess(stderr_text=progress)

    def popen_factory(*args, **kwargs):
        return mock_proc

    result = run_streaming(["git", "clone"], popen_factory=popen_factory)
    assert result.stderr_lines == ["done"]


def test_streaming_forwards_raw_chunks_to_callback():
    """The forward callback receives RAW chunks so live \\r-based progress
    can reach the Reporter spinner with sub-second latency."""
    progress = "tick1\rtick2\rtick3\rdone\n"
    mock_proc = FakeProcess(stderr_text=progress)
    forwarded: list[str] = []

    def popen_factory(*args, **kwargs):
        return mock_proc

    run_streaming(
        ["git", "clone"],
        popen_factory=popen_factory,
        on_stderr=forwarded.append,
    )
    # Forwarder sees the raw chunk including the \r ticks. The Reporter
    # decides how to display it (spinner update vs scrollback print).
    assert "".join(forwarded) == progress


def test_streaming_logs_argv_at_debug_level(caplog):
    """Observability contract: every subprocess invocation (dvc included)
    logs its full argv at debug level, so `mintd -vv` shows exactly what was
    invoked — the silent dvc-checkout hunt was blind without it."""
    import logging

    mock_proc = FakeProcess(stdout_text="ok\n")

    def popen_factory(*args, **kwargs):
        return mock_proc

    with caplog.at_level(logging.DEBUG, logger="mintd._subprocess"):
        run_streaming(["dvc", "checkout", "data/final.dvc"], popen_factory=popen_factory)
    argv_lines = [r.message for r in caplog.records if "subprocess argv:" in r.message]
    assert len(argv_lines) == 1
    assert "dvc checkout data/final.dvc" in argv_lines[0]


def test_crlf_lines_are_captured_not_emptied():
    """Windows. dvc and git terminate lines with `\\r\\n`, and the re-wrap in
    `run_streaming` keeps `\\r` on purpose so progress ticks survive. Hunting
    for the last `\\r` then found the TERMINATOR, and captured everything
    after it — the empty string, for every line ever read on Windows.

    Nothing noticed for a long time because the display path forwards the raw
    chunk, so output still LOOKED correct. Only `stdout_lines` /
    `stderr_lines` were empty — which is what `_is_dvc_module_missing`,
    `dvc init`'s already-initialized tolerance, and every error
    classification in `_dvc_ops` and the git ops read. On Windows all of them
    were inert, and `dvc import` into an occupied destination surfaced as a
    bare `DvcOpError` with no message at all.

    Mutation: drop the `removesuffix("\\r")` -> both CRLF cases return [''].
    """
    def result_for(text: str):
        proc = FakeProcess(stderr_text=text, returncode=1)
        return run_streaming(["dvc", "import"], popen_factory=lambda *a, **k: proc)

    plain = "ERROR: bad DVC file name 'final/final.dvc' is git-ignored."
    assert result_for(plain + "\n").stderr_lines == [plain]
    assert result_for(plain + "\r\n").stderr_lines == [plain]

    # ...and a real progress tick still collapses to its last frame, which is
    # the behaviour the `\\r` hunt exists for.
    assert result_for("50%\r75%\r100%\n").stderr_lines == ["100%"]
    assert result_for("50%\r75%\r100%\r\n").stderr_lines == ["100%"]


@pytest.mark.parametrize("first", [KeyboardInterrupt(), subprocess.TimeoutExpired("dvc", 1.0)],
                         ids=["ctrl-c", "wall-timeout"])
def test_a_second_ctrl_c_during_the_wait_still_kills_and_reaps_the_child(first):
    """A Ctrl-C, or a wall timeout, stops the wait for dvc; then a Ctrl-C lands
    in the kill ladder's 5 s wait after SIGTERM. The ladder must still SIGKILL
    and reap dvc, and a KeyboardInterrupt must be what leaves `run_streaming`.
    A push's caller puts the pointer files back once it gets the exception,
    and a dvc still running its `finally` would rewrite them after that; and a
    caller that catches Exception must not carry on past the user's Ctrl-C.

    Mutations: catch only `subprocess.TimeoutExpired` in `_kill_ladder` -> the
    Ctrl-C leaves before kill, red; drop the `raise` after the reap -> the
    wall-timeout case raises WallTimeoutExceeded, red.
    """
    calls = []

    class InterruptedInTheLadder(FakeProcess):
        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if len(calls) == 1:
                raise first
            if len(calls) == 3:  # the Ctrl-C in the ladder's wait
                raise KeyboardInterrupt
            calls.append("reaped")
            return -9

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

    with pytest.raises(KeyboardInterrupt):
        run_streaming(["dvc", "push"], wall_timeout=1.0, popen_factory=lambda *a, **k: InterruptedInTheLadder())
    assert calls == [("wait", 1.0), "terminate", ("wait", 5), "kill", ("wait", None), "reaped"]


def test_a_childs_last_stderr_line_without_a_newline_is_still_printed(capsys):
    from mintd._console import Reporter

    run_streaming(["dvc"], reporter=Reporter(no_color=True),
                  popen_factory=lambda *a, **k: FakeProcess(stderr_text="first\nERROR: from http://u:tok@h/r", returncode=1))
    err = capsys.readouterr().err
    assert "first" in err and "ERROR: from http://h/r" in err and "tok" not in err


def test_a_timed_out_childs_last_words_are_still_printed(capsys):
    """Mutation: raise WallTimeoutExceeded before flushing -> red."""
    from mintd._console import Reporter
    from mintd._subprocess import WallTimeoutExceeded

    class Hung(FakeProcess):
        waited = False

        def wait(self, timeout=None):
            if timeout is not None and not self.waited:
                self.waited = True
                raise subprocess.TimeoutExpired("dvc", timeout)
            return self.returncode

    with pytest.raises(WallTimeoutExceeded):
        run_streaming(["dvc"], reporter=Reporter(no_color=True), wall_timeout=0.1,
                      popen_factory=lambda *a, **k: Hung(stdout_text="last words without newline"))
    assert "last words without newline" in capsys.readouterr().out


def test_an_interrupted_childs_last_words_are_still_printed(capsys):
    """Mutation: re-raise KeyboardInterrupt before flushing -> red."""
    from mintd._console import Reporter

    class Interrupted(FakeProcess):
        waited = False

        def wait(self, timeout=None):
            if not self.waited:
                self.waited = True
                raise KeyboardInterrupt
            return self.returncode

    with pytest.raises(KeyboardInterrupt):
        run_streaming(["dvc"], reporter=Reporter(no_color=True),
                      popen_factory=lambda *a, **k: Interrupted(stdout_text="last words without newline"))
    assert "last words without newline" in capsys.readouterr().out
