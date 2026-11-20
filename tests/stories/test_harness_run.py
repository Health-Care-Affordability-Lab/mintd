"""The harness can start mintd, interrupt it partway and wait for it: what a
story that stops a transfer midway, and the live tier's killed clone, build on.
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@contextlib.contextmanager
def _mid_clone(mintd, tmp_path: Path) -> Iterator[tuple[object, int, int]]:
    """A started `mintd data clone`, waiting on its first child: a fake git
    that says it started, through a FIFO, then sleeps holding a second FIFO
    open. Yields the run, the fake git's pid and that second FIFO's read end,
    which reads EOF once the fake git has died, reaped or not. No sleep or
    poll: the test waits once on a FIFO."""
    started, held = tmp_path / "git-started", tmp_path / "git-alive"
    os.mkfifo(started)
    os.mkfifo(held)
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "git").write_text(
        f"#!/bin/sh\nexec 3>'{held}'\necho $$ > '{started}'\nexec sleep 300\n", encoding="utf-8",
    )
    (fake / "git").chmod(0o755)
    mintd.env["PATH"] = f"{fake}{os.pathsep}{mintd.env['PATH']}"  # mintd's first spawn is a git, found on PATH
    mintd.config.write_text(
        f"registry_url: {tmp_path / 'registry.git'}\ncache_dir: {tmp_path / 'cache'}\n", encoding="utf-8",
    )
    # Read-write, so the FIFO never reads as closed before the fake git opens it.
    fifo = os.open(started, os.O_RDWR | os.O_NONBLOCK)
    # Opened before the fake git, so its write end opens at once; EOF only
    # once that write end has been opened and then closed.
    alive = os.open(held, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with mintd.start("data", "clone", "anything", cwd=tmp_path) as run:
            ready, _, _ = select.select([fifo], [], [], 60)
            assert ready, "mintd did not start its git within 60 s"
            yield run, int(os.read(fifo, 32)), alive
    finally:
        os.close(fifo)
        os.close(alive)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_interrupt_stops_mintd_mid_run_and_leaves_no_process(mintd, tmp_path: Path) -> None:
    """Mutations that turn it red: `Run.interrupt` doing nothing (the wait
    times out), or `start` without `start_new_session=True` (no group to
    signal; the wait then outlasts the fake git's sleep)."""
    with _mid_clone(mintd, tmp_path) as (run, git_pid, _):
        run.interrupt()
        done = run.wait(30)
    assert done.returncode == 130, done.stderr
    assert _gone(git_pid), f"mintd's git ({git_pid}) outlived the interrupt"


def test_a_wait_that_times_out_kills_the_whole_group(mintd, tmp_path: Path) -> None:
    """Mutations that turn it red: `Run.wait` not killing on a timeout (it then
    blocks until the fake git's 300 s sleep ends, so the timed-out wait no
    longer returns within 60 s), or killing mintd alone (the fake git lives
    on). The SIGKILLed mintd cannot reap its git, so `kill(pid, 0)` could
    still find it as a zombie: the test waits for the git's FIFO to close."""
    with _mid_clone(mintd, tmp_path) as (run, _, alive):
        asked = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run.wait(0.5)
        assert time.monotonic() - asked < 60, "the timed-out wait did not kill mintd; it waited for mintd to finish"
        assert run.proc.returncode is not None, "mintd still runs after the timed-out wait"
        ready, _, _ = select.select([alive], [], [], 10)
        assert ready, "mintd's git outlived the timed-out wait"
