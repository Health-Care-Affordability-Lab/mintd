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

def test_aws_profile_name_mintd_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patched at default_credentials_path -- the home-FALLBACK branch's own
    # seam (and the conftest ratchet's boom, which this override replaces
    # hermetically). Path.home is no longer consulted: resolution goes
    # through the one shared_credentials_path chokepoint.
    creds = tmp_path / "credentials"
    creds.write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    from mintd import _aws_credentials

    monkeypatch.setattr(_aws_credentials, "default_credentials_path", lambda: creds)
    assert Config().aws_profile_name == "mintd"


def test_aws_profile_name_only_default_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "credentials"
    creds.write_text("[default]\naws_access_key_id = 123\n")
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    from mintd import _aws_credentials

    monkeypatch.setattr(_aws_credentials, "default_credentials_path", lambda: creds)
    assert Config().aws_profile_name is None


def test_aws_profile_name_no_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    from mintd import _aws_credentials

    monkeypatch.setattr(
        _aws_credentials, "default_credentials_path", lambda: tmp_path / "absent"
    )
    assert Config().aws_profile_name is None


# --- aws_profile_name honours AWS_SHARED_CREDENTIALS_FILE -------------------
# The AWS SDK (and therefore the boto3 inside DVC) resolves the credentials
# file from $AWS_SHARED_CREDENTIALS_FILE before falling back to
# ~/.aws/credentials. Detection must read the same file, or mintd and DVC
# disagree under an OS sandbox that fences $HOME (see
# notes/issues/issue-aws-profile-detection-ignores-shared-credentials-file.md).


def test_aws_profile_name_honours_aws_shared_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[mintd] lives only in the redirected file; $HOME has none at all."""
    home = tmp_path / "home"
    home.mkdir()
    redirected = tmp_path / "scratch-credentials"
    redirected.write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(redirected))
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name == "mintd"


def test_aws_profile_name_redirected_file_wins_over_home_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When set, the env var replaces ~/.aws/credentials outright (SDK
    semantics) — the home file is not consulted even when it has [mintd]."""
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[mintd]\naws_access_key_id = 123\n")
    redirected = tmp_path / "scratch-credentials"
    redirected.write_text("[default]\naws_access_key_id = 123\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(redirected))
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name is None


def test_aws_profile_name_shared_credentials_file_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """boto3 expanduser()s the env var; an unexpanded ~ must not defeat
    detection. expanduser reads $HOME (posix) / $USERPROFILE (windows), not
    Path.home, so redirect both."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "scratch-credentials").write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "~/scratch-credentials")
    assert Config().aws_profile_name == "mintd"


def test_aws_profile_name_empty_env_var_means_no_file_like_boto3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A set-but-empty $AWS_SHARED_CREDENTIALS_FILE means 'no file', like boto3.

    boto3 1.43.0 (measured 2026-08-26) treats the empty value as path '' and
    raises ProfileNotFound for an explicit profile — so falling back to home
    here would detect [mintd] and hand DVC's boto3 an AWS_PROFILE it cannot
    resolve. Necessity is proven by mutation M4 in the stage's table."""
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "credentials").write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "")
    with patch("pathlib.Path.home", return_value=home):
        assert Config().aws_profile_name is None


def test_aws_profile_name_non_utf8_credentials_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undecodable credentials file (e.g. UTF-16 from PowerShell
    Set-Content) must mean 'no profile', not a UnicodeDecodeError traceback
    out of every command that touches config.aws_profile_name."""
    creds = tmp_path / "creds"
    creds.write_bytes(b"\xff\xfe\x00binary[garbage")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    assert Config().aws_profile_name is None


def test_aws_profile_name_malformed_credentials_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Syntactically broken INI means 'no profile' (configparser.Error is
    caught); pins the other half of the except clause (mutation M6)."""
    creds = tmp_path / "creds"
    creds.write_text("[mintd\naws_access_key_id = x\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    assert Config().aws_profile_name is None


def test_aws_profile_name_unresolvable_tilde_user_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~nobody/...`` that expanduser cannot resolve means 'no profile'.

    boto3's SharedCredentialProvider.load() returns None for the identical
    value (measured 2026-08-29); Path.expanduser instead raised RuntimeError
    ('Could not determine home directory.') straight out of `mintd init`."""
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", "~no_such_user_xyz/credentials"
    )
    assert Config().aws_profile_name is None


def test_aws_profile_name_expands_env_vars_like_botocore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """botocore expandvars()s the env var before expanduser (its
    configloader does both); detection must too, or DVC's boto3 reads the
    very file mintd declined to detect."""
    creds = tmp_path / "creds"
    creds.write_text("[mintd]\naws_access_key_id = 123\n")
    monkeypatch.setenv("CREDDIR", str(tmp_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "$CREDDIR/creds")
    assert Config().aws_profile_name == "mintd"
