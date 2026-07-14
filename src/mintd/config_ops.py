"""Business logic for `mintd config show / setup / validate`.

Three concerns:
- **render**: pretty-print a ``Config`` as YAML or JSON.
- **mutate**: read-modify-write the on-disk config atomically.
- **validate**: schema check + AWS profile + boto3 connectivity.

This module owns the durability machinery (fsync, tmp-rename) so
``_config.py`` stays import-cheap on the CLI hot path — ``Config.load``
runs on every ``mintd <anything>`` invocation and shouldn't pay for
write-side concerns.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ._atomic import _try_fsync_parent_dir
from ._config import Config, ConfigError, _default_config_path


# ---------------------------------------------------------------------------
# Path resolution + atomic write
# ---------------------------------------------------------------------------


def _resolve_path(path: Path | None) -> Path:
    return path if path is not None else _default_config_path()


def _atomic_write_yaml(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    Sequence: tmp file → fsync the tmp's contents → rename → fsync the
    parent directory. Mirrors the slice-15 ``_atomic_write_json`` pattern
    in ``publish.py``; uses ``r+``/``f.flush()``/``os.fsync(fileno)``
    rather than ``O_RDONLY``+``fsync`` because the latter only flushes
    inode metadata on Linux, not the file contents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    with open(tmp, "r+") as f:
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    _try_fsync_parent_dir(path)


# ---------------------------------------------------------------------------
# --set parsing
# ---------------------------------------------------------------------------


def parse_set_pair(s: str) -> tuple[str, str]:
    """Parse ``KEY=VALUE`` into a tuple.

    Uses ``split("=", 1)`` so values containing ``=`` (e.g.,
    ``registry_url=https://x/y?token=...``) survive intact.
    """
    if "=" not in s:
        raise ConfigError(f"--set expects KEY=VALUE, got: {s!r}")
    key, value = s.split("=", 1)
    if not key:
        raise ConfigError(f"--set expects KEY=VALUE, got: {s!r}")
    return key, value


def _coerce_set_value(value: str) -> str | None:
    """Empty string clears the field (Pydantic optional becomes None).

    Non-empty values pass through as strings; Pydantic 2 coerces them to
    the field's declared type during ``model_validate``.
    """
    return None if value == "" else value


_V1_KEYS = frozenset({"defaults", "platform", "registry", "storage", "tools"})


def _check_unknown_keys(data: dict) -> None:
    """Reject unknown config keys before Pydantic silently drops them.

    Pydantic 2's default ``extra="ignore"`` would silently discard
    unknown fields. We probe up-front so users see a clear error.

    When the unknowns look like a v1 config (legacy `defaults`/`platform`/
    `registry`/`storage`/`tools` shape), surface a migration hint pointing
    at the interactive setup as the v2 onboarding path.
    """
    known = set(Config.model_fields)
    unknown = sorted(set(data) - known)
    if not unknown:
        return
    looks_v1 = bool(set(unknown) & _V1_KEYS)
    msg = f"unknown config keys: {unknown}"
    if looks_v1:
        msg += (
            "\nhint: this looks like a v1 mintd config. v2 stores fewer "
            "fields (registry_url, cache_dir, dvc_timeout, git_timeout, "
            "aws_profile_name). Run `mintd config setup` (no flags) for "
            "an interactive walkthrough, or use --set to seed v2 fields "
            "directly. Full v1→v2 migration ships in a follow-up slice."
        )
    raise ConfigError(msg)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_config(config: Config, *, json_out: bool = False) -> str:
    """Render ``config`` as YAML (default) or JSON.

    ``exclude_none=True`` so the output is the minimal valid config a user
    could paste back through ``--from FILE``. ``mode="json"`` ensures
    ``Path`` values serialize as strings (matters for both YAML and JSON
    paths through ``yaml.safe_dump``).
    """
    import json

    data = config.model_dump(exclude_none=True, mode="json")
    if json_out:
        return json.dumps(data, indent=2)
    return yaml.safe_dump(data, default_flow_style=False, sort_keys=False, indent=2)


# ---------------------------------------------------------------------------
# setup --set / --from
# ---------------------------------------------------------------------------


def _validate_data(data: dict) -> Config:
    _check_unknown_keys(data)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid config: {e}") from e


def apply_set_updates(
    config_path: Path | None,
    updates: list[tuple[str, str]],
    *,
    write: bool = True,
) -> Config:
    """Merge ``--set KEY=VALUE`` updates into the existing config.

    Reads the on-disk YAML (or empty dict if absent), applies updates,
    schema-validates, and atomically writes. Returns the validated
    ``Config``. When ``write=False`` (``--dry-run``), the validated
    config is returned without touching disk.
    """
    path = _resolve_path(config_path)
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"malformed YAML in {path}: {e}") from e
    else:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"config at {path} is not a YAML mapping")
    for key, value in updates:
        data[key] = _coerce_set_value(value)
    config = _validate_data(data)
    if write:
        _atomic_write_yaml(path, render_config(config))
    return config


def apply_from_file(
    config_path: Path | None,
    source: str | None,
    *,
    write: bool = True,
) -> Config:
    """Replace the config wholesale with the contents of ``source``.

    ``source=None`` reads stdin (used by the CLI's ``--from -`` form).
    ``source=path-string`` reads from disk. Either way, the result is
    schema-validated and atomically written to ``config_path``. When
    ``write=False``, the validated config is returned without writing.
    """
    target = _resolve_path(config_path)
    if source is None:
        if sys.stdin.isatty():
            raise ConfigError("--from - requires piped input on stdin")
        text = sys.stdin.read()
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except FileNotFoundError as e:
            raise ConfigError(f"--from FILE not found: {source}") from e
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML from --from source: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("--from source is not a YAML mapping")
    config = _validate_data(data)
    if write:
        _atomic_write_yaml(target, render_config(config))
    return config


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class ValidationStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: Literal["schema", "aws_profile", "s3"]
    status: Literal["ok", "fail", "skipped"]
    message: str
    latency_ms: int | None = None


def validate_config(
    config_path: Path | None,
    *,
    bucket: str | None = None,
) -> list[ValidationStep]:
    """Run three checks in order: schema → AWS profile → S3 connectivity.

    The schema step is load-bearing: if the config doesn't parse, the
    later steps are marked ``skipped`` (we have no values to test
    against). The AWS profile step is informational — it reports which
    boto3 credential source will be used but never fails. The S3 step
    is only meaningful when a ``bucket`` is supplied; otherwise it's
    ``skipped`` (auto-discovery from the registry repo is slice-22+).
    """
    path = _resolve_path(config_path)
    try:
        config = Config.load(path)
    except ConfigError as e:
        return [
            ValidationStep(name="schema", status="fail", message=str(e)),
            ValidationStep(name="aws_profile", status="skipped", message="schema failed"),
            ValidationStep(name="s3", status="skipped", message="schema failed"),
        ]

    populated = config.model_dump(exclude_none=True)
    schema_step = ValidationStep(
        name="schema",
        status="ok",
        message=f"config parses cleanly ({len(populated)} fields)",
    )

    profile = config.aws_profile_name
    if profile == "mintd":
        profile_step = ValidationStep(
            name="aws_profile",
            status="ok",
            message="'mintd' profile found in ~/.aws/credentials",
        )
    else:
        profile_step = ValidationStep(
            name="aws_profile",
            status="ok",
            message="using default boto3 credential chain (no [mintd] section)",
        )

    if bucket is None:
        s3_step = ValidationStep(
            name="s3",
            status="skipped",
            message="no --bucket provided (auto-discovery is slice-22+)",
        )
    else:
        s3_step = _check_bucket(bucket, profile, config.storage_endpoint)

    return [schema_step, profile_step, s3_step]


def _check_bucket(
    bucket: str, profile: str | None, endpoint_url: str | None = None
) -> ValidationStep:
    try:
        import boto3
        from botocore.exceptions import (
            ClientError,
            EndpointConnectionError,
            NoCredentialsError,
        )
    except ImportError:
        return ValidationStep(
            name="s3",
            status="skipped",
            message="boto3 not installed",
        )
    try:
        if profile:
            session = boto3.Session(profile_name=profile)
            client = session.client("s3", endpoint_url=endpoint_url)
        else:
            client = boto3.client("s3", endpoint_url=endpoint_url)
    except Exception as e:
        return ValidationStep(
            name="s3",
            status="fail",
            message=f"failed to create S3 client: {type(e).__name__}: {e}",
        )
    via = f" via {endpoint_url}" if endpoint_url else ""
    start = time.monotonic()
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        return ValidationStep(
            name="s3",
            status="fail",
            message=f"head_bucket on s3://{bucket}{via} — failed: {code}",
        )
    except (NoCredentialsError, EndpointConnectionError) as e:
        return ValidationStep(
            name="s3",
            status="fail",
            message=f"head_bucket: {type(e).__name__}: {e}",
        )
    except Exception as e:
        return ValidationStep(
            name="s3",
            status="fail",
            message=f"head_bucket: {type(e).__name__}: {e}",
        )
    ms = int((time.monotonic() - start) * 1000)
    return ValidationStep(
        name="s3",
        status="ok",
        message=f"head_bucket on s3://{bucket}{via} — 200 OK (latency {ms}ms)",
        latency_ms=ms,
    )


# ---------------------------------------------------------------------------
# v1 → v2 migration
# ---------------------------------------------------------------------------


def migrate_v1_to_v2(v1_data: dict) -> dict:
    """Translate a legacy ``mintd`` v1 config dict to the v2 shape.

    Maps the v1 keys that have a v2 equivalent (per ``notes/V1-PORT-AUDIT.md``);
    silently drops the rest (``platform``, ``storage.region``,
    ``storage.cache_type``, ``storage.versioning``, ``tools.github_cli``,
    ``registry.default_branch``). Unknown top-level keys outside the v1 set
    pass through unchanged so a partially-migrated user file isn't silently
    mangled.
    """
    out: dict = {}
    # Pass through any already-v2-shaped keys.
    for key in Config.model_fields:
        if key in v1_data:
            out[key] = v1_data[key]

    defaults = v1_data.get("defaults") or {}
    if isinstance(defaults, dict):
        if "author" in defaults:
            out.setdefault("author", defaults["author"])
        if "organization" in defaults:
            out.setdefault("organization", defaults["organization"])

    registry = v1_data.get("registry") or {}
    if isinstance(registry, dict):
        if "url" in registry:
            out.setdefault("registry_url", registry["url"])
        if "org" in registry:
            out.setdefault("registry_org", registry["org"])
        if "admin_team" in registry:
            out.setdefault("admin_team", registry["admin_team"])
        if "researcher_team" in registry:
            out.setdefault("researcher_team", registry["researcher_team"])

    storage = v1_data.get("storage") or {}
    if isinstance(storage, dict):
        if "endpoint" in storage:
            out.setdefault("storage_endpoint", storage["endpoint"])
        if "bucket_prefix" in storage:
            out.setdefault("storage_bucket_prefix", storage["bucket_prefix"])

    tools = v1_data.get("tools") or {}
    if isinstance(tools, dict):
        stata = tools.get("stata") or {}
        if isinstance(stata, dict):
            # Prefer detected_path (the actual installed binary name); fall
            # back to `executable` if the user set that explicitly.
            for key in ("detected_path", "executable"):
                if stata.get(key):
                    out.setdefault("stata_executable", stata[key])
                    break
    return out


def apply_migrate_v1(
    config_path: Path | None,
    source: str,
    *,
    write: bool = True,
) -> Config:
    """Read a v1 mintd config, translate to v2, validate, write atomically."""
    try:
        text = Path(source).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"--migrate-v1 source not found: {source}") from e
    try:
        v1_data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"malformed YAML in {source}: {e}") from e
    if not isinstance(v1_data, dict):
        raise ConfigError(f"--migrate-v1 source is not a YAML mapping: {source}")
    v2_data = migrate_v1_to_v2(v1_data)
    config = _validate_data(v2_data)
    if write:
        target = _resolve_path(config_path)
        _atomic_write_yaml(target, render_config(config))
    return config


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------


def _prompt_field(name: str, current: object, *, prompt_fn=input) -> str:
    """Prompt for a single field, defaulting to the current value.

    Returns the raw user input (empty string to keep current). Separated
    so tests can monkeypatch ``prompt_fn``.
    """
    current_display = "(unset)" if current is None else str(current)
    return prompt_fn(f"  {name} [{current_display}]: ").strip()


# Config fields the interactive walkthrough does not prompt for (S1 polish
# debt); still settable via `config setup --set` or the YAML directly.
_SETUP_SKIP_FIELDS = frozenset({"share_user"})


def interactive_setup(
    config_path: Path | None,
    *,
    write: bool = True,
    prompt_fn=input,
    secret_prompt_fn=None,
    aws_credentials_path: Path | None = None,
) -> Config:
    """Walk the user through each Config field; return the validated result.

    Empty input keeps the existing value (or the field's Pydantic default
    when no prior value exists). Atomic-writes the result unless
    ``write=False``. Aborts cleanly on ``KeyboardInterrupt`` / EOF by
    re-raising as ``ConfigError`` (caught by the CLI handler).

    Slice 30: if ``~/.aws/credentials`` doesn't already have a ``[mintd]``
    section, offer to capture S3 access keys and write them. The actual
    write is guarded by ``write=True`` (same flag that gates the
    config.yaml write). ``secret_prompt_fn`` defaults to ``getpass.getpass``
    so secrets don't echo; tests inject a deterministic callable.
    ``aws_credentials_path`` overrides ``~/.aws/credentials`` for tests.
    """
    import getpass

    from ._aws_credentials import (
        CredentialsWriteError,
        default_credentials_path,
        has_profile,
        write_profile,
    )

    if secret_prompt_fn is None:
        # When prompt_fn was injected (tests, headless), default the
        # secret prompt to the same scripted callable so we don't fall
        # through to getpass.getpass — which would hang the test runner
        # waiting on tty input.
        if prompt_fn is input:
            secret_prompt_fn = getpass.getpass
        else:
            secret_prompt_fn = prompt_fn

    path = _resolve_path(config_path)
    if path.is_file():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"malformed YAML in {path}: {e}") from e
        if not isinstance(existing, dict):
            existing = {}
        # Drop v1-shaped keys so the wizard starts from a clean v2 base.
        existing = {k: v for k, v in existing.items() if k in Config.model_fields}
    else:
        existing = {}

    print(f"Interactive setup for {path}")
    print("Press <return> to keep the current value; type a value to change it.")
    print("(Empty value on an optional field clears it.)")

    data = dict(existing)
    try:
        for field_name in Config.model_fields:
            if field_name in _SETUP_SKIP_FIELDS:
                # share_user prompting is deferred (S1 polish); it is still
                # settable via `config setup --set share_user=` or the YAML.
                continue
            current = data.get(field_name)
            raw = _prompt_field(field_name, current, prompt_fn=prompt_fn)
            if raw == "":
                # Empty input → keep existing. (To clear a field, run
                # `mintd config setup --set <field>=` after.)
                continue
            data[field_name] = raw
    except (KeyboardInterrupt, EOFError) as e:
        raise ConfigError("interactive setup aborted") from e

    config = _validate_data(data)
    if write:
        _atomic_write_yaml(path, render_config(config))

    # Slice 30: optionally capture AWS credentials into ~/.aws/credentials.
    # Decoupled from config.yaml because the keys go to a different file
    # (security-sensitive, mode 0600) and the prompt is opt-in.
    creds_path = aws_credentials_path or default_credentials_path()
    if write and not has_profile("mintd", credentials_path=creds_path):
        print()
        print("AWS profile [mintd] not found in ~/.aws/credentials.")
        print("DVC needs S3 access keys to push/pull data. Set them up now?")
        try:
            answer = prompt_fn("  Configure [mintd] profile now? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError) as e:
            raise ConfigError("interactive setup aborted") from e
        if answer in ("", "y", "yes"):
            try:
                access_key = prompt_fn("  AWS access key ID: ").strip()
                secret_key = secret_prompt_fn("  AWS secret access key (hidden): ").strip()
            except (KeyboardInterrupt, EOFError) as e:
                raise ConfigError("interactive setup aborted") from e
            if not access_key or not secret_key:
                print("  (skipped — access key or secret was empty)")
            else:
                try:
                    write_profile(
                        access_key, secret_key,
                        profile_name="mintd",
                        credentials_path=creds_path,
                    )
                except CredentialsWriteError as e:
                    raise ConfigError(str(e)) from e
                print(f"  wrote [mintd] section to {creds_path} (mode 0600)")

    return config


# ---------------------------------------------------------------------------
# Rendering validation output
# ---------------------------------------------------------------------------


_STATUS_GLYPH = {"ok": "✓", "fail": "✗", "skipped": "~"}


def render_validation(
    steps: list[ValidationStep], *, json_out: bool
) -> tuple[str, int]:
    """Render validation steps as text or JSON; return (text, exit_code)."""
    if json_out:
        import json

        # JSON shape: {step_name: {"status": ..., "message": ...}}. Tests
        # assert structure (keys present) rather than exact message strings.
        text = json.dumps(
            {
                step.name: {
                    "status": step.status,
                    "message": step.message,
                    "latency_ms": step.latency_ms,
                }
                for step in steps
            },
            indent=2,
        )
    else:
        lines = [f"{_STATUS_GLYPH[step.status]} {step.name}: {step.message}" for step in steps]
        text = "\n".join(lines)
    exit_code = 1 if any(step.status == "fail" for step in steps) else 0
    return text, exit_code
