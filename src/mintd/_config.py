"""CLI-layer config loader.

Reads ``~/.config/mintd/config.yaml`` (or ``$MINTD_CONFIG_DIR/config.yaml``).
The CLI is the only consumer; library code (slices 1-9) takes its
dependencies explicitly and never reaches into a global config.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigError(Exception):
    """Malformed YAML or Pydantic-validation failure while loading config."""


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    # CLI-runtime settings.
    registry_url: str | None = None
    cache_dir: Path | None = None
    dvc_timeout: float = 120.0
    git_timeout: float = 30.0

    # User identity — surfaces in generated scaffolds (READMEs, R DESCRIPTION,
    # citations.md, etc.). Absent → templates render empty strings.
    author: str | None = None
    organization: str | None = None

    # Registry conventions — used by the registry PR flow and by scaffold
    # templates that embed team / org names in metadata.json.
    registry_org: str | None = None
    admin_team: str | None = None
    researcher_team: str | None = None

    # Storage — needed for non-AWS S3 endpoints (Wasabi, MinIO) and for the
    # fast-sync bucket discovery.
    storage_endpoint: str | None = None
    storage_bucket_prefix: str | None = None

    # Toolchain overrides — Windows / Stata variants need this; Unix users
    # with stata-mp also benefit.
    stata_executable: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None:
            path = _default_config_path()
        if not path.is_file():
            return cls()
        try:
            with path.open() as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"malformed YAML in {path}: {e}") from e
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"invalid config in {path}: {e}") from e

    def resolved_cache_dir(self) -> Path:
        return self.cache_dir if self.cache_dir is not None else Path.home() / ".cache" / "mintd"

    @property
    def aws_profile_name(self) -> str | None:
        """Returns 'mintd' if ~/.aws/credentials has a [mintd] section, else None
        (= boto3 default credential chain)."""
        import configparser
        from pathlib import Path

        cred_path = Path.home() / ".aws" / "credentials"
        if not cred_path.is_file():
            return None
        cp = configparser.ConfigParser()
        try:
            cp.read(cred_path)
        except configparser.Error:
            return None
        return "mintd" if cp.has_section("mintd") else None


def _default_config_path() -> Path:
    base = os.environ.get("MINTD_CONFIG_DIR")
    if base:
        return Path(base) / "config.yaml"
    return Path.home() / ".config" / "mintd" / "config.yaml"
