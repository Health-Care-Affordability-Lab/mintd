"""The pre-units binding question, end to end.

`PLAN-pre-units.md` shipped four slices (P2, P1, issue28, P3) and asks one
question of the result: a researcher running the ordinary local loop must not
hit a failure that is about mintd's own wiring, and must not lose a byte of a
file mintd did not create.

Two rules this file exists to keep:

- **The project is seeded by migrating the v1 fixture**, not by scaffolding at
  v2. A v2 scaffold never invokes `update metadata`, so P3's storage carry is
  load-bearing nowhere in the journey and the question gets signed off on three
  slices out of four.
- **`check_project`, `download_object` and `write_profile` are never patched.**
  Those are the three functions the plan repairs; substituting any of them
  means the fix landed at the wrong layer. What stands in is the process
  boundaries: dvc, `gh`, S3, and one `os.replace`. `_resolve_catalog_client` is
  deliberately *not* stubbed either — its `registry_url` guard is what issue30
  is about, so `gh` is faked by swapping the client class the real resolver
  constructs, leaving the resolver itself running.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from moto import mock_aws

from mintd import cli
from mintd._config import ConfigError
from mintd.catalog import CatalogAlreadyExists, GitCatalogClient
from mintd.model import Metadata
from tests._enclave_fixtures import stage_enclave_manifest
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.registry_git_ops import _FakeRegistryGitOps
from mintd._dvc_invoke import dvc_cmd as _dvc_cmd
from tests._harness.git import _git

FIXTURES = Path(__file__).parent / "fixtures"
V1_REAL_WORLD = FIXTURES / "metadata_v1_real_world.json"
V2_MINIMAL = FIXTURES / "metadata_v2_minimal.json"

PRIMARY = "data/final/weights.parquet"
SLACK = "#data-eng"
DOI = "10.5281/zenodo.7654321"


# Keyed on the prompt text so a reworded prompt fails loudly here rather than
# silently answering "" and skipping the credentials write this test is about.
_SETUP_ANSWERS = {
    "Configure [mintd] profile now?": "y",
    "AWS access key ID": "AKIAMINTD",
    "AWS secret access key": "mintdsecret",
}


def _run_setup(tmp_path: Path, creds: Path) -> None:
    """`config setup`'s wizard, scripted: every config field keeps its current
    value, and the AWS prompts are answered."""
    from mintd import config_ops

    def answer(prompt: str) -> str:
        for key, value in _SETUP_ANSWERS.items():
            if key in prompt:
                return value
        return ""  # every Config field: keep the current value

    config_ops.interactive_setup(
        tmp_path / "config" / "config.yaml",
        prompt_fn=answer,
        aws_credentials_path=creds,
    )


def _drain(capsys) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def _write_config(
    config_dir: Path,
    *,
    registry_url: str | None,
    cache_dir: Path,
    registry_org: str | None = None,
) -> None:
    """A real config.yaml under MINTD_CONFIG_DIR — `Config.load` is never patched,
    so 'no registry_url configured' is a fact about the file, not about a stub."""
    lines = [
        f"cache_dir: {cache_dir}",
        "storage_bucket_prefix: test-bucket",
        "storage_endpoint: https://s3",
        "author: Test Researcher",
    ]
    if registry_url is not None:
        lines.append(f"registry_url: {registry_url}")
    if registry_org is not None:
        lines.append(f"registry_org: {registry_org}")
    (config_dir / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_project(tmp_path: Path) -> Path:
    """A v1 project migrated to v2 — the shape a real lab repo arrives in."""
    proj = tmp_path / "project"
    proj.mkdir()
    v1 = json.loads(V1_REAL_WORLD.read_text(encoding="utf-8"))
    (proj / "metadata.json").write_text(json.dumps(v1, indent=2) + "\n", encoding="utf-8")

    # The repo arrives with its DVC remote already wired — that is the v1 state,
    # written before the migration runs, not something derived from its output.
    # Anchoring it here is what makes P3 load-bearing: a migration that drops the
    # storage block leaves this remote with nothing in metadata to match, and
    # `check` reports drift.
    remote = v1["storage"]["dvc"]["remote_name"]
    (proj / ".dvc").mkdir()
    (proj / ".dvc" / "config").write_text(
        f"[core]\n"
        f"    remote = {remote}\n"
        f"['remote \"{remote}\"']\n"
        f"    url = {v1['storage']['dvc']['remote_url']}\n",
        encoding="utf-8",
    )

    assert cli.main(["update", "metadata", str(proj)]) == 0

    # The researcher's own keys, added *after* migration: migration drops stray
    # nested keys by design (it reports them), so planting them in the v1 file
    # would test the wrong thing. These are what issue28 must carry through
    # `publish`.
    data = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    data["ownership"]["slack"] = SLACK
    data["metadata"]["doi"] = DOI
    data["data_products"]["primary"] = PRIMARY
    data["data_products"]["outputs"] = [
        {
            "path": PRIMARY,
            "description": "analysis-ready weights",
            "primary": True,
            "last_published": "",
        }
    ]
    (proj / "metadata.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    (proj / "data" / "final").mkdir(parents=True)
    (proj / PRIMARY).write_text("weights", encoding="utf-8")

    _git(["init", "-b", "main"], cwd=proj)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-m", "migrated"], cwd=proj)
    return proj


def _register_provider(registry_url: str, work_dir: Path) -> None:
    """Seed the one entry the staged enclave manifest approves."""
    data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = "https://github.com/example-org/provider-xw"
    client = GitCatalogClient(
        registry_repo_url=registry_url,
        work_dir=work_dir,
        git_ops=_FakeRegistryGitOps(),
    )
    client.register(Metadata.model_validate(data))


@pytest.fixture
def journey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_registry_empty: Path):
    """The world: a migrated v1 project with an approved enclave product, a real
    local-git registry, a fake dvc and a stubbed `gh`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config_dir))
    _write_config(config_dir, registry_url=None, cache_dir=cache_dir)

    registry_url = str(remote_registry_empty)
    _register_provider(registry_url, tmp_path / "seed-work")

    proj = _seed_project(tmp_path)
    stage_enclave_manifest(proj)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-m", "enclave manifest"], cwd=proj)

    git_ops = _FakeRegistryGitOps()
    dvc_ops = _FakeDvcOps()
    monkeypatch.setattr("mintd.cli._resolve_dvc_ops", lambda cfg, reporter=None, **_: dvc_ops)
    monkeypatch.setattr("mintd.cli._resolve_git_ops", lambda cfg, reporter=None, **_: git_ops)
    # `_resolve_catalog_client` is left alone on purpose.
    # Stubbing either replaces the `registry_url` guard that issue30 is about, and
    # the `data add` leg below then passes whether or not that fix is present —
    # verified: with the guard restored to `data add`, a stubbed resolver keeps
    # this file green. Only the `gh` boundary inside the real client stands in.
    monkeypatch.setattr(
        "mintd.cli.GitCatalogClient",
        lambda **kw: GitCatalogClient(**kw, git_ops=git_ops),
    )
    monkeypatch.setattr("mintd.cli._resolve_fast_sync_ops", lambda cfg, **_: None)

    def configure_registry() -> None:
        _write_config(config_dir, registry_url=registry_url, cache_dir=cache_dir)

    return proj, configure_registry


def test_local_loop_never_manufactures_a_failure(journey, capsys) -> None:
    proj, configure_registry = journey

    # `data add` wraps dvc and never reads the catalog — issue30: it must not
    # demand a registry_url on a machine that has none.
    assert cli.main(["data", "add", str(proj / PRIMARY)]) == 0

    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-m", "track the primary output"], cwd=proj)

    configure_registry()

    assert cli.main(["check", str(proj)]) == 0, _drain(capsys)
    # The third call site P2 repaired: an approved enclave product blocked
    # `register` the same way it blocked `check` and `publish`.
    assert cli.main(["registry", "register", str(proj)]) == 0, _drain(capsys)
    assert cli.main(["publish", "--path", str(proj), "--yes"]) == 0, _drain(capsys)


def test_local_loop_never_destroys_a_file_mintd_did_not_create(
    journey, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    proj, configure_registry = journey

    # ---- the researcher's own scratch file, sitting exactly where share get's
    # ---- temp used to land (issue15)
    dest = tmp_path / "report.csv"
    squatter = tmp_path / "report.csv.tmp"
    squatter.write_bytes(b"my own half-finished notes")
    squatter_before = squatter.read_bytes()

    payload_src = tmp_path / "src.csv"
    payload_src.write_bytes(b"col_a,col_b\n1,2\n" * 500)

    with mock_aws():
        import boto3

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        monkeypatch.setattr("mintd._share_ops._create_s3_client", lambda _c, _p: s3)

        assert cli.main(["share", "put", str(payload_src)]) == 0, _drain(capsys)
        assert cli.main(
            ["share", "get", "test-researcher/src.csv", "--out", str(dest)]
        ) == 0, _drain(capsys)

    assert squatter.read_bytes() == squatter_before
    assert dest.read_bytes() == payload_src.read_bytes()

    # ---- the researcher's own [default] AWS profile (issue29). A library call
    # ---- rather than `mintd config setup`, which requires a TTY — a recorded
    # ---- limitation of this journey, not a silent shortcut.
    #
    # The defect only materialises when the write dies mid-flight, so the crash
    # is injected at `os.replace` — the close criterion issue29 names. Injecting
    # it there is not substituting the function under test: `write_profile` runs
    # in full, and what is faked is the syscall it is being asked to perform
    # atomically.
    creds = tmp_path / "aws" / "credentials"
    creds.parent.mkdir()
    creds.write_bytes(
        b"[default]\naws_access_key_id = THEIR_OWN_KEY\n"
        b"aws_secret_access_key = THEIR_OWN_SECRET\n"
    )
    creds_before = creds.read_bytes()

    real_replace = os.replace

    def _die_on_the_credentials_write(src, dst, *a, **kw):
        # `os` is one shared module object; narrow the crash to this one file
        # or the config.yaml write dies too and the test proves nothing.
        if Path(dst) == creds:
            raise OSError("laptop lost power")
        return real_replace(src, dst, *a, **kw)

    with monkeypatch.context() as crashing:
        crashing.setattr("mintd._aws_credentials.os.replace", _die_on_the_credentials_write)
        with pytest.raises(ConfigError):
            _run_setup(tmp_path, creds)

    assert creds.read_bytes() == creds_before
    assert list(creds.parent.glob("credentials.*.tmp")) == []

    # and the ordinary run still lands the profile beside theirs
    _run_setup(tmp_path, creds)
    after = creds.read_text(encoding="utf-8")
    assert "THEIR_OWN_KEY" in after and "THEIR_OWN_SECRET" in after
    assert "[mintd]" in after

    # ---- the researcher's hand-added metadata keys (issue28)
    assert cli.main(["data", "add", str(proj / PRIMARY)]) == 0
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-m", "track the primary output"], cwd=proj)
    configure_registry()
    assert cli.main(["registry", "register", str(proj)]) == 0, _drain(capsys)
    assert cli.main(["publish", "--path", str(proj), "--yes"]) == 0, _drain(capsys)

    final = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    assert final["ownership"]["slack"] == SLACK
    assert final["metadata"]["doi"] == DOI


# ---------------------------------------------------------------------------
# The scaffold lane (rule 3a)
#
# Everything above seeds by migrating the v1 fixture, and the file's own
# docstring makes that a rule — so nothing here has ever walked a project
# `mintd init` produced. That is the only lane reaching the two blockers PRs
# #26 and #27 fixed, and both were shipped without a journey that crosses them.
# ---------------------------------------------------------------------------

SCAFFOLD_ORG = "example-org"


@pytest.fixture
def scaffolded_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_registry_empty: Path
) -> Path:
    """A project as `mintd init` leaves it, then filled in the way a researcher
    fills one in.

    Returns `(project_path, dvc_spawn_envs)`.

    `init_project` is called directly rather than through
    `cli.main(["init", …])`. That path prompts for a storage classification and
    raises `InitNonInteractive` off a TTY — verified, it exits 1 with "init is
    interactive; run from a terminal" — and the only way past it is
    `monkeypatch.setattr("mintd.init._prompt_classification", …)`, which is a
    `BANNED_TARGETS` entry and shrink-only. What this lane is *about* lives in
    `render_scaffold`, which this reaches; argparse coverage of the verb is
    `tests/test_cli.py:1486`'s job.

    Ops are real: a fake `InitOps` records `dvc init` instead of performing it,
    which leaves no `.dvc/config` and makes `check` report storage drift for a
    reason no researcher would ever see. HOME and the dvc site cache are
    redirected under `tmp_path` so the real binaries stay off the developer's
    machine.
    """
    from mintd.init import init_project

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DVC_SITE_CACHE_DIR", str(tmp_path / "dvc-site"))
    # DVC_NO_ANALYTICS is deliberately NOT set here. Production passes it via
    # `dvc_env()`, and the telemetry assertion in the test below is what proves
    # that — setting it in the fixture would make the assertion measure the
    # fixture instead of `SubprocessInitOps`.
    (tmp_path / "home").mkdir()

    _write_config(
        config_dir,
        registry_url=str(remote_registry_empty),
        cache_dir=tmp_path / "cache",
        registry_org=SCAFFOLD_ORG,
    )

    git_ops = _FakeRegistryGitOps()
    monkeypatch.setattr(
        "mintd.cli.GitCatalogClient",
        lambda **kw: GitCatalogClient(**kw, git_ops=git_ops),
    )
    monkeypatch.setattr(
        "mintd.cli._resolve_git_ops", lambda cfg, reporter=None, **_: git_ops
    )

    # Observe what `SubprocessInitOps` actually hands each dvc spawn. A
    # pass-through spy on the process boundary, not a stub: dvc still runs.
    dvc_spawn_envs: list[dict[str, str]] = []
    real_run = subprocess.run

    def _record_dvc_spawns(argv, *a, **kw):
        if isinstance(argv, list) and any("dvc" in str(x) for x in argv[:3]):
            dvc_spawn_envs.append(dict(kw.get("env") or {}))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr("mintd._init_ops.subprocess.run", _record_dvc_spawns)

    proj, _ = init_project(
        project_type="data",
        name="scaffolded",
        target_dir=tmp_path,
        classification="labonly",
        bucket="test-bucket",
        endpoint="https://s3",
    )

    # The one field the scaffold deliberately leaves for the researcher.
    meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    meta["data_products"] = {
        "primary": "data/final/",
        "outputs": [
            {
                "path": "data/final/",
                "description": "analysis-ready dataset",
                "primary": True,
                "last_published": "",
            }
        ],
    }
    (proj / "metadata.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return proj, dvc_spawn_envs


def test_scaffolded_project_checks_and_publishes_clean(
    scaffolded_journey, capsys
) -> None:
    """A scaffold that a researcher filled in passes both gates.

    PR #26 gave the scaffold a derived `repository.github_url` and made an
    empty one an *error*; PR #27 stamped a publishable `mint.version`. Both
    are load-bearing here: without either, one of these two calls returns 1.
    """
    proj, dvc_spawn_envs = scaffolded_journey

    meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
    assert meta["repository"]["github_url"] == (
        f"https://github.com/{SCAFFOLD_ORG}/data_scaffolded"
    )
    assert meta["mint"]["version"]

    assert cli.main(["check", str(proj)]) == 0, _drain(capsys)
    assert cli.main(["publish", "--path", str(proj), "--dry-run"]) == 0, _drain(capsys)

    # Every dvc spawn `SubprocessInitOps` made carried the telemetry opt-out.
    #
    # Asserted on the env at the syscall boundary rather than on the absence of
    # dvc's telemetry id file. That file is written by a *detached* daemon
    # ~0.3s after dvc exits, so an absence check races it and fails in the
    # silent direction — a slow CI runner means telemetry is on and the test
    # is green — and on Windows the file lands under %LOCALAPPDATA%, outside
    # the redirected HOME, so the check would no-op entirely.
    assert dvc_spawn_envs, "no dvc spawn observed — this stopped testing real init ops"
    assert all(e.get("DVC_NO_ANALYTICS") == "1" for e in dvc_spawn_envs), [
        e.get("DVC_NO_ANALYTICS") for e in dvc_spawn_envs
    ]


def test_scaffolded_project_with_an_emptied_github_url_fails_check(
    scaffolded_journey, capsys
) -> None:
    """The red twin. Every other `cli.main` assertion in this file is `== 0`,
    so no verb here has ever had a failing arm — which is what leaves a green
    journey unable to distinguish "the gate passed" from "the gate is inert".

    Emptying the field is the state PR #26 found in four live catalog entries,
    reached from the opposite direction: a human editing metadata.json after
    the scaffold derived it correctly.
    """
    proj, _ = scaffolded_journey
    path = proj / "metadata.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["repository"]["github_url"] = ""
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    assert cli.main(["check", str(proj)]) == 1

    out = _drain(capsys)
    assert "repository.github_url is not set" in out
    # publish must refuse for the same reason, or `check` is advisory.
    assert cli.main(["publish", "--path", str(proj), "--dry-run"]) == 1


# ---------------------------------------------------------------------------
# Rule 3b — the payload journeys
#
# These are the file's first tests where bytes actually move. Everything above
# runs against `_FakeDvcOps`, which is right for the questions it asks ("did
# the handler refuse the work?") and structurally unable to ask this one ("did
# the researcher end up with the data?").
#
# **Nothing in `src/mintd/` is patched here — not one function.** The existing
# `bump_import` tests (`tests/test_data.py:317`, `:342`) monkeypatch
# `mintd.data.check_project`, which is the very detector the bump consults to
# decide whether the producer moved, so they assert the bump acts on a verdict
# the test itself supplied. Below, the producer really advances, `check_project`
# really runs, and real dvc really fetches. The only stand-ins are `chdir` and
# `MINTD_CONFIG_DIR`, and `_resolve_dvc_ops` / `_resolve_catalog_client` are
# left alone — they already build the production objects.
# ---------------------------------------------------------------------------

PRODUCT = "test_project"
#: Matches `data_products.primary` in `metadata_v2_minimal.json` (`data/final/`),
#: so the producer's own metadata needs no doctoring to describe its payload.
PRIMARY_OUT = "data/final"
#: `project.full_name` in the fixture — the `data/imports/` folder.
FULL_NAME = "data_test_project"
#: D-A's contract: the positional is the DATA PRODUCT NAME (the catalog key)
#: for `import` and `--bump` alike; the output is selected with `--path`.
#: There is one identifier — the old local-stem key (`final`) is gone.


@pytest.fixture
def payload_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_registry_empty: Path
):
    """A real producer serving real bytes, a real registry, and real dvc.

    Returns `(producer, consumer, register)`. `register` re-publishes the
    producer's catalog entry, which is how the consumer learns where to fetch
    from — the entry's `github_url` is the local bare repo, so `dvc import`
    clones over the filesystem instead of the network.
    """
    import itertools

    from tests._harness.producer import build_local_producer

    seed_counter = itertools.count()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config_dir))
    # dvc's own config knobs, not HOME — see `tests/_harness/dvc.py` for why
    # HOME alone fails open.
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DVC_GLOBAL_CONFIG_DIR": str(home / "dvc-global"),
        "DVC_SYSTEM_CONFIG_DIR": str(home / "dvc-system"),
        "DVC_SITE_CACHE_DIR": str(tmp_path / "dvc-site"),
    }.items():
        monkeypatch.setenv(key, value)

    producer = build_local_producer(tmp_path / "prod")
    registry_url = str(remote_registry_empty)
    _write_config(
        config_dir, registry_url=registry_url, cache_dir=tmp_path / "cache"
    )

    def register() -> None:
        """(Re)publish the producer's entry. Called again after the producer
        moves, because the catalog is how a consumer discovers HEAD."""
        data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
        data["repository"]["github_url"] = producer.url
        # `gh` is a network boundary and is the one thing faked here, by
        # CONSTRUCTOR ARGUMENT rather than monkeypatch — the same seam
        # `_register_provider` above uses. The catalog client itself, the git
        # clone into the bare registry and the entry it writes are all real,
        # which is what the `data import` leg below then reads back through
        # the untouched `_resolve_catalog_client`.
        client = GitCatalogClient(
            registry_repo_url=registry_url,
            work_dir=tmp_path / f"seed-{next(seed_counter)}",
            git_ops=_FakeRegistryGitOps(),
        )
        meta = Metadata.model_validate(data)
        try:
            client.register(meta)
        except CatalogAlreadyExists:
            client.update(meta)

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(["init", "-b", "main", str(consumer)])
    (consumer / "metadata.json").write_text(
        V2_MINIMAL.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(
        [*_dvc_cmd(), "init"], cwd=str(consumer), capture_output=True, check=True
    )
    _git(["add", "-A"], cwd=consumer)
    _git(["commit", "-m", "consumer"], cwd=consumer)

    monkeypatch.chdir(consumer)
    return producer, consumer, register


def _registry_url_from_config() -> str:
    """The `registry_url` the fixture wrote — read back from the real config
    rather than threaded through, so it cannot drift from what the CLI reads."""
    from mintd._config import Config

    url = Config.load().registry_url
    assert url, "payload_journey did not write a registry_url"
    return url


def _pins_of(consumer: Path) -> dict[str, str]:
    """Every import's pin, by local-path name, through production's parser.

    `scan_imports` is what `check_project` and `bump_import` both walk, so a
    pin read any other way could agree with the file and still disagree with
    what mintd believes.
    """
    from mintd.imports import scan_imports

    return {dep.local_path: dep.contract_pin for dep in scan_imports(consumer)}


def _imported_bytes(consumer: Path, rel_path: str) -> bytes:
    # `<dest_root>/<full_name>/<producer's own path>` — D-A mirrors the
    # producer's path under the product's full-name folder.
    return (
        consumer / "data" / "imports" / FULL_NAME / rel_path / "part.csv"
    ).read_bytes()


def test_import_bump_advances_the_pin_and_lands_new_bytes(
    payload_journey, capsys
) -> None:
    """The drift chain, end to end: producer moves, consumer bumps, new bytes.

    This is the journey `bump_import` exists for. The producer republishes a
    refreshed payload at the SAME path — the commonest real case, invisible
    under the old primary-path comparison (`check` said "up to date" however
    many commits and bytes sat behind an unchanged path). Under the md5 rule
    the pointer hash moves, `check` reports drift, and the bump takes the
    data product name — the same identifier `import` takes (D-A); the old
    local-stem key is retired.
    """
    producer, consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()

    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    assert _imported_bytes(consumer, PRIMARY_OUT) == b"v1\n"
    pin_before = _pins_of(consumer)["final"]

    # New bytes, same path. `publish()` advances HEAD by exactly one commit
    # (a peer of `commit_more()`, not a prelude to one).
    producer.publish({PRIMARY_OUT: {"part.csv": b"v2\n"}}, message="v2 payload")
    register()

    assert cli.main(["data", "import", PRODUCT, "--bump"]) == 0, _drain(capsys)

    pins = _pins_of(consumer)
    assert pins["final"] == producer.head_sha
    assert pins["final"] != pin_before
    assert _imported_bytes(consumer, PRIMARY_OUT) == b"v2\n", (
        "the pin moved but the bytes did not — a bump that rewrites the "
        "pointer without re-fetching is the failure this asserts against"
    )
    # The SAME `.dvc` was rewritten — no orphaned sibling pointer.
    assert set(pins) == {"final"}


def test_import_bump_of_a_non_primary_row_rewrites_that_row_only(
    payload_journey, capsys
) -> None:
    """D-C2's proof at the real collaborator: bump a `--path` row and the
    producer's primary is not consulted — THAT row's `.dvc` is rewritten,
    the primary's is untouched, and nothing is orphaned."""
    producer, consumer, register = payload_journey
    producer.publish({
        PRIMARY_OUT: {"part.csv": b"v1\n"},
        "data/extract": {"part.csv": b"e1\n"},
    })
    register()

    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    assert cli.main(["data", "import", PRODUCT, "--path", "data/extract"]) == 0, \
        _drain(capsys)
    final_pin_before = _pins_of(consumer)["final"]

    producer.publish({"data/extract": {"part.csv": b"e2\n"}}, message="extract v2")
    register()

    assert cli.main(
        ["data", "import", PRODUCT, "--path", "data/extract", "--bump"]
    ) == 0, _drain(capsys)

    pins = _pins_of(consumer)
    assert pins["extract"] == producer.head_sha
    assert pins["final"] == final_pin_before, (
        "bumping the extract row moved the primary's pin — the bump touched "
        "a sibling row"
    )
    assert _imported_bytes(consumer, "data/extract") == b"e2\n"
    assert set(pins) == {"final", "extract"}


def test_import_bump_at_head_is_a_no_op(payload_journey, capsys) -> None:
    """The negative arm. Without it, a bump that always rewrites the pin would
    pass the test above perfectly."""
    producer, consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()

    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    pin_before = _pins_of(consumer)["final"]

    assert cli.main(["data", "import", PRODUCT, "--bump"]) == 0, _drain(capsys)

    assert _pins_of(consumer)["final"] == pin_before
    assert "up to date" in _drain(capsys)


def test_clone_puts_the_product_on_disk(payload_journey, tmp_path, capsys) -> None:
    """`mintd data clone` — the consumer's first contact with a product.

    The half of the binding question that is not about drift: a researcher who
    has never seen this product runs one command and ends up with the bytes.
    """
    producer, _consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()

    dest = tmp_path / "clonedest"
    assert cli.main(["data", "clone", PRODUCT, "--dest", str(dest)]) == 0, _drain(capsys)

    landed = dest / PRIMARY_OUT / "part.csv"
    assert landed.read_bytes() == b"v1\n", f"clone left nothing at {landed}"


def test_enclave_pull_lands_approved_bytes(payload_journey, capsys) -> None:
    """`mintd enclave pull` — the restricted lane, with real bytes.

    The enclave arm has had a consumer *fixture* since 1a but no journey: no
    test had ever driven `enclave pull` to the point where a file appears. It
    matters more here than on the ordinary lane because the enclave's whole
    premise is that only approved products cross the boundary, and "it was
    approved" and "it arrived" are different claims.

    `_FakeRegistryGitOps` stands in for `gh` and nothing else; the manifest,
    the catalog entry, the pin and the dvc import are all real.
    """
    producer, consumer, _register = payload_journey
    producer.publish({"outputs/cms_based": {"part.csv": b"enclave\n"}})

    # The catalog entry the manifest approves, pointed at the local producer.
    data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = producer.url
    data["data_products"]["primary"] = "outputs/cms_based/"
    data["data_products"]["outputs"] = [
        {"path": "outputs/cms_based/", "description": "approved", "primary": True,
         "last_published": ""}
    ]
    GitCatalogClient(
        registry_repo_url=_registry_url_from_config(),
        work_dir=consumer.parent / "enclave-seed",
        git_ops=_FakeRegistryGitOps(),
    ).register(Metadata.model_validate(data))

    # The manifest, pinned at the producer's REAL head — the fixture's
    # hardcoded pin is a dead sha and would fail at fetch, not at approval.
    (consumer / "enclave_manifest.yaml").write_text(
        "schema_version: '2.0'\n"
        "enclave_name: my_workspace\n"
        "approved_products:\n"
        "  - repo: provider-xw\n"
        "    registry_entry: catalog/data/provider-xw.yaml\n"
        f"    pin: {producer.head_sha}\n"
        "    source_path: outputs/cms_based/\n"
        "downloaded: []\n"
        "transferred: []\n",
        encoding="utf-8",
    )

    assert cli.main(["enclave", "pull"]) == 0, _drain(capsys)

    landed = sorted((consumer / "downloads").rglob("part.csv"))
    assert landed, f"nothing under downloads/: {sorted((consumer / 'downloads').rglob('*'))}"
    assert landed[0].read_bytes() == b"enclave\n"


def test_bare_enclave_bump_moves_an_inherited_pin(payload_journey, capsys) -> None:
    """R1, asserted where the harm lands: the manifest on disk.

    The P5 shape — a second subscription inherits the repo's recorded pin,
    and its path only exists at a LATER commit. The old drift walk
    short-circuited that row to "up to date" (path absent from the pinned
    outputs), so bare `enclave bump` returned "up to date" with the pin
    unmoved and the advisory sent users through `--force`, which skips every
    guard. Under the md5 rule the row is drift, and the guarded path moves
    the pin.
    """
    import yaml as _yaml

    producer, consumer, _register = payload_journey
    producer.publish({"outputs/cms_based": {"part.csv": b"v1\n"}})
    pin = producer.head_sha

    data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = "provider-xw"
    data["project"]["full_name"] = "data_provider-xw"
    data["repository"]["github_url"] = producer.url
    data["data_products"]["primary"] = "outputs/cms_based/"
    data["data_products"]["outputs"] = [
        {"path": "outputs/cms_based/", "description": "approved", "primary": True,
         "last_published": ""}
    ]
    GitCatalogClient(
        registry_repo_url=_registry_url_from_config(),
        work_dir=consumer.parent / "enclave-seed",
        git_ops=_FakeRegistryGitOps(),
    ).register(Metadata.model_validate(data))

    # The producer registers AND publishes a second output — after the pin.
    producer.add_output("outputs/extra/")
    producer.publish({"outputs/extra": {"part.csv": b"x1\n"}}, message="extra")
    assert producer.head_sha != pin

    (consumer / "enclave_manifest.yaml").write_text(
        "schema_version: '2.0'\n"
        "enclave_name: my_workspace\n"
        "approved_products:\n"
        "  - repo: provider-xw\n"
        "    registry_entry: catalog/data/provider-xw.yaml\n"
        f"    pin: {pin}\n"
        "    source_path: outputs/cms_based/\n"
        "  - repo: provider-xw\n"
        "    registry_entry: catalog/data/provider-xw.yaml\n"
        f"    pin: {pin}\n"  # inherited (D1) — predates outputs/extra/
        "    source_path: outputs/extra/\n"
        "downloaded: []\n"
        "transferred: []\n",
        encoding="utf-8",
    )

    assert cli.main(["enclave", "bump", "provider-xw"]) == 0, _drain(capsys)

    manifest = _yaml.safe_load(
        (consumer / "enclave_manifest.yaml").read_text(encoding="utf-8")
    )
    pins = {ap["pin"] for ap in manifest["approved_products"]}
    assert pins == {producer.head_sha}, (
        f"bare bump left the manifest at {pins}; expected every row moved "
        f"to {producer.head_sha}"
    )
