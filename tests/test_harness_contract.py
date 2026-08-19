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

from mintd._fast_sync_ops import parse_dvc_lock_outs
from mintd.catalog import InMemoryCatalogClient
from mintd.check import check_project
from mintd.imports import scan_imports
from mintd.model import Metadata
from mintd.producer import ProducerError, ProducerView
from tests._harness.consumer import Import
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


def test_publish_names_the_slice_that_owes_the_payload(
    local_producer: LocalProducer,
) -> None:
    """`publish()` is present and raising. An absent method fails with an
    `AttributeError` that reads like a typo; this one says which slice owes
    the bytes."""
    with pytest.raises(NotImplementedError, match="1b"):
        local_producer.publish()


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


def test_enclave_manifest_consumer_variant_loads(
    consumer_project, local_producer: LocalProducer, tmp_path: Path
) -> None:
    """The enclave arm of `check`, walked end to end.

    `tests/fixtures/enclave_manifest_v2_minimal.yaml` has existed for a while
    but nothing composed a *project* around it, so `check.py`'s enclave walker
    was reached by no fixture. This is the variant unit A (position 8) needs.
    """
    # The manifest approves `outputs/cms_based/`; a producer that never
    # published that path short-circuits to "up to date" at check.py:506
    # before the pin/HEAD comparison is reached, so the arm would be walked
    # only as far as its first early return.
    local_producer.rename_primary(APPROVED_PATH)
    proj = consumer_project(enclave=True, enclave_pin=local_producer.head_sha)

    # ... and now the producer moves the product, which is what the researcher
    # is asking `check --upgrades` about.
    local_producer.rename_primary("outputs/cms_v2/")

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
    assert "outputs/cms_v2/" in consumer[0].message
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
