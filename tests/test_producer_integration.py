"""Integration tests for `GitArchiveFetcher` against a real local bare repo.

Skipped by default. Run with `MINTD_RUN_INTEGRATION=1 pytest -m integration`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mintd._producer_git_ops import FetchError, GitArchiveFetcher
from tests._harness.git import _git

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MINTD_RUN_INTEGRATION") != "1",
        reason="set MINTD_RUN_INTEGRATION=1 to run",
    ),
]


def _init_producer_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    bare = tmp_path / "producer.git"
    _git(["init", "--bare", "-b", "main", str(bare)])

    seed = tmp_path / "_seed"
    _git(["clone", str(bare), str(seed)])

    metadata = {
        "schema_version": "2.0",
        "data_products": {"primary": "outputs/main.parquet", "outputs": []},
    }
    (seed / "metadata.json").write_text(json.dumps(metadata))
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "add", "metadata.json"], cwd=seed)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "seed"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    pin = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(seed),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Allow `git archive --remote` against the local bare repo.
    _git(["config", "-f", str(bare / "config"), "uploadarch.allowed", "true"])

    return bare, pin


def test_real_git_archive_against_local_bare_repo(tmp_path: Path) -> None:
    bare, pin = _init_producer_bare_repo(tmp_path)

    result = GitArchiveFetcher().fetch_metadata_at(str(bare), pin)

    parsed = json.loads(result)
    assert parsed["schema_version"] == "2.0"


@pytest.mark.xfail(
    strict=True,
    reason="scope boundary: GitArchiveFetcher reports UNREACHABLE for an unknown pin; "
    "PIN_MISSING is owed by the unit that owns --rev. strict=True so the XPASS "
    "fires the day it is fixed — but the module skipif above gates that on "
    "MINTD_RUN_INTEGRATION=1, which no CI job sets, so the trip only happens "
    "when a human runs the gate by hand.",
)
def test_real_unknown_pin_raises_pin_missing(tmp_path: Path) -> None:
    bare, _ = _init_producer_bare_repo(tmp_path)
    fake_pin = "0" * 40

    with pytest.raises(FetchError) as ei:
        GitArchiveFetcher().fetch_metadata_at(str(bare), fake_pin)

    assert ei.value.reason == FetchError.Reason.PIN_MISSING
