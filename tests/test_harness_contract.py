"""What `tests/_harness/` promises.

Not a test of mintd — a test of the fixtures, which are now load-bearing for
several downstream units. Each case here names the capability no helper in the
tree had before, so a regression in a builder fails next to the reason it
exists rather than four modules away.

Every assertion is on an artifact — a SHA, a file dvc parsed, a finding a real
walker produced — never on a double's recorded call.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mintd._config import Timeouts
from mintd._dvc_ops import DvcOpError, SubprocessDvcOps
from mintd._fast_sync_ops import (
    SubprocessFastSyncOps,
    parse_dvc_lock_outs,
    partition_pipeline_outs,
)
from mintd.catalog import InMemoryCatalogClient
from mintd.check import check_project
from mintd.data_ops import PullSummary, data_pull
from mintd.imports import scan_imports
from mintd.model import Metadata
from mintd.producer import ProducerError, ProducerView
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._harness import HarnessError
from tests._harness.consumer import Import
from tests._harness.expect import Mismatch, assert_landed_match_pointers, md5
from tests._harness.git import _git
from tests._harness.producer import LocalProducer, build_local_producer
from tests._harness.live import (
    MARKER, LiveMintd, Product, catalog_entry, check_basetemp, read_only, readonly_credentials, redact,
    run_read_only,
)

FIXTURES = Path(__file__).parent / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"

ALPHA_URL = "https://github.com/example-org/alpha"
BETA_URL = "https://github.com/example-org/beta"


def _catalog_pointing_at(url: str) -> InMemoryCatalogClient:
    """A catalog whose `demo-xw` entry resolves to `url` — the manifest
    fixture approves that repo by name, so this is what makes the enclave arm
    reach a producer instead of a 404."""
    client = InMemoryCatalogClient()
    data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "demo-xw"
    data["project"]["full_name"] = "data_demo-xw"
    data["repository"]["github_url"] = url
    client.register(Metadata.model_validate(data))
    return client


# ---------------------------------------------------------------------------
# local_producer
# ---------------------------------------------------------------------------


def test_producer_metadata_loads_through_producer_view(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The producer serves metadata the *production* reader accepts.

    The integration stub (`test_producer_integration.py:43-46`) is two keys, so
    `Metadata.model_validate_json` rejects it with eight missing sections
    (`access_control, governance, metadata, mint, ownership, project,
    repository, status`). A producer that cannot be read by `ProducerView` can
    only ever be used with a stubbed factory, which is the archetype this
    harness exists to end.
    """
    view = ProducerView.try_at(
        local_producer.url, local_producer.head_sha, cache_dir=tmp_path / "pcache"
    )

    assert not isinstance(view, ProducerError), getattr(view, "detail", view)
    assert view.metadata.data_products.primary == "data/final/"


def test_producer_serves_the_git_archive_fast_path(
    local_producer: LocalProducer,
) -> None:
    """The bare repo answers `git archive --remote`, so `ProducerView` takes
    the fast path rather than `_fallback_clone`
    (`src/mintd/_producer_git_ops.py:189`).

    Pinned by SHA on purpose. The fetcher always sends one — `at_head`
    resolves HEAD through `_git_ls_remote_head` first — and a SHA is not an
    advertised ref, so the remote refuses it unless the repo opts in. A probe
    using a *ref* name succeeds either way and would certify nothing: that is
    how `tests/test_producer_integration.py:52`'s `uploadarch.allowed` (not a
    git config key) went unnoticed while every fetch there silently cloned.
    """
    result = subprocess.run(
        [
            "git",
            "archive",
            "--remote",
            local_producer.url,
            local_producer.head_sha,
            "metadata.json",
        ],
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout, "empty archive"


def test_builders_do_not_alias_across_calls(
    consumer_project, synthetic_project
) -> None:
    """Two calls, two trees.

    A shared directory makes the merge silent: the builder hands back the same
    `Path`, `exist_ok=True` accepts the second `mkdir`, and `metadata.json` is
    re-copied over any edit — so a test asserting on "its" project passes while
    measuring the other one.
    """
    first = consumer_project(
        imports=[Import(name="alpha", producer_url=ALPHA_URL, pin="a" * 40)]
    )
    second = consumer_project(
        imports=[Import(name="beta", producer_url=BETA_URL, pin="b" * 40)]
    )

    assert first != second
    assert [d.source.name for d in scan_imports(first)] == ["alpha.dvc"]
    assert [d.source.name for d in scan_imports(second)] == ["beta.dvc"]

    flat = synthetic_project()
    templated = synthetic_project(foreach=True)

    assert flat != templated
    assert "  build:\n" in (flat / "dvc.yaml").read_text(encoding="utf-8")
    assert "  base:\n" in (templated / "dvc.yaml").read_text(encoding="utf-8")


def test_commit_more_advances_head_and_keeps_primary(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """A producer that moved without moving its product.

    This is the distinction drift detection turns on, and `_view_with_primary`
    (`tests/test_check.py:170`) cannot express it: it returns a `ProducerView`
    hardcoded to one pin (`:187`), so "later commit, same primary" and "later
    commit, different primary" are the same object.
    """
    before = local_producer.head_sha
    after = local_producer.commit_more()

    assert after != before

    cache = tmp_path / "pcache"
    view_before = ProducerView.try_at(local_producer.url, before, cache_dir=cache)
    view_after = ProducerView.try_at(local_producer.url, after, cache_dir=cache)

    assert not isinstance(view_before, ProducerError)
    assert not isinstance(view_after, ProducerError)
    assert (
        view_before.metadata.data_products.primary
        == view_after.metadata.data_products.primary
        == "data/final/"
    )


def test_moved_tag_resolves_to_the_new_commit(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """A tag that is re-pointed at a later commit, and a resolution that sees it.

    The producer-cache staleness family hangs off exactly this: nothing in the
    tree can move a tag, so "the pin you cached is no longer what the tag
    means" has never been constructible.
    """
    local_producer.tag("v1")
    before = local_producer.resolve_remote_tag("v1")
    assert before == local_producer.head_sha
    assert local_producer.remote_tags() == ["v1"]

    local_producer.rename_primary("data/v2/")
    local_producer.move_tag("v1")

    after = local_producer.resolve_remote_tag("v1")
    assert after != before
    assert after == local_producer.head_sha

    view = ProducerView.try_at(local_producer.url, after, cache_dir=tmp_path / "pcache")
    assert not isinstance(view, ProducerError)
    assert view.metadata.data_products.primary == "data/v2/"


#: One flat out and one directory out. The directory is not decoration: it is
#: what drives dvc's `.dir` manifest, the piece mechanism (b) would have had to
#: re-implement and the piece a pointer-only assertion never touches.
PAYLOAD = {
    "data/final.csv": b"a,b\n1,2\n",
    "data/parts": {"p1.csv": b"1\n", "p2.csv": b"2\n"},
}


def _clone(producer: LocalProducer, dest: Path) -> Path:
    """What a consumer does: a plain git clone of the bare repo. The payload
    is NOT here yet — only the `.dvc` pointers are — which is the distinction
    every test below turns on."""
    _git(["clone", producer.url, str(dest)])
    return dest


def _pull(project: Path, targets: list[str] | None = None) -> PullSummary:
    """Production's own pull, through the real dvc seam, with the REAL
    `SubprocessFastSyncOps` — not a double and no longer `fast_sync_ops=None`,
    which issue18 removed from the signature. The route is unchanged:
    `test_local_remote_degrades_fast_sync_to_the_fallback_route` asserts that a
    local-directory remote degrades every target to `fallback_targets` before a
    byte moves, so this lane still certifies the FALLBACK route. What changed
    is that it now reaches it through the code production runs.

    This helper used to wrap the call in `os.chdir(project)` / `finally:
    os.chdir(cwd)`, mirroring the same block `data.py` carried, because
    `DvcOps` had no way to say which repo a pull acted on. Unit A gave it one,
    so `project_path` now does the aiming and the chdir is gone. Deleting it
    is the strongest gate in that unit: with `data_pull` no longer passing
    `cwd`, the three real-dvc tests below fail outright rather than quietly
    passing on ambient process state."""
    return data_pull(
        project_path=project,
        targets=targets,
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()),
        fast_sync_ops=SubprocessFastSyncOps(),
    )



@pytest.mark.skipif(sys.platform == "win32", reason="DVC's trust of a read-only cache object, reproduced on POSIX")
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Target: a cached .dir that does not hash to its name, left read-only as DVC leaves its cache objects, "
    "is trusted by the fallback dvc pull, so pull returns normally with p2.csv missing; it should land both "
    "files or exit non-zero. Predates the enclave change; the fix is follow-up work"
))
def test_a_pull_never_returns_normally_with_a_file_its_cached_tree_dropped(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """A changed .dir in the clone's cache (valid JSON, p2.csv dropped, read-only
    again): pull must repair it, so both files land, or fail. Either fix flips
    this Target."""
    from mintd._dvc_state import dvc_cache_dir
    from mintd.errors import MintdError

    local_producer.publish(PAYLOAD)
    clone = _clone(local_producer, tmp_path / "consumer")
    _pull(clone)
    (listing,) = dvc_cache_dir(clone).rglob("*.dir")
    listing.chmod(0o644)
    entries = [e for e in json.loads(listing.read_text(encoding="utf-8")) if e["relpath"] != "p2.csv"]
    listing.write_text(json.dumps(entries), encoding="utf-8")
    listing.chmod(0o444)
    shutil.rmtree(clone / "data" / "parts")

    try:
        summary = _pull(clone)
    except MintdError:
        return  # pull failed, which the CLI turns into exit 1
    if summary.error_count:
        return  # reported as not delivered, which the CLI turns into exit 1
    assert sorted(p.name for p in (clone / "data" / "parts").iterdir()) == ["p1.csv", "p2.csv"]

def test_clone_from_local_producer_lands_payload_bytes(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The capability this whole slice exists for: a consumer clone that ends
    with the producer's real bytes on disk.

    Until now "the consumer pulled it" was always a double's return value.
    Here it is `stat` + `read_bytes` on files a real `dvc pull` fetched from a
    real remote, so a pull that silently no-ops has somewhere to fail.
    """
    local_producer.publish(PAYLOAD)
    clone = _clone(local_producer, tmp_path / "consumer")

    assert not (clone / "data" / "final.csv").exists(), (
        "a fresh clone must carry pointers only — if the bytes are already "
        "here, the pull below proves nothing"
    )

    _pull(clone)

    assert (clone / "data" / "final.csv").read_bytes() == PAYLOAD["data/final.csv"]
    for name, body in PAYLOAD["data/parts"].items():
        assert (clone / "data" / "parts" / name).read_bytes() == body


def test_published_payload_is_byte_identical_to_a_subprocess_dvc_push(
    local_producer: LocalProducer, tmp_path: Path, real_dvc
) -> None:
    """The licence for mechanism (c).

    `publish_payload` drives `dvc.repo.Repo` in-process; mintd itself drives
    the `dvc` CLI (`_dvc_ops.py`, `[*dvc_cmd(), ...]`). mintd pins a **range**
    (`pyproject.toml:12`, `dvc >= 3.66, < 4.0`), so nothing but this test
    stands between "the harness is dvc" and "the harness is a second dvc that
    used to agree". Build the same payload both ways; diff the artifacts.

    Reds the day the in-process API diverges from the CLI — which is the day
    every payload assertion in this suite silently starts certifying the wrong
    thing. That is why mechanism (a) is kept alive here and nowhere else.
    """
    local_producer.publish(PAYLOAD)

    # Mechanism (a): same outs, same remote layout, through the CLI.
    cli_work = tmp_path / "cli-work"
    cli_remote = tmp_path / "cli-remote"
    cli_remote.mkdir()
    _git(["init", "-b", "main", str(cli_work)])
    real_dvc(["init"], cwd=cli_work, check=True)
    real_dvc(["remote", "add", "-d", "storage", str(cli_remote)], cwd=cli_work, check=True)
    for rel, body in PAYLOAD.items():
        target = cli_work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, dict):
            target.mkdir(exist_ok=True)
            for name, blob in body.items():
                (target / name).write_bytes(blob)
        else:
            target.write_bytes(body)
        real_dvc(["add", rel], cwd=cli_work, check=True)
    real_dvc(["push"], cwd=cli_work, check=True)

    def _tree(root: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }

    # The remote object tree: same content addresses, same bytes. This is the
    # assertion that would catch a cache-layout or hash-algorithm change.
    assert _tree(local_producer.remote) == _tree(cli_remote)

    # And the pointers the consumer actually reads. `.dvc` files carry an
    # `md5`/`size`/`path` triple; a divergence here means a consumer resolving
    # the same product would address different blobs.
    for rel in PAYLOAD:
        ours = (local_producer.work / f"{rel}.dvc").read_text(encoding="utf-8")
        theirs = (cli_work / f"{rel}.dvc").read_text(encoding="utf-8")
        assert ours == theirs, f"pointer divergence for {rel}"


def test_targeted_pull_lands_only_the_requested_out(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The negative control. A pull that fetched *everything* would pass
    `test_clone_from_local_producer_lands_payload_bytes` just as well, so
    without this one that test cannot tell delivery from a blanket fetch."""
    local_producer.publish(PAYLOAD)
    clone = _clone(local_producer, tmp_path / "consumer")

    _pull(clone, targets=["data/parts.dvc"])

    assert (clone / "data" / "parts" / "p1.csv").read_bytes() == b"1\n"
    assert not (clone / "data" / "final.csv").exists(), (
        "targeted pull fetched an out nobody asked for"
    )


def test_pipeline_stage_out_is_servable(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """A `dvc.lock` that **dvc wrote**, parsed by production's own reader.

    Every lock in this suite before now was hand-authored text, so
    `parse_dvc_lock_outs` had never once been fed dvc's own output — it was
    checked against the suite's idea of the format, which is exactly how a
    parser and a producer drift apart. Resolver S5-S6 gates on this.
    """
    local_producer.publish_pipeline(
        "stages:\n"
        "  build:\n"
        "    cmd: python -c \"import pathlib; "
        "d=pathlib.Path('data/built'); d.mkdir(parents=True, exist_ok=True); "
        "(d/'out.csv').write_bytes(b'x\\n')\"\n"
        "    outs:\n"
        "      - data/built\n"
    )

    lock = local_producer.work / "dvc.lock"
    assert lock.exists(), "reproduce() wrote no lock — the stage did not run"

    # `partition_pipeline_outs`, the function the criterion names — not
    # `parse_dvc_lock_outs`, which it wraps. Behaviourally identical for the
    # `all_outs` half (`_fast_sync_ops.py:582` returns it verbatim), so the
    # substitution changed no verdict; calling the named one anyway removes a
    # discrepancy between the criterion and the test that satisfies it.
    fast_syncable, all_outs = partition_pipeline_outs(local_producer.work, "storage")
    assert [o.path for o in all_outs] == ["data/built"], (
        f"production's lock reader disagrees with dvc's own lock: {all_outs}"
    )

    # And the classifier half is EMPTY here, deliberately stated rather than
    # left to look like an oversight: a local-directory remote has no
    # `version_id`/`cloud` block, so `_is_fast_syncable_pipeline_out` rejects
    # every out. This lane is the fallback route — see
    # `test_local_remote_degrades_fast_sync_to_the_fallback_route`.
    assert fast_syncable == []

    clone = _clone(local_producer, tmp_path / "consumer")
    _pull(clone)
    assert (clone / "data" / "built" / "out.csv").read_bytes() == b"x\n"


def test_local_remote_degrades_fast_sync_to_the_fallback_route(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The honesty test. This lane certifies the FALLBACK route, not fast-sync.

    A local-directory remote is not an S3 url, so `try_fast_pull` degrades
    every target before a single byte moves. Stated here as an assertion
    rather than a comment so that nobody downstream reads a green payload test
    as fast-sync coverage — units 9 and 10 have to say which of their claims
    are which, and this is the artifact they can point at.
    """
    local_producer.publish(PAYLOAD)
    clone = _clone(local_producer, tmp_path / "consumer")
    targets = ["data/final.csv.dvc", "data/parts.dvc"]

    result = SubprocessFastSyncOps().try_fast_pull(
        project_path=clone, targets=targets, remote_name="storage",
    )

    assert result.success is False
    assert "non-S3 remote" in (result.reason or "")
    assert sorted(result.fallback_targets) == sorted(targets)


def test_in_process_dvc_leaves_no_state_in_the_pytest_process(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """`Repo` runs in *this* process, so its side effects outlive the call.

    A subprocess takes its logging config and its cwd to the grave; an
    imported `dvc.repo` does not. Both leaks below are silent and land in an
    unrelated test later in the session — dvc output appearing in another
    module's captured stream, or a relative path resolving somewhere new.

    Pins the autouse fixture in `conftest.py`. That cleanup deliberately does
    not live inside `publish_payload`: there it would be a property of calling
    the builder, and any other route into `dvc.repo` would skip it.
    """
    import logging

    # ASSERTED FIRST, and the ordering is the whole point. dvc attaches its
    # handlers to the `dvc` / `dvc_data` / `dvc_objects` loggers and NEVER to
    # root, so the root-handler check below is vacuous for the thing the
    # fixture actually cleans up — measured, root 0 -> 0 while `dvc` 0 -> 4,
    # and deleting `handlers.clear()` from conftest left this test green. The
    # plan's own transcript recorded that same root count and drew the same
    # false comfort from it.
    #
    # Every payload test above this one builds a `Repo`, so in a full-suite run
    # their handlers are only absent here because the autouse fixture cleared
    # them at each teardown. Run this test ALONE (`-k in_process`) and the
    # assertion is trivially true — it pins the fixture in the suite, which is
    # where the gate runs, not in isolation.
    for name in ("dvc", "dvc_data", "dvc_objects"):
        assert logging.getLogger(name).handlers == [], (
            f"{name!r} logger entered this test with handlers still attached — "
            "the autouse cleanup in conftest.py is not running, or not covering "
            "this logger. dvc output leaks into other tests' captured streams."
        )

    root_before = list(logging.getLogger().handlers)
    cwd_before = os.getcwd()

    local_producer.publish(PAYLOAD)
    local_producer.publish_pipeline(
        "stages:\n  noop:\n    cmd: python -c pass\n"
    )

    assert os.getcwd() == cwd_before
    assert list(logging.getLogger().handlers) == root_before

    # Hermeticity: the in-process `Repo` must not read the developer's real dvc
    # config. Before `_in_process_dvc_is_hermetic`, a single publish inherited
    # 28 real S3 remotes plus `core.autostage=True` and wrote a 116 KB entry to
    # `/Library/Caches/dvc/repo` — 169 MB there after one day of this suite.
    from dvc.dirs import global_config_dir, site_cache_dir

    for label, path in (
        ("global config", global_config_dir()),
        ("site cache", site_cache_dir()),
    ):
        assert str(tmp_path) in str(path), (
            f"dvc {label} resolves to {path}, outside tmp_path — the in-process "
            "lane is reading and writing the real machine"
        )



# ---------------------------------------------------------------------------
# strict_targets
# ---------------------------------------------------------------------------

#: A `dvc.yaml` whose stage writes a DIRECTORY out. Both halves matter: the
#: stage NAME and a path UNDER the dir out are the two cases the previous
#: attempt at a strict fake (`GraphAwareDvcOps`) wrongly rejected.
PIPELINE_YAML = (
    "stages:\n"
    "  build:\n"
    "    cmd: python -c \"import pathlib; "
    "d=pathlib.Path('data/built'); d.mkdir(parents=True, exist_ok=True); "
    "(d/'out.csv').write_bytes(b'x\\n')\"\n"
    "    outs:\n"
    "      - data/built\n"
    # A `foreach` stage, because `dvc.yaml` names it `fan` while `dvc.lock`
    # names the instances `fan@a` / `fan@b`, and `fan@a` is the ONLY way real
    # dvc lets you target one instance. Without this stage the oracle's graph
    # cannot express the shape at all — which is exactly how a false-reject on
    # `fan@a` shipped and had to be caught in review.
    "  fan:\n"
    "    foreach:\n"
    "      a: {out: data/fan_a.csv}\n"
    "      b: {out: data/fan_b.csv}\n"
    "    do:\n"
    "      cmd: python -c \"import pathlib; "
    "pathlib.Path('${item.out}').write_bytes(b'${key}\\n')\"\n"
    "      outs:\n"
    "        - ${item.out}\n"
)


def _graph(producer: LocalProducer, tmp_path: Path) -> Path:
    """One workspace carrying both out kinds — a `.dvc` pointer and a
    pipeline stage — plus a stage that exists ONLY in `dvc.lock`."""
    producer.publish(PAYLOAD)
    producer.publish_pipeline(PIPELINE_YAML)

    # The orphan: a stage in the lock that `dvc.yaml` never declares. This is
    # a real drift state (someone edits dvc.yaml and does not re-run), and it
    # is the one case where reading the lock for ACCEPTS silently makes a fake
    # wrong.
    lock = producer.work / "dvc.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8")
        + "  ghost:\n"
        "    cmd: python -c pass\n"
        "    outs:\n"
        "    - path: data/ghost.csv\n"
        f"      md5: {'a' * 32}\n"
        "      size: 3\n",
        encoding="utf-8",
    )
    producer._commit_and_push("orphan the lock")
    return _clone(producer, tmp_path / "consumer")


def _strict_fake(workspace: Path) -> _FakeDvcOps:
    fake = _FakeDvcOps()
    fake.workspace = workspace
    fake.strict_targets = True
    return fake


def test_strict_fake_rejects_a_lock_only_orphan(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """`data/ghost.csv` is in `dvc.lock` and in no `dvc.yaml` stage.

    Before `strict_targets`, `_FakeDvcOps.pull` recorded the call and
    returned `None` for any target at all, so it neither rejected nor
    accepted anything — "the pull succeeded" was unfalsifiable.
    """
    clone = _graph(local_producer, tmp_path)

    with pytest.raises(DvcOpError, match="ghost"):
        _strict_fake(clone).pull(targets=["data/ghost.csv"], cwd=clone)


def test_strict_fake_accepts_a_declared_out(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The other direction, and the one that is easy to get wrong.

    A fake that rejects everything passes the rejection test perfectly. This
    is the half that keeps `strict_targets` a double rather than a second,
    stricter dvc.
    """
    clone = _graph(local_producer, tmp_path)
    fake = _strict_fake(clone)

    fake.pull(targets=["data/final.csv.dvc", "data/built", "build"], cwd=clone)

    assert fake.pull_calls, "an accepted pull must still be recorded"


def test_strict_fake_rejects_an_absolute_path_outside_the_graph(
    local_producer: LocalProducer, tmp_path: Path
) -> None:
    """An absolute path nothing declares is refused, like real dvc refuses it.

    Regression. `rglob("*.dvc")` matches the **`.dvc` DIRECTORY** that every
    dvc repo has, not just pointer files, so the declared-paths list picked up
    a `""` entry (`".dvc"` minus the suffix). The prefix test for
    directory-out subpaths is `nt.startswith(f"{p}/")`, which with `p == ""`
    reads `nt.startswith("/")` — so **every absolute path was accepted**,
    `/etc/passwd` included. Verified against dvc 3.67.1: it returns rc=1,
    `'/etc/passwd' does not exist as an output or a stage name`.

    The oracle did not catch this because none of its ten rows is an absolute
    path — a reminder that an oracle only licenses the cases in its table.
    """
    clone = _graph(local_producer, tmp_path)

    with pytest.raises(DvcOpError):
        _strict_fake(clone).pull(targets=["/etc/passwd"], cwd=clone)


def test_strict_fake_agrees_with_real_dvc_on_one_graph(
    local_producer: LocalProducer, tmp_path: Path, real_dvc
) -> None:
    """The oracle. Same graph, same targets, both directions, both engines.

    Without the ACCEPT half this proves only "dvc rejects something" — which
    is exactly what issue01's own oracle turned out to prove, mutation-proven
    non-load-bearing. And an accept-only oracle is what killed
    `GraphAwareDvcOps`: measured against dvc 3.67.1, a bare stage name
    (`build`) and a directory-out subpath (`data/built/out.csv`) are both
    `rc=0`, and rejecting them would have false-failed issues 06 and 07.

    Both cases are in the table below deliberately. If the fake is ever made
    stricter than dvc, this reds on the accept half rather than shipping and
    false-failing a downstream unit.

    **The last four rows were added after the table's first version missed
    them**, and they are the reason an oracle is only as good as its cases: an
    empty target and an absolute path INSIDE the workspace are both `rc=0` on
    real dvc, and the fake rejected both. `/etc/passwd` and `.dvc` are the
    matching negatives, so accept and reject are covered on the same axis
    rather than one being assumed.

    The three `dvc.yaml:` rows pin the ROOT-qualified addresses: review r2
    proved a mutation dropping them survived the entire suite, so the accept
    the fake's own comment calls measured was bound by no test at all.
    """
    clone = _graph(local_producer, tmp_path)
    fake = _strict_fake(clone)

    targets = [
        "data/final.csv.dvc",   # a .dvc pointer
        "data/final.csv",       # the out path itself
        "data/parts",           # a directory out
        "build",                # a bare STAGE NAME
        "data/built",           # a pipeline stage's dir out
        "data/built/out.csv",   # a path UNDER a dir out
        "dvc.yaml",             # the pipeline file
        "",                     # empty: dvc takes it as no filter at all
        str(clone / "data" / "final.csv"),  # ABSOLUTE, inside the workspace
        "data/ghost.csv",       # lock-only orphan
        "nope.dvc",             # nothing declares it
        "data/nothing.csv",     # nor this
        "/etc/passwd",          # absolute and genuinely outside
        ".dvc",                 # the dvc directory itself
        "fan",                  # a foreach stage's dvc.yaml name
        "fan@a",                # a foreach INSTANCE, only nameable via the lock
        "data/fan_a.csv",       # that instance's out
        "fan@zzz",              # an instance that does not exist
        "dvc.yaml:build",       # root-QUALIFIED stage address
        "dvc.yaml:fan@a",       # root-qualified foreach instance
        "dvc.yaml:fan@zzz",     # qualified instance the lock never made
    ]

    real_verdict, fake_verdict = {}, {}
    for target in targets:
        real_verdict[target] = real_dvc(["pull", target], cwd=clone).returncode == 0
        try:
            fake.pull(targets=[target], cwd=clone)
        except DvcOpError:
            fake_verdict[target] = False
        else:
            fake_verdict[target] = True

    assert fake_verdict == real_verdict

    # Belt and braces: an oracle where everything landed on one side would
    # agree trivially. Assert the graph actually exercised both directions.
    assert True in real_verdict.values() and False in real_verdict.values()


#: A pipeline living in a SUBDIRECTORY's `dvc.yaml` — dvc resolves stage outs
#: wherever the `dvc.yaml` lives, not only at the workspace root. Same two
#: stage kinds as `PIPELINE_YAML` (plain out + `foreach`), but a FILE out,
#: because every row asserted below was measured against exactly this shape.
SUBDIR_PIPELINE_YAML = (
    "stages:\n"
    "  build:\n"
    "    cmd: python -c \"import pathlib; "
    "d=pathlib.Path('data'); d.mkdir(parents=True, exist_ok=True); "
    "(d/'built.csv').write_bytes(b'x\\n')\"\n"
    "    outs:\n"
    "      - data/built.csv\n"
    "  fan:\n"
    "    foreach:\n"
    "      a: {out: data/fan_a.csv}\n"
    "      b: {out: data/fan_b.csv}\n"
    "    do:\n"
    "      cmd: python -c \"import pathlib; "
    "pathlib.Path('${item.out}').write_bytes(b'${key}\\n')\"\n"
    "      outs:\n"
    "        - ${item.out}\n"
)


def test_strict_fake_agrees_with_real_dvc_on_a_subdir_pipeline(
    tmp_path: Path, real_dvc
) -> None:
    """The oracle again, for a `dvc.yaml` that does NOT live at the root.

    `_declared_targets` used to read `root / "dvc.yaml"` and nothing else, so
    every out a subdirectory's pipeline declares was refused — stricter than
    dvc, the one direction substrate rule 2 forbids. Measured, dvc 3.67.1,
    `sub/dvc.yaml` repro'd from inside `sub`, every pull from the workspace
    ROOT, no remote configured:

        sub/data/built.csv      rc=0   the stage out, root-relative
        sub/data/fan_a.csv      rc=0   a foreach INSTANCE's out
        sub/dvc.yaml            rc=0   the pipeline file itself
        sub/dvc.yaml:build      rc=0   qualified stage address
        sub/dvc.yaml:fan        rc=0   qualified foreach base name
        sub/dvc.yaml:fan@a      rc=0   qualified foreach instance
        sub/dvc.yaml:fan@zzz    rc=1   instance the lock never made
        build                   rc=1   BARE names resolve against the root
        fan@a                   rc=1   `dvc.yaml` only — "'dvc.yaml' does
                                       not exist"
        sub/data/nothing.csv    rc=1   nothing declares it

    The reject half matters as much as the accept half: bare stage names are
    root-only in real dvc, so a scan that widened them to every `dvc.yaml`
    would be lenient exactly where this table says dvc refuses.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-b", "main", str(ws)])
    real_dvc(["init"], cwd=ws, check=True)
    sub = ws / "sub"
    sub.mkdir()
    (sub / "dvc.yaml").write_text(SUBDIR_PIPELINE_YAML, encoding="utf-8")
    real_dvc(["repro"], cwd=sub, check=True)

    fake = _strict_fake(ws)
    targets = [
        "sub/data/built.csv",
        "sub/data/fan_a.csv",
        "sub/dvc.yaml",
        "sub/dvc.yaml:build",
        "sub/dvc.yaml:fan",
        "sub/dvc.yaml:fan@a",
        "sub/dvc.yaml:fan@zzz",
        "build",
        "fan@a",
        "sub/data/nothing.csv",
    ]

    real_verdict, fake_verdict = {}, {}
    for target in targets:
        real_verdict[target] = real_dvc(["pull", target], cwd=ws).returncode == 0
        try:
            fake.pull(targets=[target], cwd=ws)
        except DvcOpError:
            fake_verdict[target] = False
        else:
            fake_verdict[target] = True

    assert fake_verdict == real_verdict
    assert True in real_verdict.values() and False in real_verdict.values()


def test_strict_fake_survives_a_lockless_pipeline_file_earlier_in_scan_order(
    tmp_path: Path, real_dvc
) -> None:
    """A lockless `dvc.yaml` earlier in scan order must not end the scan.

    A pipeline file that was declared but never repro'd has no sibling
    `dvc.lock`, so the fake's per-file parse raises `OSError` on the lock
    read. While that handler said `return` instead of `continue`, the root
    file's missing lock silently dropped EVERY later-sorted `dvc.yaml` from
    the scan, and the fake refused a subdirectory's declared outs that real
    dvc pulls -- stricter than dvc, the one direction the module docstring
    forbids. Nothing else in the suite puts a lockless pipeline file ahead
    of a locked one, so the shape is pinned here, against the real-dvc
    oracle rather than a hand-written verdict table.

    Mutation: revert the `except (OSError, yaml.YAMLError)` handler in
    `_declared_targets` from `continue` to `return names, paths` -> the two
    accept rows below go red.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-b", "main", str(ws)])
    real_dvc(["init"], cwd=ws, check=True)
    # Root pipeline declared but never repro'd: no root `dvc.lock`, and
    # `dvc.yaml` sorts before `sub/dvc.yaml`.
    (ws / "dvc.yaml").write_text(
        "stages:\n"
        "  never:\n"
        "    cmd: python -c pass\n"
        "    outs:\n"
        "      - data/never.csv\n",
        encoding="utf-8",
    )
    sub = ws / "sub"
    sub.mkdir()
    (sub / "dvc.yaml").write_text(SUBDIR_PIPELINE_YAML, encoding="utf-8")
    real_dvc(["repro"], cwd=sub, check=True)

    fake = _strict_fake(ws)
    targets = [
        "sub/data/built.csv",
        "sub/dvc.yaml:build",
        "sub/data/nothing.csv",
    ]

    real_verdict, fake_verdict = {}, {}
    for target in targets:
        real_verdict[target] = real_dvc(["pull", target], cwd=ws).returncode == 0
        try:
            fake.pull(targets=[target], cwd=ws)
        except DvcOpError:
            fake_verdict[target] = False
        else:
            fake_verdict[target] = True

    assert fake_verdict == real_verdict
    assert True in real_verdict.values() and False in real_verdict.values()


#: A ROOT pipeline whose `foreach` stage hides `wdir` under `do:` — the same
#: spelling `_wdir_pipeline(style="foreach")` ships. `dvc.lock` records the
#: instance's out RELATIVE TO THE WDIR (`path: out_a.csv`, no `wdir` key), so
#: a scan that reads `wdir` only from the stage's top level anchors the out
#: at the pipeline file and is wrong in BOTH directions at once.
FOREACH_WDIR_PIPELINE_YAML = (
    "stages:\n"
    "  fan:\n"
    "    foreach:\n"
    "      - a\n"
    "    do:\n"
    "      wdir: work\n"
    "      cmd: python -c \"import pathlib; "
    "pathlib.Path('out_${item}.csv').write_bytes(b'x\\n')\"\n"
    "      outs:\n"
    "        - out_${item}.csv\n"
)


def test_strict_fake_agrees_with_real_dvc_on_a_foreach_wdir_pipeline(
    tmp_path: Path, real_dvc
) -> None:
    """`wdir` under `do:` anchors a foreach instance's lock out, like real dvc.

    Measured for review r2, dvc 3.67.1, the graph above repro'd at the root:

        work/out_a.csv   rc=0   the out, root-relative
        out_a.csv        rc=1   the lock's wdir-relative spelling

    Reading `wdir` from the stage's TOP level only — where `foreach` never
    puts it — made the fake reject `work/out_a.csv` and accept `out_a.csv`:
    stricter than dvc on the real out and laxer on a phantom, the double
    failure the module docstring forbids, on a shape the harness itself
    authors (`_wdir_pipeline(style="foreach")`).

    Mutation: revert `_declared_targets` to `wdir = (stages[base] or
    {}).get("wdir", ".")` -> both rows go red.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-b", "main", str(ws)])
    real_dvc(["init"], cwd=ws, check=True)
    (ws / "work").mkdir()
    (ws / "dvc.yaml").write_text(FOREACH_WDIR_PIPELINE_YAML, encoding="utf-8")
    real_dvc(["repro"], cwd=ws, check=True)

    fake = _strict_fake(ws)
    targets = ["work/out_a.csv", "out_a.csv"]

    real_verdict, fake_verdict = {}, {}
    for target in targets:
        real_verdict[target] = real_dvc(["pull", target], cwd=ws).returncode == 0
        try:
            fake.pull(targets=[target], cwd=ws)
        except DvcOpError:
            fake_verdict[target] = False
        else:
            fake_verdict[target] = True

    assert fake_verdict == real_verdict
    assert True in real_verdict.values() and False in real_verdict.values()


#: Three stages covering every shape `parse_dvc_lock_outs` must get right at
#: once: a `foreach` hiding `wdir` under `do:`, a plain stage whose out climbs
#: ABOVE its `wdir`, and a third stage that is deleted from `dvc.yaml` after
#: `repro` so it survives only in `dvc.lock` — a real drift state (someone
#: edits dvc.yaml and does not re-run).
ORPHAN_WDIR_PIPELINE_YAML = (
    "stages:\n"
    "  fan:\n"
    "    foreach:\n"
    "      - a\n"
    "      - b\n"
    "    do:\n"
    "      wdir: code\n"
    "      cmd: python -c \"import pathlib; p=pathlib.Path('out/${item}.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'x\\n')\"\n"
    "      outs:\n"
    "        - out/${item}.csv\n"
    "  climb:\n"
    "    wdir: code\n"
    "    cmd: python -c \"import pathlib; "
    "p=pathlib.Path('../data/final/plain.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'y\\n')\"\n"
    "    outs:\n"
    "      - ../data/final/plain.csv\n"
    "  ingest:\n"
    "    cmd: python -c \"import pathlib; p=pathlib.Path('data/raw.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'z\\n')\"\n"
    "    outs:\n"
    "      - data/raw.csv\n"
)

#: `ingest` removed — the stage stays in `dvc.lock`, so its out is an orphan.
ORPHAN_WDIR_PIPELINE_YAML_PRUNED = ORPHAN_WDIR_PIPELINE_YAML.split("  ingest:\n")[0]


def test_parse_dvc_lock_outs_agrees_with_real_dvc_on_foreach_wdir(
    tmp_path: Path, real_dvc
) -> None:
    """The oracle: what mintd enumerates is what real dvc will accept.

    `parse_dvc_lock_outs` exists to hand `dvc pull` a target list. Every out
    it returns that dvc rejects becomes an exit 1 for the whole pull; every
    out it drops that dvc would accept is data silently not delivered. So the
    only honest test is agreement with dvc itself, on one graph carrying all
    three shapes at once.

    Measured here, dvc 3.67.1, no remote configured (a configured target is
    "up to date" at rc 0; an unknown one is rc 1, which is exactly the
    accept/reject signal this compares):

        code/out/a.csv        rc=0   foreach instance, wdir from `do:`
        data/final/plain.csv  rc=0   out climbing above its wdir
        data/raw.csv          rc=1   orphan: in dvc.lock, not in dvc.yaml

    RED before the orphan guard on the `data/raw.csv` row: mintd enumerated
    it and dvc refuses it. The guard IS this test's mutation -- an
    orphan-free graph here would land green and never touch the change.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-b", "main", str(ws)])
    real_dvc(["init"], cwd=ws, check=True)
    (ws / "code").mkdir()  # dvc will not run a stage whose wdir does not exist
    (ws / "dvc.yaml").write_text(ORPHAN_WDIR_PIPELINE_YAML, encoding="utf-8")
    real_dvc(["repro"], cwd=ws, check=True)
    # Orphan `ingest`: drop it from dvc.yaml, leave it in the lock dvc wrote.
    (ws / "dvc.yaml").write_text(ORPHAN_WDIR_PIPELINE_YAML_PRUNED, encoding="utf-8")

    targets = ["code/out/a.csv", "data/final/plain.csv", "data/raw.csv"]
    enumerated = {o.path for o in parse_dvc_lock_outs(ws, "storage")}

    real = {t: real_dvc(["pull", t], cwd=ws).returncode == 0 for t in targets}
    mintd = {t: t in enumerated for t in targets}

    assert mintd == real
    # Without both verdicts present, a change that empties one side passes.
    assert True in real.values() and False in real.values()


#: Four stages covering every lock shape `outs_matching` must expand: a
#: `foreach` fan under `data/final/hhi/`, a plain stage landing a lock out in a
#: prefix that ALSO holds a `.dvc` pointer (the mixed row), a `wdir`-anchored
#: stage, and a `foreach` whose out climbs above its `wdir`.
RESOLVER_PIPELINE_YAML = (
    "stages:\n"
    "  hhi:\n"
    "    foreach:\n"
    "      - t1\n"
    "      - t2\n"
    "    do:\n"
    "      cmd: python -c \"import pathlib; "
    "p=pathlib.Path('data/final/hhi/${item}.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'${item}\\n')\"\n"
    "      outs:\n"
    "        - data/final/hhi/${item}.csv\n"
    "  mixedlock:\n"
    "    cmd: python -c \"import pathlib; "
    "p=pathlib.Path('data/final/mixed/lock.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'L\\n')\"\n"
    "    outs:\n"
    "      - data/final/mixed/lock.csv\n"
    "  wd:\n"
    "    wdir: code\n"
    "    cmd: python -c \"import pathlib; p=pathlib.Path('out/p.csv'); "
    "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'P\\n')\"\n"
    "    outs:\n"
    "      - out/p.csv\n"
)


def test_outs_matching_agrees_with_real_dvc_on_one_graph(
    local_producer: LocalProducer, tmp_path: Path, real_dvc
) -> None:
    """The oracle that licenses `outs_matching`: what it expands, dvc accepts
    AND lands; what it expands FROM, dvc rejects.

    Three assertions, and the shape of each is load-bearing:

    (a) REJECT, **per row and direction-3 only.** A blanket "the declared path
        dvc rejects" loop false-reds, because a direction-2 subpath UNDER a
        tracked directory out is rc 0 on real dvc (measured: `dvc pull
        data/parts/p1.csv` rc=0). Only the direction-3 prefixes are rejected.

    (b) ACCEPT, as ONE fresh clone and ONE `dvc pull` of the WHOLE expansion,
        asserting the landed FILE SET. Pulling one target per clone passes
        under both the correct stamp and the broken one, which is exactly how
        the `X.dvc` stamp defect survived review: a mixed argv of `.dvc` paths
        and bare out paths trips dvc 3.67.1's `index_from_targets` defect
        and under-delivers at
        exit 0. The targets go in `_all_dvc_outs` enumeration order —
        pointers first, the failing order.

    (c) TRAP CONTROL. `dvc pull ""` is rc 0 and lands outs from OUTSIDE the
        declared prefix, which is what licenses (b)'s landed-set assertion as
        the thing an exit code cannot prove.

    Mutation: return `out` instead of `dataclasses.replace(out, target=rel)` in
    `outs_matching` -> (b) fails, because a `target=''` among the argv makes
    dvc pull the whole repo and the landed set exceeds the expansion.
    """
    from mintd._dvc_state import outs_matching

    # Pointers: two under data/final/, one sharing data/final/mixed/ with a lock
    # out, plus PAYLOAD's `data/parts` directory out for the direction-2 row.
    local_producer.publish({
        "data/final/a.parquet": b"A\n",
        "data/final/b.parquet": b"B\n",
        "data/final/mixed/ptr.csv": b"P\n",
        "data/parts": {"p1.csv": b"1\n", "p2.csv": b"2\n"},
    })
    # dvc will not run a stage whose `wdir` does not exist.
    (local_producer.work / "code").mkdir(exist_ok=True)
    local_producer.publish_pipeline(
        RESOLVER_PIPELINE_YAML, seed={"code/keep.txt": b"k\n"},
    )

    clone = _clone(local_producer, tmp_path / "reject")

    # (a) reject -- direction-3 prefixes only, one verdict per row.
    for prefix in ("data/final", "data/final/hhi", "data/final/mixed", "code/out"):
        rc = real_dvc(["pull", prefix], cwd=clone).returncode
        assert rc == 1, f"expected dvc to reject {prefix!r}, got rc={rc}"
    # ...and the direction-2 subpath is NOT rejected, which is why (a) is per-row.
    assert real_dvc(["pull", "data/parts/p1.csv"], cwd=clone).returncode == 0

    # (b) accept and land -- one clone, one combined pull, exact landed set.
    outs = outs_matching(clone, "data/final/", "storage")
    assert outs, "direction 3 expanded to nothing"
    assert all(o.target for o in outs), [o.target for o in outs]
    targets = [o.target for o in outs]          # enumeration order: pointers first
    expected = {
        "data/final/a.parquet", "data/final/b.parquet",
        "data/final/mixed/ptr.csv", "data/final/mixed/lock.csv",
        "data/final/hhi/t1.csv", "data/final/hhi/t2.csv",
    }
    assert set(targets) == expected, sorted(targets)

    fresh = _clone(local_producer, tmp_path / "accept")
    r = real_dvc(["pull", *targets], cwd=fresh)
    assert r.returncode == 0, f"dvc rejected the expansion:\n{r.stdout}\n{r.stderr}"
    landed = {
        p.relative_to(fresh).as_posix()
        for p in (fresh / "data").rglob("*")
        if p.is_file() and p.suffix != ".dvc" and p.name != ".gitignore"
    }
    assert landed == expected, f"landed {sorted(landed)}"
    # Nothing outside the declared prefix came along.
    assert not (fresh / "code" / "out" / "p.csv").exists()

    # (c) trap control -- the empty target is rc 0 and pulls beyond the prefix.
    trap = _clone(local_producer, tmp_path / "trap")
    assert real_dvc(["pull", ""], cwd=trap).returncode == 0
    assert (trap / "code" / "out" / "p.csv").exists(), (
        "dvc pull '' should have pulled outs outside data/final/"
    )


# ---------------------------------------------------------------------------
# consumer_project
# ---------------------------------------------------------------------------


def test_two_producers_colliding_on_final_are_both_addressable(
    consumer_project,
) -> None:
    """Two imports whose consumer-side path is the same string.

    Every existing consumer builder writes one import per project
    (`_stage_project`, `tests/test_data.py:43-55`), so a second producer cannot
    be attached and the collision cannot be built at all. Unblocks issue09.
    """
    proj = consumer_project(
        imports=[
            Import(name="alpha", producer_url=ALPHA_URL, pin="a" * 40),
            Import(name="beta", producer_url=BETA_URL, pin="b" * 40),
        ]
    )

    deps = scan_imports(proj)

    assert len(deps) == 2, [d.source.name for d in deps]
    # They genuinely collide on the consumer-side path ...
    assert {d.local_path for d in deps} == {"final"}
    # ... and are still separable by producer and by the file each came from.
    assert {d.producer_repo for d in deps} == {ALPHA_URL, BETA_URL}
    assert {d.source.name for d in deps} == {"alpha.dvc", "beta.dvc"}
    assert {d.contract_pin for d in deps} == {"a" * 40, "b" * 40}


def test_colliding_imports_resolve_independently_via_the_namespace_index(
    consumer_project,
) -> None:
    """The index half of issue09 (M1): keyed on the recorded producer path
    within ONE product's namespace folder, two producers publishing the same
    output path cannot shadow each other — the old `local_path` keying kept
    exactly one of these."""
    from mintd.data import _imports_index
    from tests._harness.consumer import write_import

    proj = consumer_project()
    alpha = write_import(
        proj,
        Import(name="final", producer_url=ALPHA_URL, pin="a" * 40),
        under="data/imports/data_alpha",
    )
    beta = write_import(
        proj,
        Import(name="final", producer_url=BETA_URL, pin="b" * 40),
        under="data/imports/data_beta",
    )

    alpha_index = _imports_index(proj / "data/imports/data_alpha", name="alpha")
    beta_index = _imports_index(proj / "data/imports/data_beta", name="beta")

    # Both record the producer path `outputs/final/`; each namespace resolves
    # its OWN file — nothing shadowed, nothing merged.
    assert alpha_index == {"outputs/final": alpha}
    assert beta_index == {"outputs/final": beta}


# ---------------------------------------------------------------------------
# synthetic_project
# ---------------------------------------------------------------------------


def test_synthetic_project_dvc_yaml_parses_under_real_dvc(
    synthetic_project, real_dvc
) -> None:
    """Real dvc reads the authored pipeline.

    issue01's prototype wrote `outs: [{path: …}]`, which is valid YAML and
    which dvc rejects with ``'./dvc.yaml' validation failed … expected a
    dictionary``. A fixture only production's own parsers accept is a fixture
    that can disagree with the tool it is standing in for.
    """
    proj = synthetic_project()

    result = real_dvc(["stage", "list"], cwd=proj)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "build" in result.stdout


def test_producer_commits_on_a_machine_with_no_git_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_git` supplies a committer identity, so a fixture repo does not need
    the developer's global git config to commit.

    Three of the five merged `_git` copies had no identity at all and worked
    only because whoever ran them had a global `user.email`; a fourth
    hardcoded one.

    Emptying the global config is NOT enough to make that visible. With no
    identity anywhere, git 2.48.1 invents one from the OS user and hostname
    and commits successfully — it only refuses when the guessed address has no
    domain (`fatal: unable to auto-detect email address (got 'u@host.(none)')`).
    Whether it refuses therefore depends on how the hostname happens to resolve
    in the running process: measured on this machine, git commits fine from an
    interactive shell and refuses under pytest. A test resting on that passes
    on one host and rots on the next. `user.useConfigOnly` removes the guess
    entirely, and a `-c` identity still counts as config — so the merged
    default is the only thing standing between the builder and the error.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

    # Both the seed commit inside the builder and this one run identity-less.
    producer = build_local_producer(tmp_path / "identity-less")
    seeded = producer.head_sha
    assert len(seeded) == 40

    assert producer.commit_more() != seeded


def test_real_dvc_reads_and_writes_only_under_tmp_path(
    synthetic_project, real_dvc, tmp_path: Path
) -> None:
    """dvc's config and cache dirs are redirected under `tmp_path` — asserted on
    what dvc actually read and wrote, not on the fixture's own env dict, which
    would still pass if the dict were built correctly and never handed to the
    subprocess.

    Without the redirects a contract test reads whoever-ran-it's global dvc
    config and shares the machine's site cache, so two machines disagree about
    what "real dvc" means. Verified load-bearing twice over: an earlier draft's
    mutation result for the redirect was a false positive, and the draft after
    that redirected `HOME` — which dvc does not consult on Windows at all, so
    this assertion reddened the required `windows-test` job while reporting
    green here.
    """
    proj = synthetic_project()

    # Planted by hand rather than written with `dvc config --global`: on the
    # one run where the redirect is broken, a dvc *write* would land in the
    # developer's real config. One location, because the fixture names dvc's
    # own `DVC_GLOBAL_CONFIG_DIR` rather than guessing at what `platformdirs`
    # derives from HOME on this platform.
    for key, remote in (
        ("DVC_GLOBAL_CONFIG_DIR", "gsentinel"),
        ("DVC_SYSTEM_CONFIG_DIR", "ssentinel"),
    ):
        cfg = Path(real_dvc.env[key])
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config").write_text(
            f"['remote \"{remote}\"']\n    url = s3://{remote}\n", encoding="utf-8"
        )

    # Neither name is reachable except through the redirected dirs: the
    # project's own .dvc/config declares `origin` and nothing else. The system
    # level is checked too — it is a separate knob, and left alone "real dvc"
    # still reads /etc/xdg/dvc or /Library/Application Support/dvc from the
    # machine on every platform.
    for remote in ("gsentinel", "ssentinel"):
        read = real_dvc(["config", f"remote.{remote}.url"], cwd=proj)
        assert read.stdout.strip() == f"s3://{remote}", read.stdout + read.stderr

    # The fixture only mkdirs the site dir, so anything inside it came from dvc.
    real_dvc(["stage", "list"], cwd=proj)
    assert list((tmp_path / "dvc-site").rglob("*"))


def test_foreach_lock_stages_are_authorable(synthetic_project, real_dvc) -> None:
    """The `base` / `base@a` split, which no writer in the tree can produce.

    `_write_lock` / `_write_dvc_file_*` (`tests/test_fast_sync.py:84-153`) only
    emit flat stage names, so the templated form every lab pipeline actually
    uses is unrepresentable — and with it, the question of whether
    `parse_dvc_lock_outs` finds every instance's outs.
    """
    proj = synthetic_project(foreach=True)

    assert "  base:\n" in (proj / "dvc.yaml").read_text(encoding="utf-8")
    lock = (proj / "dvc.lock").read_text(encoding="utf-8")
    assert "base@a:" in lock and "base@b:" in lock

    result = real_dvc(["stage", "list"], cwd=proj)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "base@a" in result.stdout and "base@b" in result.stdout

    outs = parse_dvc_lock_outs(proj, "origin")

    assert sorted(o.path for o in outs) == ["data/a.parquet", "data/b.parquet"]


def test_strict_fake_reads_the_out_path_from_the_pointer_not_its_filename(
    tmp_path, real_dvc
) -> None:
    """A `dvc import` pointer's out path is unrelated to its filename.

    `dvc add data/final.csv` writes `data/final.csv.dvc`, so stem and out path
    agree and a fake can derive one from the other and look correct forever.
    `dvc import` breaks that: it writes `<name>.dvc` whose out is the LOCAL
    path. `tests/_harness/consumer.py::write_import` has always emitted exactly
    that shape — `Import(name="alpha", local_path="final")` gives `alpha.dvc`
    carrying `path: final` — so the two names differ in the tree already.

    The first cut of `strict_targets` derived the out from the stem and so
    INVERTED the first two rows below: it rejected the real out and accepted a
    name dvc refuses. Stricter than dvc in one direction and more lenient in
    the other — the same double failure that killed `GraphAwareDvcOps`.

    Not caught by the 18-row oracle above, because every row there uses an
    `add`-shaped pointer where stem and out agree. An oracle only licenses the
    shapes in its table; this is the shape it was missing.
    """
    work = tmp_path / "ws"
    (work / "data" / "imports").mkdir(parents=True)
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init", "-b", "main", str(work)])
    real_dvc(["init"], cwd=work)
    real_dvc(["remote", "add", "-d", "storage", str(remote)], cwd=work)

    (work / "data" / "imports" / "final").write_text("a,b\n1,2\n", encoding="utf-8")
    real_dvc(["add", "data/imports/final"], cwd=work)
    real_dvc(["push"], cwd=work)
    # Rename to the `dvc import` shape: pointer named for the import, out
    # still `final`.
    (work / "data" / "imports" / "final.dvc").rename(
        work / "data" / "imports" / "alpha.dvc"
    )

    targets = [
        "data/imports/final",      # the real out — dvc accepts
        "data/imports/alpha",      # the pointer's STEM — dvc does not know it
        "data/imports/alpha.dvc",  # the pointer file itself — dvc accepts
    ]

    fake = _strict_fake(work)
    real_verdict, fake_verdict = {}, {}
    for target in targets:
        real_verdict[target] = real_dvc(["pull", target], cwd=work).returncode == 0
        try:
            fake.pull(targets=[target], cwd=work)
        except DvcOpError:
            fake_verdict[target] = False
        else:
            fake_verdict[target] = True

    assert fake_verdict == real_verdict
    # The stem and the out MUST disagree, or this test is a tautology that
    # passes on the very bug it exists to catch.
    assert real_verdict["data/imports/final"] is True
    assert real_verdict["data/imports/alpha"] is False


def test_enclave_package_caches_into_the_enclave_not_the_outer_repo(
    local_producer: LocalProducer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The custody defect, through real dvc, with the enclave nested inside
    another DVC repo — the shape that failed SILENTLY at exit 0.

    Standing in `outer/` with the enclave's manifest named relative to it,
    dvc used to import into `outer`, because that is where the process was:
    `outer/.dvc/cache` gained the producer's blob and the enclave's cache
    stayed empty, rc=0. In the one lane whose purpose is data custody, a repo
    that is not the enclave held the restricted bytes.

    The manifest path is DELIBERATELY RELATIVE, because that is production's
    default (`--manifest` defaults to `Path("enclave_manifest.yaml")`): the
    imports must run in `manifest_path.parent`, with `-o` absolutized at the
    seam, or real dvc reads the destination against the wrong directory.
    """
    from mintd._config import Timeouts
    from mintd._dvc_ops import SubprocessDvcOps
    from mintd.enclave import enclave_package

    for key, value in {
        "HOME": str(tmp_path / "dvc-home"),
        "USERPROFILE": str(tmp_path / "dvc-home"),
        "DVC_GLOBAL_CONFIG_DIR": str(tmp_path / "dvc-home" / "global"),
        "DVC_SYSTEM_CONFIG_DIR": str(tmp_path / "dvc-home" / "system"),
        "DVC_SITE_CACHE_DIR": str(tmp_path / "dvc-site"),
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "dvc-home").mkdir(parents=True, exist_ok=True)

    pin = local_producer.publish({"outputs/data.csv": b"restricted-producer-bytes\n"})

    ops = SubprocessDvcOps(timeouts=Timeouts())
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(["init", "-b", "main", str(outer)])
    ops.init(cwd=outer)
    enclave = outer / "enclave"
    enclave.mkdir()
    _git(["init", "-b", "main", str(enclave)])
    (enclave / "enclave_manifest.yaml").write_text(
        "enclave_name: test\napproved_products:\n"
        f"  - repo: prod\n    pin: {pin}\n    source_path: outputs/data.csv\n"
        "transferred: []\n",
        encoding="utf-8",
    )

    class _Entry:
        def model_dump(self):
            return {"repository": {"github_url": local_producer.url}, "project": {"full_name": "data_prod"}}

    class _Client:
        def fetch(self, name):
            return _Entry()

    monkeypatch.chdir(outer)
    # RELATIVE on purpose -- see the docstring. This is what production passes.
    enclave_package(
        _Client(), ops, manifest_path=Path("enclave/enclave_manifest.yaml"),
        output_dir=Path("enclave/transfers"),
    )

    def blobs(repo: Path) -> list[str]:
        d = repo / ".dvc" / "cache" / "files" / "md5"
        return sorted(p.name for p in d.rglob("*") if p.is_file()) if d.exists() else []

    assert blobs(outer) == [], (
        f"the OUTER repo cached the producer's bytes: {blobs(outer)}"
    )
    assert blobs(enclave), "the enclave's cache is empty; the import went elsewhere"
    delivered = enclave / "data" / "imports" / "data_prod" / "outputs" / "data.csv"
    assert delivered.read_bytes() == b"restricted-producer-bytes\n"
    assert list((enclave / "transfers").glob("*.zip")), "and the bundle was built"


#: The mintd data scaffold's stage shape (`src/mintd/files/dvc_data.yaml.j2`):
#: the stage runs in the source dir and its out is recorded RELATIVE TO THAT
#: WDIR, so dvc writes `path: ../shared/final/` into `dvc.lock`. `build.py` is
#: seeded INTO the wdir because dvc refuses a stage whose `wdir` does not
#: exist (`ReproductionError: failed to reproduce 'build'`, dvc 3.67.1).
WDIR_BUILD_PY = (
    b"import pathlib, sys\n"
    b"d = pathlib.Path('../shared/final')\n"
    b"d.mkdir(parents=True, exist_ok=True)\n"
    b"(d / 'out.csv').write_bytes(sys.argv[1].encode() + b'\\n')\n"
)

#: The consumer subscribes to the REPO-RELATIVE path. `wdir` is nested and the
#: out climbs to a sibling of `code/`, so "join the wdir" and "strip the
#: leading `../`" give DIFFERENT answers (`code/shared/final` vs
#: `shared/final`). With the scaffold's own `wdir: code` + `../data/final/`
#: the two agree, and a comparator that never read `dvc.yaml` would pass.
WDIR_SUBSCRIBED = "code/shared/final/"


def _wdir_pipeline(style: str, tag: str) -> str:
    """One scaffold-shaped stage, in each of dvc's three stage spellings."""
    head, cmd = {
        "plain": ("stages:\n  build:\n", f"python build.py {tag}"),
        # dvc.yaml still names the stage `build`; dvc.lock names the
        # instance `build@main`.
        "matrix": (
            "stages:\n  build:\n    matrix:\n      k:\n        - main\n",
            f"python build.py {tag}-${{item.k}}",
        ),
        # `foreach` additionally hides the stage body — `wdir` included —
        # under `do:`.
        "foreach": (
            "stages:\n  build:\n    foreach:\n      - main\n    do:\n",
            f"python build.py {tag}-${{item}}",
        ),
    }[style]
    body = (
        "    wdir: code/steps\n"
        f"    cmd: {cmd}\n"
        "    deps:\n"
        "      - build.py\n"
        "    outs:\n"
        "      - ../shared/final/\n"
    )
    if style == "foreach":
        body = "".join("  " + line for line in body.splitlines(keepends=True))
    return head + body


@pytest.mark.parametrize(
    "style,advance,expected",
    [
        ("plain", "republish", "drift"),
        ("plain", "commit_only", "up_to_date"),
        ("matrix", "republish", "drift"),
        ("foreach", "republish", "drift"),
    ],
    ids=["bytes-moved", "producer-moved-product-did-not", "matrix", "foreach"],
)
def test_wdir_relative_lock_from_real_dvc_drives_check(
    local_producer: LocalProducer,
    consumer_project,
    tmp_path: Path,
    style: str,
    advance: str,
    expected: str,
) -> None:
    """The wdir comparator, against bytes dvc actually wrote.

    `test_wdir_relative_lock_out_resolves` (`tests/test_check.py`) pins the
    same rule against `_scaffold_lock` — bytes in the shape we BELIEVE dvc
    emits. Nothing proved dvc emits it: `PIPELINE_YAML` declares no `wdir`,
    and `publish()` uses `dvc add`, which writes a per-path `.dvc` that
    `_pointer_md5` matches FIRST and so never reaches the lock branch.
    The premise of the whole comparator — that dvc leaves the out
    wdir-relative in the lock instead of normalizing it to the repo root —
    was assumed, not measured.

    Here the producer really runs `dvc repro`, and dvc 3.67.1 writes:

        outs:
        - path: ../shared/final/
          hash: md5
          md5: c99d7d56d905e5dfc25b0307c212dc6d.dir

    The `matrix` and `foreach` cells are the same product under a fan-out:
    dvc.yaml names the stage `build`, dvc.lock names the instance
    `build@main`, and `foreach` moves `wdir` under `do:`. Both spellings once
    resolved every out against `wdir="."`, dropped it as escaping the root,
    and reported the product "not published at the producer's HEAD".

    Mutations: make `resolve_out` ignore its `wdir` argument / make
    `_stage_wdirs` return `{}` -> every cell reddens. Drop the `@` fallback
    in `stage_wdir` -> matrix and foreach redden. Read `wdir` from the stage
    instead of `do:` -> foreach reddens.

    The `up_to_date` cell advances HEAD with `commit_more()` rather than
    republishing identical bytes on purpose: dvc skips an unchanged stage
    ("Stage 'build' didn't change, skipping") and `_commit_and_push` then
    fails on a clean tree.
    """
    local_producer.publish_pipeline(
        _wdir_pipeline(style, "v1"), seed={"code/steps/build.py": WDIR_BUILD_PY}
    )
    pin = local_producer.head_sha

    lock = (local_producer.work / "dvc.lock").read_text(encoding="utf-8")
    # The `../` is the premise; the trailing slash is dvc echoing our own
    # spelling and is not asserted.
    assert "path: ../shared/final" in lock, lock

    proj = consumer_project(imports=[
        Import(
            name="final",
            producer_url=local_producer.url,
            pin=pin,
            producer_path=WDIR_SUBSCRIBED,
        )
    ])

    if advance == "republish":
        local_producer.publish_pipeline(
            _wdir_pipeline(style, "v2-longer"),
            seed={"code/steps/build.py": WDIR_BUILD_PY},
            message="v2",
        )
    else:
        local_producer.commit_more()
    head = local_producer.head_sha
    assert head != pin

    cache = tmp_path / "pcache"

    def at(repo: str, rev: str):
        # `""` is check.py's HEAD sentinel, as in the enclave case above.
        return ProducerView.try_at(repo, rev or head, cache_dir=cache)

    findings = check_project(proj, upgrades=True, producer_view_factory=at)

    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.kind for f in consumer] == [expected], [f.message for f in consumer]


_POINTERS = (
    "code/b/sub/x.dvc", "code/k/drop.dvc", "code/k/keep.dvc", "data/drop.dvc", "data/ext/in/demo/l.dvc",
    "data/ext/vendor/deep/w.dvc", "data/ext/vendor/z.dvc", "data/keep.dvc", "data/lib/b.dvc",
)


@pytest.mark.parametrize("layout", ["journey", "scaffold-nested", "negation", "file-rule"])
def test_discover_all_outs_reads_the_pointers_dvc_reads(tmp_path: Path, real_dvc, monkeypatch, layout) -> None:
    """Real dvc is the oracle for which `.dvc` files exist: `dvc stage list
    --all` names exactly the pointers mintd discovers, and the rest are the
    ones mintd reports as skipped because of .gitignore. Which pointers DVC
    reads changes with the installed dulwich, so the expected list is never
    written down; the comparison holds on any version.

    Mutations: no ignore verdict -> every layout red; `git check-ignore
    --no-index` in place of DVC's check -> `negation` red (and
    `scaffold-nested` on dulwich 1.2.4); no folder check or no inheritance
    from an ignored folder -> `negation` red; no per-file check ->
    `file-rule` red."""
    import mintd
    from mintd._fast_sync_ops import discover_all_outs, gitignored_pointers

    scaffold = (Path(mintd.__file__).parent / "files" / "gitignore.txt").read_text()
    ignores = {
        "journey": {"data/ext/.gitignore": "vendor\n"},
        "scaffold-nested": {".gitignore": scaffold, "data/ext/in/.gitignore": "demo\n"},
        # git reads code/k/keep.dvc here; DVC (dulwich 1.2.21) reads none of the three
        "negation": {"code/.gitignore": "k/*\n!k/keep.dvc\nb\n!b/sub/\n!b/sub/*.dvc\n"},
        "file-rule": {".gitignore": "drop.dvc\n"},
    }[layout]
    # In-process dulwich and the dvc subprocess must read the same (empty)
    # global excludes, not the developer's.
    home = real_dvc.env["HOME"]
    for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(key, home)
    real_dvc.env["XDG_CONFIG_HOME"] = home

    ws = tmp_path / "ws"
    ws.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=ws)
    real_dvc(["init", "-q"], cwd=ws, check=True)
    for rel, text in ignores.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text)
    for rel in _POINTERS:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # A hex md5 with letters: an all-digit one is read by YAML as an int.
        p.write_text(f"outs:\n- md5: ffffffffffffffffffffffffffffffff\n  size: 1\n  hash: md5\n  path: {p.stem}.bin\n")

    listed = real_dvc(["stage", "list", "--all", "--name-only"], cwd=ws, check=True).stdout.split()
    dvc_reads = sorted(line.replace("\\", "/") for line in listed if line.endswith(".dvc"))

    assert discover_all_outs(ws) == dvc_reads
    assert gitignored_pointers(ws) == sorted(set(_POINTERS) - set(dvc_reads))
    assert gitignored_pointers(ws) != []  # every layout skips at least one, on any dulwich


def test_dir_manifest_md5_matches_the_tree_dvc_add_records(tmp_path: Path, real_dvc) -> None:
    """Real dvc is the oracle for `data push`'s tree check: hashing the file
    list a folder's cached manifest holds gives the md5 `dvc add` wrote into
    its pointer, so a complete list always names a tree the cache holds."""
    import yaml

    from mintd._fast_sync_ops import dir_manifest_md5, read_cached_dir_manifest

    ws = tmp_path / "ws"
    (ws / "data" / "final").mkdir(parents=True)
    for name in ("x.csv", "y.csv"):
        (ws / "data" / "final" / name).write_text(f"{name}\n")
    real_dvc(["init", "--no-scm", "-q"], cwd=ws, check=True)
    real_dvc(["add", "data/final"], cwd=ws, check=True)

    md5 = yaml.safe_load((ws / "data" / "final.dvc").read_text())["outs"][0]["md5"]
    entries = read_cached_dir_manifest(ws / ".dvc" / "cache", md5)
    assert entries is not None and len(entries) == 2
    assert dir_manifest_md5(entries) == md5


def test_s3_server_records_each_object_get_and_the_bytes_it_served(s3_server) -> None:
    """Stories prove "downloaded once" and "nothing else downloaded" from these
    records: a HEAD and a failed GET are not downloads, and a key outside the
    prefix is left out."""
    from botocore.exceptions import ClientError

    from tests._harness.expect import s3_state
    from tests._harness.s3 import BUCKET

    s3 = s3_server.client
    s3.put_object(Bucket=BUCKET, Key="lab/p/a.csv", Body=b"alpha\n")
    s3.put_object(Bucket=BUCKET, Key="lab/other/b.csv", Body=b"beta\n")
    for key in ("lab/p/a.csv", "lab/p/a.csv", "lab/other/b.csv"):
        s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    s3.head_object(Bucket=BUCKET, Key="lab/p/a.csv")
    with pytest.raises(ClientError):
        s3.get_object(Bucket=BUCKET, Key="lab/p/missing.csv")

    state = s3_state(s3_server, "lab/p")
    assert state.puts == {"a.csv": 1}
    assert state.gets == {"a.csv": 2}
    assert state.bytes_served == {"a.csv": 12}


def test_story_2_deletes_only_on_the_emulator() -> None:
    """Any other endpoint is refused with HarnessError, which the
    partial-transfer case's `xfail(raises=AssertionError)` cannot report as
    XFAIL. Mutation that turns it red: the guard back to a plain assert."""
    from tests.stories.test_story_2_failed_import import _delete_pinned

    server = SimpleNamespace(endpoint="https://s3.wasabisys.com", client=None)
    with pytest.raises(HarnessError, match="refusing to delete on https://s3.wasabisys.com"):
        _delete_pinned(SimpleNamespace(), server, {"b.csv"})


def _tracked_project(root: Path) -> None:
    """A project shaped like the lab's real products: a foreach stage with a
    wdir, one cached out and one `cache: false` out in `dvc.lock`, and a
    folder pointer written with its `files:` list and no top-level md5."""
    (root / "dvc.yaml").write_text(yaml.safe_dump({"stages": {"build": {"foreach": ["x"], "do": {
        "wdir": "code", "cmd": "true",
        "outs": ["../data/final/a.csv", {"../data/final/report.json": {"cache": False}}],
    }}}}), encoding="utf-8")
    (root / "dvc.lock").write_text(yaml.safe_dump({"schema": "2.0", "stages": {"build@x": {"cmd": "true", "outs": [
        {"path": "../data/final/a.csv", "hash": "md5", "md5": md5(b"a"), "size": 1},
        {"path": "../data/final/report.json", "hash": "md5", "md5": "0" * 32, "size": 2},
    ]}}}), encoding="utf-8")
    (root / "data" / "final").mkdir(parents=True)
    (root / "data" / "raw").mkdir()
    (root / "data" / "raw.dvc").write_text(yaml.safe_dump({"outs": [
        {"hash": "md5", "path": "raw", "files": [{"relpath": "r.csv", "md5": md5(b"r")}]},
    ]}), encoding="utf-8")
    (root / "data" / "final" / "a.csv").write_bytes(b"a")
    (root / "data" / "final" / "report.json").write_bytes(b"{}")
    (root / "data" / "raw" / "r.csv").write_bytes(b"r")


def test_landed_files_include_dvc_lock_outs_and_files_list_folders(tmp_path: Path) -> None:
    """Mutations that turn it red: drop the `dvc.lock` loop in `_outs`, the
    `cache: false` skip, the `do` lookup, or `_files_of` reading `files:` first."""
    _tracked_project(tmp_path)
    assert assert_landed_match_pointers(tmp_path) == {"data/final/a.csv": md5(b"a"), "data/raw/r.csv": md5(b"r")}


def test_the_live_tier_runs_only_read_only_commands(tmp_path: Path) -> None:
    """Mutation that turns it red: `read_only` returning without raising (the
    push then reaches Popen on a binary that is not there)."""
    live = LiveMintd(tmp_path / "no-mintd", {}, tmp_path / "config.yaml")
    with pytest.raises(HarnessError, match="read-only"):
        live("data", "push", cwd=tmp_path)
    with pytest.raises(HarnessError, match="read-only"):
        live.start("registry", "register", "x", cwd=tmp_path)
    for argv in (["git", "push"], ["dvc", "push"], ["dvc", "gc"], ["git", "-c", "a=b", "clone"]):
        with pytest.raises(HarnessError, match="read-only"):
            run_read_only(argv, cwd=tmp_path, env={})  # an empty folder: a broken guard pushes nothing
    read_only(["mintd", "data", "clone", "x"])
    read_only(["dvc", "pull"])
    spawns = re.compile(r"\bsubprocess\.(run|Popen|call|check_)|\bos\.system|\b_git\(")
    for module in (Path(__file__).parent / "live").glob("*.py"):
        assert not spawns.search(module.read_text(encoding="utf-8")), f"{module} spawns outside the read-only runners"


def test_a_hidden_products_failure_says_counts_only(tmp_path: Path) -> None:
    """Mutations that turn it red: `redact` returning at its top, or
    `Product.shown` always True."""
    _tracked_project(tmp_path)
    (tmp_path / "data" / "final" / "a.csv").write_bytes(b"changed")
    (tmp_path / "data" / "raw" / "r.csv").unlink()
    with pytest.raises(Mismatch) as caught:
        assert_landed_match_pointers(tmp_path)
    exc = caught.value
    named = ("data/final", "data/raw", md5(b"a"), md5(b"r"))
    assert "data/final/a.csv" in str(exc)  # the full message names files; a hidden product's report must not
    item = SimpleNamespace(callspec=SimpleNamespace(params={"product": "made-up"}))
    call = SimpleNamespace(excinfo=SimpleNamespace(value=exc))

    def failed() -> SimpleNamespace:
        return SimpleNamespace(failed=True, longrepr=str(exc), sections=[("Captured stdout call", str(exc))])

    for classification in ("licensed", "private", None, "something-new"):
        report = failed()
        redact(item, call, report, {"made-up": Product("made-up", classification, None)})
        assert "1 missing, 1 differ" in report.longrepr, report.longrepr
        assert not any(n in report.longrepr for n in named), report.longrepr
        assert report.sections == []
    public, unseen = failed(), failed()
    redact(item, call, public, {"made-up": Product("made-up", "public", None)})
    redact(item, call, unseen, {})  # failed before its catalog entry was read
    assert public.longrepr == unseen.longrepr == str(exc) and public.sections and unseen.sections
    passed = SimpleNamespace(failed=False, longrepr=None, sections=[("Captured stdout call", "data/raw/r.csv")])
    redact(item, call, passed, {"made-up": Product("made-up", "licensed", None)})
    assert passed.sections == []
    assert Product("made-up", "licensed", None).line("clone", "2 files", "9 bytes") == "made-up clone: 2 files"


def test_the_live_tier_takes_only_the_read_only_key(tmp_path: Path) -> None:
    """Mutations that turn it red: accepting a credentials file that also holds
    `[mintd]` or `[DEFAULT]`, or a section without both halves of a key, or
    reading it with configparser's defaults (it inherits [DEFAULT]'s); running without
    `AWS_CONFIG_FILE`; copying more of the read-only section than its key, or
    of the profile than its region and endpoint."""
    secrets = ("AKIAREADONLY", "readonly-secret", "AKIAWRITE", "write-secret", "AKIAOTHER", "other-secret",
               "AKIACONFIG", "config-secret", "AKIADEFAULT", "default-secret")
    good = tmp_path / "readonly"
    good.write_text("[mintd-live-readonly]\naws_access_key_id = AKIAREADONLY\naws_secret_access_key = readonly-secret\n"
                    "credential_process = /bin/false\n", encoding="utf-8")
    halves = tmp_path / "halves"  # boto would go on down its chain for a key
    halves.write_text("[mintd-live-readonly]\naws_access_key_id = AKIAREADONLY\naws_secret_access_key =\n", encoding="utf-8")
    keyless = tmp_path / "keyless"
    keyless.write_text("[mintd-live-readonly]\nregion = us-east-1\n", encoding="utf-8")
    inherited = tmp_path / "inherited"  # configparser and botocore let the section inherit [DEFAULT]'s key
    inherited.write_text("[DEFAULT]\naws_access_key_id = AKIADEFAULT\naws_secret_access_key = default-secret\n"
                         "[mintd-live-readonly]\n", encoding="utf-8")
    beside = tmp_path / "beside"
    beside.write_text(good.read_text(encoding="utf-8") + "[DEFAULT]\naws_access_key_id = AKIADEFAULT\n", encoding="utf-8")
    both = tmp_path / "both"
    both.write_text(good.read_text(encoding="utf-8")
                    + "[mintd]\naws_access_key_id = AKIAWRITE\naws_secret_access_key = write-secret\n", encoding="utf-8")
    other = tmp_path / "other"
    other.write_text("[default]\naws_access_key_id = AKIAOTHER\naws_secret_access_key = other-secret\n", encoding="utf-8")
    conf = tmp_path / "readonly-config"
    conf.write_text("[profile mintd-live-readonly]\nregion = us-east-1\nendpoint_url = https://s3.example.test\n"
                    "output = json\naws_access_key_id = AKIACONFIG\naws_secret_access_key = config-secret\n",
                    encoding="utf-8")
    ok = {"AWS_SHARED_CREDENTIALS_FILE": str(good), "AWS_CONFIG_FILE": str(conf)}
    for env in (
        {"AWS_CONFIG_FILE": str(conf)}, {**ok, "AWS_SHARED_CREDENTIALS_FILE": ""},
        {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "missing")},
        {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(both)}, {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(other)},
        {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(halves)}, {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(keyless)},
        {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(inherited)}, {**ok, "AWS_SHARED_CREDENTIALS_FILE": str(beside)},
        {"AWS_SHARED_CREDENTIALS_FILE": str(good)}, {**ok, "AWS_CONFIG_FILE": ""},
        {**ok, "AWS_CONFIG_FILE": str(tmp_path / "missing")}, {**ok, "AWS_CONFIG_FILE": str(other)},
    ):
        with pytest.raises(HarnessError) as refused:
            readonly_credentials(env, tmp_path / "out")
        assert not any(s in str(refused.value) for s in secrets), refused.value
    path = readonly_credentials(ok, tmp_path / "out")
    copy = configparser.ConfigParser(interpolation=None)
    copy.read(path, encoding="utf-8")
    assert copy.sections() == ["mintd"]
    assert dict(copy["mintd"]) == {"aws_access_key_id": "AKIAREADONLY", "aws_secret_access_key": "readonly-secret"}
    profile = configparser.ConfigParser(interpolation=None)
    profile.read(path.with_name("config"), encoding="utf-8")
    assert profile.sections() == ["profile mintd"]
    assert dict(profile["profile mintd"]) == {"region": "us-east-1", "endpoint_url": "https://s3.example.test"}
    if os.name != "nt":  # Windows keeps no group or other bits; the tier runs on a Linux box
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_live_tier_failure_prints_no_frame_arguments(tmp_path: Path) -> None:
    """A refusal, under pytest's defaults and under `--tb=long -l --full-trace`,
    names the missing file and prints none of the operator's environment.
    Mutations that turn it red: dropping any one of the traceback overrides in
    `tests/live/conftest.py`'s `pytest_configure` (`auto` or `long` to short,
    no locals, no full trace)."""
    planted = {"AWS_ACCESS_KEY_ID": "AKIAPLANTEDWRITE", "AWS_SECRET_ACCESS_KEY": "planted-write-secret"}
    creds = tmp_path / "readonly"
    creds.write_text("[mintd-live-readonly]\naws_access_key_id = AKIAREADONLY\naws_secret_access_key = readonly-secret\n",
                     encoding="utf-8")
    (tmp_path / "config.yaml").write_text("registry_url: https://example.invalid/registry.git\n", encoding="utf-8")
    env = {**planted, **{k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "MINTD_"))},
           "MINTD_CONFIG_DIR": str(tmp_path), "AWS_SHARED_CREDENTIALS_FILE": str(creds)}  # no AWS_CONFIG_FILE
    for i, flags in enumerate(([], ["--tb=long", "-l", "--full-trace"])):
        got = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/live", "-x", *flags, "-p", "no:cacheprovider",
             f"--basetemp={tmp_path / f'base{i}'}"],
            cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, timeout=120,
        )
        out = got.stdout + got.stderr
        assert got.returncode != 0 and "set AWS_CONFIG_FILE" in out, out
        assert not any(v in out for v in (*planted.values(), "readonly-secret")), out


#: A fake mintd for the live tier's own tests. It records each call's argv,
#: environment and the key file it was given, then acts by product: see
#: `_live_world` for what each made-up product does.
_FAKE_MINTD = """
import hashlib, json, os, pathlib, sys, time

world = pathlib.Path(os.environ["FAKE_MINTD_WORLD"])
args = [a for a in sys.argv[1:] if a != "--json"]
creds = pathlib.Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent"))
with (world / "calls.jsonl").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "env": dict(os.environ),
                         "creds": creds.read_text(encoding="utf-8") if creds.is_file() else None}) + "\\n")
if args == ["--version"]:
    sys.exit(print("mintd 0.0.0 (fake)"))


def track(root, rel, body, *, cached=True, landed=True):
    h = hashlib.md5(body).hexdigest()
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name(path.name + ".dvc").write_text(f"outs:\\n- md5: {h}\\n  size: {len(body)}\\n  path: {path.name}\\n")
    if cached:
        obj = root / ".dvc" / "cache" / "files" / "md5" / h[:2] / h[2:]
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(body)
    if landed and not path.exists():
        path.write_bytes(body)


def track_folder(root, rel, files):
    # A folder out with its files list, as dvc import writes it: every file of the folder, git-tracked ones too.
    folder = root / rel
    entries = "".join(f"  - relpath: {n}\\n    md5: {hashlib.md5(b).hexdigest()}\\n" for n, b in files.items())
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.with_name(folder.name + ".dvc").write_text(f"outs:\\n- path: {folder.name}\\n  files:\\n{entries}")
    for n, b in files.items():
        (folder / n).parent.mkdir(parents=True, exist_ok=True)
        (folder / n).write_bytes(b)


def metadata(dest, name, classification, primary="data"):
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(json.dumps({
        "fake": name, "governance": {"classification": classification} if classification else {},
        "data_products": {"primary": primary}}))


cmd, rest, here = args[1], args[2:], pathlib.Path.cwd()
if cmd == "clone":
    name, dest = rest[0], pathlib.Path(rest[rest.index("--dest") + 1])
elif cmd == "import":
    name, dest = rest[0], here
else:
    name, dest = json.loads((here / "metadata.json").read_text())["fake"], here
killed = "killed" in str(dest)  # the killed-clone check's own folder
if name == "made-up-licensed" and cmd == "clone" and not killed:  # the repo says public: licensed still wins
    metadata(dest, name, "public")
    sys.exit("PLANTED-STDERR: the clone failed on data/secret.csv")
elif name == "made-up-licensed" and cmd == "clone":
    pass  # exits 0 having landed nothing
elif name == "made-up-public" and cmd == "clone":  # the repo says licensed, and says it before the clone fails
    metadata(dest, name, "licensed")
    sys.exit("PLANTED-STDERR: the clone failed on data/secret.csv")
elif name in ("made-up-tiny", "made-up-wide", "made-up-odd", "made-up-file") and cmd == "clone":
    tiny = name == "made-up-tiny"  # its repo gives no classification (counts only) and writes its primary oddly
    primary = "./data/" if tiny else "notes.md" if name == "made-up-file" else "data"
    metadata(dest, name, None if tiny else "public", primary)
    (dest / "notes.md").write_bytes(b"notes\\n")  # kept in git, no pointer
    track(dest, "data/x.csv", b"x\\n", cached=False)
    (dest / "data" / ".gitignore").write_bytes(b"/x.csv\\n")
    if name == "made-up-wide":  # a sibling of the primary whose name starts with the primary's
        track(dest, "data2/y.csv", b"y\\n", cached=False)
    if name == "made-up-odd" and killed:
        sys.exit("the clone failed after every file landed")
elif name == "made-up-file" and cmd == "import":  # its one-file primary, kept in git
    track(dest, f"data/imports/data_{name}/notes.md", b"notes\\n", cached=False)
elif name in ("made-up-tiny", "made-up-wide", "made-up-odd") and cmd == "import":  # the primary whole, .gitignore too
    root = f"data/imports/data_{name}"
    ignore = b"other\\n" if name == "made-up-odd" else b"/x.csv\\n"
    track_folder(dest, f"{root}/data", {"x.csv": b"x\\n", ".gitignore": ignore})
    if name == "made-up-wide":  # more than the primary
        track(dest, f"{root}/data2/y.csv", b"y\\n", cached=False)
elif name in ("made-up-tiny", "made-up-wide", "made-up-odd", "made-up-file", "made-up-empty") and cmd == "pull":
    pass  # changes nothing
elif name == "made-up-empty" and cmd == "clone":  # its pointer's file never lands
    metadata(dest, name, "public")
    track(dest, "data/x.csv", b"x\\n", cached=False, landed=False)
elif name == "made-up-empty":
    sys.exit("the import failed")
elif name == "made-up-small" and cmd == "clone":
    metadata(dest, name, "public")
    track(dest, "data/x.csv", b"x\\n", landed=not killed)
    if killed:  # every object has landed: wait for the Ctrl-C
        time.sleep(300)
elif name == "made-up-small" and cmd == "pull":  # lands what is missing, touching nothing else
    if not (dest / "data" / "x.csv").exists():
        (dest / "data" / "x.csv").write_bytes(b"x\\n")
elif name == "made-up-small" and cmd == "import":  # bytes the producer never tracked
    track(dest, "data/imports/data_made-up-small/data/x.csv", b"other\\n", cached=False)
elif name == "made-up-held-git" and cmd == "clone":  # works, so the time check's plain git clone runs (and holds)
    metadata(dest, name, "public")
    track(dest, "data/x.csv", b"x\\n")
elif name == "made-up-held":  # says it started, through a FIFO, and holds a second one open until it dies
    held = os.open(world / "held", os.O_WRONLY)
    (world / "started").write_text(f"{os.getpid()}\\n")
    time.sleep(300)
else:
    sys.exit("PLANTED-STDERR: mintd failed on data/secret.csv")
"""

#: The fake git's hold: says it started, through a FIFO, and holds a second one open until it dies.
_HOLD = """
import os, pathlib, time
world = pathlib.Path(__file__).parent
held = os.open(world / "held", os.O_WRONLY)
(world / "started").write_text(f"{os.getpid()}\\n")
time.sleep(300)
"""

#: A pytest plugin for the tier's own run: whether the signal handlers it
#: found at the start are back at the end, written beside it.
_SIGNAL_CHECK = """
import pathlib, signal
_found = {}
def pytest_configure(config):
    _found.update({s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)})
def pytest_unconfigure(config):
    back = all(signal.getsignal(s) == h for s, h in _found.items())
    (pathlib.Path(__file__).parent / "restored").write_text(str(back))
"""

#: What the operator's environment holds that must never reach mintd.
_PLANTED_ENV = {
    "AWS_ACCESS_KEY_ID": "AKIAPLANTEDWRITE", "AWS_SECRET_ACCESS_KEY": "planted-write-secret",
    "AWS_SESSION_TOKEN": "planted-token", "AWS_PROFILE": "planted-profile", "AWS_ROLE_ARN": "planted-role",
    "AWS_WEB_IDENTITY_TOKEN_FILE": "/planted/token", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://planted.invalid/",
    "AWS_ENDPOINT_URL": "https://planted.invalid", "BOTO_CONFIG": "/planted/boto",
    "FSSPEC_S3_KEY": "AKIAPLANTEDFSSPEC", "FSSPEC_S3_SECRET": "planted-fsspec-secret",
    "FSSPEC_CONFIG_DIR": "/planted/fsspec",
    "DVC_GLOBAL_CONFIG_DIR": "/planted/dvc-global", "DVC_SYSTEM_CONFIG_DIR": "/planted/dvc-system",
}


def _live_world(tmp_path: Path, names: list[str]) -> SimpleNamespace:
    """A registry of made-up products, the box's config, the read-only key
    files and a fake mintd, for running `pytest tests/live` with no storage.

    - made-up-licensed: licensed in the catalog, public in its repo; its clone
      fails with a planted line after writing metadata.json, and its killed
      clone exits 0 having landed nothing.
    - made-up-broken: its catalog entry does not parse, quoting a planted line.
    - made-up-public: public in the catalog, licensed in its repo; both its
      clones fail with a planted line after writing metadata.json.
    - made-up-tiny: public in the catalog, no classification in its repo, its
      primary written `./data/`; one file and a .gitignore beside it, all of
      which the import brings. Every check passes or skips (too small to
      interrupt).
    - made-up-wide: as tiny, but public, and its import also brings a file of
      the clone's from outside the primary, in `data2/` beside `data/`.
    - made-up-odd: as tiny, but public; its import's .gitignore has other
      bytes, and its killed clone exits 1 once every file has landed.
    - made-up-file: as tiny, but public, and its primary is one file kept in
      git, which the import brings.
    - made-up-empty: public; its clone exits 0 with its one file missing, a
      pull lands nothing, its import fails.
    - made-up-small: public; one file, which the killed clone has wholly
      cached when it is interrupted; its import lands other bytes.
    - made-up-held: its clone says it started, then waits (the signal tests).
    - made-up-held-git: its clone works; the plain `git clone` of the time
      check says it started, then waits (the signal tests). Every other git
      call is the real git.
    """
    registry = tmp_path / "registry"
    (registry / "catalog" / "data").mkdir(parents=True)
    for name in ("made-up-licensed", "made-up-public", "made-up-tiny", "made-up-wide", "made-up-odd", "made-up-file",
                 "made-up-empty", "made-up-small", "made-up-held", "made-up-held-git"):
        classification = "licensed" if name == "made-up-licensed" else "public"
        (registry / "catalog" / "data" / f"{name}.yaml").write_text(
            f"governance: {{classification: {classification}}}\nrepository: {{github_url: https://example.invalid/{name}}}\n",
            encoding="utf-8")
    (registry / "catalog" / "data" / "made-up-broken.yaml").write_text(
        'governance: {classification: public}\nrepository:\n  github_url: "PLANTED-ENTRY\n', encoding="utf-8")
    _git(["init", "-q", "-b", "main", str(registry)])
    _git(["add", "-A"], cwd=registry)
    _git(["commit", "-q", "-m", "catalog"], cwd=registry)
    (tmp_path / "config.yaml").write_text(f"registry_url: {registry}\n", encoding="utf-8")
    creds, conf = tmp_path / "readonly", tmp_path / "readonly-config"
    creds.write_text("[mintd-live-readonly]\naws_access_key_id = AKIAREADONLY\naws_secret_access_key = readonly-secret\n",
                     encoding="utf-8")
    conf.write_text("[profile mintd-live-readonly]\nregion = us-east-1\nendpoint_url = https://s3.example.test\n",
                    encoding="utf-8")
    world = tmp_path / "world"
    world.mkdir()
    (world / "mintd.py").write_text(_FAKE_MINTD, encoding="utf-8")
    fake = world / "mintd"
    fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{world / "mintd.py"}" "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    (world / "hold.py").write_text(_HOLD, encoding="utf-8")
    git = world / "git"  # first on the tier's PATH, beside the fake mintd
    git.write_text(f'#!/bin/sh\ncase "$*" in *made-up-held-git*) exec "{sys.executable}" "{world / "hold.py"}" ;; esac\n'
                   f'exec "{shutil.which("git")}" "$@"\n', encoding="utf-8")
    git.chmod(0o755)
    (world / "plugin").mkdir()
    (world / "plugin" / "live_signal_check.py").write_text(_SIGNAL_CHECK, encoding="utf-8")
    (tmp_path / "products.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    kept = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "BOTO_", "FSSPEC_", "MINTD_", "DVC_"))}
    env = {**_PLANTED_ENV, **kept,
           "AWS_CA_BUNDLE": "/planted/ca-bundle.pem", "AWS_SHARED_CREDENTIALS_FILE": str(creds),
           "AWS_CONFIG_FILE": str(conf), "MINTD_CONFIG_DIR": str(tmp_path), "MINTD_BIN": str(fake),
           "MINTD_LIVE_PRODUCTS": str(tmp_path / "products.txt"), "FAKE_MINTD_WORLD": str(world)}
    return SimpleNamespace(env=env, world=world, base=(tmp_path / "base").resolve())


def _live_pytest(*args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "tests/live", "-p", "no:cacheprovider", "-rA", *args]


_ON_POSIX = pytest.mark.skipif(os.name == "nt", reason="the live tier runs on Linux and macOS: a shell-script mintd")


@_ON_POSIX
def test_the_live_tier_gives_mintd_only_its_copy_of_the_key_and_withholds_hidden_details(tmp_path: Path) -> None:
    """Every mintd call sees the key copy, the shut boto and fsspec sources and
    dvc's empty config dirs, and none of the operator's AWS, boto or fsspec
    settings but `AWS_CA_BUNDLE`. No planted line reaches the output, a hidden
    product's results show counts only, the key copy is gone afterwards, and
    each check fails or skips where it should.

    Mutations that turn it red: the env filter dropped, or fsspec left out of
    it; the key, config, boto, metadata, fsspec or dvc-dir lines not set;
    `redact` not called from `pytest_runtest_makereport`, or showing a
    non-Mismatch failure's message; the SEEN placeholder dropped; either clone
    not consulting the repo's metadata.json, or the shared clone consulting it
    only after its exit check; `stricter` always taking the repo's, or keeping
    the catalog's when the repo has none; results lines reading the catalog's
    classification; the key-copy finalizer, or the one restoring the signal
    handlers, dropped; the second pull not
    checking what landed; the import not compared with the clone's primary,
    refusing the git-tracked files a folder import brings, accepting a file
    from outside the primary or with other bytes, refusing a one-file primary
    kept in git, or reading `./data/` as no primary at all; a killed clone that exits non-zero, or lands nothing,
    skipping as too small, or a too-small one failing; a wholly cached one
    passing."""
    world = _live_world(tmp_path, ["made-up-licensed", "made-up-broken", "made-up-public", "made-up-tiny",
                                   "made-up-wide", "made-up-odd", "made-up-file", "made-up-empty", "made-up-small"])
    got = subprocess.run(
        _live_pytest("-k", "not time", f"--basetemp={world.base}", "-p", "live_signal_check"),
        cwd=Path(__file__).parents[1], env={**world.env, "PYTHONPATH": str(world.world / "plugin")},
        capture_output=True, text=True, timeout=300,
    )
    out = got.stdout + got.stderr
    assert (world.world / "plugin" / "restored").read_text() == "True", "the tier left its signal handlers installed"
    assert "PLANTED" not in out and not any(v in out for v in _PLANTED_ENV.values()), out
    calls = [json.loads(line) for line in (world.world / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(calls) > 5 and calls[0]["argv"] == ["--version"], calls
    aws = world.base / "aws"
    for call in calls:
        env = call["env"]
        assert {k for k in env if k.startswith(("AWS_", "BOTO_", "FSSPEC_"))} == {
            "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_CA_BUNDLE", "BOTO_CONFIG", "AWS_EC2_METADATA_DISABLED",
            "FSSPEC_CONFIG_DIR",
        }, (call["argv"], sorted(env))
        assert Path(env["AWS_SHARED_CREDENTIALS_FILE"]) == aws / "credentials", call["argv"]
        assert Path(env["AWS_CONFIG_FILE"]) == aws / "config", call["argv"]
        assert "[mintd]" in call["creds"] and "AKIAREADONLY" in call["creds"], call["argv"]
        assert (Path(env["BOTO_CONFIG"]), env["AWS_EC2_METADATA_DISABLED"]) == (world.base / "boto", "true")
        assert Path(env["FSSPEC_CONFIG_DIR"]) == world.base / "fsspec" and not (world.base / "fsspec").exists()
        assert env["AWS_CA_BUNDLE"] == "/planted/ca-bundle.pem"
        assert (Path(env["DVC_GLOBAL_CONFIG_DIR"]), Path(env["DVC_SYSTEM_CONFIG_DIR"])) == (
            world.base / "dvc-global", world.base / "dvc-system")
    assert (world.base / "boto").read_bytes() == b""
    assert not (aws / "credentials").exists(), "the key copy outlived the run"
    summary = out[out.index("short test summary info"):]
    for outcome, check, product in (
        ("PASSED", "test_a_second_pull_downloads_nothing", "made-up-small"),
        ("FAILED", "test_a_second_pull_downloads_nothing", "made-up-empty"),
        ("FAILED", "test_import_lands_the_primary", "made-up-small"),
        ("PASSED", "test_import_lands_the_primary", "made-up-tiny"),
        ("FAILED", "test_import_lands_the_primary", "made-up-wide"),
        ("FAILED", "test_import_lands_the_primary", "made-up-odd"),
        ("PASSED", "test_import_lands_the_primary", "made-up-file"),
        ("FAILED", "test_a_killed_clone_restarts_and_finishes", "made-up-licensed"),
        ("FAILED", "test_a_killed_clone_restarts_and_finishes", "made-up-odd"),
    ):
        assert f"{outcome} tests/live/test_live.py::{check}[{product}]" in summary, (outcome, check, product, summary)
    assert "too small to interrupt: mintd finished before any object was seen" in summary, summary
    assert re.search(r"not interrupted: all 1 objects had landed when mintd stopped \(.*1 objects cached at the "
                     r"interrupt", summary), summary
    results = out[out.index(" live results "):out.index("short test summary info")]
    tiny = [ln for ln in results.splitlines() if ln.startswith("made-up-tiny ")]
    assert len(tiny) == 4 and not any(";" in ln for ln in tiny), results  # clone, second pull, import, killed
    # The killed clone alone, with no shared clone to read the repo's metadata.json first.
    alone = subprocess.run(
        _live_pytest("-k", "killed and made-up-public", f"--basetemp={tmp_path / 'alone'}"),
        cwd=Path(__file__).parents[1], env=world.env, capture_output=True, text=True, timeout=120,
    )
    assert "1 failed" in alone.stdout and "PLANTED" not in alone.stdout + alone.stderr, alone.stdout + alone.stderr


@_ON_POSIX
def test_the_live_tier_refuses_to_run_without_its_own_basetemp(tmp_path: Path) -> None:
    """Mutation that turns it red: `pytest_configure` not calling `check_basetemp`."""
    world = _live_world(tmp_path, ["made-up-licensed"])
    got = subprocess.run(_live_pytest(), cwd=Path(__file__).parents[1], env=world.env, capture_output=True,
                         text=True, timeout=120)
    assert got.returncode == pytest.ExitCode.USAGE_ERROR and "needs --basetemp" in got.stderr, got.stdout + got.stderr
    assert not (world.world / "calls.jsonl").exists(), "mintd ran"


@_ON_POSIX
@pytest.mark.parametrize("sig, nohup, child", [
    ("SIGTERM", False, "mintd"), ("SIGHUP", False, "mintd"), ("SIGHUP", True, "mintd"), ("SIGTERM", False, "git"),
], ids=["SIGTERM", "SIGHUP", "SIGHUP-under-nohup", "SIGTERM-during-plain-git"])
def test_a_signal_to_the_live_tier_kills_its_children_and_deletes_the_key_copy(
        tmp_path: Path, sig: str, nohup: bool, child: str) -> None:
    """mintd, and the time check's plain git clone, each run in a session of
    their own, which the signal never reaches. Under nohup a hangup stays
    ignored, so the SIGTERM sent after it is what ends the run. Mutations that
    turn it red: no handler for the signal (pytest dies at once, leaving the
    child running and the key copy on disk); the handler installed over an
    inherited SIG_IGN (the hangup ends the run); `run_read_only`'s child not
    tracked, or not in a session of its own (it outlives the signal)."""
    product, check = ("made-up-held", "clone_lands") if child == "mintd" else ("made-up-held-git", "time")
    world = _live_world(tmp_path, [product])
    os.mkfifo(world.world / "started")
    os.mkfifo(world.world / "held")
    started = os.open(world.world / "started", os.O_RDWR | os.O_NONBLOCK)
    alive = os.open(world.world / "held", os.O_RDONLY | os.O_NONBLOCK)  # EOF once the fake mintd has died
    hangup = signal.SIG_IGN if nohup else signal.SIG_DFL  # as nohup leaves it, or as a terminal does
    pid = None
    try:
        with subprocess.Popen(
            _live_pytest("-k", check, f"--basetemp={world.base}"), cwd=Path(__file__).parents[1],
            env=world.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: signal.signal(signal.SIGHUP, hangup),
        ) as tier:
            ready, _, _ = select.select([started], [], [], 60)
            assert ready, f"the fake {child} did not start within 60 s"
            pid = int(os.read(started, 32))
            tier.send_signal(getattr(signal, sig))
            if nohup:  # an ignored hangup changes nothing: the run is still going, and SIGTERM ends it
                with pytest.raises(subprocess.TimeoutExpired):
                    tier.wait(2)
                tier.send_signal(signal.SIGTERM)
            assert tier.wait(30) == -(signal.SIGTERM if nohup else getattr(signal, sig))
        ready, _, _ = select.select([alive], [], [], 10)
        assert ready, f"{child} outlived the signal"
        assert not (world.base / "aws" / "credentials").exists(), "the key copy outlived the signal"
    finally:
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        os.close(started)
        os.close(alive)


@_ON_POSIX
def test_a_ctrl_c_during_plain_git_or_dvc_kills_its_whole_group(tmp_path: Path) -> None:
    """`run_read_only`'s child runs in a session of its own, which the
    terminal's Ctrl-C never reaches. Mutation that turns it red: no
    `except` block killing its group (the held git outlives the Ctrl-C)."""
    (tmp_path / "hold.py").write_text(_HOLD, encoding="utf-8")
    git = tmp_path / "git"
    git.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "hold.py"}"\n', encoding="utf-8")
    git.chmod(0o755)
    os.mkfifo(tmp_path / "started")
    os.mkfifo(tmp_path / "held")
    started = os.open(tmp_path / "started", os.O_RDWR | os.O_NONBLOCK)
    alive = os.open(tmp_path / "held", os.O_RDONLY | os.O_NONBLOCK)  # EOF once the held git has died

    def ctrl_c(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    def press() -> None:  # once the git says it started, to the main thread, which waits on it
        if select.select([started], [], [], 60)[0]:
            signal.pthread_kill(threading.main_thread().ident, signal.SIGUSR1)

    previous = signal.signal(signal.SIGUSR1, ctrl_c)
    presser = threading.Thread(target=press, daemon=True)
    try:
        presser.start()
        with pytest.raises(KeyboardInterrupt):
            run_read_only(["git", "clone", "anything"],
                          env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"})
        assert select.select([alive], [], [], 10)[0], "git outlived the Ctrl-C"
    finally:
        signal.signal(signal.SIGUSR1, previous)
        presser.join(5)
        with contextlib.suppress(OSError, ValueError):  # a git the mutation left running
            os.kill(int(os.read(started, 32)), signal.SIGKILL)
        os.close(started)
        os.close(alive)


def test_the_live_tier_needs_its_own_basetemp_and_reads_the_entry_mintd_reads(tmp_path: Path) -> None:
    """Mutations that turn it red: `check_basetemp` accepting a missing or busy
    folder, or `catalog_entry` searching folders in another order."""
    for given in (None, ""):
        with pytest.raises(HarnessError, match="needs --basetemp"):
            check_basetemp(given)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(HarnessError, match="did not make"):
        check_basetemp(str(busy))
    check_basetemp(str(tmp_path / "new"))
    (busy / MARKER).touch()
    check_basetemp(str(busy))
    for kind in ("code", "data"):
        (tmp_path / "catalog" / kind).mkdir(parents=True)
        (tmp_path / "catalog" / kind / "made-up.yaml").write_text(f"kind: {kind}\n", encoding="utf-8")
    assert catalog_entry(tmp_path, "made-up") == tmp_path / "catalog" / "data" / "made-up.yaml"
    assert catalog_entry(tmp_path, "absent") is None

