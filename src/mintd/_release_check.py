"""Which release `releases/latest` names, so `mintd --version` can say when
this install is behind it, or newer than it.

- It reads where `releases/latest` redirects rather than calling the GitHub
  API: no token, no rate limit, and `releases/latest` never names a
  prerelease, so "newest" means the newest release the maintainer promoted.
  One HEAD request; the redirect is not followed, since its Location header
  is the answer (urllib would follow it as a GET of the whole page).
- "Behind" is "not equal". A dev build never equals a release, so it is
  always nudged, which is accurate.
- An install newer than the newest release reads its own release (one
  unauthenticated API call) to say which it is: a prerelease not yet promoted
  (its notes hold the release run's "A prerelease: " line from the moment it is
  published, and promote drops it), withdrawn (published without that line:
  promoted, then rolled back, or a full release that isn't the newest), or not
  released (no published release for its tag). Only withdrawn gets the update
  line: going back is the fix there, and a downgrade anywhere else.
- Silent when anything fails. The lookup gets 2 seconds in all, DNS included:
  the socket timeout doesn't cover name lookup, so it runs on a daemon thread
  that is abandoned at the deadline.
- Off when MINTD_NO_UPDATE_CHECK=1, or when CI is set (any non-empty value,
  as GitHub Actions and most CI services set it): no request, no notice.
- At most one lookup a day per install: the answer, or the failure, is kept
  in a file in the cache folder with the version it was looked up for, so an
  upgrade asks again at once. An answer that the install is newer than the
  newest release is kept 10 minutes, so a promote or a rollback shows soon. A cache folder that can't be written or
  reached means no lookup at all, since it would otherwise happen on every
  run. Any filesystem error means "unknown", never a crash of --version.

This is mintd's only HTTP client of its own; ruff's banned-api rule lets this
module, and no other, import urllib.request.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from typing import Callable
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._atomic import _atomic_write_text

LATEST_URL = "https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest"
RELEASE_API = "https://api.github.com/repos/Health-Care-Affordability-Lab/mintd/releases/tags/v"
# The line the release run writes into a prerelease's notes, and promote drops (release.yml).
PRERELEASE_MARKER = "A prerelease: "
# It redirects to .../releases/tag/vX.Y.Z, or to .../releases when none is promoted.
# At most 9 digits a part, so int() never meets a number too long to convert.
_TAG = re.compile(r"/releases/tag/v(\d{1,9}\.\d{1,9}\.\d{1,9}(?:\.post\d{1,9})?)$")
_RELEASE = re.compile(r"(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:\.post(\d{1,9}))?")
_DAY = 86400.0
_SHORT = 600.0  # an install newer than the newest release


class _KeepRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # not followed: urllib then raises HTTPError carrying the Location


def _in_time(read: Callable[[], str], url: str, timeout: float) -> str:
    """`read()`'s answer, or "" when it fails or takes longer than `timeout`, DNS included: it runs on a daemon
    thread that is abandoned at the deadline."""
    found = [""]

    def run() -> None:
        with contextlib.suppress(Exception):  # offline, DNS, TLS, a bad answer: all mean "unknown"
            found[0] = read()

    thread = threading.Thread(target=run, name=f"mintd-release-check {url}", daemon=True)
    thread.start()
    thread.join(max(timeout, 0.0))
    return found[0]


def _fetch(url: str, timeout: float) -> str:
    """The version `url` redirects to, or "" on any failure or none."""

    def read() -> str:
        try:
            build_opener(_KeepRedirect).open(Request(url, method="HEAD"), timeout=timeout).close()
            return ""  # not a redirect
        except HTTPError as e:
            match = _TAG.search(e.headers.get("Location", ""))
        return match[1] if match else ""

    return _in_time(read, url, timeout)


def _standing(url: str, timeout: float) -> str:
    """What the release at `url` (the API's release for a tag) is: "pending", "withdrawn", "unreleased", or "" when
    it can't be read."""

    def read() -> str:
        try:
            with build_opener().open(Request(url, headers={"Accept": "application/vnd.github+json"}), timeout=timeout) as r:
                release = json.loads(r.read(1_000_000))
        except HTTPError as e:
            return "unreleased" if e.code == 404 else ""
        lines = str(release.get("body") or "").splitlines()
        pending = release.get("prerelease") is True and any(line.startswith(PRERELEASE_MARKER) for line in lines)
        return "pending" if pending else "withdrawn"

    return _in_time(read, url, timeout)


def is_release(version: str) -> bool:
    """A promoted release's version, not a dev or local build."""
    return _order(version) is not None


def newest_release(
    cache_dir: Path, installed: str, url: str = LATEST_URL, timeout: float = 2.0, api: str = RELEASE_API,
) -> tuple[str | None, str]:
    """The newest promoted release, e.g. "0.12.0", or None when unknown or turned off; and, for an install newer
    than it, what the install's own release is (see `_standing`), else ""."""
    if os.environ.get("MINTD_NO_UPDATE_CHECK") == "1" or os.environ.get("CI"):
        return None, ""
    try:
        return _lookup(cache_dir, installed, url, timeout, api)
    except OSError:  # e.g. a parent of the cache folder that can't be entered: unknown, never a crash
        return None, ""


def _lookup(cache_dir: Path, installed: str, url: str, timeout: float, api: str) -> tuple[str | None, str]:
    cache = cache_dir / "newest-release"  # "<newest>\n<the install it was looked up for>\n<its standing>\n"
    with contextlib.suppress(OSError, ValueError):  # ValueError: bytes that aren't UTF-8
        newest, checked_for, standing = [*cache.read_text(encoding="utf-8").split("\n"), "", ""][:3]
        keep = _SHORT if _newer(installed, newest) else _DAY
        if 0 <= time.time() - cache.stat().st_mtime < keep and checked_for == installed:  # a future date is stale
            return (newest if is_release(newest) else None), standing  # another install, e.g. an upgrade, asks again
    with contextlib.suppress(OSError):
        cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir.is_dir() and os.access(cache_dir, os.W_OK)):  # a file there passes os.access
        return None, ""  # the answer couldn't be kept, so every run would ask again
    start = time.monotonic()
    newest = _fetch(url, timeout)
    standing = _standing(api + installed, timeout - (time.monotonic() - start)) if _newer(installed, newest) else ""
    with contextlib.suppress(OSError):
        _atomic_write_text(cache, f"{newest}\n{installed}\n{standing}\n")  # "" keeps a failure: one wait offline
    return newest or None, standing


def _newer(version: str, than: str | None) -> bool:
    mine, other = _order(version), _order(than) if than else None
    return mine is not None and other is not None and mine > other


def _order(version: str) -> tuple[int, ...] | None:
    match = _RELEASE.fullmatch(version)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def release_notice(installed: str, newest: str | None, standing: str = "") -> tuple[str | None, bool]:
    """What to tell someone running `installed`, or None when nothing; and whether to add the update line. Never
    one that goes backwards, except from a withdrawn release, where going back is the fix; never for a dev build."""
    if newest is None or installed == newest:
        return None, False
    if not _newer(installed, newest):
        return f"newest release: v{newest}", is_release(installed)
    line, hint = {
        "pending": (f"mintd {installed} is a prerelease; the newest release is v{newest}",
                    "nothing to do until it's promoted or withdrawn"),
        "withdrawn": (f"mintd {installed} was withdrawn; the newest release is v{newest}",
                      f"go back to v{newest} with the update line below"),
        "unreleased": (f"mintd {installed} is not released; the newest release is v{newest}",
                       "no published release has its tag, so no update is offered"),
    }.get(standing, (f"mintd {installed} is newer than the newest release, v{newest}",
                     "its own release couldn't be read, so no update is offered"))
    return f"{line}\n  hint: {hint}", standing == "withdrawn"
