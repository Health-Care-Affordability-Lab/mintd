"""CI's Windows job runs the suite in shards, one per runner (--test-shard i/n, the hook in
tests/conftest.py). A test that fell in no shard would never run there, and nothing would say so, so the
shards must split the suite exactly."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[1]


def _collect(shard: str | None, *paths: str) -> subprocess.CompletedProcess[str]:
    """`pytest --collect-only` in a fresh process, with `--test-shard shard` unless `shard` is None."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    args = [] if shard is None else ["--test-shard", shard]
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args, *paths],
                          cwd=_REPO, env=env, capture_output=True, text=True)


def _tests(shard: str | None) -> list[str]:
    result = _collect(shard)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_the_shards_split_the_whole_suite_exactly() -> None:
    """Each test of the whole suite is in exactly one of CI's three shards, and every shard has tests.
    Mutations: keep every test in every shard (no filter); compare with i instead of i - 1 -> red."""
    everything = _tests(None)
    shards = [_tests(f"{i}/3") for i in (1, 2, 3)]
    assert all(shards), [len(s) for s in shards]
    seen = [test for shard in shards for test in shard]
    assert len(seen) == len(set(seen)), "a test is in more than one shard"
    assert sorted(seen) == sorted(everything), "a test is in no shard"


@pytest.mark.parametrize("shard", ["", "0/3", "4/3", "1/0", "3", "a/3", " 1/3", "1/3 "])
def test_a_shard_that_isnt_i_of_n_stops_the_run(shard: str) -> None:
    """A typo in the shard must fail the run, never run nothing and pass. Mutation: run every test on a bad
    value -> red."""
    result = _collect(shard, "tests/test_test_shards.py")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "--test-shard must be i/n" in result.stdout + result.stderr


def test_ci_runs_every_shard_and_one_required_check_reports_them() -> None:
    """test.yml's Windows matrix runs shards 1 to N, its Test step passes --test-shard with that same N, and the
    required check windows-test (3.13) passes only when every shard did. It must run even after a shard fails:
    a skipped job would leave the required check skipped, and GitHub counts that as passed.
    Mutations: drop a shard from the matrix; another N in the Test step; drop always(); accept any result -> red."""
    jobs = yaml.safe_load((_REPO / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8"))["jobs"]
    shard_job, required = jobs["windows-shard"], jobs["windows-test"]
    test_step = next(step for step in shard_job["steps"] if step.get("name") == "Test")
    assert test_step["env"] == {"SHARD": "${{ matrix.shard }}"}
    n = re.search(r'--test-shard "\$env:SHARD/([0-9]+)"$', test_step["run"].strip())
    assert n is not None, test_step["run"]
    assert shard_job["strategy"]["matrix"]["shard"] == list(range(1, int(n[1]) + 1))
    assert (required["name"], required["needs"], required["if"]) == ("windows-test (3.13)", "windows-shard", "always()")
    check = required["steps"][-1]
    assert check["env"] == {"RESULT": "${{ needs.windows-shard.result }}"}
    assert check["run"].strip() == 'test "$RESULT" = success'
