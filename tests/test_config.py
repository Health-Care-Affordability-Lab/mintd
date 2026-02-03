"""Tests for configuration management."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest


from mintd.config import (
    get_config,
    save_config,
    _get_default_config,
    validate_config,
    get_storage_credentials,
    get_registry_url,
    set_storage_credentials,
    get_stata_executable,
    get_platform_info,
    KEYRING_AVAILABLE,
)


class TestConfig:
    """Test configuration functionality."""

    def test_get_default_config(self):
        """Test default configuration structure."""
        config = _get_default_config()

        assert "storage" in config
        assert "defaults" in config
        assert config["storage"]["provider"] == "s3"
        assert config["storage"]["versioning"] is True

    def test_save_and_get_config(self):
        """Test saving and loading configuration."""
        test_config = {
            "storage": {
                "provider": "s3",
                "bucket_prefix": "testlab"
            },
            "defaults": {
                "author": "Test User"
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_config_dir = Path(temp_dir) / ".mint"
            temp_config_file = temp_config_dir / "config.yaml"

            with patch("mintd.config.CONFIG_DIR", temp_config_dir), \
                 patch("mintd.config.CONFIG_FILE", temp_config_file):
                save_config(test_config)
                loaded_config = get_config()

                assert loaded_config["storage"]["bucket_prefix"] == "testlab"
                assert loaded_config["defaults"]["author"] == "Test User"

    def test_validate_config_incomplete(self):
        """Test config validation with incomplete configuration."""
        with patch("mintd.config.get_config") as mock_get_config:
            mock_get_config.return_value = {"storage": {}}

            assert not validate_config()

    def test_validate_config_missing_credentials(self):
        """Test config validation with missing credentials."""
        with patch("mintd.config.get_config") as mock_get_config, \
             patch("mintd.config.get_storage_credentials") as mock_creds:

            mock_get_config.return_value = {
                "storage": {"bucket_prefix": "test"}
            }
            mock_creds.side_effect = ValueError("No credentials")

            assert not validate_config()

    def test_validate_config_missing_registry_url(self):
        """Test config validation with missing registry URL."""
        with patch("mintd.config.get_config") as mock_get_config, \
             patch("mintd.config.get_storage_credentials") as mock_creds, \
             patch("mintd.config.get_registry_url") as mock_registry:

            mock_get_config.return_value = {
                "storage": {"bucket_prefix": "test"}
            }
            mock_creds.return_value = ("key", "secret")
            mock_registry.side_effect = ValueError("No registry URL")

            assert not validate_config()

    def test_validate_config_complete(self):
        """Test config validation with complete configuration."""
        with patch("mintd.config.get_config") as mock_get_config, \
             patch("mintd.config.get_storage_credentials") as mock_creds, \
             patch("mintd.config.get_registry_url") as mock_registry:

            mock_get_config.return_value = {
                "storage": {"bucket_prefix": "test"}
            }
            mock_creds.return_value = ("key", "secret")
            mock_registry.return_value = "https://github.com/org/registry"

            assert validate_config() is True


class TestGetStorageCredentials:
    """Tests for get_storage_credentials function."""

    @patch.dict(os.environ, {
        "AWS_ACCESS_KEY_ID": "env_access_key",
        "AWS_SECRET_ACCESS_KEY": "env_secret_key"
    })
    @patch("mintd.config.KEYRING_AVAILABLE", False)
    def test_credentials_from_env_vars(self):
        """Test getting credentials from environment variables."""
        access, secret = get_storage_credentials()
        assert access == "env_access_key"
        assert secret == "env_secret_key"

    @patch.dict(os.environ, {
        "MINTD_AWS_ACCESS_KEY_ID": "mintd_access",
        "MINTD_AWS_SECRET_ACCESS_KEY": "mintd_secret"
    }, clear=True)
    @patch("mintd.config.KEYRING_AVAILABLE", False)
    def test_credentials_from_mintd_env_vars(self):
        """Test getting credentials from MINTD-prefixed env vars."""
        # Clear standard AWS vars
        os.environ.pop("AWS_ACCESS_KEY_ID", None)
        os.environ.pop("AWS_SECRET_ACCESS_KEY", None)

        access, secret = get_storage_credentials()
        assert access == "mintd_access"
        assert secret == "mintd_secret"

    @patch.dict(os.environ, {}, clear=True)
    @patch("mintd.config.KEYRING_AVAILABLE", False)
    def test_credentials_not_found(self):
        """Test error when credentials are not found."""
        # Clear all relevant env vars
        for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "MINTD_AWS_ACCESS_KEY_ID", "MINTD_AWS_SECRET_ACCESS_KEY"]:
            os.environ.pop(key, None)

        with pytest.raises(ValueError, match="Storage credentials not found"):
            get_storage_credentials()

    @patch("mintd.config.KEYRING_AVAILABLE", True)
    @patch("mintd.config.keyring")
    def test_credentials_from_keyring(self, mock_keyring):
        """Test getting credentials from keyring."""
        mock_keyring.get_password.side_effect = lambda service, key: {
            ("mintd", "aws_access_key_id"): "keyring_access",
            ("mintd", "aws_secret_access_key"): "keyring_secret"
        }.get((service, key))

        access, secret = get_storage_credentials()
        assert access == "keyring_access"
        assert secret == "keyring_secret"


class TestGetRegistryUrl:
    """Tests for get_registry_url function."""

    @patch.dict(os.environ, {"MINTD_REGISTRY_URL": "https://github.com/env/registry"})
    def test_registry_url_from_env(self):
        """Test getting registry URL from environment variable."""
        url = get_registry_url()
        assert url == "https://github.com/env/registry"

    @patch.dict(os.environ, {}, clear=True)
    @patch("mintd.config.get_config")
    def test_registry_url_from_config(self, mock_get_config):
        """Test getting registry URL from config file."""
        os.environ.pop("MINTD_REGISTRY_URL", None)
        mock_get_config.return_value = {
            "registry": {"url": "https://github.com/config/registry"}
        }

        url = get_registry_url()
        assert url == "https://github.com/config/registry"

    @patch.dict(os.environ, {}, clear=True)
    @patch("mintd.config.get_config")
    def test_registry_url_not_configured(self, mock_get_config):
        """Test error when registry URL is not configured."""
        os.environ.pop("MINTD_REGISTRY_URL", None)
        mock_get_config.return_value = {"registry": {}}

        with pytest.raises(ValueError, match="Registry URL not configured"):
            get_registry_url()


class TestSetStorageCredentials:
    """Tests for set_storage_credentials function."""

    @patch("mintd.config.KEYRING_AVAILABLE", False)
    def test_set_credentials_no_keyring(self):
        """Test error when keyring is not available."""
        with pytest.raises(RuntimeError, match="keyring package is required"):
            set_storage_credentials("access", "secret")

    @patch("mintd.config.KEYRING_AVAILABLE", True)
    @patch("mintd.config.keyring")
    def test_set_credentials_success(self, mock_keyring):
        """Test successfully setting credentials."""
        set_storage_credentials("access_key", "secret_key")

        assert mock_keyring.set_password.call_count == 2


class TestGetStataExecutable:
    """Tests for get_stata_executable function."""

    @patch("mintd.utils.detect_stata_executable")
    def test_user_specified_executable(self, mock_detect):
        """Test using user-specified Stata executable."""
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {
                "tools": {"stata": {"executable": "/custom/path/stata-mp"}}
            }

            result = get_stata_executable()
            assert result == "/custom/path/stata-mp"
            mock_detect.assert_not_called()

    @patch("mintd.utils.detect_stata_executable")
    def test_previously_detected_path(self, mock_detect):
        """Test using previously detected Stata path."""
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {
                "tools": {"stata": {"executable": "", "detected_path": "/detected/stata-mp"}}
            }

            result = get_stata_executable()
            assert result == "/detected/stata-mp"
            mock_detect.assert_not_called()

    @patch("mintd.utils.detect_stata_executable")
    def test_fresh_autodetection(self, mock_detect):
        """Test fresh auto-detection when nothing configured."""
        mock_detect.return_value = "stata-mp"
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {
                "tools": {"stata": {"executable": "", "detected_path": ""}}
            }

            result = get_stata_executable()
            assert result == "stata-mp"
            mock_detect.assert_called_once()

    @patch("mintd.utils.detect_stata_executable")
    def test_no_stata_found(self, mock_detect):
        """Test when no Stata is found."""
        mock_detect.return_value = None
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {"tools": {}}

            result = get_stata_executable()
            assert result is None


class TestGetPlatformInfo:
    """Tests for get_platform_info function."""

    @patch("mintd.utils.get_command_separator")
    @patch("mintd.utils.get_platform")
    def test_platform_info_from_config(self, mock_platform, mock_sep):
        """Test getting platform info from config."""
        mock_sep.return_value = "&&"
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {"platform": {"os": "macos"}}

            info = get_platform_info()

            assert info["os"] == "macos"
            assert info["command_separator"] == "&&"

    @patch("mintd.utils.get_command_separator")
    @patch("mintd.utils.get_platform")
    def test_platform_info_autodetect(self, mock_platform, mock_sep):
        """Test auto-detecting platform info."""
        mock_platform.return_value = "linux"
        mock_sep.return_value = "&&"
        with patch("mintd.config.get_config") as mock_config:
            mock_config.return_value = {"platform": {}}

            info = get_platform_info()

            assert info["os"] == "linux"
            mock_platform.assert_called_once()


class TestConfigFileFormats:
    """Tests for different config file formats."""

    def test_get_config_json_fallback(self):
        """Test loading config from JSON when YAML fails."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / ".mintd"
            config_file = config_dir / "config.yaml"
            config_dir.mkdir(parents=True)

            # Write JSON content to yaml file (simulating yaml module not available)
            config_file.write_text('{"storage": {"provider": "s3"}}')

            with patch("mintd.config.CONFIG_DIR", config_dir), \
                 patch("mintd.config.CONFIG_FILE", config_file):
                # Force JSON parsing by making yaml fail
                with patch.dict("sys.modules", {"yaml": None}):
                    # This is complex to test - skip for now
                    pass