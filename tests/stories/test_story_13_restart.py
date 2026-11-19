"""Story 13: A product with many files, or large ones, still moves whole.

The restartable case, for push.

- Given: a published product whose primary now holds 12 new files, added with
  `mintd data add` and not yet uploaded.
- When: `mintd data push` stops after 3 files reach S3, either from Ctrl-C to
  its process group or from the connection dropping, and I run it again.
- Pass:
  - the stopped push leaves the pointer byte for byte as `data add` wrote it;
  - the rerun exits 0 and uploads only the files S3 lacks, each once;
  - a teammate's import lands all 12 files, with the producer's bytes.
- Catches: a stopped push that leaves a pointer listing only the files it
  finished, so the rerun refuses, or uploads nothing and exits 0.
- Mutation: in `_push_verify.uploading`, call `restore(prepared)` instead of
  putting the pointer bytes back -> the pointer lists only the 3 stored files, red.

dvc rewrites the pointer as it exits however the upload ends, and on Ctrl-C
mintd stops dvc at once, so whether dvc's rewrite lands first is a race: the
Ctrl-C case catches the bug most runs, the dropped connection every run.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess

import pytest

from tests._harness.expect import assert_landed_match_pointers, md5, s3_state

N, K = 12, 3  # files in the output; files on S3 when the push stops


@pytest.mark.parametrize("stop", ["ctrl-c", "connection-drops"])
def test_stopped_push_keeps_the_pointer_and_the_rerun_uploads_the_rest(
    product, fresh_project, mintd, s3_server, stop,
) -> None:
    work = product.producer.work
    shutil.rmtree(work / "data" / "final")
    (work / "data" / "final").mkdir()
    files = {f"data/final/part-{i:02d}.csv": f"row {i}\n".encode() for i in range(N)}
    for rel, body in files.items():
        (work / rel).write_bytes(body)
    add = mintd("data", "add", "data/final", cwd=work)
    assert add.returncode == 0, add.stderr
    pointer = work / "data" / "final.dvc"
    added = pointer.read_bytes()

    # The S3 server stores K PUTs and holds the rest, so the push stops with
    # exactly K files stored and the others mid-upload. One try per request,
    # so a dropped connection fails the push in seconds, not after retries.
    s3_server.hold_puts_after = K
    with subprocess.Popen(
        [str(mintd.bin), "data", "push"], cwd=work, env={**mintd.env, "AWS_MAX_ATTEMPTS": "1"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    ) as proc:
        try:
            assert s3_server.holding.wait(timeout=120), "the push never reached its 4th upload"
            if stop == "ctrl-c":
                os.killpg(proc.pid, signal.SIGINT)
            else:
                s3_server.release.set()  # every held and later PUT fails with 503
            proc.communicate(timeout=120)
        finally:
            s3_server.release.set()
            with contextlib.suppress(ProcessLookupError):  # the group already exited
                os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode != 0
    stored = set(s3_state(s3_server, product.prefix).puts)
    assert len(stored) == K
    assert pointer.read_bytes() == added, "the stopped push rewrote the pointer"

    s3_server.hold_puts_after = None
    s3_server.puts.clear()
    rerun = mintd("data", "push", cwd=work, json=False)

    assert rerun.returncode == 0, rerun.stderr
    assert "✓" in rerun.stdout + rerun.stderr
    puts = s3_state(s3_server, product.prefix).puts
    assert set(puts) == set(files) - stored, "the rerun uploads what S3 lacks, and nothing else"
    assert set(puts.values()) == {1}, "each missing file uploads once"

    project = fresh_project()
    imported = mintd("data", "import", product.name, cwd=project)
    assert imported.returncode == 0, imported.stderr
    root = f"data/imports/data_{product.name}"
    assert assert_landed_match_pointers(project) == {f"{root}/{rel}": md5(body) for rel, body in files.items()}
