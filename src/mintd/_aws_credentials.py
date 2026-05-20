"""Write and inspect ``~/.aws/credentials`` profile sections.

Security-sensitive: writes shared-secret material (S3 access keys) to
disk. The ``write_profile`` helper enforces mode 0600 from open time
(no TOCTOU window where the file is briefly world-readable), refuses to
write through symlinks, and preserves any other profiles in the file.

Slice 30 ports v1's ``mintd/config/credentials.py:set_storage_credentials``.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path


class CredentialsWriteError(Exception):
    """Refusing to write credentials (symlink, permission error, etc.)."""


def default_credentials_path() -> Path:
    return Path.home() / ".aws" / "credentials"


def has_profile(
    profile_name: str = "mintd",
    *,
    credentials_path: Path | None = None,
) -> bool:
    """Return True iff the credentials file has a section named
    ``profile_name`` AND the section has both keys populated."""
    path = credentials_path or default_credentials_path()
    if not path.is_file():
        return False
    cp = configparser.ConfigParser()
    try:
        cp.read(path)
    except configparser.Error:
        return False
    if not cp.has_section(profile_name):
        return False
    ak = cp.get(profile_name, "aws_access_key_id", fallback=None)
    sk = cp.get(profile_name, "aws_secret_access_key", fallback=None)
    return bool(ak and sk)


def write_profile(
    access_key: str,
    secret_key: str,
    *,
    profile_name: str = "mintd",
    credentials_path: Path | None = None,
    sync_default: bool = False,
) -> None:
    """Write ``access_key`` + ``secret_key`` to the named profile.

    Creates ``~/.aws/`` with mode 0700 if missing; writes the file with
    mode 0600 from open time. Preserves any other profiles. When
    ``sync_default=True``, also writes the same credentials to the
    ``[default]`` section (off by default — silently overwriting
    ``[default]`` would break non-mintd AWS workflows).

    Raises ``CredentialsWriteError`` if the target is a symlink.
    """
    if not access_key or not secret_key:
        raise CredentialsWriteError("access_key and secret_key are required")

    path = credentials_path or default_credentials_path()

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir mode is masked by umask; fix explicitly.
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        # Caller may not own ~/.aws (rare; surface as a real error).
        pass

    if path.is_symlink():
        raise CredentialsWriteError(
            f"{path} is a symlink — refusing to write credentials. "
            "Remove the symlink and retry."
        )

    cp = configparser.ConfigParser()
    if path.exists():
        cp.read(path)

    sections = [profile_name]
    if sync_default:
        sections.append("default")

    for section in sections:
        if not cp.has_section(section):
            cp.add_section(section)
        cp.set(section, "aws_access_key_id", access_key)
        cp.set(section, "aws_secret_access_key", secret_key)

    # 0600 from open time — no window where the file is world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        cp.write(f)
