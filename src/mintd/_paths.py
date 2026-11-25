"""Whether a path is inside the project, and what it is relative to the root.

The one place that answers it. Callers name how the path is read:

- ``recorded``: a POSIX string from a file (a pointer, ``dvc.lock``, a folder list,
  the catalog, an archive member). ``/`` is the only separator; a drive, a UNC prefix
  or a ``\\`` is outside, on POSIX too; ``..`` collapses as text; nothing on disk is read.
- ``lexical``: a native path, read as text by the OS's rules (on POSIX a folder named
  ``c:`` is a folder). For targets as dvc reads them, walk results, and dvc's own
  checkouts, which can be links into ``.dvc/cache/`` that following would misplace.
- ``follow``: both paths resolved, so ``..`` is taken after the links. For where a read
  or a write really lands.
- ``folder``: the folder resolved and the leaf's own name kept, so a link is named as the
  link, and a file under a linked folder is named where it lands.

Collapsing ``..`` as text and then following links is not a mode: where dvc asks both (a
dep as written, then its real file) the caller makes two calls. ``follow`` and ``folder``
take ``..`` where the OS does: after the links on POSIX, as text on Windows. Case is folded
only where the OS's path rules fold it (Windows); on POSIX it isn't, macOS's
case-insensitive disks included (``path_covers`` folds it everywhere).
"""

from __future__ import annotations

import errno
import os
import posixpath
from pathlib import Path, PureWindowsPath
from typing import Literal

__all__ = ["Read", "inside", "physical", "rel"]

Read = Literal["recorded", "lexical", "follow", "folder"]


def inside(root: Path, path: Path | str, *, read: Read, root_ok: bool = False,
           dotdot: Literal["collapse", "refuse"] = "collapse", wdir: str | None = None) -> str | None:
    """``path`` relative to ``root``, as a posix string, or None when it isn't inside
    ``root``. A recorded ``path`` is relative to ``root``; a native one, like ``root``,
    is read from the working folder when relative, as ``os.path`` reads it, so a caller
    joins its own fragment (``root / rel``) and a root-built path isn't joined twice.

    ``root_ok``: whether ``root`` itself (``"."``) counts as inside. ``dotdot="refuse"``:
    any ``..`` segment in ``path`` as written refuses it, even one that would stay inside.
    ``wdir`` (``read="recorded"`` only): the recorded working folder ``path`` is relative
    to; both strings are tested as recorded paths before they are joined. A link loop
    reads as outside, and so does a path holding a NUL."""
    if "\x00" in str(path) or (wdir is not None and "\x00" in wdir):
        return None
    if read == "recorded":
        return _recorded(str(path), root_ok=root_ok, dotdot=dotdot, wdir=wdir)
    if wdir is not None:
        raise ValueError("wdir is for read='recorded' only")
    p = Path(path)
    if dotdot == "refuse" and ".." in p.parts:
        return None
    # `absolute()` keeps `..` (`link/..` is not the root here) and asks for the working folder only for a
    # relative path, so absolute paths work from a deleted one, as `relative_to` did.
    if p.absolute() == Path(root).absolute():
        # The root itself, however a link names it, in every mode; a root that is a link loop is nowhere.
        return "." if root_ok and (read == "lexical" or _real(str(root)) is not None) else None
    base = os.path.abspath(root) if read == "lexical" else _real(str(root))
    there = os.path.abspath(p) if read == "lexical" else _real(str(p)) if read == "follow" else _real_folder(p)
    if there is None or base is None:
        return None
    try:
        return _verdict(Path(os.path.relpath(there, base)).as_posix(), root_ok)
    except ValueError:  # Windows: another drive
        return None


def rel(root: Path, path: Path | str, *, read: Read = "lexical", root_ok: bool = True) -> str:
    """:func:`inside` for a path that must be inside ``root`` (a walk result, a pointer
    mintd found under it): raises ValueError when it isn't."""
    found = inside(root, path, read=read, root_ok=root_ok)
    if found is None:
        raise ValueError(f"{path} is not inside {root}")
    return found


def _recorded(path: str, *, root_ok: bool, dotdot: str, wdir: str | None) -> str | None:
    parts = [path] if wdir is None else [wdir, path]
    for s in parts:
        if s.startswith("/") or "\\" in s or PureWindowsPath(s).drive:
            return None
        if dotdot == "refuse" and ".." in s.split("/"):
            return None
    return _verdict(posixpath.normpath(posixpath.join(*parts)), root_ok)


def _verdict(found: str, root_ok: bool) -> str | None:
    """A posix path relative to the root: itself (``.``) only with ``root_ok``, never above it."""
    if found == ".":
        return "." if root_ok else None
    return None if found == ".." or found.startswith("../") else found


def _real(p: str) -> str | None:
    """``p`` with every link resolved, or None for a link loop. A path that doesn't
    exist yet resolves as far as it can, as ``Path.resolve()`` does."""
    try:
        return os.path.realpath(p, strict=True)
    except OSError as exc:
        if exc.errno == errno.ELOOP or getattr(exc, "winerror", None) == 1921:  # Windows' ERROR_CANT_RESOLVE_FILENAME
            return None
        return os.path.realpath(p)


def physical(p: Path) -> Path:
    """``p`` where the OS reaches it: its folder with links and ``..`` resolved, since
    ``link/..`` is the folder above where ``link`` points, not the folder ``link`` is in,
    and its own name kept, so a link there is named as the link. ``read="folder"``."""
    p = p.absolute()  # its `..` kept
    if p.name in ("", ".."):
        return Path(os.path.realpath(p))
    return Path(os.path.realpath(p.parent)) / p.name


def _real_folder(full: Path) -> str | None:
    """:func:`physical`, or None when the folder it resolves holds a link loop."""
    full = full.absolute()
    return None if _real(str(full if full.name in ("", "..") else full.parent)) is None else str(physical(full))
