"""CLI package for mintd - modular command groups.

This package provides the command-line interface for mintd,
split into focused modules for better maintainability.
"""

# Import main entry point first
from .main import main

# Import all command groups to register them with main
from . import config_cmd  # noqa: F401
from . import create  # noqa: F401
from . import data  # noqa: F401
from . import enclave  # noqa: F401
from . import manifest  # noqa: F401
from . import registry_cmd  # noqa: F401
from . import templates  # noqa: F401
from . import update  # noqa: F401

# Register custom template commands
from .create import register_custom_commands

register_custom_commands()

__all__ = ["main"]
