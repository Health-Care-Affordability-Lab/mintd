#!/usr/bin/env python3
"""Write a release's tag into its own install.sh and install.ps1: the one stamper.

release.yml's build runs `python3 scripts/stamp_install_scripts.py vX.Y.Z --out dist` at the repository's root. CI's
install checks, which come in a later change, will run the same script, so they test exactly what users get. Each script must hold its placeholder
line exactly once (`MINTD_TAG=""`, `$MintdTag = ""`), or nothing is written: an unstamped copy would install the
newest release instead of this one.

`--releases URL` also rewrites the scripts' one releases URL line, for those checks to install from a local
server; the release build never passes it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# (file, the placeholder line, the releases line's start)
SCRIPTS = (("install.sh", 'MINTD_TAG=""', "RELEASES="), ("install.ps1", '$MintdTag = ""', "$Releases = "))
_TAG = re.compile(r"v\d+\.\d+\.\d+(\.post\d+)?")


def stamp(text: str, name: str, placeholder: str, releases_start: str, tag: str, releases: str | None) -> str:
    line = "\n" + placeholder + "\n"
    if text.count(line) != 1:
        raise ValueError(f"{name}: the line {placeholder!r} must appear exactly once")
    text = text.replace(line, "\n" + placeholder.replace('""', f'"{tag}"') + "\n")
    if releases is not None:
        found = [ln for ln in text.split("\n") if ln.startswith(releases_start)]
        if len(found) != 1:
            raise ValueError(f"{name}: the line starting {releases_start!r} must appear exactly once")
        text = text.replace(found[0], f'{releases_start}"{releases}"')
    return text


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Write a release's tag into its install scripts.")
    parser.add_argument("tag")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--releases", help="CI only: the releases URL to install from")
    args = parser.parse_args(argv)
    if not _TAG.fullmatch(args.tag):
        sys.stderr.write(f"stamp_install_scripts: {args.tag!r} is not vX.Y.Z or vX.Y.Z.postN\n")
        return 1
    try:
        stamped = []
        for name, placeholder, releases_start in SCRIPTS:
            with open(name, encoding="utf-8", newline="") as f:  # line endings kept exactly
                text = f.read()
            stamped.append((name, stamp(text, name, placeholder, releases_start, args.tag, args.releases)))
    except ValueError as why:
        sys.stderr.write(f"stamp_install_scripts: {why}\n")
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in stamped:  # nothing is written unless both scripts stamped
        with open(args.out / name, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
