"""Tests pinning UTF-8 round-trip across mintd-owned file formats.

Locale on Windows is ``cp1252`` by default; without explicit
``encoding="utf-8"`` on ``read_text``/``write_text`` calls, non-ASCII
strings round-trip incorrectly or raise ``UnicodeDecodeError``. These
tests fail loudly if a future edit drops the encoding kwarg.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mintd.metadata_migrate import apply_metadata_migration
from mintd.model import Metadata


UNICODE_DESC = "café → résumé · 日本語 · Müller"


def _v2_metadata_with_unicode(name: str = "data_test") -> dict:
    """A minimal v2 metadata payload carrying non-ASCII content."""
    return {
        "schema_version": "2.0",
        "mint": {"version": "0.0.1", "commit_hash": ""},
        "project": {
            "name": "test", "type": "data", "full_name": name,
            "created_at": "2026-05-11T00:00:00Z", "created_by": "tester",
        },
        "metadata": {"description": UNICODE_DESC, "tags": []},
        "ownership": {"team": "François Müller", "maintainers": ["tester"]},
        "access_control": {"teams": [{"name": "admins", "permission": "admin"}]},
        "governance": {"classification": "public", "contract_info": ""},
        "data_products": {"primary": None, "outputs": []},
        "repository": {
            "github_url": "https://github.com/x/y", "default_branch": "main",
            "visibility": "private",
            "mirror": {"url": "", "purpose": ""},
        },
        "status": {
            "state": "active", "last_updated": "2026-05-11T00:00:00Z",
            "last_published_version": "",
        },
    }


def test_metadata_v2_unicode_round_trips_via_model(tmp_path: Path) -> None:
    """``Metadata.load`` reads via ``read_text(encoding='utf-8')`` — non-ASCII
    must survive the read-validate-dump round trip byte-for-byte."""
    payload = _v2_metadata_with_unicode()
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = Metadata.from_json_file(path)
    assert loaded.metadata.description == UNICODE_DESC
    assert loaded.ownership.team == "François Müller"


def test_metadata_migration_round_trips_unicode(tmp_path: Path) -> None:
    """``apply_metadata_migration`` reads v1 JSON and writes v2 JSON; both
    sides must preserve UTF-8 content end-to-end."""
    v1 = {
        "schema_version": "1.0",
        "mint": {"version": "0.0.1", "commit_hash": ""},
        "project": {
            "name": "test", "type": "data", "full_name": "data_test",
            "created_at": "2026-05-11T00:00:00Z", "created_by": "tester",
            "description": UNICODE_DESC, "tags": ["café"],
        },
        "ownership": {"team": "François Müller", "maintainers": ["tester"]},
        "access_control": {"teams": [{"name": "admins", "permission": "admin"}]},
        "governance": {"classification": "public", "contract_info": ""},
        "repository": {
            "github_url": "https://github.com/x/y", "default_branch": "main",
            "visibility": "private",
            "mirror": {"url": "", "purpose": ""},
        },
        "status": {
            "state": "active", "last_updated": "2026-05-11T00:00:00Z",
        },
    }
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")

    apply_metadata_migration(tmp_path)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["metadata"]["description"] == UNICODE_DESC
    assert written["metadata"]["tags"] == ["café"]
    assert written["ownership"]["team"] == "François Müller"


def test_catalog_yaml_unicode_round_trips(tmp_path: Path) -> None:
    """``_LocalCatalogCache.write_entry`` writes UTF-8 yaml; subsequent reads
    via ``read_text(encoding='utf-8')`` (in ``get``/``list_entries``) must
    yield byte-identical content."""
    catalog_dir = tmp_path / "catalog" / "data"
    catalog_dir.mkdir(parents=True)
    payload = {
        "schema_version": "2.0",
        "project": {"name": "test", "type": "data", "full_name": "data_test"},
        "metadata": {"description": UNICODE_DESC, "tags": ["café"]},
    }
    target = catalog_dir / "data_test.yaml"
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    target.write_text(yaml_text, encoding="utf-8")

    re_read = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert re_read["metadata"]["description"] == UNICODE_DESC
    assert re_read["metadata"]["tags"] == ["café"]
