"""The CLI's surface, every command, flag, positional and default, matches `cli_surface.txt`.

A PR that adds, renames or removes a command or flag, or changes its choices or
default, regenerates the file and commits it, so the change shows in the diff:

    MINTD_WRITE_SURFACE=1 pytest tests/test_cli_surface.py

Without that flag the test only compares. Help text is left out, so rewording a
help line needs no update.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePath

import pytest

from mintd.cli import _build_parser

SNAPSHOT = Path(__file__).with_name("cli_surface.txt")
REGENERATE = "MINTD_WRITE_SURFACE=1 pytest tests/test_cli_surface.py"


def _default(action: argparse.Action) -> str:
    d = action.default
    if d is None or d is argparse.SUPPRESS or isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return ""
    if isinstance(d, (str, int, float, bool, list)):
        return f" default={d!r}"
    return f" default=Path({d.as_posix()!r})" if isinstance(d, PurePath) else " default=<computed>"


def _line(path: str, action: argparse.Action) -> str:
    name = ", ".join(action.option_strings) or f"<{action.dest}>"
    kind = type(action).__name__.lstrip("_").removesuffix("Action").lower()
    nargs = "" if action.nargs is None else f" nargs={action.nargs}"
    choices = f" choices={','.join(map(str, action.choices))}" if action.choices else ""
    required = " required" if action.required and action.option_strings else ""
    return f"{path}  {name}  {kind}{nargs}{choices}{required}{_default(action)}"


def surface(parser: argparse.ArgumentParser, path: str = "mintd") -> list[str]:
    """One line per command, then one per flag or positional under it, in the parser's order."""
    lines = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                lines.append(f"{path} {name}")
                lines += surface(sub, f"{path} {name}")
            continue
        lines.append(_line(path, action))
    return lines


def _check(snapshot: Path, *, write: bool) -> None:
    got = "\n".join(surface(_build_parser())) + "\n"
    if write:
        snapshot.write_text(got, encoding="utf-8")
    assert got == snapshot.read_text(encoding="utf-8"), (
        f"the CLI's commands or flags changed: run {REGENERATE} and commit tests/cli_surface.txt with the change"
    )


def test_the_cli_surface_matches_its_snapshot() -> None:
    _check(SNAPSHOT, write=bool(os.environ.get("MINTD_WRITE_SURFACE")))


def test_a_stale_snapshot_fails_and_is_left_alone_without_the_flag(tmp_path: Path) -> None:
    """Regenerating is an explicit step. Mutation: write the snapshot on every run -> red."""
    stale = tmp_path / "cli_surface.txt"
    stale.write_text("mintd gone\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="MINTD_WRITE_SURFACE=1"):
        _check(stale, write=False)
    assert stale.read_text(encoding="utf-8") == "mintd gone\n"
