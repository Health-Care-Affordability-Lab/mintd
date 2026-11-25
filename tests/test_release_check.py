"""mintd._release_check follows the releases/latest redirect, at most once a day, and is silent on failure.

A real HTTP server on the loopback interface stands in for github.com, so the
redirect, the cache and the time limit run for real.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import yaml

from mintd._release_check import PRERELEASE_MARKER, newest_release, release_notice


_RELEASE_YML = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def _release_steps(job: str) -> str:
    return "\n".join(step.get("run", "") for step in yaml.safe_load(_RELEASE_YML.read_text())["jobs"][job]["steps"])


def _first_note() -> str:
    """The notes a prerelease is published with, before the evidence job writes its own, as release.yml has them."""
    note = re.search(r'notes="((?:[^"\\]|\\.)*)"', _release_steps("prerelease"))
    assert note is not None
    return note[1].replace("\\`", "`").replace("$run_url", "https://github.com/o/r/actions/runs/1")


# The API's release for each tag, as an install newer than the newest release reads it.
_RELEASES = {
    "/api/releases/tags/v1.8.0": (200, {"prerelease": True, "body": _first_note()}),  # published, evidence not yet run
    "/api/releases/tags/v1.3.0": (200, {"prerelease": True, "body": "notes\r\n\r\nA prerelease: `releases/latest` skips it."}),
    "/api/releases/tags/v1.4.0": (200, {"prerelease": True, "body": "notes\nIts gate wrote A prerelease: once; promote dropped it."}),
    "/api/releases/tags/v1.5.0": (404, {"message": "Not Found"}),
    "/api/releases/tags/v1.6.0": (200, {"prerelease": False, "body": "A prerelease: but marked a full release"}),
    "/api/releases/tags/v1.7.0": (500, {"message": "Server Error"}),
}


class _GitHub(BaseHTTPRequestHandler):
    """HEAD for releases/latest, like the check, which reads the first response's Location and never follows
    it; GET for the API's release of a tag."""

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

    def do_GET(self) -> None:
        _GitHub.requests.append(self.path)
        status, release = _RELEASES.get(self.path, (404, {}))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(release).encode())

    def _redirect(self, to: str) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("no_proxy", "*")  # a developer's proxy must not see these
    monkeypatch.setenv("NO_PROXY", "*")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GitHub)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _GitHub.requests = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def lookup_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup turned on: the suite turns it off (conftest), and a CI runner sets CI."""
    monkeypatch.delenv("MINTD_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture
def github(server: str, lookup_on: None) -> str:
    return server


def _cache(tmp_path: Path, text: str, age: float = 0.0) -> None:
    (tmp_path / "newest-release").write_text(text)
    when = time.time() - age
    os.utime(tmp_path / "newest-release", (when, when))


def test_follows_the_redirect_and_asks_once_a_day(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == ("1.2.3", "")
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == ("1.2.3", "")
    assert _GitHub.requests == ["/releases/latest"]


@pytest.mark.parametrize("age", [2 * 86400, -2 * 86400])  # a day old, or dated in the future
def test_asks_again_when_the_cache_is_out_of_date(github: str, tmp_path: Path, age: float) -> None:
    _cache(tmp_path, "1.0.0\n1.0.0\n", age)
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == ("1.2.3", "")
    assert _GitHub.requests == ["/releases/latest"]


def test_an_upgrade_asks_again_at_once(github: str, tmp_path: Path) -> None:
    """Just upgraded to a just-promoted release, the cached answer is older
    than the install, and would call it withdrawn."""
    _cache(tmp_path, "1.2.2\n1.2.2\n")
    assert newest_release(tmp_path, "1.2.3", f"{github}/releases/latest") == ("1.2.3", "")
    assert _GitHub.requests == ["/releases/latest"]


@pytest.mark.parametrize(("installed", "standing"), [
    ("1.3.0", "pending"), ("1.8.0", "pending"), ("1.4.0", "withdrawn"), ("1.6.0", "withdrawn"), ("1.5.0", "unreleased"),
    ("1.7.0", ""),
])
def test_an_install_newer_than_the_newest_release_reads_its_own_release(
    github: str, tmp_path: Path, installed: str, standing: str,
) -> None:
    """A prerelease not yet promoted has the release run's marker line, from its first notes (1.8.0: published,
    the evidence job not yet run) to promote; a withdrawn one (promoted, then made a prerelease again, or a full
    release that isn't the newest) doesn't; no release for the tag is not released; a failed read is unknown.
    Mutations: read the marker anywhere in a line; take any 404 as unknown; read a full release as unknown;
    publish the first notes without the marker -> red."""
    api = f"{github}/api/releases/tags/v"
    assert newest_release(tmp_path, installed, f"{github}/releases/latest", api=api) == ("1.2.3", standing)
    assert _GitHub.requests == ["/releases/latest", f"/api/releases/tags/v{installed}"]
    assert (tmp_path / "newest-release").read_text() == f"1.2.3\n{installed}\n{standing}\n"


def test_an_install_not_newer_never_reads_its_own_release(github: str, tmp_path: Path) -> None:
    api = f"{github}/api/releases/tags/v"
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest", api=api) == ("1.2.3", "")
    assert _GitHub.requests == ["/releases/latest"]


@pytest.mark.parametrize(("age", "asks"), [(9 * 60, False), (11 * 60, True)])
def test_a_newer_than_newest_answer_is_kept_ten_minutes_not_a_day(
    github: str, tmp_path: Path, age: float, asks: bool,
) -> None:
    """Checked during a prerelease's live check, the answer must not outlive its promote by a day.
    Mutation: keep it a day -> red."""
    _cache(tmp_path, "1.2.2\n1.3.0\npending\n", age)
    api = f"{github}/api/releases/tags/v"
    result = newest_release(tmp_path, "1.3.0", f"{github}/releases/latest", api=api)
    assert result == (("1.2.3", "pending") if asks else ("1.2.2", "pending"))
    assert bool(_GitHub.requests) is asks


def test_a_withdrawn_install_does_not_ask_on_every_run(github: str, tmp_path: Path) -> None:
    _cache(tmp_path, "1.2.2\n1.2.3\n")  # looked up for this same install today
    assert newest_release(tmp_path, "1.2.3", f"{github}/releases/latest") == ("1.2.2", "")
    assert _GitHub.requests == []


def test_no_promoted_release_is_unknown(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/none/releases/latest") == (None, "")


def test_offline_is_unknown_and_kept_for_the_day(github: str, tmp_path: Path) -> None:
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert newest_release(tmp_path, "1.0.0", f"http://127.0.0.1:{closed}/releases/latest") == (None, "")
    assert (tmp_path / "newest-release").read_text() == "\n1.0.0\n\n"
    # The failure is remembered: no second wait the same day.
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == (None, "")
    assert _GitHub.requests == []


@pytest.mark.parametrize("garbled", [b"not a version\n1.0.0\n", b"0.1\xff\n1.0.0\n", ("9" * 5000 + ".0.0\n1.0.0\n").encode()])
def test_a_garbled_cache_is_unknown(lookup_on: None, tmp_path: Path, garbled: bytes) -> None:
    (tmp_path / "newest-release").write_bytes(garbled)
    assert newest_release(tmp_path, "1.0.0", "http://127.0.0.1:9/unused", timeout=0.3) == (None, "")


def test_a_huge_version_in_the_redirect_is_unknown(github: str, tmp_path: Path) -> None:
    assert newest_release(tmp_path, "1.0.0", f"{github}/huge/releases/latest") == (None, "")


def test_no_lookup_when_the_cache_folder_is_a_file(github: str, tmp_path: Path) -> None:
    (tmp_path / "cache").write_text("")  # a file where the folder should be
    assert newest_release(tmp_path / "cache", "1.0.0", f"{github}/releases/latest") == (None, "")
    assert _GitHub.requests == []


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
def test_no_lookup_when_the_answer_cannot_be_kept(github: str, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o500)
    try:
        assert newest_release(cache, "1.0.0", f"{github}/releases/latest") == (None, "")
    finally:
        cache.chmod(0o700)
    assert _GitHub.requests == []


def test_gives_up_at_the_time_limit_even_when_bytes_keep_coming(github: str, tmp_path: Path) -> None:
    started = time.monotonic()
    assert newest_release(tmp_path, "1.0.0", f"{github}/slow/releases/latest", timeout=0.3) == (None, "")
    assert time.monotonic() - started < 1.5


def test_the_lookup_thread_ends_when_a_server_says_nothing(github: str, tmp_path: Path) -> None:
    """The socket timeout ends the abandoned thread; without it, it would
    wait for as long as the server stays silent."""
    url = f"{github}/silent/releases/latest"
    assert newest_release(tmp_path, "1.0.0", url, timeout=0.3) == (None, "")
    deadline = time.monotonic() + 1.5
    while any(t.name == f"mintd-release-check {url}" for t in threading.enumerate()):
        assert time.monotonic() < deadline, "the lookup thread outlived its socket timeout"
        time.sleep(0.05)


@pytest.mark.parametrize(("name", "value"), [("MINTD_NO_UPDATE_CHECK", "1"), ("CI", "true")])
def test_the_variable_or_a_ci_run_turns_the_lookup_off(
    github: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """No request and no answer, not even a fresh one from the cache.
    Mutation: drop either half of the check -> red."""
    monkeypatch.setenv(name, value)
    _cache(tmp_path, "1.2.3\n1.0.0\n")
    assert newest_release(tmp_path, "1.0.0", f"{github}/releases/latest") == (None, "")
    assert _GitHub.requests == []


def test_the_suite_never_looks_up_a_release(server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest turns the lookup off for every test, even off CI.
    Mutation: drop that line from conftest -> red."""
    monkeypatch.delenv("CI", raising=False)
    assert newest_release(tmp_path, "1.0.0", f"{server}/releases/latest") == (None, "")
    assert _GitHub.requests == [], "a test reached the release lookup: conftest must set MINTD_NO_UPDATE_CHECK=1"


_HINTS = {
    "pending": ("mintd 1.3.0 is a prerelease; the newest release is v1.2.3", "nothing to do until it's promoted or withdrawn"),
    "withdrawn": ("mintd 1.3.0 was withdrawn; the newest release is v1.2.3", "go back to v1.2.3 with the update line below"),
    "unreleased": ("mintd 1.3.0 is not released; the newest release is v1.2.3",
                   "no published release has its tag, so no update is offered"),
    "": ("mintd 1.3.0 is newer than the newest release, v1.2.3",
         "its own release couldn't be read, so no update is offered"),
}


@pytest.mark.parametrize(
    ("installed", "newest", "standing", "notice", "update"),
    [
        ("1.2.3", "1.2.3", "", None, False),
        ("1.2.3", None, "", None, False),
        ("1.2.2", "1.2.3", "", "newest release: v1.2.3", True),
        ("1.2.4.dev3+g0123abc", "1.2.3", "", "newest release: v1.2.3", False),  # a dev build is never a release
        *[("1.3.0", "1.2.3", standing, f"{line}\n  hint: {hint}", standing == "withdrawn")
          for standing, (line, hint) in _HINTS.items()],
        ("1.2.3", "1.2.3.post1", "", "newest release: v1.2.3.post1", True),
        ("9" * 5000 + ".0.0", "1.2.3", "", "newest release: v1.2.3", False),  # not a release: no int() of 5000 digits
    ],
)
def test_release_notice(installed: str, newest: str | None, standing: str, notice: str | None, update: bool) -> None:
    """Only a withdrawn install is offered the update line, a way back; a pending prerelease, an unreleased or an
    unknown one never a downgrade. Mutation: offer it to every install newer than the newest release -> red."""
    assert release_notice(installed, newest, standing) == (notice, update)


@pytest.mark.parametrize("standing", list(_HINTS))
def test_every_new_message_is_a_short_line_and_a_short_hint(standing: str) -> None:
    notice, _ = release_notice("0.17.14", "0.16.0", standing)
    assert notice is not None
    line, hint = notice.split("\n")
    assert len(line) < 70 and len(hint) < 70 and hint.startswith("  hint: "), notice


def test_the_prerelease_marker_is_the_line_the_release_run_writes_and_promote_drops() -> None:
    """The check tells a pending prerelease from a withdrawn one by this line. Mutation: change it on either
    side -> red."""
    assert f"\n{PRERELEASE_MARKER}" in _first_note()  # published with it, before evidence runs
    assert f"\n{PRERELEASE_MARKER}" in _release_steps("evidence")
    assert f"grep -v '^{PRERELEASE_MARKER}'" in _release_steps("promote")
