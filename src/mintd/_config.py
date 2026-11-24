"""CLI-layer config loader.

Reads ``~/.config/mintd/config.yaml`` (or ``$MINTD_CONFIG_DIR/config.yaml``).
The CLI is the only consumer; library code (slices 1-9) takes its
dependencies explicitly and never reaches into a global config.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import MintdError


class ConfigError(MintdError):
    """Malformed YAML or Pydantic-validation failure while loading config."""


class Timeouts(BaseModel):
    model_config = ConfigDict(frozen=True)
    fast: float | None = Field(default=30.0, gt=0, allow_inf_nan=False, description="Seconds to wait on a quick git or dvc call. Optional; default 30.")
    transfer: float | None = Field(default=None, gt=0, allow_inf_nan=False, description="Seconds a push, pull or clone may run. Optional; unset means no limit.")


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # CLI-runtime settings.
    registry_url: str | None = Field(default=None, description="The git URL of the lab's catalog repo. Required for data list, import and publish.")
    cache_dir: Path | None = Field(default=None, description="Where mintd keeps its own cache. Optional; unset means ~/.cache/mintd.")
    timeouts: Timeouts = Field(default_factory=Timeouts)

    # User identity — surfaces in generated scaffolds (READMEs, R DESCRIPTION,
    # citations.md, etc.). Absent → templates render empty strings.
    author: str | None = Field(default=None, description="Your name, written into new repos' READMEs and citations. Optional.")
    # Pins the identity used by `mintd share put` keys. Falls back to a
    # slugified `author` when unset (the CLI prints the nudge).
    share_user: str | None = None
    organization: str | None = Field(default=None, description="Your organization, written into new repos. Optional.")

    # Registry conventions — used by the registry PR flow and by scaffold
    # templates that embed team / org names in metadata.json.
    registry_org: str | None = Field(default=None, description="The GitHub organization new repos belong to. Required for a new repo to pass mintd check: it makes the repo's GitHub URL.")
    admin_team: str | None = Field(default=None, description="The GitHub team given admin access in new repos' metadata.json. Optional.")
    researcher_team: str | None = Field(default=None, description="The GitHub team given read access in new repos' metadata.json. Optional.")

    # Storage — needed for non-AWS S3 endpoints (Wasabi, MinIO) and for the
    # fast-sync bucket discovery.
    storage_endpoint: str | None = Field(default=None, description="The S3 endpoint URL, for storage that isn't AWS (Wasabi, MinIO). Required for share; leave unset for AWS.")
    storage_bucket_prefix: str | None = Field(default=None, description="The S3 bucket for the lab's data. Required for init of data, code and project repos, and for share.")

    # Toolchain overrides — Windows / Stata variants need this; Unix users
    # with stata-mp also benefit.
    stata_executable: str | None = Field(default=None, description="The Stata command to run. Optional; unset means stata.")

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        if path is None:
            path = _default_config_path()
        if not path.is_file():
            return cls()
        try:
            with path.open(encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"malformed YAML in {path}: {e}") from e
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise ConfigError(
                f"invalid config in {path} (note the timeouts: block added in v2): {_plain_errors(e)}"
            ) from e

    def resolved_cache_dir(self) -> Path:
        return self.cache_dir if self.cache_dir is not None else Path.home() / ".cache" / "mintd"

    @property
    def aws_profile_name(self) -> str | None:
        """Returns 'mintd' if the AWS shared credentials file has a [mintd]
        section, else None (= boto3 default credential chain).

        Resolves the file the way the AWS SDK does — $AWS_SHARED_CREDENTIALS_FILE
        when set (how a sandbox that fences ~/.aws redirects it; set-but-empty
        means "no file", exactly as boto3 reads it, NOT a fallback to home),
        else ~/.aws/credentials — so detection and DVC's boto3 read the same
        file. An unreadable or undecodable file means no profile, never a
        traceback.
        """
        import configparser

        # One resolver, shared with the setup wizard -- see its docstring for
        # the expansion rules (vars then user, never raising; set-but-empty
        # means "no file", exactly as boto3 reads it, NOT a home fallback --
        # Path("") resolves to ".", a directory, so it falls out of is_file()
        # below as None).
        from ._aws_credentials import shared_credentials_path

        cred_path = shared_credentials_path()
        if not cred_path.is_file():
            return None
        cp = configparser.ConfigParser()
        try:
            cp.read(cred_path)
        except (configparser.Error, UnicodeDecodeError):
            return None
        return "mintd" if cp.has_section("mintd") else None


def _plain_errors(e: ValidationError) -> str:
    """One ``<field>: <message>`` per error, without pydantic's dump and links."""
    parts = []
    for err in e.errors():
        field = ".".join(str(part) for part in err["loc"])
        parts.append(f"{field}: {err['msg']}" if field else err["msg"])
    return "; ".join(parts)


def _default_config_path() -> Path:
    base = os.environ.get("MINTD_CONFIG_DIR")
    if base:
        return Path(base) / "config.yaml"
    return Path.home() / ".config" / "mintd" / "config.yaml"
