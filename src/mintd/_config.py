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

    registry_url: str | None = None
    cache_dir: Path | None = None
    dvc_timeout: float = 120.0
    git_timeout: float = 30.0

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


def _default_config_path() -> Path:
    base = os.environ.get("MINTD_CONFIG_DIR")
    if base:
        return Path(base) / "config.yaml"
    return Path.home() / ".config" / "mintd" / "config.yaml"
