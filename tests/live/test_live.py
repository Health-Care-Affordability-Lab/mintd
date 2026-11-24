"""The live tier's checks, run for each product (how to run them: conftest.py)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from tests._harness.expect import assert_landed_match_pointers, md5_file
from tests._harness.live import RESULTS, SEEN, Product, run_read_only

GRACE = 60  # seconds mintd gets to stop after Ctrl-C before its group is killed


def _line(product: Product, check: str, counts: str, details: str = "") -> str:
    """A results line, shown by the product's classification as it stands now:
    a clone's metadata.json can make it stricter than the catalog's."""
    return SEEN[product.name].line(check, counts, details)


def _landed(product: Product, check: str, root: Path, seconds: float) -> dict[str, str]:
    landed = assert_landed_match_pointers(root)
    size = sum((root / path).stat().st_size for path in landed)
    RESULTS.append(_line(product, check, f"{len(landed)} files", f"{len(landed)} md5 match, {size} bytes, {seconds:.1f} s"))
    return landed


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
    landed = assert_landed_match_pointers(dest)  # so this check alone cannot pass on a clone that landed nothing
    RESULTS.append(_line(product, "second pull", f"{len(landed)} files landed, {len(before)} unchanged", f"{seconds:.1f} s"))


def test_import_lands_the_primary(product, cloned, live, fresh_project) -> None:
    dest, _ = cloned
    project = fresh_project()
    started = time.monotonic()
    run = live("data", "import", product.name, cwd=project)
    seconds = time.monotonic() - started
    assert run.returncode == 0, run.stderr
    landed = _landed(product, "import", project, seconds)
    # What the producer tracks for its primary, by the clone's own pointers.
    primary = Path(json.loads((dest / "metadata.json").read_text(encoding="utf-8"))["data_products"]["primary"]).as_posix()

    def inside(key: str) -> bool:  # one test for both sides, so they cannot disagree on what the primary holds
        return key == primary or key.startswith(f"{primary}/")

    tracked = {k: v for k, v in assert_landed_match_pointers(dest).items() if inside(k)}
    imported = {k.split("/", 3)[3]: v for k, v in landed.items()}  # under data/imports/<product>/
    differ = sum(imported.get(k) != v for k, v in tracked.items())
    # dvc imports a folder whole, so its git-tracked files (the .gitignore dvc writes) come too: each must be the
    # clone's own file in the primary, same md5
    extra = sum(not (inside(k) and (dest / k).is_file() and md5_file(dest / k) == v)
                for k, v in imported.items() if k not in tracked)
    assert not differ and not extra, (f"the import differs from the clone's {primary}: {differ} of {len(tracked)} "
                                      f"tracked files missing or different, {extra} other files not the clone's own there")


def test_a_killed_clone_restarts_and_finishes(product, live, tmp_path) -> None:
    dest = tmp_path / "product"
    cache = dest / ".dvc" / "cache" / "files" / "md5"
    with live.start("data", "clone", product.name, "--dest", str(dest), cwd=tmp_path) as run:
        while run.proc.poll() is None and not _whole_objects(cache):
            time.sleep(0.2)
        SEEN[product.name] = SEEN[product.name].stricter(dest)
        if run.proc.poll() is not None:
            done = run.wait()
            assert done.returncode == 0, done.stderr
            landed = assert_landed_match_pointers(dest)  # it finished: everything must be there
            RESULTS.append(_line(product, "killed clone",
                                 f"not interrupted: mintd finished, {len(landed)} files, before any object was seen"))
            pytest.skip("too small to interrupt: mintd finished before any object was seen")
        cached = len(_whole_objects(cache))
        run.interrupt()
        # ponytail: Ctrl-C lets fast-sync's ThreadPoolExecutor drain its queue
        # (_fast_sync_ops.py:1627); the grace bounds it, and the report says how it stopped
        try:
            stopped = f"exit {run.wait(GRACE).returncode} on Ctrl-C"
        except subprocess.TimeoutExpired:
            stopped = f"killed: Ctrl-C did not stop it within {GRACE} s"
    at_stop = len(_whole_objects(cache))
    stopped += f"; {cached} objects cached at the interrupt, {at_stop} when it stopped"
    started = time.monotonic()
    rerun = live("data", "pull", cwd=dest)  # a second clone into dest is refused; mintd's own hint is a pull
    seconds = time.monotonic() - started
    assert rerun.returncode == 0, rerun.stderr
    landed = assert_landed_match_pointers(dest)
    objects = len(set(landed.values()))
    if at_stop >= objects:  # the rerun had nothing left to fetch, so no restart was checked
        RESULTS.append(_line(product, "killed clone", f"not interrupted: all {objects} objects had landed ({stopped})"))
        pytest.skip(f"not interrupted: all {objects} objects had landed when mintd stopped ({stopped})")
    RESULTS.append(_line(
        product, "killed clone", f"{len(landed)} files after the rerun ({stopped})", f"{seconds:.1f} s rerun",
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
    RESULTS.append(_line(
        product, "time", "measured", f"mintd {mintd_seconds:.1f} s, plain {plain_seconds:.1f} s, ratio {ratio:.2f}",
    ))
    k = request.config.getoption("--live-k")
    assert k is None or ratio <= k, f"mintd's clone took {ratio:.2f} times plain git clone + dvc pull, over k={k}"
