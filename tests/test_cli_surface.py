"""The CLI's surface matches `cli_surface.txt`: every command and whether one is required
under it, every flag and positional with its action, type, nargs, choices, required and
default, and which flags exclude each other.

A PR that changes any of these regenerates the file and commits it, so the change shows in
the diff, and the maintainers approve it (CODEOWNERS):

    MINTD_WRITE_SURFACE=1 pytest tests/test_cli_surface.py

Only `1` writes; without it the test only compares. Help text is left out, so rewording a
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
    if d is None or d is argparse.SUPPRESS:
        return ""
    if isinstance(d, (str, int, float, bool, list)):
        return f" default={d!r}"
    return f" default=Path({d.as_posix()!r})" if isinstance(d, PurePath) else " default=<computed>"


def _line(path: str, action: argparse.Action) -> str:
    name = ", ".join(action.option_strings) or f"<{action.dest}>"
    kind = type(action).__name__.lstrip("_").removesuffix("Action").lower()
    typ = "" if action.type is None else f" type={getattr(action.type, '__name__', '<computed>')}"
    nargs = "" if action.nargs is None else f" nargs={action.nargs}"
    choices = f" choices={','.join(map(str, action.choices))}" if action.choices else ""
    required = " required" if action.required and action.option_strings else ""
    return f"{path}  {name}  {kind}{typ}{nargs}{choices}{required}{_default(action)}"


def surface(parser: argparse.ArgumentParser, path: str = "mintd") -> list[str]:
    """Per command: its groups of flags that exclude each other, then one line per flag,
    positional or subcommand slot, in the parser's order, each subcommand followed by its own."""
    lines = [
        f"{path}  one of: {' | '.join('/'.join(a.option_strings) for a in g._group_actions)}"
        + (" required" if g.required else "")
        for g in parser._mutually_exclusive_groups
    ]
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            lines.append(f"{path}  <{action.dest}>  subcommand" + (" required" if action.required else ""))
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
        f"the CLI's commands or flags changed: run {REGENERATE} and commit tests/cli_surface.txt with the change; "
        "the maintainers approve this file (CODEOWNERS)"
    )


def _write_requested() -> bool:
    return os.environ.get("MINTD_WRITE_SURFACE") == "1"


def test_the_cli_surface_matches_its_snapshot() -> None:
    _check(SNAPSHOT, write=_write_requested())


def test_a_stale_snapshot_fails_and_is_left_alone_without_the_flag(tmp_path: Path) -> None:
    """Regenerating is an explicit step. Mutation: write the snapshot on every run -> red."""
    stale = tmp_path / "cli_surface.txt"
    stale.write_text("mintd gone\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"MINTD_WRITE_SURFACE=1 .*the maintainers approve this file \(CODEOWNERS\)"):
        _check(stale, write=False)
    assert stale.read_text(encoding="utf-8") == "mintd gone\n"


@pytest.mark.parametrize("value, writes", [("1", True), ("0", False), ("", False), ("true", False)])
def test_only_the_value_1_asks_for_a_rewrite(monkeypatch: pytest.MonkeyPatch, value: str, writes: bool) -> None:
    """Mutation: any non-empty value writes (MINTD_WRITE_SURFACE=0 rewrote the file) -> red."""
    monkeypatch.setenv("MINTD_WRITE_SURFACE", value)
    assert _write_requested() is writes
