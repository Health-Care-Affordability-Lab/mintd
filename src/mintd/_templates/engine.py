"""Jinja2 environment for vendored mintdv2 templates.

``StrictUndefined`` makes missing context keys explode at render time
rather than silently producing empty strings — cheap insurance against
slice-19's binding-question risk (legacy templates referencing keys we
don't pass).
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined


_env = Environment(
    loader=PackageLoader("mintd", "files"),
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


def render_template(template_name: str, context: dict[str, object]) -> str:
    """Render ``template_name`` (e.g., ``"README_data.md.j2"``) with ``context``."""
    return _env.get_template(template_name).render(**context)
