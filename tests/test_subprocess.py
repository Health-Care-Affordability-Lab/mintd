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
