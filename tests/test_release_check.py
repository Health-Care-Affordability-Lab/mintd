"""mintd._release_check follows the releases/latest redirect, at most once a day, and is silent on failure.

A real HTTP server on the loopback interface stands in for github.com, so the
redirect, the cache and the time limit run for real.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mintd._release_check import newest_release, release_notice


class _GitHub(BaseHTTPRequestHandler):
    """HEAD only, like the check: a GET gets 501. The check reads the first
    response's Location and never follows it."""

    requests: list[str] = []

    def do_HEAD(self) -> None:
        _GitHub.requests.append(self.path)
        if self.path == "/releases/latest":
            self._redirect("/releases/tag/v1.2.3")
        elif self.path == "/none/releases/latest":
            self._redirect("/none/releases")  # nothing promoted
        elif self.path == "/huge/releases/latest":
            self._redirect("/releases/tag/v" + "9" * 5000 + ".0.0")
        elif self.path == "/slow/releases/latest":
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            try:  # one header byte at a time: the socket timeout never fires
                for _ in range(50):
                    self.wfile.write(b"X")
                    self.wfile.flush()
                    time.sleep(0.1)
            except OSError:
                pass
        elif self.path == "/silent/releases/latest":
            time.sleep(3)  # says nothing: only the socket timeout ends the wait
        else:
            self.send_response(200)
            self.end_headers()

    def _redirect(self, to: str) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("no_proxy", "*")  # a developer's proxy must not see these
    monkeypatch.setenv("NO_PROXY", "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHub)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _GitHub.requests = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _cache(tmp_path: Path, text: str, age: float = 0.0) -> None:
    (tmp_path / "newest-release").write_text(text)
    when = time.time() - age
    os.utime(tmp_path / "newest-release", (when, when))


def test_follows_the_redirect_and_asks_once_a_day(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == "1.2.3"
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == "1.2.3"
    assert _GitHub.requests == ["/releases/latest"]


@pytest.mark.parametrize("age", [2 * 86400, -2 * 86400])  # a day old, or dated in the future
def test_asks_again_when_the_cache_is_out_of_date(github: str, tmp_path: Path, age: float) -> None:
    _cache(tmp_path, "1.0.0\n1.0.0\n", age)
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == "1.2.3"
    assert _GitHub.requests == ["/releases/latest"]


def test_an_upgrade_asks_again_at_once(github: str, tmp_path: Path) -> None:
    """Just upgraded to a just-promoted release, the cached answer is older
    than the install, and would call it withdrawn."""
    _cache(tmp_path, "1.2.2\n1.2.2\n")
    assert newest_release(tmp_path, "1.2.3", f"{github}/releases/latest") == "1.2.3"
    assert _GitHub.requests == ["/releases/latest"]


def test_a_withdrawn_install_does_not_ask_on_every_run(github: str, tmp_path: Path) -> None:
    _cache(tmp_path, "1.2.2\n1.2.3\n")  # looked up for this same install today
    assert newest_release(tmp_path, "1.2.3", f"{github}/releases/latest") == "1.2.2"
    assert _GitHub.requests == []


def test_no_promoted_release_is_unknown(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/none/releases/latest") is None


def test_offline_is_unknown_and_kept_for_the_day(github: str, tmp_path: Path) -> None:
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert newest_release(tmp_path, "1.0.0", f"http://127.0.0.1:{closed}/releases/latest") is None
    assert (tmp_path / "newest-release").read_text() == "\n1.0.0\n"
    # The failure is remembered: no second wait the same day.
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") is None
    assert _GitHub.requests == []


@pytest.mark.parametrize("garbled", [b"not a version\n1.0.0\n", b"0.1\xff\n1.0.0\n", ("9" * 5000 + ".0.0\n1.0.0\n").encode()])
def test_a_garbled_cache_is_unknown(tmp_path: Path, garbled: bytes) -> None:
    (tmp_path / "newest-release").write_bytes(garbled)
    assert newest_release(tmp_path, "1.0.0", "http://127.0.0.1:9/unused", timeout=0.3) is None


def test_a_huge_version_in_the_redirect_is_unknown(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/huge/releases/latest") is None


def test_no_lookup_when_the_cache_folder_is_a_file(github: str, tmp_path: Path) -> None:
    (tmp_path / "cache").write_text("")  # a file where the folder should be
    assert newest_release(tmp_path / "cache", "1.0.0", f"{github}/releases/latest") is None
    assert _GitHub.requests == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
def test_no_lookup_when_the_answer_cannot_be_kept(github: str, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o500)
    try:
        assert newest_release(cache, "1.0.0", f"{github}/releases/latest") is None
    finally:
        cache.chmod(0o700)
    assert _GitHub.requests == []


def test_gives_up_at_the_time_limit_even_when_bytes_keep_coming(github: str, tmp_path: Path) -> None:
    started = time.monotonic()
    assert newest_release(tmp_path, "1.0.0", f"{github}/slow/releases/latest", timeout=0.3) is None
    assert time.monotonic() - started < 1.5


def test_the_lookup_thread_ends_when_a_server_says_nothing(github: str, tmp_path: Path) -> None:
    """The socket timeout ends the abandoned thread; without it, it would
    wait for as long as the server stays silent."""
    url = f"{github}/silent/releases/latest"
    assert newest_release(tmp_path, "1.0.0", url, timeout=0.3) is None
    deadline = time.monotonic() + 1.5
    while any(t.name == f"mintd-release-check {url}" for t in threading.enumerate()):
        assert time.monotonic() < deadline, "the lookup thread outlived its socket timeout"
        time.sleep(0.05)


@pytest.mark.parametrize(
    ("installed", "newest", "notice"),
    [
        ("1.2.3", "1.2.3", None),
        ("1.2.3", None, None),
        ("1.2.2", "1.2.3", "newest release: v1.2.3"),
        ("1.2.4.dev3+g0123abc", "1.2.3", "newest release: v1.2.3"),  # a dev build is never a release
        ("1.3.0", "1.2.3", "mintd 1.3.0 was withdrawn or is not released yet; the newest release is v1.2.3"),
        ("1.2.3.post1", "1.2.3", "mintd 1.2.3.post1 was withdrawn or is not released yet; the newest release is v1.2.3"),
        ("1.2.3", "1.2.3.post1", "newest release: v1.2.3.post1"),
        ("1.10.0", "1.9.0", "mintd 1.10.0 was withdrawn or is not released yet; the newest release is v1.9.0"),
        ("9" * 5000 + ".0.0", "1.2.3", "newest release: v1.2.3"),  # not a release: no int() of 5000 digits
    ],
)
def test_release_notice(installed: str, newest: str | None, notice: str | None) -> None:
    assert release_notice(installed, newest) == notice
