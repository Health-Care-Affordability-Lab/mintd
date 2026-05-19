from mintd._subprocess import run_streaming

class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines=None, returncode=0):
        self.stdout = stdout_lines
        self.stderr = stderr_lines or []
        self.returncode = returncode
        self.poll_called = False

    def poll(self):
        self.poll_called = True
        return 0

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self): pass
    def kill(self): pass

def test_streaming_stdout_forwarding():
    stdout_lines = ["line1\n", "line2\n"]
    mock_proc = FakeProcess(stdout_lines)
    
    def popen_factory(*args, **kwargs):
        return mock_proc

    result = run_streaming(["echo"], popen_factory=popen_factory)
    assert result.stdout_lines == ["line1", "line2"]
