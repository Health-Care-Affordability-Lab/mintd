"""Main CLI entry point and root command group."""

import click

from .utils import console


@click.group()
@click.version_option(version="1.0.0")
def main():
    """mintd - Lab Project Scaffolding Tool"""
    pass
