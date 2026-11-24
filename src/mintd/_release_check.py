"""Which release `releases/latest` names, so `mintd --version` can say when
this install is behind it, or newer than it.

- It reads where `releases/latest` redirects rather than calling the GitHub
  API: no token, no rate limit, and `releases/latest` never names a
  prerelease, so "newest" means the newest release the maintainer promoted.
  One HEAD request; the redirect is not followed, since its Location header
  is the answer (urllib would follow it as a GET of the whole page).
- "Behind" is "not equal". A dev build never equals a release, so it is
  always nudged, which is accurate.
- Silent when anything fails. The lookup gets 2 seconds in all, DNS included:
  the socket timeout doesn't cover name lookup, so it runs on a daemon thread
  that is abandoned at the deadline.
- Off when MINTD_NO_UPDATE_CHECK=1, or when CI is set (any non-empty value,
  as GitHub Actions and most CI services set it): no request, no notice.
- At most one lookup a day per install: the answer, or the failure, is kept
  in a file in the cache folder with the version it was looked up for, so an
  upgrade asks again at once. A cache folder that can't be written or
  reached means no lookup at all, since it would otherwise happen on every
  run. Any filesystem error means "unknown", never a crash of --version.

This is mintd's only HTTP client of its own; ruff's banned-api rule lets this
module, and no other, import urllib.request.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._atomic import _atomic_write_text

LATEST_URL = "https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest"
# It redirects to .../releases/tag/vX.Y.Z, or to .../releases when none is promoted.
# At most 9 digits a part, so int() never meets a number too long to convert.
_TAG = re.compile(r"/releases/tag/v(\d{1,9}\.\d{1,9}\.\d{1,9}(?:\.post\d{1,9})?)$")
_RELEASE = re.compile(r"(\d{1,9})\.(\d{1,9})\.(\d{1,9})(?:\.post(\d{1,9}))?")
_DAY = 86400.0


class _KeepRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # not followed: urllib then raises HTTPError carrying the Location


def _fetch(url: str, timeout: float) -> str:
    """The version `url` redirects to, or "" on any failure or none."""
    found = [""]

    def run() -> None:
        try:
            build_opener(_KeepRedirect).open(Request(url, method="HEAD"), timeout=timeout).close()
            return  # not a redirect
        except HTTPError as e:
            match = _TAG.search(e.headers.get("Location", ""))
        except Exception:  # offline, DNS, TLS: all mean "unknown"
            return
        if match:
            found[0] = match[1]

    thread = threading.Thread(target=run, name=f"mintd-release-check {url}", daemon=True)
    thread.start()
    thread.join(timeout)
    return found[0]


def is_release(version: str) -> bool:
    """A promoted release's version, not a dev or local build."""
    return _order(version) is not None


def newest_release(cache_dir: Path, installed: str, url: str = LATEST_URL, timeout: float = 2.0) -> str | None:
    """The newest promoted release, e.g. "0.12.0", or None when unknown or turned off."""
    if os.environ.get("MINTD_NO_UPDATE_CHECK") == "1" or os.environ.get("CI"):
        return None
    try:
        return _lookup(cache_dir, installed, url, timeout)
    except OSError:  # e.g. a parent of the cache folder that can't be entered: unknown, never a crash
        return None


def _lookup(cache_dir: Path, installed: str, url: str, timeout: float) -> str | None:
    cache = cache_dir / "newest-release"  # "<newest>\n<the install it was looked up for>\n"
    with contextlib.suppress(OSError, ValueError):  # ValueError: bytes that aren't UTF-8
        if 0 <= time.time() - cache.stat().st_mtime < _DAY:  # a future date is stale too
            newest, _, checked_for = cache.read_text(encoding="utf-8").partition("\n")
            if checked_for.strip() == installed:  # another install, e.g. an upgrade, asks again
                return newest if is_release(newest) else None
    with contextlib.suppress(OSError):
        cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir.is_dir() and os.access(cache_dir, os.W_OK)):  # a file there passes os.access
        return None  # the answer couldn't be kept, so every run would ask again
    newest = _fetch(url, timeout)
    with contextlib.suppress(OSError):
        _atomic_write_text(cache, f"{newest}\n{installed}\n")  # "" keeps a failure: one wait a day offline
    return newest or None


def _newer(version: str, than: str | None) -> bool:
    mine, other = _order(version), _order(than) if than else None
    return mine is not None and other is not None and mine > other


def _order(version: str) -> tuple[int, ...] | None:
    match = _RELEASE.fullmatch(version)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def release_notice(installed: str, newest: str | None) -> str | None:
    """What to tell someone running `installed`, or None when nothing."""
    if newest is None or installed == newest:
        return None
    if _newer(installed, newest):
        return f"mintd {installed} was withdrawn or is not released yet; the newest release is v{newest}"
    return f"newest release: v{newest}"
