"""mintd — lightweight data product framework for research labs."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("mintd")
except PackageNotFoundError:  # source / editable run with no installed dist metadata
    __version__ = "0.0.0+unknown"
