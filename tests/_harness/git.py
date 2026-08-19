"""The suite's one `git` runner.

Merged from the five module-level `_git` copies that existed at `409139e`
(`conftest.py:18`, `test_enclave_pull_integration.py:34`,
`test_pre_units_journey.py:82`, `test_producer_integration.py:26`,
`test_registry_git_ops.py:36`), which had reconciled to three different
signatures. This form takes the widest of each: it returns ``stdout`` (only
`test_registry_git_ops.py`'s copy carried information back), ``cwd`` is
optional, and the committer identity is a **default** rather than a hardcode,
so a caller needing a different one passes ``ident=`` instead of growing a
sixth copy.

``tests/test_import_rescue.py:128``'s ``_git_repo`` is a different thing (a
repo builder, not a git runner) and is deliberately not merged here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Committer identity used by fixture repos, plus the one ambient setting that
#: reliably breaks a fixture commit. A default, not a hardcode — `-c` flags
#: supplied by a caller's own args come later on the argv and win.
#:
#: `commit.gpgsign = true` in a developer's global config makes every commit
#: here fail with `gpg failed to sign the data`; since five per-module copies
#: merged into this one runner, that single setting would take out every
#: harness-backed test at once. Turned off explicitly rather than by blanking
#: the whole global config, which would also blank what
#: `test_producer_commits_on_a_machine_with_no_git_identity` plants there.
IDENT: tuple[str, ...] = (
    "-c", "user.email=test@mintd",
    "-c", "user.name=test",
    "-c", "commit.gpgsign=false",
)


def _git(
    args: list[str],
    cwd: Path | None = None,
    *,
    ident: tuple[str, ...] = IDENT,
) -> str:
    """Run git, raise on non-zero, return stdout.

    Raises with git's own stderr rather than `CalledProcessError`'s bare exit
    code: `capture_output=True` swallows the message, so a fixture failure
    otherwise reads as `returned non-zero exit status 128` with no cause.
    """
    result = subprocess.run(
        ["git", *ident, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode})"
            f"{f' in {cwd}' if cwd else ''}: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout
