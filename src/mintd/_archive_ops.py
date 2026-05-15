"""Subprocess seam for tar.gz archive operations.

Slice 16: produces and inspects .tar.gz transfer archives. Path-traversal
validation is the load-bearing guarantee — see `_validate_member_safe`.
This module guards against CVE-2007-4559 (tarfile path-traversal) by
refusing absolute-path members, `..` segments, and symlinks pointing
outside the archive at both `pack` and `list_safe_members` time.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Protocol


class ArchiveError(Exception):
    pass


class ArchiveAlreadyExists(ArchiveError):
    pass


class InvalidArchive(ArchiveError):
    pass


class UnsafeArchiveMember(ArchiveError):
    """Member would extract outside the target (CVE-2007-4559 family)."""


class ArchiveOps(Protocol):
    def pack(self, src_dir: Path, dest_archive: Path) -> None: ...
    def list_safe_members(self, archive_path: Path) -> list[str]: ...


class TarGzArchiveOps:
    """Default `ArchiveOps` implementation using stdlib `tarfile`."""

    def pack(self, src_dir: Path, dest_archive: Path) -> None:
        if dest_archive.exists():
            raise ArchiveAlreadyExists(str(dest_archive))
        dest_archive.parent.mkdir(parents=True, exist_ok=True)
        # Pre-pack symlink guard. We refuse to bundle a symlink whose
        # resolved target escapes `src_dir`. The `os.sep` suffix on the
        # comparison prefix prevents sibling-directory false positives
        # (e.g., `/tmp/a` vs `/tmp/ab`).
        src_dir_abs = str(src_dir.resolve())
        prefix = src_dir_abs + os.sep
        for p in src_dir.rglob("*"):
            if p.is_symlink():
                resolved = str(p.resolve())
                if resolved != src_dir_abs and not resolved.startswith(prefix):
                    raise UnsafeArchiveMember(
                        f"symlink {p} resolves outside src_dir"
                    )
        with tarfile.open(dest_archive, "w:gz") as tf:
            tf.add(src_dir, arcname=".")

    def list_safe_members(self, archive_path: Path) -> list[str]:
        members: list[str] = []
        with tarfile.open(archive_path, "r:gz") as tf:
            for m in tf.getmembers():
                _validate_member_safe(m)
                members.append(m.name)
        return members


def _validate_member_safe(member: tarfile.TarInfo) -> None:
    """Refuse absolute paths, `..` segments, and symlinks outside the archive."""
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        raise UnsafeArchiveMember(name)
    if member.issym() or member.islnk():
        link_target = member.linkname
        if link_target.startswith("/") or ".." in Path(link_target).parts:
            raise UnsafeArchiveMember(f"{name} -> {link_target}")
