"""The error a seam raises for the user to read: ``cli.main`` renders it once,
and the advice more than one verb shares."""

from __future__ import annotations

import re
import shlex

# Credentials a catalog URL may carry. An http(s) URL loses its whole userinfo
# (a token often sits there as the user); any other scheme loses only the
# password, so `ssh://git@host` keeps its login name.
# `\b` lets `git+https://TOKEN@` match; the password pattern starts only where a
# run of scheme characters starts (the lookbehind), so a long `a-b-c-...` token
# costs one pass, not one per letter.
# The userinfo stops at `#`, `?` and `,` too, so `https://host#frag@x` (no path,
# an @ later) is left alone rather than eaten.
_HTTP_USERINFO = re.compile(r"(?i)\b(https?://)[^/\s@'\"#?,]+@")
_PASSWORD = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://[^/\s:@'\"#?,]+):[^/\s@'\"#?,]+@")


def strip_userinfo(text: str) -> str:
    """``text`` with the credentials taken out of every URL in it. Everything
    mintd prints passes through this. A quote in the userinfo stops the match,
    so it never runs past a quoted URL or across compact-JSON fields; a
    password containing one is left as is."""
    if "://" not in text or "@" not in text:
        return text
    return _PASSWORD.sub(r"\1@", _HTTP_USERINFO.sub(r"\1", text))


def unreachable_repo_hint(repo_url: str) -> str:
    """What to do when a producer's repository can't be reached. One wording
    for every verb that meets it; the URL is quoted where it sits in a command,
    since the producer controls it."""
    from ._registry_git_ops import NETWORK_TOO, github_access_hint, github_owner, url_git_uses

    url = strip_userinfo(repo_url)
    moved = "if it moved, the catalog entry is out of date, so tell the producer"
    used = url_git_uses(repo_url)
    if github_owner(used):  # GitHub is reached through gh: say which account to use
        return f"could not reach the producer's repository {url}: {github_access_hint(used)}; {NETWORK_TOO}; {moved}"
    return (
        f"could not reach the producer's repository {url}: check your network and git access "
        f"(git ls-remote {shlex.quote(url)}); {moved}"
    )


class MintdError(Exception):
    """What failed (the message), what to do next (``hint``) and the exit code.

    A subclass may set ``hint`` or ``exit_code`` on the class; the constructor
    overrides them only when given.
    """

    hint: str | None = None
    exit_code: int = 1

    def __init__(self, message: str = "", hint: str | None = None, exit_code: int | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint
        if exit_code is not None:
            self.exit_code = exit_code
