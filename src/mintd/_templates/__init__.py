"""Scaffold rendering — vendored Jinja templates from legacy `mintd`.

Public API: ``render_scaffold(...)`` produces the file set for a typed
project. Tests can call ``render_template(...)`` directly when they only
need a single rendered string.
"""

from __future__ import annotations

from ._render import (
    InitNameInvalid,
    project_full_name,
    render_scaffold,
    render_template,
    validate_project_name,
)

__all__ = [
    "InitNameInvalid",
    "project_full_name",
    "render_scaffold",
    "render_template",
    "validate_project_name",
]
