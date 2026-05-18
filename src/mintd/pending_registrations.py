"""Tracks open pull requests for catalog registrations.

`GitCatalogClient.status(name)` consults this file before falling back to a
`gh pr list` query. Writes are atomic (temp file + rename) so a crashed mintd
process never leaves a half-written file.

State file path: `<work_dir>/.mintd_pending.json`, where `<work_dir>` is the
GitCatalogClient's local clone of the registry repo. One file per registry.

Schema:
    {
      "version": 1,
      "entries": [
        {
          "name": "data_alpha",
          "pr_number": 42,
          "kind": "register",
          "created_at": "2026-05-13T18:00:00+00:00"
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

PendingKind = Literal["register", "update"]


@dataclass(frozen=True)
class PendingRegistration:
    name: str
    pr_number: int
    kind: PendingKind
    created_at: datetime


class PendingRegistrations:
    """File-backed tracker for open registration/update PRs.

    Concurrent processes are not supported — mintd commands are single-user,
    single-shell. Atomic write protects against crash, not against races.
    """

    _SCHEMA_VERSION = 1

    def __init__(self, *, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def add(self, entry: PendingRegistration) -> None:
        items = self._read()
        # Replace any existing entry for the same name — newer wins.
        items = [e for e in items if e.name != entry.name]
        items.append(entry)
        self._write(items)

    def find(self, name: str) -> PendingRegistration | None:
        for e in self._read():
            if e.name == name:
                return e
        return None

    def remove(self, name: str) -> None:
        items = [e for e in self._read() if e.name != name]
        self._write(items)

    def all_entries(self) -> list[PendingRegistration]:
        return self._read()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def _read(self) -> list[PendingRegistration]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [
            PendingRegistration(
                name=item["name"],
                pr_number=int(item["pr_number"]),
                kind=item["kind"],
                created_at=datetime.fromisoformat(item["created_at"]),
            )
            for item in raw.get("entries", [])
        ]

    def _write(self, entries: list[PendingRegistration]) -> None:
        payload = {
            "version": self._SCHEMA_VERSION,
            "entries": [
                {
                    "name": e.name,
                    "pr_number": e.pr_number,
                    "kind": e.kind,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tempfile + rename.
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".mintd_pending.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_name, self._path)
        except Exception:
            # Best effort: clean up the temp file on failure.
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
