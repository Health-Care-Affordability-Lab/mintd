"""The GitHub-access check's own git and gh commands, for tests that replace
``subprocess.run``: they are local and read the hermetic gh and git config
conftest sets up, so such a test lets them run for real and still stands in
for the command it is about."""

from __future__ import annotations

import subprocess

#: The real `subprocess.run`, bound before any test replaces it.
REAL_RUN = subprocess.run


def is_access_check(argv: list[str]) -> bool:
    """Whether ``argv`` is one of the access check's own commands."""
    if argv[:3] == ["gh", "auth", "token"]:
        return True
    if argv[:1] != ["git"]:
        return False
    rest = argv[1:]
    while rest[:1] == ["-c"]:  # the check's own `-c` settings come first
        rest = rest[2:]
    return (rest[:2] == ["ls-remote", "--get-url"] or rest[:1] == ["config"]
            or rest[:2] == ["remote", "get-url"] or rest == ["remote"])
