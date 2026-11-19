"""`mintd._s3`: the one module that imports boto3 or botocore."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("missing", [["boto3"], ["boto3", "botocore"]], ids=["boto3", "boto3-and-botocore"])
def test_a_broken_install_says_reinstall(tmp_path: Path, missing: list[str]) -> None:
    """boto3 is a hard dependency, but an install without it must still import
    mintd and say "reinstall", not crash. A fresh process: in this one,
    `mintd.cli` is already imported, so patching `sys.modules` here proves
    nothing about import time.

    Mutation that must redden this: an unguarded `from botocore... import` on
    `mintd.cli`'s import path (as `_s3_listing_ops.py` had)."""
    (tmp_path / ".dvc").mkdir()
    patch = "; ".join(f"sys.modules[{m!r}] = None" for m in missing)
    code = (
        f"import sys; {patch}; from mintd import cli; "
        f"raise SystemExit(cli.main(['data', 'pull', '--path', {str(tmp_path)!r}]))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    err = " ".join(r.stderr.split())
    assert r.returncode == 2, r.stderr
    assert "mintd's boto3 dependency is missing" in err
    assert "pip install 'dvc[s3]'" in err
    assert "Traceback" not in r.stderr


# --- the connection pool follows the thread count -----------------------------


@pytest.fixture
def client_pools(s3_versioned, monkeypatch: pytest.MonkeyPatch):
    """``max_pool_connections`` of every S3 client built after this fixture, read
    from real clients at boto3's boundary (no mintd function is replaced).
    Depends on ``s3_versioned`` so that fixture's own client is built first."""
    import boto3.session

    for key in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/mintd-tests-no-config-file")  # a developer's ~/.aws/config can't redirect it
    real = boto3.session.Session.client
    pools: list[int] = []

    def spy(self, *args, **kwargs):
        client = real(self, *args, **kwargs)
        pools.append(client.meta.config.max_pool_connections)
        return client

    monkeypatch.setattr("boto3.session.Session.client", spy)
    return pools, s3_versioned[1]


@pytest.mark.parametrize(("pool", "want"), [(None, 10), (4, 10), (16, 16), (32, 32)])
def test_a_client_pools_as_many_connections_as_its_threads_and_never_fewer_than_10(client_pools, pool, want) -> None:
    from mintd import _s3

    pools, _ = client_pools
    _s3.create_client({"region": "us-east-1"}, None, pool=pool)
    assert pools == [want]


@pytest.mark.parametrize("jobs", [4, 32])
def test_fast_sync_pools_one_connection_per_job(client_pools, tmp_path: Path, jobs: int) -> None:
    """Mutation that must redden this: drop ``pool=jobs`` in ``try_fast_pull``."""
    from mintd._fast_sync_ops import SubprocessFastSyncOps

    pools, bucket = client_pools
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(
        f"[core]\n    remote = origin\n['remote \"origin\"']\n    url = s3://{bucket}\n"
    )
    SubprocessFastSyncOps().try_fast_pull(project_path=tmp_path, targets=[], remote_name="origin", jobs=jobs)
    assert pools == [max(jobs, 10)]


def test_the_cache_lane_pools_one_connection_per_job(client_pools, tmp_path: Path) -> None:
    """Push's and pull's transfer clients get ``--jobs`` connections; the
    one-thread listing clients keep the default. Mutation that must redden this:
    drop ``pool=jobs`` in ``cache_push`` or ``cache_pull``."""
    from mintd._cache_ops import cache_pull, cache_push
    from mintd._config import Config
    from mintd._console import Reporter

    pools, bucket = client_pools
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\n    url = s3://{bucket}/lab/proj\n')
    (tmp_path / "a.bin").write_bytes(b"x" * 64)
    cache_push(project_path=tmp_path, paths=["a.bin"], config=Config(), reporter=Reporter(json_mode=True), jobs=32)
    cache_pull(project_path=tmp_path, config=Config(), reporter=Reporter(json_mode=True), jobs=32)
    assert pools.count(32) == 2, pools


def test_the_push_check_pools_as_many_connections_as_its_probe_threads(client_pools, tmp_path: Path) -> None:
    """``probe_versions`` runs 16 HEADs at once on the push check's client.
    Mutation that must redden this: ``pool=8`` in ``_push_verify.prepare``."""
    import inspect

    from mintd import _s3
    from mintd._push_verify import prepare

    pools, bucket = client_pools
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(
        f"[core]\n    remote = origin\n['remote \"origin\"']\n    url = s3://{bucket}/p\n    version_aware = true\n"
    )
    prepared = prepare(tmp_path, remote_arg=None, selection=None, aws_profile=None)
    if prepared is not None:
        prepared.close()
    assert pools == [inspect.signature(_s3.probe_versions).parameters["jobs"].default]
