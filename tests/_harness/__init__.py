"""Hermetic builders for the mintd test suite.

Promoted out of the individual test modules so a capability is written once.
Import from the module that owns it (`tests._harness.producer`, `.consumer`,
`.synthetic`, `.dvc`, `.git`); the fixtures are registered suite-wide by
`tests/conftest.py`. See `notes/mintd-check/PLAN-hermetic-harness.md`.
"""
