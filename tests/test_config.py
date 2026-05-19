"""Tests for `mintd._config` — slice-1 ``Config.load()`` + slice-18 ``aws_profile_name``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mintd._config import Config, ConfigError, _default_config_path

FIXTURE = Path(__file__).parent / "fixtures" / "cli_config.yaml"


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path / "missing.yaml")
    assert cfg.registry_url is None
    assert cfg.cache_dir is None
    assert cfg.timeouts.fast == 30.0
    assert cfg.timeouts.transfer is None


def test_load_valid_yaml() -> None:
    cfg = Config.load(FIXTURE)
    assert cfg.registry_url == "https://example.com/registry.git"
    assert cfg.cache_dir == Path("/tmp/mintd-test-cache")
    assert cfg.timeouts.fast == 60.0


def test_load_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: : :::\n: not yaml :")
    with pytest.raises(ConfigError):
        Config.load(bad)


def test_load_invalid_schema_raises_config_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text('timeouts:\n  fast: "not a number"\n')
    with pytest.raises(ConfigError):
        Config.load(bad)


def test_legacy_dvc_timeout_key_raises_clear_error(tmp_path: Path) -> None:
    """Slice 25: dvc_timeout/git_timeout hard-removed; clear error points users
    at the new timeouts: block."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("dvc_timeout: 120.0\n")
    with pytest.raises(ConfigError) as exc:
        Config.load(legacy)
    assert "timeouts" in str(exc.value).lower()


def test_resolved_cache_dir_defaults_when_none() -> None:
    cfg = Config()
    assert cfg.resolved_cache_dir() == Path.home() / ".cache" / "mintd"


def test_env_var_overrides_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path))
    assert _default_config_path() == tmp_path / "config.yaml"


# --- slice 18: aws_profile_name --------------------------------------------

def test_aws_profile_name_mintd_exists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[mintd]\naws_access_key_id = 123\n")
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name == "mintd"


def test_aws_profile_name_only_default_exists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[default]\naws_access_key_id = 123\n")
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name is None


def test_aws_profile_name_no_credentials_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name is None
