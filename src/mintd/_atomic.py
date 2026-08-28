"""Atomic-write helpers shared by publish, config_ops, and fast-sync.

Imports only stdlib (os, pathlib, uuid) — safe to import from anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def _try_fsync_file(path: Path) -> None:
    """Best-effort fsync of a just-written file for pre-rename durability.

    The data is already written (``write_bytes`` / ``download_file``) before
    this call; the fsync only flushes it to stable storage ahead of the
    rename. On Windows, ``os.open(..., O_RDONLY)`` + ``os.fsync`` on a fresh
    regular file can raise ``OSError`` (``[Errno 9] Bad file descriptor``),
    so — like :func:`_try_fsync_parent_dir` — we swallow ``OSError`` and
    continue rather than crash the cache write on a durability refinement.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _try_fsync_parent_dir(path: Path) -> None:
    """Best-effort fsync of ``path.parent`` for rename durability.

    POSIX systems support opening a directory ``O_RDONLY`` and fsyncing
    its fd, which durably persists a prior rename. Windows and some
    other platforms reject either the open or the fsync; the durability
    step is a refinement (the rename has already happened) so we
    swallow ``OSError`` and continue.
    """
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# The default temp name must END in this, and it must stay a suffix the
# scaffolded ignore files already match (`*.tmp`, in both `files/gitignore.txt`
# and `files/dvcignore.txt`). A hard kill between the open and the rename
# strands the temp, and `publish` commits through `commit_all`, which is
# `git add -A` (`_registry_git_ops.py`). Under a non-matching tail those
# orphans go from invisible to committed into the registry -- and changing the
# scaffold templates instead would only reach repos scaffolded from then on,
# never the ones already in the wild. Pinned by
# `test_the_default_temp_tail_is_one_a_scaffolded_repo_already_ignores`.
_TMP_TAIL = ".tmp"


def _atomic_write_text(path: Path, content: str, *, tmp_suffix: str | None = None) -> None:
    """Write ``content`` to ``path`` atomically, destroying no neighbour.

    Sequence: sibling temp → fsync the temp's contents → rename onto ``path``
    → fsync the parent directory, which is what makes the rename durable on
    POSIX. Not ``os.sync()``: that is a system-wide flush that can stall on a
    slow filesystem.

    The temp name and its open flags are the load-bearing part, and all three
    copies of this body (``publish._atomic_write_json``,
    ``config_ops._atomic_write_yaml``, ``enclave.EnclaveManifest.save``) got
    them wrong. They derived the temp as
    ``path.with_suffix(path.suffix + ".tmp")`` and wrote it with
    ``Path.write_text``, so writing ``metadata.json`` always went through the
    guessable sibling ``metadata.json.tmp``:

    * a user's own scratch file of that name was destroyed, silently, exit 0;
    * a symlink planted there was FOLLOWED — ``write_text`` opens for writing,
      so the payload landed on the link's target.

    ``O_CREAT | O_EXCL`` fixes the second half outright (POSIX requires the
    open to fail with ``EEXIST`` on a symlink, so no link is ever traversed)
    and the ``uuid4`` token fixes the first, matching what
    ``_aws_credentials.write_profile`` and ``_share_ops.download_object``
    already do. The mode is spelled out because ``os.open``
    defaults to 0o777 where ``write_text`` uses 0o666 — without it every
    ``metadata.json`` this writes comes out executable (0o755 under the usual
    umask). ``os.replace`` carries the temp's mode onto ``path``, so this is
    the only place it can be set; ``tempfile.mkstemp`` is not an option here
    either, as its 0600 would narrow files the lab reads across accounts.

    ``tmp_suffix`` pins the temp name. No production caller passes it: it
    exists so the ``O_EXCL`` guard is reachable at all — with an unguessable
    name no test can plant anything at the temp path, so dropping ``O_EXCL``
    is a mutation nothing kills. ``download_object`` carries the same
    parameter for the same reason.

    The failure arm is not optional. A guessable temp self-healed (the next
    run overwrote it); an unguessable one would strand a fresh orphan per
    failed write, and nothing in this lane sweeps them. It starts AFTER the
    open on purpose: a temp we never created is somebody else's file, and
    unlinking it on the ``EEXIST`` path would destroy exactly the neighbour
    ``O_EXCL`` just refused to overwrite.

    A hard kill between the open and the rename strands an orphan this arm
    never runs for, which is why the name still ends in ``.tmp`` -- see
    ``_TMP_TAIL``.

    This does not close the defect class. ``_fast_sync_ops.write_dir_manifest``
    and ``fetch_to_cache`` still build guessable temps (the latter via
    ``with_suffix``, which additionally collides the cache entries ``<md5>`` and
    ``<md5>.dir`` onto one temp path). Both are bytes/boto3-download shaped and
    cannot route through a text writer.
    """
    suffix = tmp_suffix if tmp_suffix is not None else f".{uuid4().hex}{_TMP_TAIL}"
    tmp = path.with_name(path.name + suffix)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Ctrl-C between the open and the replace lands here too.
        tmp.unlink(missing_ok=True)
        raise
    _try_fsync_parent_dir(path)
