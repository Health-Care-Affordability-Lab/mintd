"""Story 9: Cache push and pull stay out of DVC and survive a partial failure (Holds).

- Given: a project with files in its cache lane (not tracked by DVC), and a
  fresh clone.
- When: I run `mintd cache push`, then `mintd cache pull` on the clone
  (including a run where S3 returns 500 on one file).
- Then: on a clean run, the clone's cached files match byte for byte with no
  `.dvc` pointers created; when one file returns 500, the other files still
  land, the failed key is named with `retry: mintd cache pull`, and a rerun
  lands the missing file.
- Pass:
  - every pulled file hashes to the pushed bytes, and `git status` shows no new
    `.dvc` files;
  - on a single-file 500, all other files land, exit is non-zero with no raw
    traceback, and a rerun exits 0 with all files present.
- Catches: one S3 5xx aborting the whole `cache pull` with a botocore
  traceback (the bug fixed in #68).

Beyond the story's text (the coordinator's gate, 2026-11-20): a 500 that
happens once is retried, so the first pull lands every file.

The 500 is the one real S3 sent, checksum header included (`fail_gets` in
`tests/_harness/s3.py`): through it botocore raises AttributeError, #68's
failure. Mutations that turn it red: undoing #68's two layers (the
AttributeError conversion in `_share_ops.download_object` and the per-file
catch in `_cache_ops.cache_pull`; either alone stays green) turns the 500 case
red with a traceback; `_cache_ops._pull_one` returning before its download
turns the clean case red; `_pull_one` also writing `<dest>.dvc` turns the
no-pointer check red; a pull that stops at its first failure turns the 500
case red (the failing key is pulled first); a ✓ or exit 0 over a failure, in
either output mode, turns it red; no retry turns the one-off case red.
"""

from __future__ import annotations

from pathlib import Path

from tests._harness import HarnessError
from tests._harness.expect import md5, result, s3_state
from tests._harness.git import _git
from tests._harness.s3 import BUCKET

FILES = {"results/a.txt": b"alpha\n", "results/sub/b.txt": b"beta\n", "results/c.txt": b"gamma\n"}
FAILING = "results/a.txt"  # the first key pulled: a pull that stops at its first failure lands nothing else


def _new_pointers(project: Path) -> set[str]:
    """The `.dvc` files git sees as new or changed."""
    status = _git(["status", "--porcelain", "--untracked-files=all"], cwd=project)
    return {line[3:] for line in status.splitlines() if line.endswith(".dvc")}


def _push_and_clone(product, mintd, s3_server, tmp_path: Path) -> Path:
    """FILES pushed to the producer's cache lane, untracked, and a fresh clone of the producer."""
    work = product.producer.work
    for rel, body in FILES.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(body)
    pushed = mintd("cache", "push", "results", cwd=work)
    assert pushed.returncode == 0, pushed.stderr
    if f"cache/{FAILING}" not in s3_state(s3_server, product.prefix).puts:
        raise HarnessError(f"cache push uploaded no cache/{FAILING}: the injected key would miss")
    assert _new_pointers(work) == set()
    clone = tmp_path / "clone"
    _git(["clone", "-q", product.producer.url, str(clone)])
    return clone


def _landed(clone: Path) -> dict[str, str]:
    return {rel: md5((clone / rel).read_bytes()) for rel in FILES if (clone / rel).is_file()}


def test_cache_push_then_pull_lands_every_file_outside_dvc(product, mintd, s3_server, tmp_path: Path) -> None:
    clone = _push_and_clone(product, mintd, s3_server, tmp_path)

    pulled = mintd("cache", "pull", cwd=clone)

    assert pulled.returncode == 0, pulled.stderr
    assert _landed(clone) == {rel: md5(body) for rel, body in FILES.items()}
    assert _new_pointers(clone) == set()


def test_one_file_that_fails_fails_alone_and_a_rerun_lands_it(product, mintd, s3_server, tmp_path: Path) -> None:
    clone = _push_and_clone(product, mintd, s3_server, tmp_path)
    s3_server.fail_gets[f"/{BUCKET}/{product.prefix}/cache/{FAILING}"] = (500, None)  # every GET, until cleared

    failed = mintd("cache", "pull", cwd=clone)

    assert "Traceback" not in failed.stdout + failed.stderr, failed.stderr
    assert failed.returncode != 0, failed.stderr
    assert {k: result(failed.stdout)[k] for k in ("failed", "pulled")} == {"failed": 1, "pulled": len(FILES) - 1}
    assert _landed(clone) == {rel: md5(body) for rel, body in FILES.items() if rel != FAILING}
    shown = mintd("cache", "pull", cwd=clone, json=False)  # the ✓ and "pull incomplete" exist only here
    said = "".join((shown.stdout + shown.stderr).split())  # the console wraps at 80 columns, mid-word too
    assert shown.returncode != 0 and "✓" not in said and "pullincomplete" in said, shown.stderr
    assert FAILING in said and "retry:mintdcachepull" in said, shown.stderr

    s3_server.fail_gets.clear()
    rerun = mintd("cache", "pull", cwd=clone)

    assert rerun.returncode == 0, rerun.stderr
    assert _landed(clone) == {rel: md5(body) for rel, body in FILES.items()}
    assert _new_pointers(clone) == set()


def test_a_one_off_500_is_retried_and_the_pull_lands_everything(product, mintd, s3_server, tmp_path: Path) -> None:
    clone = _push_and_clone(product, mintd, s3_server, tmp_path)
    s3_server.fail_gets[f"/{BUCKET}/{product.prefix}/cache/{FAILING}"] = (500, 1)

    pulled = mintd("cache", "pull", cwd=clone)

    assert pulled.returncode == 0, pulled.stderr
    assert _landed(clone) == {rel: md5(body) for rel, body in FILES.items()}
    assert s3_server.failed_gets == [f"/{BUCKET}/{product.prefix}/cache/{FAILING}"], "the 500 was served once"
