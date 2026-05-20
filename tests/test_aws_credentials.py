"""Slice 30: tests for the AWS-credentials writer.

Security invariants pinned here:
- File mode is 0600 from open time (no TOCTOU window).
- Other profiles in the file are preserved on update.
- Symlinks at the target path are refused.
"""

from __future__ import annotations

import configparser
import os
import stat
from pathlib import Path

import pytest

from mintd._aws_credentials import (
    CredentialsWriteError,
    has_profile,
    write_profile,
)


def test_write_profile_creates_new_file_with_mode_0600(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    write_profile("AKIA0001", "secret-1", credentials_path=creds)
    assert creds.is_file()
    mode = stat.S_IMODE(os.stat(creds).st_mode)
    assert mode == 0o600
    cp = configparser.ConfigParser()
    cp.read(creds)
    assert cp.get("mintd", "aws_access_key_id") == "AKIA0001"
    assert cp.get("mintd", "aws_secret_access_key") == "secret-1"


def test_write_profile_preserves_other_sections(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(
        "[other]\naws_access_key_id = AKIAOTHER\naws_secret_access_key = sk-other\n"
    )
    os.chmod(creds, 0o600)
    write_profile("AKIA0001", "secret-1", credentials_path=creds)
    cp = configparser.ConfigParser()
    cp.read(creds)
    assert cp.get("other", "aws_access_key_id") == "AKIAOTHER"
    assert cp.get("mintd", "aws_access_key_id") == "AKIA0001"


def test_write_profile_updates_existing_section(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(
        "[mintd]\naws_access_key_id = AKIAOLD\naws_secret_access_key = sk-old\n"
    )
    write_profile("AKIANEW", "sk-new", credentials_path=creds)
    cp = configparser.ConfigParser()
    cp.read(creds)
    assert cp.get("mintd", "aws_access_key_id") == "AKIANEW"
    assert cp.get("mintd", "aws_secret_access_key") == "sk-new"


def test_write_profile_refuses_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real-creds"
    real.write_text("")
    link = tmp_path / "credentials"
    link.symlink_to(real)
    with pytest.raises(CredentialsWriteError, match="symlink"):
        write_profile("AKIA", "sk", credentials_path=link)


def test_write_profile_rejects_empty_credentials(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    with pytest.raises(CredentialsWriteError, match="required"):
        write_profile("", "sk", credentials_path=creds)
    with pytest.raises(CredentialsWriteError, match="required"):
        write_profile("AKIA", "", credentials_path=creds)


def test_write_profile_sync_default(tmp_path: Path) -> None:
    """sync_default=True also writes the same keys to [default]."""
    creds = tmp_path / "credentials"
    write_profile(
        "AKIA0001", "secret-1",
        credentials_path=creds, sync_default=True,
    )
    cp = configparser.ConfigParser()
    cp.read(creds)
    assert cp.get("mintd", "aws_access_key_id") == "AKIA0001"
    assert cp.get("default", "aws_access_key_id") == "AKIA0001"
    assert cp.get("default", "aws_secret_access_key") == "secret-1"


def test_has_profile_true_when_keys_populated(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    write_profile("AKIA", "sk", credentials_path=creds)
    assert has_profile("mintd", credentials_path=creds) is True


def test_has_profile_false_when_section_missing(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text("[other]\naws_access_key_id = AKIAOTHER\naws_secret_access_key = sk\n")
    assert has_profile("mintd", credentials_path=creds) is False


def test_has_profile_false_when_file_missing(tmp_path: Path) -> None:
    assert has_profile("mintd", credentials_path=tmp_path / "nope") is False


def test_has_profile_false_when_keys_blank(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    creds.write_text("[mintd]\naws_access_key_id =\naws_secret_access_key =\n")
    assert has_profile("mintd", credentials_path=creds) is False
