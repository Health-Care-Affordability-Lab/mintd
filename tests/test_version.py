"""Single-source-of-truth wiring for the package version.

These pass only against an installed (editable) tree, since
``importlib.metadata.version`` reads ``.dist-info``/``.egg-info`` metadata.
"""

from __future__ import annotations

from importlib.metadata import version as pkg_version

import mintd


def test_dunder_version_matches_metadata() -> None:
    assert mintd.__version__ == pkg_version("mintd")


def test_version_not_static_literal() -> None:
    # Guards against re-introducing a hardcoded "0.0.1" literal.
    assert mintd.__version__ != ""
