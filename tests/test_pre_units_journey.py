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
import shlex
import subprocess
from pathlib import Path

import pytest
from moto import mock_aws

from mintd import cli
from mintd._config import ConfigError
from mintd.catalog import CatalogAlreadyExists, GitCatalogClient
from mintd.model import Metadata
from tests._dvc_fixtures import write_lock
from tests._enclave_fixtures import stage_enclave_manifest
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.fast_sync_ops import _FakeFastSyncOps
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
    # A real lab repo has somewhere to push. `publish` sends the bump commit
    # and the release tag to origin before it writes the catalog entry, so a
    # project without one cannot publish a product anybody else can import.
    origin = tmp_path / "project-origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], cwd=tmp_path)
    _git(["remote", "add", "origin", str(origin)], cwd=proj)
    _git(["push", "-u", "origin", "main"], cwd=proj)
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
    monkeypatch.setattr(
        "mintd.cli._resolve_fast_sync_ops",
        lambda cfg, **_: _FakeFastSyncOps(),
    )

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
    # ...and produced + pushed: the dvc.lock entry a researcher who ran
    # `dvc repro` and `mintd data push` would have committed, standing in for
    # the run and the push this test cannot make. The scaffold's own `validate`
    # stage declares `../data/final/` under its wdir, so `dvc add` would refuse
    # the path ("overlaps with an output of stage") -- producing it is the only
    # way to track it. Files-format with a per-entry version ID under the
    # scaffold's OWN remote name is what dvc writes for a directory on a
    # version-aware remote (which `mintd init` always configures). Without it
    # `check` reports `data/final/` as undeliverable -- the delivery gate
    # working, not a scaffold defect.
    write_lock(
        proj,
        "validate",
        "../data/final/",
        files={"survey.parquet": "v-1"},
        remote=meta["storage"]["dvc"]["remote_name"],
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

#: The field-report shape: the primary `data/final/` is declared as a
#: directory, but DVC tracks only per-file foreach outs beneath it, written
#: from `wdir: code`, so `dvc.lock` is dvc's own and nothing tracks the
#: directory itself (`dvc pull data/final` is rc 1 on this repo).
#: `data/other.csv` is the out OUTSIDE the selector (negative control).
FOREACH_FINAL_YAML = (
    "stages:\n"
    "  build:\n"
    "    foreach:\n"
    "      - a\n"
    "      - b\n"
    "    do:\n"
    "      wdir: code\n"
    "      cmd: python -c \"import pathlib; d=pathlib.Path('../data/final'); "
    "d.mkdir(parents=True, exist_ok=True); "
    "(d/'${item}.csv').write_bytes(b'${item}\\n')\"\n"
    "      outs:\n"
    "        - ../data/final/${item}.csv\n"
    "  other:\n"
    "    cmd: python -c \"import pathlib; "
    "pathlib.Path('data/other.csv').write_bytes(b'other\\n')\"\n"
    "    outs:\n"
    "      - data/other.csv\n"
)
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

    def register(mutate=None) -> None:
        """(Re)publish the producer's entry. Called again after the producer
        moves, because the catalog is how a consumer discovers HEAD.
        `mutate(data)` edits the entry before it is registered — for a
        catalog that declares something the producer does not track."""
        data = json.loads(V2_MINIMAL.read_text(encoding="utf-8"))
        data["repository"]["github_url"] = producer.url
        if mutate is not None:
            mutate(data)
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


def test_import_of_a_path_only_on_a_branch_points_at_rev(
    payload_journey, capsys
) -> None:
    """The field report: `--path` names an out that exists only on a producer
    branch. Real dvc words that as a bare `unexpected error - ['<path>']`,
    which must surface as "not found at rev" with a hint naming `--rev` —
    and `--rev <branch>` must then actually land the bytes."""
    from tests._harness.payload import publish_payload

    producer, consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()
    _git(["checkout", "-b", "wip"], cwd=producer.work)
    publish_payload(producer.work, producer.remote, {"data/wip_only": {"part.csv": b"w1\n"}})
    _git(["add", "-A"], cwd=producer.work)
    _git(["commit", "-m", "wip payload"], cwd=producer.work)
    _git(["push", "origin", "wip"], cwd=producer.work)

    rc = cli.main(["data", "import", PRODUCT, "--path", "data/wip_only"])
    err = " ".join(_drain(capsys).split())
    assert rc == 1, err
    assert "not found at rev 'HEAD'" in err
    assert "--rev" in err
    assert "--dvc-arg=-v" not in err

    assert cli.main(
        ["data", "import", PRODUCT, "--path", "data/wip_only", "--rev", "wip"]
    ) == 0, _drain(capsys)
    assert _imported_bytes(consumer, "data/wip_only") == b"w1\n"


#: One stage that writes the primary as a directory, so `dvc.lock` is dvc's own
#: `.dir` out with no `files:` list: what a lab pipeline commits.
FINAL_DIR_YAML = (
    "stages:\n"
    "  build:\n"
    "    cmd: python -c \"import pathlib; d=pathlib.Path('data/final'); "
    "d.mkdir(parents=True, exist_ok=True); (d/'part.csv').write_bytes(b'v1\\n')\"\n"
    "    outs:\n"
    "      - data/final\n"
)


def _set_version_aware(producer, on: bool) -> str:
    """Flip the producer's remote to (or back from) `version_aware` and push
    the commit; returns it.

    On a version-aware remote a pointer with no S3 version ID pulls nothing
    at exit 0, which is the fleet's broken state; this reproduces it with a
    local remote. S3 lists an absent prefix as empty, while a local remote
    without the directory makes dvc exit 255 "failed to load directory"
    (measured), hence the mkdir. `_configure_remote` rewrites the remote on
    every `publish`, so call this AFTER the last publish. A warm consumer
    cache still serves an already-imported `.dir` hash from a version-aware
    remote, so a test that imported first must publish NEW bytes before
    calling this.
    """
    flag = ["version_aware", "true"] if on else ["--unset", "version_aware"]
    subprocess.run(
        [*_dvc_cmd(), "remote", "modify", "storage", *flag],
        cwd=str(producer.work), capture_output=True, check=True,
    )
    (producer.remote / PRIMARY_OUT).mkdir(parents=True, exist_ok=True)
    return producer._commit_and_push("remote is version-aware" if on else "remote is md5 again")


def _import_dir(consumer: Path) -> Path:
    return consumer / "data" / "imports" / FULL_NAME / "data"


def _autostage(consumer: Path) -> None:
    """dvc `git add`s every pointer and `.gitignore` it writes, as on a
    machine whose global dvc config sets `core.autostage`. Local config, so
    the consumer's tracked tree stays clean."""
    subprocess.run(
        [*_dvc_cmd(), "config", "--local", "core.autostage", "true"],
        cwd=str(consumer), capture_output=True, check=True,
    )


def test_import_of_an_unpinned_directory_out_exits_nonzero_and_leaves_nothing(
    payload_journey, capsys
) -> None:
    """The field report: the committed `dvc.lock` pins `data/final` as a
    `.dir` out with no S3 version ID on a version-aware remote. Before, the
    import exited 0 with an empty directory. Now it exits 1, says the
    producer must fix it, and leaves the consumer's tree as it was, git's
    index included although dvc's autostage staged the pointer and
    `.gitignore`.

    `check` errors on the same producer first: the two verbs judge with one
    predicate. Mutations: delete the `_require_deliverable(...)` call -> rc 0;
    drop the index restore -> `git status` shows the pointer still staged.
    """
    from mintd.check import check_project

    producer, consumer, register = payload_journey
    _autostage(consumer)
    producer.publish_pipeline(FINAL_DIR_YAML)
    _set_version_aware(producer, True)
    register()
    assert "data_products_output_unpinned" in {f.kind for f in check_project(producer.work)}

    rc = cli.main(["data", "import", PRODUCT])
    out = " ".join(_drain(capsys).split())

    assert rc == 1, out
    assert "cannot be delivered by 'mintd data import'" in out
    assert "S3 version ID" in out
    assert "ask them to run 'mintd check'" in out
    assert "✓" not in out
    assert not (_import_dir(consumer) / "final.dvc").exists()
    assert not (_import_dir(consumer) / "final").exists()
    assert _git(["status", "--porcelain"], cwd=consumer).strip() == ""


def test_import_succeeds_on_a_plain_rerun_once_the_producer_fixes_it(
    payload_journey, capsys
) -> None:
    """What the hint promises. The import fails and leaves nothing, so after
    the producer makes the output deliverable a plain re-run (no `--force`)
    lands the bytes. Before that, `--rev` at the broken commit, which is no
    longer the producer's tip, still exits 1: the diagnosis fetches the pin
    dvc recorded, not HEAD.

    Mutation: drop the fresh-import cleanup -> the re-run exits 1 on
    "already exists".
    """
    producer, consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    broken = _set_version_aware(producer, True)
    register()
    # `--force` with nothing to replace: the hint must not ask for it again.
    assert cli.main(["data", "import", PRODUCT, "--force"]) == 1
    out = " ".join(_drain(capsys).split())
    assert "then re-run this import" in out and "with --force" not in out

    _set_version_aware(producer, False)  # the producer's fix
    register()
    rc = cli.main(["data", "import", PRODUCT, "--rev", broken])
    out = " ".join(_drain(capsys).split())
    assert rc == 1, out
    assert f"@ {broken[:7]} cannot be delivered" in out
    assert "import a later --rev" in out  # the producer's own check is clean now

    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    assert _imported_bytes(consumer, PRIMARY_OUT) == b"v1\n"


def test_forced_reimport_that_lands_nothing_keeps_the_previous_import(
    payload_journey, capsys
) -> None:
    """`--force` onto a broken version: the import exits 1 and the previous
    pointer and bytes are both back where they were, in git's index as well
    as on disk, though dvc's autostage staged the broken pointer.

    Mutations: drop the pointer restore -> the pin moves; run the
    postcondition after `_payload_backup` -> the v1 bytes are gone; forget
    what the index held -> the committed pointer is staged for deletion.
    """
    producer, consumer, register = payload_journey
    _autostage(consumer)
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()
    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    _git(["commit", "-q", "-m", "import v1"], cwd=consumer)  # autostage staged it
    pin_before = _pins_of(consumer)["final"]

    producer.publish({PRIMARY_OUT: {"part.csv": b"v2\n"}}, message="v2 payload")
    _set_version_aware(producer, True)
    register()
    rc = cli.main(["data", "import", PRODUCT, "--force"])
    out = " ".join(_drain(capsys).split())

    assert rc == 1, out
    assert "re-run this import with --force" in out
    assert _imported_bytes(consumer, PRIMARY_OUT) == b"v1\n"
    assert _pins_of(consumer) == {"final": pin_before}
    assert not list(_import_dir(consumer).glob("*.mintd-bump-backup"))
    assert _git(["status", "--porcelain"], cwd=consumer).strip() == ""


def test_failed_import_keeps_a_gitignore_an_earlier_import_wrote(
    payload_journey, capsys
) -> None:
    """The `.gitignore` beside the pointer already lists an output imported
    earlier, and is committed. A failed import of a second output puts it
    back byte for byte, on disk and in git's index (autostage staged dvc's
    version). Mutation: unlink the `.gitignore` instead of restoring it ->
    `/extract` is gone."""
    producer, consumer, register = payload_journey
    _autostage(consumer)
    producer.publish({
        PRIMARY_OUT: {"part.csv": b"v1\n"},
        "data/extract": {"part.csv": b"e1\n"},
    })
    register()
    assert cli.main(["data", "import", PRODUCT, "--path", "data/extract"]) == 0, _drain(capsys)
    _git(["commit", "-q", "-m", "import extract"], cwd=consumer)
    gitignore = _import_dir(consumer) / ".gitignore"
    before = gitignore.read_bytes()
    assert b"/extract" in before

    _set_version_aware(producer, True)  # data/final was never imported: no warm cache
    register()
    assert cli.main(["data", "import", PRODUCT]) == 1, _drain(capsys)

    assert gitignore.read_bytes() == before
    assert _git(["status", "--porcelain"], cwd=consumer).strip() == ""


def test_no_exec_import_at_a_good_rev_keeps_its_pointer(payload_journey, capsys) -> None:
    """`--dvc-arg=--no-exec` lands nothing by design and records no
    `rev_lock`. The import at a deliverable `--rev` keeps its pointer and
    exits 0, though the producer's HEAD can no longer deliver. Mutation:
    drop the empty-pin return -> HEAD is judged and the pointer deleted."""
    producer, consumer, register = payload_journey
    good = producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    _set_version_aware(producer, True)
    register()

    rc = cli.main(["data", "import", PRODUCT, "--rev", good, "--dvc-arg=--no-exec"])
    assert rc == 0, _drain(capsys)
    assert (_import_dir(consumer) / "final.dvc").is_file()


def test_import_of_a_genuinely_empty_directory_out_still_succeeds(
    payload_journey, capsys
) -> None:
    """The deliverable control: an empty directory out lands no file on a
    version-aware remote too, and that is a correct import. Drives the real
    producer checkout. Mutation: delete `if verdict.deliverable: return` ->
    rc 1."""
    producer, consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {}})
    _set_version_aware(producer, True)
    register()

    assert cli.main(["data", "import", PRODUCT]) == 0, _drain(capsys)
    assert (_import_dir(consumer) / "final").is_dir()


def test_readme_produce_flow_names_pointers_until_committed_and_pushed(
    payload_journey, monkeypatch, capsys
) -> None:
    """The README's produce steps with real dvc and real git. `dvc repro`
    writes `dvc.lock`, so `mintd data push` names it rather than printing a
    check mark; `registry register` refuses it uncommitted and goes through
    once it is committed and pushed. Only `gh` is faked, at the catalog
    client's constructor, as `journey` does.

    `check` reads the lab's S3 remote (md5 layout, so the lock needs no
    version IDs) and a matching `metadata.storage`; the pushes go to the
    harness's local directory through `--remote local`.
    """
    from tests._dvc_fixtures import storage_block, write_dvc_config
    from tests._harness.payload import publish_pipeline_payload

    producer, _consumer, _register = payload_journey
    git_ops = _FakeRegistryGitOps()
    monkeypatch.setattr(
        "mintd.cli.GitCatalogClient",
        lambda **kw: GitCatalogClient(**kw, git_ops=git_ops),
    )
    publish_pipeline_payload(  # `dvc repro` + push; commits nothing
        producer.work, producer.remote, FOREACH_FINAL_YAML, seed={"code/.keep": b""}
    )
    write_dvc_config(producer.work, version_aware=False)
    subprocess.run(
        [*_dvc_cmd(), "remote", "add", "local", str(producer.remote)],
        cwd=str(producer.work), capture_output=True, check=True,
    )
    meta = producer.metadata()
    meta["storage"] = {**storage_block(), "versioning": False}
    producer._write_metadata(meta)
    monkeypatch.chdir(producer.work)

    assert cli.main(["data", "push", "--remote", "local"]) == 0, _drain(capsys)
    out = " ".join(_drain(capsys).split())
    assert "✓" not in out
    assert "dvc.lock" in out and "commit and push" in out

    def register() -> tuple[int, str]:
        rc = cli.main(["registry", "register", str(producer.work)])
        return rc, " ".join(_drain(capsys).split())

    rc, out = register()
    assert rc == 1, out
    assert "the project has uncommitted changes:" in out
    assert all(f in out for f in ("dvc.lock", "dvc.yaml", "metadata.json")), out

    # The README's commit step, run exactly as written there.
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    for line in ("git status", "git add -A", 'git commit -m "Record pushed data versions"', "git push"):
        assert f"\n{line}\n" in readme, f"README no longer says {line!r}"
        _git(shlex.split(line)[1:], cwd=producer.work)
    # ...and between the push and the register, where a reader meets it.
    assert (
        readme.index("\nmintd data push\n")
        < readme.index('\ngit commit -m "Record pushed data versions"\n')
        < readme.index("\nmintd registry register\n")
    ), "README's commit step is no longer between data push and registry register"
    assert cli.main(["data", "push", "--remote", "local"]) == 0
    assert "✓ already up to date" in _drain(capsys)
    rc, out = register()
    assert rc == 0, out


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


def test_cli_data_clone_directory_primary_exits_zero_with_bytes_on_disk(
    payload_journey, tmp_path, capsys
) -> None:
    """The clone half of the clone-resolver plan's binding question:
    `--primary` on a directory primary tracked as per-file outs exits 0 with
    the files on disk. Before S5: rc 1, 'data/final' does not exist as an
    output or a stage name, and the data-less clone was left behind."""
    producer, _consumer, register = payload_journey
    producer.publish_pipeline(FOREACH_FINAL_YAML, seed={"code/.keep": b""})
    register()

    dest = tmp_path / "clonedest"
    assert cli.main(["data", "clone", PRODUCT, "--primary", "--dest", str(dest)]) == 0, _drain(capsys)

    assert (dest / PRIMARY_OUT / "a.csv").read_bytes() == b"a\n"
    assert (dest / PRIMARY_OUT / "b.csv").read_bytes() == b"b\n"


def test_clone_path_directory_selector_expands_to_the_tracked_files(
    payload_journey, tmp_path, capsys
) -> None:
    """Same via `--path data/final/`."""
    producer, _consumer, register = payload_journey
    producer.publish_pipeline(FOREACH_FINAL_YAML, seed={"code/.keep": b""})
    register()

    dest = tmp_path / "clonedest"
    assert cli.main(
        ["data", "clone", PRODUCT, "--path", f"{PRIMARY_OUT}/", "--dest", str(dest)]
    ) == 0, _drain(capsys)

    assert (dest / PRIMARY_OUT / "a.csv").read_bytes() == b"a\n"
    assert (dest / PRIMARY_OUT / "b.csv").read_bytes() == b"b\n"


def test_clone_selector_does_not_materialize_an_out_outside_it(
    payload_journey, tmp_path, capsys
) -> None:
    """Negative control: the expansion is the selector's outs, not everything
    (a no-flag clone lands all three). Mutation: expand to everything -> red."""
    producer, _consumer, register = payload_journey
    producer.publish_pipeline(FOREACH_FINAL_YAML, seed={"code/.keep": b""})
    register()

    dest = tmp_path / "clonedest"
    assert cli.main(["data", "clone", PRODUCT, "--primary", "--dest", str(dest)]) == 0, _drain(capsys)

    assert (dest / PRIMARY_OUT / "a.csv").exists()
    assert not (dest / "data" / "other.csv").exists(), "a selector pulled an out outside itself"


def test_clone_stale_subpath_of_a_directory_out_exits_nonzero_with_no_tick(
    payload_journey, tmp_path, capsys
) -> None:
    """Real dvc, the clone-resolver plan's U1: `data/final` is ONE directory
    out and the catalog declares a sub-path its manifest lacks. dvc fetches
    the manifest, lands nothing and exits 0 — mintd must not print ✓. Pins
    the two dvc facts the unit test only assumes."""
    producer, _consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register(mutate=lambda d: d["data_products"]["outputs"].append(
        {"path": f"{PRIMARY_OUT}/stale/", "description": "", "primary": False,
         "last_published": ""}
    ))

    dest = tmp_path / "clonedest"
    rc = cli.main(["data", "clone", PRODUCT, "--path", f"{PRIMARY_OUT}/stale/", "--dest", str(dest)])
    out = _drain(capsys)

    assert rc == 1, out
    assert "does not contain it" in out, out
    assert "✓" not in out, out
    assert not (dest / PRIMARY_OUT / "stale").exists()
    assert dest.exists(), "a pull outcome keeps the clone"


def test_clone_primary_that_is_an_exact_out_is_unchanged(
    payload_journey, tmp_path, capsys
) -> None:
    """Green twin: `data/final/` as ONE directory out (direction 1) still
    lands as a single target. Must stay green under every mutation."""
    producer, _consumer, register = payload_journey
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}})
    register()

    dest = tmp_path / "clonedest"
    assert cli.main(["data", "clone", PRODUCT, "--primary", "--dest", str(dest)]) == 0, _drain(capsys)

    assert (dest / PRIMARY_OUT / "part.csv").read_bytes() == b"v1\n"


def _publish_two_outs_and_drop_one_blob(producer, register) -> None:
    """Two declared outs, one blob deleted from the producer's remote AFTER
    the push: the failure S1's orphan guard leaves in place (real dvc:
    `Checkout failed for following targets: data/other.csv`, exit 1, sibling
    landed). Blobs live at `files/md5/XX/YYYY` under `producer.remote`."""
    producer.publish({PRIMARY_OUT: {"part.csv": b"v1\n"}, "data/other.csv": b"other\n"})
    register()
    blobs = [p for p in (producer.remote / "files" / "md5").rglob("*") if p.is_file()]
    gone = [p for p in blobs if p.read_bytes() == b"other\n"]
    assert len(gone) == 1, blobs
    gone[0].chmod(0o644)  # dvc stores blobs read-only; Windows refuses to delete those
    gone[0].unlink()


def test_cli_data_clone_partial_pull_reports_what_landed(
    payload_journey, tmp_path, capsys
) -> None:
    """S2, real dvc: one of two outs cannot be pulled (blob gone from the
    remote). Before: rc 1 with the raw dvc stderr and no word on what landed.
    After: rc 1, the clone is kept, the sibling's bytes ARE on disk, and the
    summary names the one failed target. Mutation: re-raise DvcPullError in
    `_pull_isolating_failures` -> raw `dvc pull failed (exit 1)` line, red."""
    producer, _consumer, register = payload_journey
    _publish_two_outs_and_drop_one_blob(producer, register)

    dest = tmp_path / "clonedest"
    rc = cli.main(["data", "clone", PRODUCT, "--dest", str(dest)])
    out = _drain(capsys)

    assert rc == 1, out
    assert f"cloned {PRODUCT} → {dest.name}/ but pull incomplete: 1 target(s) failed" in out, out
    assert "cannot pull data/other.csv.dvc:" in out, out
    assert "mintd data pull data/other.csv.dvc" in out, out  # the retry hint
    assert "dvc pull failed (exit 1)" not in out, out
    assert "does not exist as an output or a stage name" not in out, out
    assert (dest / PRIMARY_OUT / "part.csv").read_bytes() == b"v1\n"
    assert not (dest / "data" / "other.csv").exists()


def test_cli_data_pull_partial_failure_prints_a_summary_not_a_traceback(
    payload_journey, tmp_path, capsys, monkeypatch
) -> None:
    """S2, real dvc, `mintd data pull` inside the clone above: the same
    partial failure renders `pull incomplete: 1 target(s) failed` plus one
    `cannot pull <t>` line with its retry hint -- never the raw dvc stderr --
    and the sibling stays on disk. Mutation: re-raise -> red."""
    producer, _consumer, register = payload_journey
    _publish_two_outs_and_drop_one_blob(producer, register)
    dest = tmp_path / "clonedest"
    assert cli.main(["data", "clone", PRODUCT, "--dest", str(dest)]) == 1, _drain(capsys)
    _drain(capsys)

    monkeypatch.chdir(dest)
    rc = cli.main(["data", "pull"])
    out = _drain(capsys)

    assert rc == 1, out
    assert "pull incomplete: 1 target(s) failed" in out, out
    assert "cannot pull data/other.csv.dvc:" in out, out
    assert "mintd data pull data/other.csv.dvc" in out, out
    assert "dvc pull failed (exit 1)" not in out, out
    assert "does not exist as an output or a stage name" not in out, out
    assert (dest / PRIMARY_OUT / "part.csv").read_bytes() == b"v1\n"


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
