"""The error a seam raises for the user to read: ``cli.main`` renders it once."""

from __future__ import annotations


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
