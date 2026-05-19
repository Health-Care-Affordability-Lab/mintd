"""Tests for ``mintd.config_ops`` — slice 21 config CLI plumbing."""

from __future__ import annotations

import io
import json as _json
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from mintd._config import Config, ConfigError
from mintd.config_ops import (
    ValidationStep,
    apply_from_file,
    apply_migrate_v1,
    apply_set_updates,
    interactive_setup,
    migrate_v1_to_v2,
    parse_set_pair,
    render_config,
    render_validation,
    validate_config,
)


# --- render (2) -----------------------------------------------------------


def test_render_config_yaml_omits_none() -> None:
    cfg = Config(registry_url="x")
    out = render_config(cfg)
    assert "cache_dir" not in out
    assert out.startswith("registry_url: x")
    # Non-None nested defaults still rendered.
    assert "timeouts:" in out
    assert "fast: 30.0" in out


def test_render_config_json() -> None:
    cfg = Config(registry_url="x")
    out = render_config(cfg, json_out=True)
    data = _json.loads(out)
    assert data["registry_url"] == "x"
    # render_config uses exclude_none; transfer=None is omitted.
    assert data["timeouts"] == {"fast": 30.0}


# --- setup --set (4) ------------------------------------------------------


def test_apply_set_writes_atomic(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text("author: someone\n")
    cfg = apply_set_updates(p, [("registry_url", "https://foo")])
    written = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert written["registry_url"] == "https://foo"
    assert written["author"] == "someone"
    assert cfg.registry_url == "https://foo"
    assert cfg.author == "someone"
    # Tmp file removed after atomic rename.
    assert not (tmp_path / "cfg.yaml.tmp").exists()


def test_apply_set_validates_types(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    with pytest.raises(ConfigError):
        # author is str|None; non-string types like a list would fail —
        # but parse_set_pair returns strings, so use unknown_key for type-class failure.
        apply_set_updates(p, [("totally_bogus_field", "x")])


def test_apply_set_rejects_unknown_key(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    with pytest.raises(ConfigError, match="unknown config keys"):
        apply_set_updates(p, [("bogus", "1")])


def test_apply_set_multiple_pairs_including_url(tmp_path: Path) -> None:
    """Pins the ``split('=', 1)`` behavior — URL values with `:` and `/` survive."""
    p = tmp_path / "cfg.yaml"
    cfg = apply_set_updates(
        p,
        [
            ("registry_url", "https://example.com/r.git"),
            ("cache_dir", "/tmp/c"),
            ("author", "alice"),
        ],
    )
    assert cfg.registry_url == "https://example.com/r.git"
    assert cfg.cache_dir == Path("/tmp/c")
    assert cfg.author == "alice"

    # Empty value clears an optional field.
    cfg2 = apply_set_updates(p, [("registry_url", "")])
    assert cfg2.registry_url is None


# --- setup --from FILE (2) -----------------------------------------------


def test_apply_from_file_replaces_wholesale(tmp_path: Path) -> None:
    target = tmp_path / "cfg.yaml"
    target.write_text("author: someone\nregistry_url: old\n")
    source = tmp_path / "new.yaml"
    source.write_text("registry_url: new\n")
    cfg = apply_from_file(target, str(source))
    # `author` reverts to default (None) — wholesale replace, not merge.
    assert cfg.author is None
    assert cfg.registry_url == "new"


def test_apply_from_file_malformed_yaml_raises(tmp_path: Path) -> None:
    target = tmp_path / "cfg.yaml"
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: : :::")
    with pytest.raises(ConfigError):
        apply_from_file(target, str(bad))


def test_apply_from_stdin_success_and_tty_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stdin mode: pipe → ok; TTY → ConfigError."""
    target = tmp_path / "cfg.yaml"
    monkeypatch.setattr("sys.stdin", io.StringIO("registry_url: piped\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    cfg = apply_from_file(target, None)
    assert cfg.registry_url == "piped"
    assert "registry_url: piped" in target.read_text(encoding="utf-8")

    # When stdin is a TTY, refuse to block on user input.
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    with pytest.raises(ConfigError, match="stdin"):
        apply_from_file(target, None)


# --- validate (4) ---------------------------------------------------------


def test_validate_schema_only_when_config_invalid(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("timeouts:\n  fast: oranges\n")
    steps = validate_config(p)
    assert [s.name for s in steps] == ["schema", "aws_profile", "s3"]
    assert steps[0].status == "fail"
    assert steps[1].status == "skipped"
    assert steps[2].status == "skipped"


def test_validate_reports_aws_profile_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    steps = validate_config(cfg)
    profile_step = next(s for s in steps if s.name == "aws_profile")
    assert profile_step.status == "ok"
    assert "mintd" in profile_step.message


def test_validate_s3_head_bucket_success(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="good-bucket")
        steps = validate_config(cfg, bucket="good-bucket")
    s3_step = next(s for s in steps if s.name == "s3")
    assert s3_step.status == "ok"
    assert "200" in s3_step.message


def test_validate_s3_head_bucket_failure(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    with mock_aws():
        # Don't create the bucket; head_bucket should report failure.
        steps = validate_config(cfg, bucket="missing-bucket")
    s3_step = next(s for s in steps if s.name == "s3")
    assert s3_step.status == "fail"
    assert "missing-bucket" in s3_step.message


# --- helpers --------------------------------------------------------------


def test_parse_set_pair_handles_url_with_equals_in_value() -> None:
    key, value = parse_set_pair("registry_url=https://x/y?token=abc")
    assert key == "registry_url"
    assert value == "https://x/y?token=abc"


def test_parse_set_pair_rejects_missing_equals() -> None:
    with pytest.raises(ConfigError):
        parse_set_pair("no-equals-here")


def test_render_validation_exit_code() -> None:
    steps = [
        ValidationStep(name="schema", status="ok", message="m"),
        ValidationStep(name="aws_profile", status="ok", message="m"),
        ValidationStep(name="s3", status="fail", message="bad"),
    ]
    text, code = render_validation(steps, json_out=False)
    assert code == 1
    assert "✗ s3" in text
    assert "✓ schema" in text
    json_text, json_code = render_validation(steps, json_out=True)
    obj = _json.loads(json_text)
    assert set(obj.keys()) == {"schema", "aws_profile", "s3"}
    # Pin the per-step shape: {status, message}, not a bare string.
    assert obj["s3"] == {"status": "fail", "message": "bad"}
    assert obj["schema"]["status"] == "ok"
    assert json_code == 1


# --- coverage gaps from review ---------------------------------------------


def test_apply_set_dry_run_does_not_write(tmp_path: Path) -> None:
    """--dry-run validates but does not touch disk."""
    p = tmp_path / "cfg.yaml"
    cfg = apply_set_updates(p, [("registry_url", "https://x")], write=False)
    assert cfg.registry_url == "https://x"
    assert not p.exists()


def test_apply_set_dry_run_still_validates(tmp_path: Path) -> None:
    """--dry-run with invalid input still raises before any write attempt."""
    p = tmp_path / "cfg.yaml"
    with pytest.raises(ConfigError):
        apply_set_updates(p, [("unknown_field_xyz", "oranges")], write=False)
    assert not p.exists()


def test_apply_from_file_dry_run_does_not_write(tmp_path: Path) -> None:
    target = tmp_path / "cfg.yaml"
    source = tmp_path / "src.yaml"
    source.write_text("registry_url: x\n")
    cfg = apply_from_file(target, str(source), write=False)
    assert cfg.registry_url == "x"
    assert not target.exists()


def test_apply_from_file_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    """A top-level list (or string) in the --from source is rejected
    with a clear error, rather than crashing Pydantic with confusing
    'list has no items() method' tracebacks."""
    target = tmp_path / "cfg.yaml"
    bad = tmp_path / "bad.yaml"
    bad.write_text("[1, 2, 3]\n")
    with pytest.raises(ConfigError, match="mapping"):
        apply_from_file(target, str(bad))


def test_apply_set_rejects_non_mapping_existing_config(tmp_path: Path) -> None:
    """If the existing config file is corrupted (top-level non-mapping),
    apply_set_updates refuses to merge rather than crashing mid-flight."""
    p = tmp_path / "cfg.yaml"
    p.write_text("just a string\n")
    with pytest.raises(ConfigError, match="mapping"):
        apply_set_updates(p, [("registry_url", "x")])


def test_validate_happy_path_no_bucket_skips_s3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case: schema ok + aws_profile ok + s3 skipped (no bucket)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    steps = validate_config(cfg)
    assert [s.status for s in steps] == ["ok", "ok", "skipped"]
    s3_step = next(s for s in steps if s.name == "s3")
    assert "--bucket" in s3_step.message


def test_apply_set_resolves_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When config_path=None, apply_set_updates writes to MINTD_CONFIG_DIR
    (verifies the _resolve_path / _default_config_path integration)."""
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path))
    cfg = apply_set_updates(None, [("registry_url", "https://x")])
    assert cfg.registry_url == "https://x"
    assert (tmp_path / "config.yaml").exists()


# --- interactive setup + v1 migration hint ---------------------------------


def test_apply_from_file_v1_config_surfaces_migration_hint(tmp_path: Path) -> None:
    """A legacy v1 mintd config has top-level keys like `registry`, `storage`,
    `tools`. The unknown-key error must mention v1→v2 migration so users
    aren't stuck."""
    target = tmp_path / "cfg.yaml"
    v1 = tmp_path / "v1.yaml"
    v1.write_text(
        "registry:\n  url: https://example.com/r.git\n"
        "storage:\n  bucket_prefix: foo\n"
        "tools:\n  stata:\n    executable: ''\n"
    )
    with pytest.raises(ConfigError, match="v1") as exc:
        apply_from_file(target, str(v1))
    # The hint should mention the v2 fields the user can seed via --set.
    assert "registry_url" in str(exc.value)


def _scripted_prompt(*values: str):
    """Return a prompt_fn that yields ``values`` in order, then empty strings
    indefinitely. Lets tests script only the prompts they care about without
    breaking when Config grows fields."""
    import itertools

    seq = itertools.chain(iter(values), itertools.repeat(""))
    return lambda _msg: next(seq)


def test_interactive_setup_walks_each_field(tmp_path: Path) -> None:
    """The wizard prompts for every Config field in order; empty input keeps
    the current value; a typed value updates it."""
    p = tmp_path / "cfg.yaml"
    p.write_text("author: existing-author\n")

    cfg = interactive_setup(
        p,
        prompt_fn=_scripted_prompt("https://example.com/r.git"),
    )
    assert cfg.registry_url == "https://example.com/r.git"
    assert cfg.author == "existing-author"          # carried over from existing
    written = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert written["registry_url"] == "https://example.com/r.git"


def test_interactive_setup_dry_run_does_not_write(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    cfg = interactive_setup(p, write=False, prompt_fn=_scripted_prompt("https://x"))
    assert cfg.registry_url == "https://x"
    assert not p.exists()


def test_interactive_setup_strips_v1_keys_from_existing(tmp_path: Path) -> None:
    """If the existing file is a v1 config, the wizard drops the unknown
    top-level keys before prompting, so the user isn't blocked by stale
    v1 fields that v2 doesn't model."""
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "defaults:\n  author: Maurice\n"
        "registry:\n  url: https://old.example/r.git\n"
        "registry_url: https://new.example/r.git\n"
    )
    cfg = interactive_setup(p, prompt_fn=_scripted_prompt())
    assert cfg.registry_url == "https://new.example/r.git"
    written = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "defaults" not in written
    assert "registry" not in written


def test_interactive_setup_abort_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"

    def kbi(_msg: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(ConfigError, match="aborted"):
        interactive_setup(p, prompt_fn=kbi)
    assert not p.exists()


# --- v1 → v2 migration -----------------------------------------------------


def test_migrate_v1_to_v2_full_mapping() -> None:
    """Every v1 field with a v2 equivalent is mapped; unmapped fields drop."""
    v1 = {
        "defaults": {"author": "Maurice", "organization": "Lab"},
        "platform": {"os": "macos"},
        "registry": {
            "url": "https://github.com/x/r",
            "org": "x",
            "admin_team": "admins",
            "researcher_team": "all",
            "default_branch": "main",
        },
        "storage": {
            "endpoint": "https://s3.wasabisys.com",
            "bucket_prefix": "lab-bucket",
            "region": "us-east-1",
            "cache_type": "reflink",
            "versioning": True,
            "provider": "s3",
        },
        "tools": {
            "github_cli": {"installed": True},
            "stata": {"detected_path": "stata-mp", "executable": ""},
        },
    }
    v2 = migrate_v1_to_v2(v1)
    assert v2 == {
        "author": "Maurice",
        "organization": "Lab",
        "registry_url": "https://github.com/x/r",
        "registry_org": "x",
        "admin_team": "admins",
        "researcher_team": "all",
        "storage_endpoint": "https://s3.wasabisys.com",
        "storage_bucket_prefix": "lab-bucket",
        "stata_executable": "stata-mp",
    }


def test_migrate_v1_already_v2_fields_pass_through() -> None:
    """If the source already has v2-shaped keys, preserve them (don't drop)."""
    v1 = {"registry_url": "https://new", "author": "alice"}
    assert migrate_v1_to_v2(v1) == {
        "registry_url": "https://new",
        "author": "alice",
    }


def test_migrate_v1_stata_falls_back_to_executable() -> None:
    """When `detected_path` is empty, `executable` is used."""
    v1 = {"tools": {"stata": {"detected_path": "", "executable": "StataMP.exe"}}}
    assert migrate_v1_to_v2(v1) == {"stata_executable": "StataMP.exe"}


def test_apply_migrate_v1_writes_translated_config(tmp_path: Path) -> None:
    src = tmp_path / "v1.yaml"
    src.write_text(
        "registry:\n  url: https://example.com/r.git\n  org: lab\n"
        "storage:\n  endpoint: https://s3.wasabisys.com\n"
    )
    target = tmp_path / "cfg.yaml"
    cfg = apply_migrate_v1(target, str(src))
    assert cfg.registry_url == "https://example.com/r.git"
    assert cfg.registry_org == "lab"
    assert cfg.storage_endpoint == "https://s3.wasabisys.com"
    assert "registry:" not in target.read_text(encoding="utf-8")  # v1 shape NOT preserved
    assert "registry_url: https://example.com/r.git" in target.read_text(encoding="utf-8")


def test_apply_migrate_v1_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        apply_migrate_v1(tmp_path / "cfg.yaml", str(tmp_path / "missing.yaml"))


def test_apply_migrate_v1_dry_run_does_not_write(tmp_path: Path) -> None:
    src = tmp_path / "v1.yaml"
    src.write_text("registry:\n  url: https://x\n")
    target = tmp_path / "cfg.yaml"
    cfg = apply_migrate_v1(target, str(src), write=False)
    assert cfg.registry_url == "https://x"
    assert not target.exists()
