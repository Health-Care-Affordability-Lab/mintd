"""Shared CLI utilities and console setup."""

import click
from rich.console import Console

# Shared console instance
console = Console()


def abort_with_error(message: str, details: str = None) -> None:
    """Print error message and abort.

    Args:
        message: Main error message
        details: Optional additional details
    """
    console.print(f"❌ {message}", style="red")
    if details:
        console.print(f"   {details}", style="dim")
    raise click.Abort()


def print_success(message: str, details: str = None) -> None:
    """Print success message.

    Args:
        message: Main success message
        details: Optional additional details
    """
    console.print(f"✅ {message}", style="green")
    if details:
        console.print(f"   {details}", style="dim")


def print_warning(message: str, details: str = None) -> None:
    """Print warning message.

    Args:
        message: Main warning message
        details: Optional additional details
    """
    console.print(f"⚠️ {message}", style="yellow")
    if details:
        console.print(f"   {details}", style="dim")
