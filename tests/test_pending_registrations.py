"""Tests for the open-PR tracker."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from mintd.pending_registrations import PendingRegistration, PendingRegistrations


def _make(name: str = "data_alpha", pr: int = 42) -> PendingRegistration:
    return PendingRegistration(
        name=name,
        pr_number=pr,
        kind="register",
        created_at=datetime(2026, 5, 13, 18, 0, 0, tzinfo=timezone.utc),
    )


def test_add_then_find_round_trips(tmp_path: Path) -> None:
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    entry = _make()
    p.add(entry)
    found = p.find("data_alpha")
    assert found == entry


def test_find_returns_none_for_unknown(tmp_path: Path) -> None:
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    assert p.find("nope") is None


def test_add_replaces_existing_for_same_name(tmp_path: Path) -> None:
    """A second add() with the same name overwrites the first — the newer
    PR is the live one (e.g., the first PR was closed and a new one opened)."""
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    p.add(_make(name="data_alpha", pr=42))
    p.add(_make(name="data_alpha", pr=43))
    found = p.find("data_alpha")
    assert found is not None
    assert found.pr_number == 43
    assert len(p.all_entries()) == 1


def test_remove_idempotent(tmp_path: Path) -> None:
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    p.add(_make())
    p.remove("data_alpha")
    p.remove("data_alpha")  # second remove is a no-op
    assert p.find("data_alpha") is None
    assert p.all_entries() == []


def test_list_returns_all_entries(tmp_path: Path) -> None:
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    p.add(_make(name="a", pr=1))
    p.add(_make(name="b", pr=2))
    p.add(_make(name="c", pr=3))
    assert {e.name for e in p.all_entries()} == {"a", "b", "c"}


def test_file_has_versioned_schema(tmp_path: Path) -> None:
    """The on-disk format declares a version so future migrations can fan out
    from one parser without ambiguity."""
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    p.add(_make())
    raw = json.loads((tmp_path / ".mintd_pending.json").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert isinstance(raw["entries"], list)


def test_atomic_write_no_temp_files_on_success(tmp_path: Path) -> None:
    """After a successful add(), no temp file remains in the parent dir."""
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    p.add(_make())
    p.add(_make(name="b", pr=2))
    leftovers = [f for f in tmp_path.iterdir() if f.name.startswith(".mintd_pending.") and f.suffix == ".tmp"]
    assert leftovers == []


def test_empty_when_file_missing(tmp_path: Path) -> None:
    """Reading before any write returns an empty list, not an error."""
    p = PendingRegistrations(path=tmp_path / ".mintd_pending.json")
    assert p.all_entries() == []
    assert p.find("any") is None


def test_parent_dirs_created_on_first_write(tmp_path: Path) -> None:
    """When `path` points into a non-existent subdirectory, add() creates it."""
    p = PendingRegistrations(path=tmp_path / "nested" / "dir" / ".mintd_pending.json")
    p.add(_make())
    assert (tmp_path / "nested" / "dir" / ".mintd_pending.json").exists()
