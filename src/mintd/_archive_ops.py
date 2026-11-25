"""Seam for tar.gz archive operations, in-process via stdlib `tarfile`.

Slice 16: produces and inspects .tar.gz transfer archives. Path-traversal
validation is the load-bearing guarantee — see `_validate_member_safe`.
This module guards against CVE-2007-4559 (tarfile path-traversal) by
refusing absolute-path members, `..` segments, and symlinks pointing
outside the archive at both `pack` and `list_safe_members` time.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from ._paths import inside
from .errors import MintdError
from typing import Protocol


class ArchiveError(MintdError):
    pass


class ArchiveAlreadyExists(ArchiveError):
    hint = "remove it or pass --output <path>"


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
        # resolved target escapes `src_dir` (a link to `src_dir` itself is fine).
        for p in src_dir.rglob("*"):
            if p.is_symlink():
                if inside(src_dir, p, read="follow", root_ok=True) is None:
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
    if inside(Path("."), name, read="recorded", root_ok=True, dotdot="refuse") is None:
        raise UnsafeArchiveMember(name)
    if member.issym() or member.islnk():
        link_target = member.linkname
        if inside(Path("."), link_target, read="recorded", root_ok=True, dotdot="refuse") is None:
            raise UnsafeArchiveMember(f"{name} -> {link_target}")
