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
from mintd.catalog import GitCatalogClient
from mintd.model import Metadata
from tests._enclave_fixtures import stage_enclave_manifest
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.registry_git_ops import _FakeRegistryGitOps

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


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@mintd", "-c", "user.name=test", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _write_config(config_dir: Path, *, registry_url: str | None, cache_dir: Path) -> None:
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
    # `_resolve_catalog_client` and `_resolve_clients` are left alone on purpose.
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
