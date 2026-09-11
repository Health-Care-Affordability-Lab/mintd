"""Shared fixtures for slice-3 tests.

`remote_registry` builds a local bare git repo with a seeded catalog tree —
used by anything that needs a "registry to clone from" without going to
GitHub.
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from tests._harness.git import _git

# Fixtures live beside the builders they wrap; importing them here is what
# registers them suite-wide. `pytest_plugins` is not an option — pytest
# rejects it in a non-rootdir conftest.
from tests._harness.consumer import consumer_project  # noqa: F401
from tests._harness.dvc import real_dvc  # noqa: F401
from tests._harness.producer import local_producer  # noqa: F401
from tests._harness.synthetic import synthetic_project  # noqa: F401


def _init_remote(tmp_path: Path, *, with_seed: bool) -> Path:
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)])

    seed = tmp_path / "_seed"
    _git(["clone", str(remote), str(seed)])
    _git(["-c", "init.defaultBranch=main", "checkout", "-b", "main"], cwd=seed)

    (seed / "catalog" / "data").mkdir(parents=True)
    (seed / "catalog" / "code").mkdir(parents=True)
    (seed / "catalog" / "project").mkdir(parents=True)
    (seed / "catalog" / "enclave").mkdir(parents=True)
    (seed / ".gitkeep").write_text("")  # ensure the initial commit isn't empty

    if with_seed:
        (seed / "catalog" / "data" / "seed_alpha.yaml").write_text(
            "project:\n"
            "  name: seed_alpha\n"
            "  type: data\n"
            "  full_name: data_seed_alpha\n"
            "metadata:\n"
            "  description: seed entry\n"
            "  tags: []\n"
        )

    _git(["-c", "user.email=test@mintd", "-c", "user.name=test", "add", "-A"], cwd=seed)
    _git(["-c", "user.email=test@mintd", "-c", "user.name=test", "commit", "-m", "initial"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    return remote


@pytest.fixture
def remote_registry(tmp_path: Path) -> Path:
    """A local bare git repo, seeded with one catalog entry on `main`.

    Use for cache tests that benefit from pre-existing content. Returns the
    bare repo's path (pass as `registry_url`).
    """
    return _init_remote(tmp_path, with_seed=True)


@pytest.fixture
def remote_registry_empty(tmp_path: Path) -> Path:
    """A local bare git repo with the catalog tree initialized but no
    seeded entries. Use for parameterized client tests where the test
    controls all visible entries."""
    return _init_remote(tmp_path, with_seed=False)


@pytest.fixture
def s3_versioned():
    """A moto-backed, versioning-enabled S3 bucket (matches the real bucket).

    Yields ``(client, bucket)``. Shared home for fast-sync and share/transport
    tests — one moto bucket definition, not two."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = "test-bucket"
        client.create_bucket(Bucket=bucket)
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        yield client, bucket


@pytest.fixture(autouse=True)
def _real_aws_credentials_are_unreachable(monkeypatch):
    """Keep the developer's real ``~/.aws/credentials`` out of every test.

    A ratchet, not a bug fix. Tests that need the writer pass an explicit
    ``credentials_path=`` / ``aws_credentials_path=`` under ``tmp_path``;
    without this, ``interactive_setup``'s default resolution reads the real
    file and those tests take a different branch depending on whether the
    machine happens to have a ``[mintd]`` profile.
    """
    from mintd import _aws_credentials

    def _boom() -> Path:
        raise AssertionError(
            "default_credentials_path() called — that is the real "
            "~/.aws/credentials; pass an explicit path under tmp_path instead"
        )

    monkeypatch.setattr(_aws_credentials, "default_credentials_path", _boom)
    # Same ratchet, other channel -- and the HERMETIC DEFAULT that keeps the
    # boom from firing on every test. A developer's ambient
    # $AWS_SHARED_CREDENTIALS_FILE must not steer Config.aws_profile_name,
    # but merely deleting it sends resolution to the home fallback -- the
    # boom -- from EVERY test that transitively touches aws_profile_name
    # (113 of them, measured 2026-08-29 the moment resolution gained one
    # chokepoint; before that they read the developer's REAL credentials
    # file, green and unhermetic). Pointing the env var at a path that never
    # exists gives every test "no file, no profile" deterministically. A test
    # that wants a profile sets the env var itself; one that wants the HOME
    # FALLBACK branch deletes the env var and overrides the boom at the
    # default_credentials_path seam.
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", "/mintd-tests-no-credentials-file"
    )


@pytest.fixture(autouse=True)
def _in_process_dvc_is_hermetic(tmp_path: Path):
    """Redirect dvc's config and site cache for EVERY test, in-process included.

    `tests/_harness/dvc.py` sets these for the `real_dvc` *subprocess* and
    `_dvc_ops._env()` sets them for production's spawns, but an in-process
    `dvc.repo.Repo` reads `dvc.dirs` from this process's own environment, which
    nothing was redirecting.

    MEASURED before this fixture existed — a single `publish_payload`:
      - inherited **28 of the developer's real S3 remote definitions** and
        `core.autostage=True` from `~/Library/Application Support/dvc/config`;
      - wrote a 116 KB site-cache entry to `/Library/Caches/dvc/repo`, which had
        reached **169 MB / 566 entries created in one day** of running this
        suite — outside `tmp_path`, so `tmp_path_retention_policy` never
        reclaims it.

    The config half is the worse one: a fixture that reads the developer's real
    remotes is a fixture whose result depends on whose laptop it runs on.

    `DVC_NO_ANALYTICS` is set here too, so subprocess dvc spawned by a test that
    does not go through `dvc_env()` still cannot phone home — the
    `payload_journey` fixture's own `dvc init` was doing exactly that.

    The knobs are dvc's own (`dvc/dirs.py`) and are read per call rather than
    cached at import, so setting them here reaches a `Repo` built later in the
    test. `HOME` is deliberately not relied on — see `tests/_harness/dvc.py`.

    Pinned by ``test_in_process_dvc_leaves_no_state_in_the_pytest_process``.
    """
    dvc_home = tmp_path / "_dvc-hermetic"
    redirect = {
        "DVC_GLOBAL_CONFIG_DIR": str(dvc_home / "global"),
        "DVC_SYSTEM_CONFIG_DIR": str(dvc_home / "system"),
        "DVC_SITE_CACHE_DIR": str(dvc_home / "site"),
        "DVC_NO_ANALYTICS": "1",
    }
    # Deliberately NOT `monkeypatch.setenv`. Requesting `monkeypatch` from an
    # autouse fixture forces it to be created before the autouse fixtures below
    # it, which inverts teardown order: the cwd guard in
    # `_in_process_dvc_leaves_no_handlers` then runs BEFORE
    # `monkeypatch.chdir`'s undo and reports a leak for every test that legally
    # chdir'd — measured, 44 teardown errors across the suite. Managing the two
    # env vars by hand keeps the autouse fixtures independent of monkeypatch's
    # position in the graph.
    previous = {key: os.environ.get(key) for key in redirect}
    os.environ.update(redirect)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@pytest.fixture(autouse=True)
def _in_process_dvc_leaves_no_handlers():
    """Strip the log handlers `dvc.repo.Repo` installs on import.

    dvc is a CLI first: importing it and building a `Repo` attaches handlers to
    the ``dvc``, ``dvc_data`` and ``dvc_objects`` loggers, and unlike a subprocess — which takes its logging
    config to the grave — an in-process `Repo` leaves them wired into *this*
    process. Every later test in the session then emits dvc's output into
    pytest's captured streams, which is how one payload test turns into a
    suite-wide diff.

    This lives here rather than inside ``publish_payload`` on purpose. Putting
    it in the builder would make the cleanup a property of *calling the
    builder*, so a test that imports `dvc.repo` any other way — or a future
    second entry point — would silently not get it. An autouse fixture pins the
    invariant to the process, which is what the invariant is actually about.
    Pinned by ``test_in_process_dvc_leaves_no_state_in_the_pytest_process``.
    """
    import logging

    before = os.getcwd()
    yield
    # dvc's `logger.setup()` attaches the same four handlers to all three of
    # these, not just "dvc" — clearing one leaves the other two wired in.
    for _name in ("dvc", "dvc_data", "dvc_objects"):
        logging.getLogger(_name).handlers.clear()
    # A `Repo(".")` built under `contextlib.chdir` restores cwd on exit; a
    # crash *inside* the with-block does too. A leak here means someone
    # chdir'd without one, and the failure it causes otherwise lands in an
    # unrelated test several files later.
    try:
        after = os.getcwd()
    except OSError:
        # cwd was left inside a directory that has since been deleted — a
        # `tmp_path` a later fixture tore down. `os.getcwd()` itself raises
        # here, so without this branch the leak surfaces as a bare
        # `FileNotFoundError` from whichever fixture happens to call it next.
        after = "<deleted directory>"
    if after != before:
        os.chdir(before)  # heal it, so the next test is not a second victim
        raise AssertionError(
            f"test left the process cwd at {after}, not {before}. Use "
            "`monkeypatch.chdir` or `contextlib.chdir`, not a bare `os.chdir`."
        )
