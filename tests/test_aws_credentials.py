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

from mintd import _aws_credentials
from mintd._aws_credentials import (
    CredentialsWriteError,
    has_profile,
    write_profile,
)


def _fail_inside_write(monkeypatch, seen: dict) -> None:
    """Raise OSError from ``cp.write(f)`` — after the O_EXCL temp exists, before
    any bytes land. Patches configparser, never the function under repair.

    ``write`` is defined on ``RawConfigParser`` (not on ``ConfigParser``), so
    patch it there or monkeypatch's undo leaves a shadowing class attribute.
    """

    def boom(self, fp, space_around_delimiters=True):
        st = os.fstat(fp.fileno())
        seen["mode"] = stat.S_IMODE(st.st_mode)
        seen["size"] = st.st_size
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(configparser.RawConfigParser, "write", boom)


# The autouse ratchet keeping the developer's real ~/.aws/credentials
# unreachable lives in tests/conftest.py — it has to cover test_config_ops.py,
# which is the module that actually resolves the default path.


def test_write_profile_creates_new_file_with_mode_0600(tmp_path: Path) -> None:
    creds = tmp_path / "credentials"
    write_profile("AKIA0001", "secret-1", credentials_path=creds)
    assert creds.is_file()
    # POSIX mode bits only. Windows uses ACLs, not st_mode perms, so the
    # 0o600 owner-only guarantee is not verifiable (and os.chmod is largely
    # a no-op) there — securing the creds file on Windows is an open
    # Windows-GA item (see project_windows_support_followup). Skip the mode
    # assertion on Windows rather than claim a protection we can't verify.
    if os.name != "nt":
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


# ---------- crash safety ----------


def test_default_credentials_path_is_never_called(tmp_path: Path) -> None:
    # A ratchet, not a bug fix: if this ever goes red, some test in this module
    # is reaching the developer's real ~/.aws/credentials.
    with pytest.raises(AssertionError, match="real "):
        _aws_credentials.default_credentials_path()
    write_profile("AKIA", "sk", credentials_path=tmp_path / "credentials")


def test_write_failure_leaves_the_original_file_intact(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "credentials"
    creds.write_text(
        "[default]\naws_access_key_id = DEFAULTKEY\naws_secret_access_key = sk-default\n\n"
        "[other]\naws_access_key_id = OTHERKEY\naws_secret_access_key = sk-other\n"
    )
    os.chmod(creds, 0o600)
    before = creds.read_bytes()
    _fail_inside_write(monkeypatch, {})
    with pytest.raises(CredentialsWriteError, match="could not write"):
        write_profile("AKIANEW", "sk-new", credentials_path=creds)
    # [default] and [other] — profiles mintd never wrote — survive intact.
    assert creds.read_bytes() == before


def test_temp_file_is_0600_before_any_bytes_and_removed_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    creds = tmp_path / "credentials"
    seen: dict = {}
    _fail_inside_write(monkeypatch, seen)
    with pytest.raises(CredentialsWriteError):
        write_profile("AKIA", "sk", credentials_path=creds)
    if os.name != "nt":  # same rationale as the mode assertion above
        assert seen["mode"] == 0o600
    assert seen["size"] == 0  # 0600 was set BEFORE any secret bytes landed
    assert list(tmp_path.iterdir()) == []  # no temp sibling survives


def test_interrupt_mid_write_strands_no_secret_bearing_temp(
    tmp_path: Path, monkeypatch
) -> None:
    # The temp holds the plaintext secret at 0600. A KeyboardInterrupt is not
    # an OSError, so without the BaseException arm it would survive forever and
    # nothing in mintd ever sweeps ~/.aws/*.tmp.
    creds = tmp_path / "credentials"

    def boom(self, fp, space_around_delimiters=True):
        fp.write("[mintd]\naws_secret_access_key = REAL-SECRET\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(configparser.RawConfigParser, "write", boom)
    with pytest.raises(KeyboardInterrupt):
        write_profile("AKIA", "REAL-SECRET", credentials_path=creds)
    assert list(tmp_path.iterdir()) == []


def test_unwritable_parent_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    # HOME on a read-only mount, or HOME=/ under a non-root uid: ~/.aws does
    # not exist yet, so mkdir fires before os.open ever runs. Both that and the
    # open itself must arrive as CredentialsWriteError for the CLI to render.
    if os.name == "nt" or os.getuid() == 0:  # chmod 0500 does not bind root
        pytest.skip("POSIX non-root only")
    home = tmp_path / "home"
    home.mkdir()
    os.chmod(home, 0o500)
    try:
        with pytest.raises(CredentialsWriteError, match="could not write"):
            write_profile("AKIA", "sk", credentials_path=home / ".aws" / "credentials")
    finally:
        os.chmod(home, 0o700)  # let tmp_path cleanup succeed


def test_write_profile_update_leaves_the_file_at_0600(tmp_path: Path) -> None:
    # The most common real path is *updating* an existing file. The old
    # O_TRUNC write left a pre-existing 0644 file at 0644; os.replace now
    # carries the temp's 0600 across. This pins the module docstring's claim.
    creds = tmp_path / "credentials"
    creds.write_text("[other]\naws_access_key_id = OTHER\naws_secret_access_key = sk\n")
    os.chmod(creds, 0o644)
    write_profile("AKIA0001", "secret-1", credentials_path=creds)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(creds).st_mode) == 0o600
