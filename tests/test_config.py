"""Tests for configuration management."""

import tempfile
from pathlib import Path
from unittest.mock import patch


from mintd.config import (
    get_config,
    save_config,
    _get_default_config,
    validate_config
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