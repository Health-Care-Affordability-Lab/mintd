"""What `tests/_harness/` promises.

Not a test of mintd — a test of the fixtures, which are now load-bearing for
several downstream units. Each case here names the capability no helper in the
tree had before, so a regression in a builder fails next to the reason it
exists rather than four modules away.

Every assertion is on an artifact — a SHA, a file dvc parsed, a finding a real
walker produced — never on a double's recorded call.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mintd._config import Timeouts
from mintd._dvc_ops import DvcOpError, SubprocessDvcOps
from mintd._fast_sync_ops import (
    SubprocessFastSyncOps,
    parse_dvc_lock_outs,
    partition_pipeline_outs,
)
from mintd.catalog import InMemoryCatalogClient
from mintd.check import check_project
from mintd.data_ops import data_pull
from mintd.imports import scan_imports
from mintd.model import Metadata
from mintd.producer import ProducerError, ProducerView
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._harness.consumer import Import
from tests._harness.git import _git
from tests._harness.producer import LocalProducer, build_local_producer

FIXTURES = Path(__file__).parent / "fixtures"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"

ALPHA_URL = "https://github.com/example-org/alpha"
BETA_URL = "https://github.com/example-org/beta"
#: `source_path` of the one product `enclave_manifest_v2_minimal.yaml` approves.
APPROVED_PATH = "outputs/cms_based/"


def _catalog_pointing_at(url: str) -> InMemoryCatalogClient:
    """A catalog whose `provider-xw` entry resolves to `url` — the manifest
    fixture approves that repo by name, so this is what makes the enclave arm
    reach a producer instead of a 404."""
    client = InMemoryCatalogClient()
    data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
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


def _pull(project: Path, targets: list[str] | None = None) -> None:
    """Production's own pull, through the real dvc seam. `fast_sync_ops=None`
    on purpose — see `test_local_remote_degrades_fast_sync_to_the_fallback_route`
    for why a local-directory remote could not use fast-sync anyway.

    This helper used to wrap the call in `os.chdir(project)` / `finally:
    os.chdir(cwd)`, mirroring the same block `data.py` carried, because
    `DvcOps` had no way to say which repo a pull acted on. Unit A gave it one,
    so `project_path` now does the aiming and the chdir is gone. Deleting it
    is the strongest gate in that unit: with `data_pull` no longer passing
    `cwd`, the three real-dvc tests below fail outright rather than quietly
    passing on ambient process state."""
    data_pull(
        project_path=project,
        targets=targets,
        dvc_ops=SubprocessDvcOps(timeouts=Timeouts()),
        fast_sync_ops=None,
    )


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


def test_enclave_manifest_consumer_variant_loads(
    consumer_project, local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The enclave arm of `check`, walked end to end.

    `tests/fixtures/enclave_manifest_v2_minimal.yaml` has existed for a while
    but nothing composed a *project* around it, so `check.py`'s enclave walker
    was reached by no fixture. This is the variant unit A (position 8) needs.
    """
    # The manifest approves `outputs/cms_based/`. Under the md5 drift rule
    # the comparison reads the producer's own DVC pointer at pin and HEAD,
    # so the payload must really exist — `publish()` DVC-tracks bytes and
    # commits, which is exactly what moves the pointer.
    local_producer.rename_primary(APPROVED_PATH)
    local_producer.publish({APPROVED_PATH: {"a.csv": b"v1"}})
    proj = consumer_project(enclave=True, enclave_pin=local_producer.head_sha)

    # ... and now the producer republishes new bytes at the SAME path — the
    # researcher's actual drift case; no rename required.
    local_producer.publish({APPROVED_PATH: {"a.csv": b"v2"}}, message="v2")

    client = _catalog_pointing_at(local_producer.url)
    cache = tmp_path / "pcache"

    def at(repo: str, pin: str):
        # `""` is check.py:443's HEAD sentinel — a contract between the walker
        # and its factory, not something ProducerView.try_at interprets.
        return ProducerView.try_at(
            repo, pin or local_producer.head_sha, cache_dir=cache
        )

    findings = check_project(proj, upgrades=True, client=client, producer_view_factory=at)

    consumer = [f for f in findings if f.section == "consumer"]
    assert [f.field_path for f in consumer] == ["approved_products[provider-xw]"]
    assert consumer[0].kind == "drift", consumer[0].message
    assert APPROVED_PATH in consumer[0].message
    assert "changed at the producer's HEAD" in consumer[0].message
    assert consumer[0].source == proj / "enclave_manifest.yaml"


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


def test_enclave_pull_caches_into_the_enclave_not_the_outer_repo(
    local_producer: LocalProducer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The custody defect, through real dvc, with the enclave nested inside
    another DVC repo — the shape that failed SILENTLY at exit 0.

    Standing in `outer/` and pulling `enclave/`'s manifest, pre-unit-A dvc
    imported into `outer` because that is where the process was: measured at
    `9a2a54e`, `outer/.dvc/cache` gained the producer's blob and the enclave's
    cache stayed empty, rc=0, with the file still delivered to the right path
    so nothing looked wrong. In the one lane whose entire purpose is data
    custody, a repo that is not the enclave held the restricted bytes.

    Two things have to be right for this to pass, and only one of them is
    "pass cwd":

    1. `import_` is aimed at `manifest_path.parent`.
    2. `-o` is absolutized at the seam. The manifest path here is DELIBERATELY
       RELATIVE, because that is production's default (`cli.py`'s `--manifest`
       defaults to `Path("enclave_manifest.yaml")` and is never resolved). With
       a relative `-o` and a re-aimed cwd, real dvc reads the destination
       against the NEW directory and fails `stage working dir
       '.../outer/enclave/enclave/downloads/_staging' does not exist` — note
       the doubled segment. An absolute-manifest version of this test passes
       either way and gates nothing.
    """
    from dataclasses import dataclass

    from mintd._config import Timeouts
    from mintd._dvc_ops import SubprocessDvcOps
    from mintd.enclave import ApprovedProduct, EnclaveManifest, enclave_pull

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
    ops.init(cwd=enclave)

    m_path = enclave / "enclave_manifest.yaml"
    EnclaveManifest(enclave_name="test", approved_products=[
        ApprovedProduct(
            repo="prod", registry_entry="e", pin=pin, source_path="outputs/data.csv",
        )
    ]).save(m_path)

    @dataclass
    class _Entry:
        repo_url: str

    class _Client:
        def fetch(self, name):
            return _Entry(repo_url=local_producer.url)

    monkeypatch.chdir(outer)
    # RELATIVE on purpose -- see the docstring. This is what production passes.
    enclave_pull(_Client(), ops, manifest_path=Path("enclave/enclave_manifest.yaml"))

    def blobs(repo: Path) -> list[str]:
        d = repo / ".dvc" / "cache" / "files" / "md5"
        return sorted(p.name for p in d.rglob("*") if p.is_file()) if d.exists() else []

    assert blobs(outer) == [], (
        f"the OUTER repo cached the producer's bytes: {blobs(outer)}"
    )
    assert blobs(enclave), "the enclave's cache is empty; the import went elsewhere"
    delivered = list((enclave / "downloads" / "prod").rglob("data.csv"))
    assert delivered, "the payload was not delivered into the enclave"
    assert delivered[0].read_bytes() == b"restricted-producer-bytes\n"


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
