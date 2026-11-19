"""The live tier's checks, run for each product (how to run them: conftest.py)."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests._harness.expect import assert_landed_match_pointers
from tests._harness.live import RESULTS, Product, run_read_only

GRACE = 60  # seconds mintd gets to stop after Ctrl-C before its group is killed


def _landed(product: Product, check: str, root: Path, seconds: float) -> None:
    landed = assert_landed_match_pointers(root)
    size = sum((root / path).stat().st_size for path in landed)
    RESULTS.append(product.line(check, f"{len(landed)} files", f"{len(landed)} md5 match, {size} bytes, {seconds:.1f} s"))


def _at_rest(root: Path) -> dict[str, tuple[int, ...]]:
    """Each file outside `.git` and `.dvc/tmp` by size and mtime. A cached `.dir`
    manifest by size only: fast-sync rewrites it on every pull, and its name is
    its md5. Every download lands in `.dvc/cache` first, so a pull that fetched
    or rewrote anything changes this."""
    state: dict[str, tuple[int, ...]] = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if rel.parts[0] == ".git" or rel.parts[:2] == (".dvc", "tmp") or not path.is_file():
            continue
        st = path.stat()
        manifest = rel.parts[0] == ".dvc" and path.suffix == ".dir"
        state[rel.as_posix()] = (st.st_size,) if manifest else (st.st_size, st.st_mtime_ns)
    return state


def _whole_objects(cache: Path) -> list[Path]:
    """Objects fetched whole. A download is written as `<md5>.tmp` (and by
    boto3 as `<md5>.tmp.<random>`) and renamed once verified; a manifest is
    `<md5>.dir`. Only a name with no dot is a finished object."""
    return [p for p in cache.glob("*/*") if "." not in p.name] if cache.is_dir() else []


def test_clone_lands_every_tracked_file(product, cloned) -> None:
    dest, seconds = cloned
    _landed(product, "clone", dest, seconds)


def test_a_second_pull_downloads_nothing(product, cloned, live) -> None:
    dest, _ = cloned
    before = _at_rest(dest)
    started = time.monotonic()
    run = live("data", "pull", cwd=dest)
    seconds = time.monotonic() - started
    assert run.returncode == 0, run.stderr
    assert _at_rest(dest) == before, "the second pull downloaded or rewrote files"
    RESULTS.append(product.line("second pull", f"{len(before)} files unchanged", f"{seconds:.1f} s"))


def test_import_lands_the_primary(product, live, fresh_project) -> None:
    project = fresh_project()
    started = time.monotonic()
    run = live("data", "import", product.name, cwd=project)
    seconds = time.monotonic() - started
    assert run.returncode == 0, run.stderr
    _landed(product, "import", project, seconds)


def test_a_killed_clone_restarts_and_finishes(product, live, tmp_path) -> None:
    dest = tmp_path / "product"
    cache = dest / ".dvc" / "cache" / "files" / "md5"
    with live.start("data", "clone", product.name, "--dest", str(dest), cwd=tmp_path) as run:
        while run.proc.poll() is None and not _whole_objects(cache):
            time.sleep(0.2)
        if run.proc.poll() is not None:
            done = run.wait()
            raise AssertionError(f"mintd exited {done.returncode} before any object landed: nothing to interrupt "
                                 f"(a larger product, if it succeeded)\n{done.stderr}")
        cached = len(_whole_objects(cache))
        run.interrupt()
        # ponytail: Ctrl-C lets fast-sync's ThreadPoolExecutor drain its queue
        # (_fast_sync_ops.py:1627); the grace bounds it, and the report says how it stopped
        try:
            stopped = f"exit {run.wait(GRACE).returncode} on Ctrl-C"
        except subprocess.TimeoutExpired:
            stopped = f"killed: Ctrl-C did not stop it within {GRACE} s"
    stopped += f"; {cached} objects cached at the interrupt, {len(_whole_objects(cache))} when it stopped"
    started = time.monotonic()
    rerun = live("data", "pull", cwd=dest)  # a second clone into dest is refused; mintd's own hint is a pull
    seconds = time.monotonic() - started
    assert rerun.returncode == 0, rerun.stderr
    landed = assert_landed_match_pointers(dest)
    RESULTS.append(product.line(
        "killed clone", f"{len(landed)} files after the rerun ({stopped})",
        f"{seconds:.1f} s rerun",
    ))


def test_time_against_plain_git_clone_and_dvc_pull(product, cloned, live, tmp_path, request) -> None:
    dest, mintd_seconds = cloned
    plain = tmp_path / "plain"
    env = {**live.env, "AWS_PROFILE": "mintd"}  # the profile mintd gives its own dvc
    started = time.monotonic()
    got = run_read_only(["git", "clone", "-q", str(product.url), str(plain)], env=env)
    assert got.returncode == 0, got.stderr
    got = run_read_only(["dvc", "pull"], cwd=plain, env=env)
    plain_seconds = time.monotonic() - started
    assert got.returncode == 0, got.stderr
    assert_landed_match_pointers(plain)  # the baseline did the same work
    ratio = mintd_seconds / plain_seconds
    RESULTS.append(product.line(
        "time", "measured", f"mintd {mintd_seconds:.1f} s, plain {plain_seconds:.1f} s, ratio {ratio:.2f}",
    ))
    k = request.config.getoption("--live-k")
    assert k is None or ratio <= k, f"mintd's clone took {ratio:.2f} times plain git clone + dvc pull, over k={k}"
