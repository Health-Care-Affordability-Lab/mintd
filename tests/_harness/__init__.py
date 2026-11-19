"""Hermetic builders for the mintd test suite.

Promoted out of the individual test modules so a capability is written once.
Import from the module that owns it (`tests._harness.producer`, `.consumer`,
`.synthetic`, `.dvc`, `.git`); the fixtures are registered suite-wide by
`tests/conftest.py`.
"""


class HarnessError(Exception):
    """The harness could not build what a test asked for: setup broke, not the
    code under test. Not an AssertionError, so a test marked
    `xfail(raises=AssertionError)` reports it as a failure, not as XFAIL."""
