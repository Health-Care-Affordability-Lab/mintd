"""Fake `ArchiveOps` for slice-16 tests.

Records every `pack` call and writes a stub archive file so callers can
treat the returned path like a real `.tar.gz` for the purposes of
existence checks. Constructor accepts `raise_on_pack` to simulate
mid-pack failure (used to pin the append-only contract).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from mintd._archive_ops import ArchiveAlreadyExists


class PackCall(NamedTuple):
    src_dir: Path
    dest_archive: Path


class _FakeArchiveOps:
    """Implements `mintd._archive_ops.ArchiveOps` structurally."""

    def __init__(self, *, raise_on_pack: Exception | None = None) -> None:
        self.calls: list[PackCall] = []
        self._raise_on_pack = raise_on_pack

    def pack(self, src_dir: Path, dest_archive: Path) -> None:
        if self._raise_on_pack is not None:
            raise self._raise_on_pack
        if dest_archive.exists():
            raise ArchiveAlreadyExists(str(dest_archive))
        dest_archive.parent.mkdir(parents=True, exist_ok=True)
        dest_archive.write_bytes(b"fake-archive")
        self.calls.append(PackCall(src_dir=src_dir, dest_archive=dest_archive))

    def list_safe_members(self, archive_path: Path) -> list[str]:
        return ["_transfer_manifest.yaml"]
