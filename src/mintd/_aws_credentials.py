"""Write and inspect ``~/.aws/credentials`` profile sections.

Security-sensitive: writes shared-secret material (S3 access keys) to
disk. The ``write_profile`` helper enforces mode 0600 from open time
(no TOCTOU window where the file is briefly world-readable), refuses to
write through symlinks, preserves any other profiles in the file, and
replaces it atomically — a crash mid-write can never empty a file
holding profiles mintd did not write.

Slice 30 ports v1's ``mintd/config/credentials.py:set_storage_credentials``.
"""

from __future__ import annotations

import configparser
import os
import uuid
from pathlib import Path

from mintd._atomic import _try_fsync_parent_dir


class CredentialsWriteError(Exception):
    """Refusing to write credentials (symlink, permission error, etc.)."""


def _write_failed(path: Path, exc: OSError) -> CredentialsWriteError:
    """One message shape for every failed write."""
    return CredentialsWriteError(
        f"could not write {path}: {exc}\n"
        "  hint: your existing credentials file was not modified"
    )


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

    Creates ``~/.aws/`` with mode 0700 if missing; writes a mode-0600 temp
    sibling and ``os.replace``s it into position, so the file is never
    observed empty or half-written. Preserves any other profiles. When
    ``sync_default=True``, also writes the same credentials to the
    ``[default]`` section (off by default — silently overwriting
    ``[default]`` would break non-mintd AWS workflows).

    Raises ``CredentialsWriteError`` if the target is a symlink, or if the
    write fails — in which case the existing file is left untouched.
    """
    if not access_key or not secret_key:
        raise CredentialsWriteError("access_key and secret_key are required")

    path = credentials_path or default_credentials_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        # A read-only $HOME, or HOME=/ under a non-root uid, reaches this on a
        # fresh install — the same bare-traceback path the write below closes.
        raise _write_failed(path, exc) from exc
    # mkdir mode is masked by umask; fix explicitly.
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        # Caller may not own ~/.aws (rare; the write below surfaces it).
        pass
    except OSError as exc:
        raise _write_failed(path, exc) from exc

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

    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        # 0600 at O_EXCL create time, before any bytes land; os.replace carries
        # that mode onto the real file. Temp sibling -> fsync -> replace so a
        # crash mid-write can never empty a file holding profiles mintd never
        # wrote. os.open sits INSIDE the try so a non-writable ~/.aws surfaces
        # as a real error rather than a bare traceback.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            cp.write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _write_failed(path, exc) from exc
    except BaseException:
        # Ctrl-C between open and replace would otherwise strand a 0600 temp
        # holding the plaintext secret, which nothing ever sweeps.
        tmp.unlink(missing_ok=True)
        raise
    _try_fsync_parent_dir(path)
