"""The subprocess environment for invoking git.

Twin of ``_dvc_invoke.dvc_env``: one helper, applied at every spawn site, so
the thing mintd reads back is the thing mintd expects to read.
"""

from __future__ import annotations

import os


#: Variables that change what every pathspec means. git sets
#: ``GIT_LITERAL_PATHSPECS`` for hooks and aliases run under
#: ``--literal-pathspecs`` (Magit passes it on every call), and literal mode
#: switches off the ``:(top,glob)`` / ``:(literal)`` magic the seams rely on:
#: the pointer check matched nothing and an import rollback dropped tracked
#: index entries. Unset, git's defaults apply.
_PATHSPEC_ENV = frozenset(
    {"GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"}
)


def git_env() -> dict[str, str]:
    """Return the parent env with git's message language pinned to English.

    git translates its own diagnostics. On a machine whose locale is not
    English, ``fatal:`` is ``Schwerwiegend:`` (de), ``致命的:`` (ja), and so on
    -- verified on this machine with git 2.48.1:

        $ LC_ALL=de_DE.UTF-8 git clone /tmp/nope.git
        Schwerwiegend: Repository '/tmp/nope.git' existiert nicht

    mintd reads those diagnostics rather than just printing them: `check`'s
    ``_git_error_summary`` picks git's ``fatal:`` line out of a clone's stderr
    so the user sees the cause instead of the ``Cloning into '<temp cache
    path>'...`` progress line, and ``_producer_git_ops._classify_stderr`` maps
    git's wording onto the typed reasons the CLI renders. Both read English.
    Left to inherit the user's locale, they silently stop matching and every
    diagnosis degrades to the fallback -- on a translated machine, with a green
    test suite everywhere else.

    ``LC_ALL`` because it outranks ``LC_MESSAGES`` and ``LANG``, so a user who
    exports it would otherwise win; ``LANGUAGE`` emptied because gettext
    consults it ahead of all of them. This changes what the CHILD emits, never
    how Python decodes it -- ``subprocess(text=True)`` decodes with the
    parent's preferred encoding either way.

    Not ``C.UTF-8``: macOS does not ship it.

    This is deliberately not user-overridable. A translated diagnostic is not a
    feature mintd offers, it is a parse failure; the user's own git is
    untouched.
    """
    env = {k: v for k, v in os.environ.items() if k not in _PATHSPEC_ENV}
    return {**env, "LC_ALL": "C", "LANGUAGE": ""}
