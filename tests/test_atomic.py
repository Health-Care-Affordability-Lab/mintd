"""Tests for the slice-23 ``_try_fsync_parent_dir`` durability helper."""

from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from mintd import _atomic
from mintd.config_ops import _atomic_write_yaml
from mintd.enclave import EnclaveManifest
from mintd.publish import _atomic_write_json


def test_try_fsync_parent_dir_succeeds_on_normal_path(tmp_path: Path) -> None:
    """Happy path: file exists, parent fsync works, helper returns silently."""
    target = tmp_path / "file.txt"
    target.write_text("hello", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)


def test_try_fsync_parent_dir_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``os.open`` itself raises (Windows behavior), the helper must
    not propagate — durability is best-effort."""

    def _raise(*_a: Any, **_kw: Any) -> int:
        raise OSError("simulated windows refusal")

    monkeypatch.setattr(_atomic.os, "open", _raise)
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)


@pytest.mark.skipif(
    os.name == "nt",
    reason="opening a directory fd (os.open(dir, O_RDONLY)) is POSIX-only; on "
    "Windows _try_fsync_parent_dir returns early before any fd is opened, so "
    "the fd-leak scenario this asserts cannot arise",
)
def test_try_fsync_parent_dir_closes_fd_on_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If ``os.open`` succeeds but ``os.fsync`` raises, the fd must still
    be closed — fd-leak guard."""
    opened: list[int] = []
    closed: list[int] = []

    real_open = _atomic.os.open
    real_close = _atomic.os.close

    def _wrap_open(*args, **kwargs) -> int:
        # Widest possible signature on purpose. `_atomic.os` IS the global `os`
        # module, so this patch is process-wide while the test runs — and
        # anything else that opens a file during that window comes through
        # here. A stub narrower than the real function turns an unrelated
        # caller's valid call into a TypeError; `shutil.rmtree` uses
        # `os.open(..., dir_fd=...)`, which a `(path, flags)` stub rejects.
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def _wrap_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _fsync_raises(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(_atomic.os, "open", _wrap_open)
    monkeypatch.setattr(_atomic.os, "close", _wrap_close)
    monkeypatch.setattr(_atomic.os, "fsync", _fsync_raises)

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_parent_dir(target)

    assert opened, "os.open should have been called"
    assert opened == closed, "every opened fd must be closed"


def test_try_fsync_file_succeeds_on_normal_path(tmp_path: Path) -> None:
    """Happy path: file exists, fsync works, helper returns silently."""
    target = tmp_path / "file.txt"
    target.write_text("hello", encoding="utf-8")
    _atomic._try_fsync_file(target)


def test_try_fsync_file_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When os.open/os.fsync raise (the Windows [Errno 9] Bad file
    descriptor case), the helper must not propagate — the fsync is a
    durability refinement, the write already happened."""

    def _raise(*_a: Any, **_kw: Any) -> int:
        raise OSError(9, "Bad file descriptor")

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(_atomic.os, "open", _raise)
    _atomic._try_fsync_file(target)  # must not raise


def test_try_fsync_file_closes_fd_on_fsync_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """os.open succeeds but os.fsync raises → fd still closed (no leak)."""
    opened: list[int] = []
    closed: list[int] = []
    real_open = _atomic.os.open
    real_close = _atomic.os.close

    def _wrap_open(*args, **kwargs) -> int:
        # Widest possible signature on purpose. `_atomic.os` IS the global `os`
        # module, so this patch is process-wide while the test runs — and
        # anything else that opens a file during that window comes through
        # here. A stub narrower than the real function turns an unrelated
        # caller's valid call into a TypeError; `shutil.rmtree` uses
        # `os.open(..., dir_fd=...)`, which a `(path, flags)` stub rejects.
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def _wrap_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(_atomic.os, "open", _wrap_open)
    monkeypatch.setattr(_atomic.os, "close", _wrap_close)
    monkeypatch.setattr(_atomic.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("boom")))

    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    _atomic._try_fsync_file(target)
    assert opened == closed, "every opened fd must be closed"


# ---------------------------------------------------------------------------
# Temp-name safety for the three metadata writers
#
# ``_atomic_write_json``, ``_atomic_write_yaml`` and ``EnclaveManifest.save``
# were three byte-identical copies of one body. Each derived its temp from
# ``path.with_suffix(path.suffix + ".tmp")``, so every write went through the
# guessable sibling ``<name>.tmp`` and landed there with a bare
# ``write_text``. That destroyed a user's own scratch file of that name and
# FOLLOWED a symlink planted at it, writing the payload onto the link's
# target. Same contract the fetch lanes already prove in
# ``test_tmp_name_parity.py``: mintd never destroys a file it did not create.
# ---------------------------------------------------------------------------

_MANIFEST = EnclaveManifest(enclave_name="x")
_MANIFEST_YAML = yaml.safe_dump(_MANIFEST.model_dump(mode="json"), sort_keys=False)


def _write_manifest(path: Path, _content: str) -> None:
    """``save`` serializes its own payload; ``_MANIFEST_YAML`` is that payload."""
    _MANIFEST.save(path)


_WRITERS: dict[str, tuple[Any, str, str]] = {
    "json": (_atomic_write_json, "metadata.json", '{"a": 1}'),
    "yaml": (_atomic_write_yaml, "config.yaml", "a: 1\n"),
    "manifest": (_write_manifest, "enclave_manifest.yaml", _MANIFEST_YAML),
}

# ``EnclaveManifest.save`` renders its own payload through ``yaml.safe_dump``,
# which escapes a lone surrogate to ASCII ``\uD800``. That lane therefore
# cannot express an unencodable write, so the failure-arm test below runs on
# the writers that take ``content`` verbatim. The arm itself is one body in
# ``_atomic_write_text``, which all three share.
_VERBATIM = [name for name in _WRITERS if name != "manifest"]


@pytest.mark.parametrize("writer", list(_WRITERS))
def test_atomic_write_leaves_user_tmp_sibling_intact(writer: str, tmp_path: Path) -> None:
    write, name, content = _WRITERS[writer]
    target = tmp_path / name
    scratch = target.with_name(target.name + ".tmp")
    scratch.write_text("user scratch", encoding="utf-8")

    write(target, content)

    assert target.read_text(encoding="utf-8") == content
    assert scratch.read_text(encoding="utf-8") == "user scratch"


@pytest.mark.parametrize("writer", list(_WRITERS))
def test_atomic_write_does_not_follow_symlink_at_tmp_path(writer: str, tmp_path: Path) -> None:
    write, name, content = _WRITERS[writer]
    target = tmp_path / name
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite me", encoding="utf-8")
    link = target.with_name(target.name + ".tmp")
    link.symlink_to(victim)

    write(target, content)

    assert target.read_text(encoding="utf-8") == content
    assert victim.read_text(encoding="utf-8") == "do not overwrite me"
    assert link.is_symlink(), "the user's own symlink is not ours to remove either"


@pytest.mark.parametrize("writer", _VERBATIM)
def test_atomic_write_removes_temp_when_write_fails(writer: str, tmp_path: Path) -> None:
    """A failed write strands nothing.

    The unguessable temp name cannot self-heal the way ``<name>.tmp`` did
    (the next run overwrote it), so the failure arm has to clean up.
    """
    write, name, _content = _WRITERS[writer]
    target = tmp_path / name

    with pytest.raises(UnicodeEncodeError):
        write(target, "\ud800")  # a lone surrogate: unencodable as utf-8

    assert list(tmp_path.iterdir()) == []


def test_atomic_write_text_refuses_a_symlink_at_its_own_temp_path(tmp_path: Path) -> None:
    """``O_EXCL``, not merely the unguessable name, is what refuses the link.

    Pinning ``tmp_suffix`` is the only way to reach this guard — production
    generates a ``uuid4`` name nothing can be planted at. ``download_object``
    carries the same parameter for the same reason
    (``test_share_transport.py`` pins ``.tmp`` there).
    """
    target = tmp_path / "metadata.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite me", encoding="utf-8")
    link = target.with_name(target.name + ".pinned")
    link.symlink_to(victim)

    with pytest.raises(FileExistsError):
        _atomic._atomic_write_text(target, '{"a": 1}', tmp_suffix=".pinned")

    assert victim.read_text(encoding="utf-8") == "do not overwrite me"
    assert not target.exists()
    # The refusal must not then delete what it refused to overwrite: the
    # cleanup arm only owns temps this call actually created.
    assert link.is_symlink()


@pytest.mark.parametrize("writer", list(_WRITERS))
def test_atomic_write_keeps_write_text_permissions(writer: str, tmp_path: Path) -> None:
    """The temp's creation mode is what lands on the target.

    ``os.open`` defaults to ``0o777``, not ``Path.write_text``'s ``0o666``, so
    an explicit mode is the only thing standing between this helper and an
    executable ``metadata.json``. Compared against a ``write_text`` reference
    rather than a hardcoded octal, because the umask does the rest.
    """
    write, name, content = _WRITERS[writer]
    reference = tmp_path / f"reference-{name}"
    reference.write_text(content, encoding="utf-8")
    target = tmp_path / name

    write(target, content)

    assert target.stat().st_mode & 0o777 == reference.stat().st_mode & 0o777


def test_the_default_temp_tail_is_one_a_scaffolded_repo_already_ignores() -> None:
    """A stranded temp must stay invisible to `git add -A`.

    The failure arm above sweeps the temp on any exception, but a hard kill --
    Ctrl-C at the wrong instant, SIGKILL, power loss -- leaves it on disk with
    nothing to sweep it. `publish` commits through `commit_all`, which is
    `git add -A`, so whether that orphan reaches the registry is decided
    entirely by whether its name matches an ignore rule.

    It did, by accident, back when the temp was the guessable `<name>.tmp`
    this helper exists to stop using. Adding the `uuid4` token without keeping
    the `.tmp` tail traded a destroyed neighbour for a committed orphan, which
    is why the tail is a named constant and not an inline literal.

    Pinned against the shipped scaffolds rather than a hardcoded rule: the two
    facts are one fact, and deleting `*.tmp` from either template should fail
    here rather than quietly re-open this.

    Mutation: `_TMP_TAIL = ".mintd-tmp"` -> this test fails.
    """
    orphan = f"metadata.json.{uuid4().hex}{_atomic._TMP_TAIL}"
    files = Path(_atomic.__file__).parent / "files"

    for template in ("gitignore.txt", "dvcignore.txt"):
        rules = (files / template).read_text(encoding="utf-8").split()
        matched = [r for r in rules if fnmatch(orphan, r)]
        assert matched, f"{template} ignores no part of {orphan}; rules were {rules}"
