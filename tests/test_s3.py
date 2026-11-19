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

    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
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
    (tmp_path / "x.csv.dvc").write_text("outs:\n- md5: " + "a" * 32 + "\n  size: 1\n  path: x.csv\n")
    SubprocessFastSyncOps().try_fast_pull(project_path=tmp_path, targets=["x.csv"], remote_name="origin", jobs=jobs)
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


# --- one constructor: a named profile that is missing is an error --------------


@pytest.fixture
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared credentials file holding `[present]` and `[other]`, and no config file."""
    creds = tmp_path / "credentials"
    creds.write_text(
        "[present]\naws_access_key_id = AKIAPRESENT\naws_secret_access_key = s\n"
        "[other]\naws_access_key_id = AKIAOTHER\naws_secret_access_key = s\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    return creds


def _key(client) -> str:
    return client._request_signer._credentials.access_key


@pytest.mark.parametrize("source", ["dvc-config", "AWS_PROFILE"])
def test_a_named_profile_that_is_missing_fails_and_says_where_the_name_came_from(
    credentials: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """Mutation that must redden this: fall back to the default chain on ProfileNotFound."""
    from mintd import _s3
    from mintd.errors import MintdError

    remote = {"profile": "nope"} if source == "dvc-config" else {}
    if source == "AWS_PROFILE":
        monkeypatch.setenv("AWS_PROFILE", "nope")
    with pytest.raises(MintdError) as raised:
        _s3.create_client(remote, "mintd")
    where = "named in .dvc/config" if source == "dvc-config" else "named by AWS_PROFILE"
    assert str(raised.value) == f"AWS profile 'nope' ({where}) isn't in {credentials}"
    assert raised.value.hint.startswith("add a [nope] section there, or ")


def test_a_named_profile_that_exists_is_used(credentials: Path) -> None:
    from mintd import _s3

    assert _key(_s3.create_client({"profile": "present"}, None)) == "AKIAPRESENT"


@pytest.mark.parametrize(("remote", "env", "mintd", "want"), [
    ({"profile": "present"}, "other", "mintd", ("present", "named in .dvc/config")),
    ({}, "other", "mintd", ("other", "named by AWS_PROFILE")),
    ({}, "default-wins", "mintd", ("default-wins", "named by AWS_DEFAULT_PROFILE")),
    ({}, None, "mintd", ("mintd", "mintd's [mintd] profile")),
    ({}, None, None, (None, "")),
])
def test_the_profile_order_is_the_remote_then_aws_profile_then_mintd(
    monkeypatch: pytest.MonkeyPatch, remote: dict, env: str | None, mintd: str | None, want: tuple,
) -> None:
    """The order dvc sees too. Mutation that must redden this: swap any two sources."""
    from mintd import _s3

    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    if env == "default-wins":  # both set: AWS_DEFAULT_PROFILE comes first, as in botocore
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", env)
        monkeypatch.setenv("AWS_PROFILE", "other")
    elif env:
        monkeypatch.setenv("AWS_PROFILE", env)
    assert _s3.resolve_profile(remote, mintd) == want


def test_nothing_named_uses_the_default_chain(credentials: Path) -> None:
    from mintd import _s3

    assert _key(_s3.create_client({}, None)) == "AKIAENV"


def test_data_pull_stops_on_a_missing_named_profile_and_never_falls_back_to_dvc(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation that must redden this: drop `except MintdError: raise` in `data_pull`,
    which lets the error fall into the plain `dvc pull` fallback."""
    from mintd import cli

    project = tmp_path / "project"
    (project / ".dvc").mkdir(parents=True)
    (project / ".dvc" / "config").write_text(
        "[core]\n    remote = origin\n['remote \"origin\"']\n    url = s3://bucket/p\n    profile = nope\n"
    )
    (project / "data.csv.dvc").write_text("outs:\n- md5: " + "a" * 32 + "\n  size: 1\n  path: data.csv\n")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    rc = cli.main(["data", "pull", "--path", str(project)])
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 1, err
    assert "error: AWS profile 'nope' (named in .dvc/config)" in err
    assert "hint: add a [nope] section there" in err
    assert "dvc pull" not in err


def test_config_validate_reports_the_profile_it_checks_and_keeps_the_hint(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mintd.config_ops import validate_config

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    monkeypatch.setenv("AWS_PROFILE", "other")
    profile_step = next(s for s in validate_config(cfg) if s.name == "aws_profile")
    assert profile_step.message == "'other' profile, named by AWS_PROFILE"
    monkeypatch.setenv("AWS_PROFILE", "nope")
    s3_step = next(s for s in validate_config(cfg, bucket="b") if s.name == "s3")
    assert s3_step.status == "fail"
    assert "AWS profile 'nope' (named by AWS_PROFILE)" in s3_step.message
    assert "add a [nope] section there, or unset AWS_PROFILE" in s3_step.message


@pytest.mark.parametrize("source", ["dvc-config", "AWS_PROFILE", "empty-in-config.local"])
def test_dvc_also_fails_on_a_missing_named_profile(s3_server, real_dvc, tmp_path: Path, source: str) -> None:
    """mintd now refuses exactly where dvc does: plain `dvc push` against an emulated S3
    fails on the same missing named profile."""
    from tests._harness.git import _git

    endpoint = s3_server.endpoint
    project = tmp_path / "dvcproj"
    project.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=project)
    real_dvc(["init", "-q"], cwd=project, check=True)
    real_dvc(["remote", "add", "-d", "storage", "s3://journey-bucket/p"], cwd=project, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", endpoint], cwd=project, check=True)
    if source == "dvc-config":
        real_dvc(["remote", "modify", "storage", "profile", "nope"], cwd=project, check=True)
    elif source == "AWS_PROFILE":
        real_dvc.env["AWS_PROFILE"] = "nope"
    else:  # dvc writes `profile = ""` and reads it as profile ''
        real_dvc(["remote", "modify", "storage", "profile", "nope"], cwd=project, check=True)
        real_dvc(["remote", "modify", "--local", "storage", "profile", ""], cwd=project, check=True)
    (project / "x.csv").write_text("x\n")
    real_dvc(["add", "-q", "x.csv"], cwd=project, check=True)
    r = real_dvc(["push"], cwd=project)
    assert r.returncode != 0
    assert ("profile ()" if source.startswith("empty") else "nope") in r.stdout + r.stderr


def test_a_profile_only_aws_default_profile_names_is_named_in_the_error(credentials: Path, monkeypatch) -> None:
    """botocore also reads AWS_DEFAULT_PROFILE; the error names it, never 'None'."""
    from mintd import _s3
    from mintd.errors import MintdError

    monkeypatch.setenv("AWS_DEFAULT_PROFILE", "nope")
    with pytest.raises(MintdError) as raised:
        _s3.create_client({}, None)
    assert "AWS profile 'nope' (named by AWS_DEFAULT_PROFILE)" in str(raised.value)
    assert raised.value.hint.endswith("unset AWS_DEFAULT_PROFILE")


def test_a_config_local_profile_overrides_the_committed_one(credentials: Path, tmp_path: Path) -> None:
    """dvc lays .dvc/config.local over .dvc/config, so mintd does too: a committed
    profile this machine lacks, overridden locally by one it has, is no error.
    Mutation that must redden this: read .dvc/config alone in get_remote_config."""
    from mintd import _s3
    from mintd._fast_sync_ops import get_remote_config

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text("['remote \"origin\"']\n    url = s3://b/p\n    profile = nope\n")
    (tmp_path / ".dvc" / "config.local").write_text(
        "['remote \"origin\"']\n    profile = present\n    url = s3://scratch/x\n    endpointurl = http://localhost:9\n"
    )
    cfg = get_remote_config(tmp_path, "origin")
    # Only the profile: the committed config stays the remote's location of record.
    assert cfg == {"url": "s3://b/p", "profile": "present"}
    assert _key(_s3.create_client(cfg, None)) == "AKIAPRESENT"


@pytest.mark.parametrize("level", ["config", "config.local"])
@pytest.mark.parametrize("value", ['"mintd"', "'mintd'", "mintd # mine", '"mintd" # mine'])
def test_a_quoted_or_commented_profile_reads_as_dvc_reads_it(real_dvc, tmp_path: Path, value: str, level: str) -> None:
    """A hand-edited `profile = "mintd"` or `profile = mintd # mine` names `mintd` for dvc
    (configobj strips one pair of quotes and an unquoted inline comment), so it does for
    fast sync, the cache lane and the push check too.
    Mutation that must redden this: drop the stripping in get_remote_config or merged_remote."""
    from mintd._fast_sync_ops import get_remote_config
    from mintd._push_verify import merged_remote

    real_dvc(["init", "-q", "--no-scm"], cwd=tmp_path, check=True)
    remote = "['remote \"storage\"']\n"
    (tmp_path / ".dvc" / "config").write_text(f"[core]\n    remote = storage\n{remote}    url = s3://b/p\n")
    with (tmp_path / ".dvc" / level).open("a") as f:
        f.write(f"{remote if level == 'config.local' else ''}    profile = {value}\n")

    dvc_reads = real_dvc(["config", "remote.storage.profile"], cwd=tmp_path, check=True).stdout.strip()
    assert dvc_reads == "mintd"
    assert get_remote_config(tmp_path, "storage")["profile"] == dvc_reads
    assert merged_remote(tmp_path, "storage")["profile"] == dvc_reads


@pytest.mark.parametrize("value", ['""', "''", ""])
def test_an_empty_remote_profile_is_named_and_missing_as_dvc_reads_it(credentials: Path, tmp_path: Path, value: str) -> None:
    """`dvc remote modify --local <remote> profile ""` writes `profile = ""`, which dvc reads
    as profile '' and fails on (test_dvc_also_fails_on_a_missing_named_profile). mintd stops
    too, never falling back to other credentials.
    Mutation that must redden this: test the remote's profile by truthiness in resolve_profile."""
    from mintd import _s3
    from mintd._fast_sync_ops import get_remote_config
    from mintd._push_verify import merged_remote
    from mintd.errors import MintdError

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text("['remote \"storage\"']\n    url = s3://b/p\n    profile = present\n")
    (tmp_path / ".dvc" / "config.local").write_text(f"['remote \"storage\"']\n    profile = {value}\n")
    for cfg in (get_remote_config(tmp_path, "storage"), merged_remote(tmp_path, "storage")):
        with pytest.raises(MintdError) as raised:
            _s3.create_client(cfg, "other")
        assert "AWS profile '' (named in .dvc/config)" in str(raised.value)


def test_a_pull_of_imports_only_never_checks_the_projects_own_profile(credentials: Path, tmp_path: Path) -> None:
    """Imports come from their producers' buckets, so a missing profile on this
    project's own remote can't stop them: they go to the dvc fallback.
    Mutation that must redden this: build the client before classifying targets."""
    from mintd._fast_sync_ops import SubprocessFastSyncOps

    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text(
        "[core]\n    remote = origin\n['remote \"origin\"']\n    url = s3://b/p\n    profile = nope\n"
    )
    (tmp_path / "imp.dvc").write_text(
        "frozen: true\ndeps:\n- path: imp\n  repo:\n    url: https://example.com/x.git\n    rev_lock: abc\n"
        "outs:\n- path: imp\n  md5: " + "b" * 32 + ".dir\n"
    )
    result = SubprocessFastSyncOps().try_fast_pull(project_path=tmp_path, targets=["imp"], remote_name="origin")
    assert result.fallback_targets == ["imp"]


def test_config_validate_fails_a_missing_named_profile_without_a_bucket(credentials: Path, tmp_path, monkeypatch) -> None:
    """Every S3 command fails on it, so validate can't call it ok.
    Mutation that must redden this: drop the existence check in the profile step."""
    from mintd.config_ops import validate_config

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    monkeypatch.setenv("AWS_PROFILE", "nope")
    step = next(s for s in validate_config(cfg) if s.name == "aws_profile")
    assert step.status == "fail"
    assert "AWS profile 'nope' (named by AWS_PROFILE)" in step.message
    assert "add a [nope] section there, or unset AWS_PROFILE" in step.message


def test_the_config_validate_cli_prints_the_hint_and_names_the_profile_it_checks(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from moto import mock_aws

    from mintd import cli

    credentials.write_text(credentials.read_text() + "[mintd]\naws_access_key_id = AKIAMINTD\naws_secret_access_key = s\n")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    monkeypatch.setenv("AWS_PROFILE", "nope")
    assert cli.main(["config", "validate", "--path", str(cfg), "--bucket", "mintd-test-bucket"]) != 0
    out = " ".join((lambda c: c.out + c.err)(capsys.readouterr()).split())
    assert "AWS profile 'nope' (named by AWS_PROFILE)" in out
    assert "add a [nope] section there, or unset AWS_PROFILE" in out

    monkeypatch.setenv("AWS_PROFILE", "other")
    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="mintd-test-bucket")
        assert cli.main(["config", "validate", "--path", str(cfg), "--bucket", "mintd-test-bucket"]) == 0
    out = " ".join((lambda c: c.out + c.err)(capsys.readouterr()).split())
    assert "'other' profile, named by AWS_PROFILE" in out  # AWS_PROFILE beats [mintd]
    assert "(profile: other)" in out


def test_an_empty_aws_profile_names_the_empty_profile_as_botocore_does(credentials: Path, monkeypatch) -> None:
    """botocore reads a set-but-empty AWS_PROFILE as profile '' and fails; so do we,
    with a message that names it, never 'None'."""
    from mintd import _s3
    from mintd.errors import MintdError

    monkeypatch.setenv("AWS_PROFILE", "")
    with pytest.raises(MintdError) as raised:
        _s3.create_client({}, None)
    assert "AWS profile '' (named by AWS_PROFILE)" in str(raised.value)
    assert raised.value.hint == "unset AWS_PROFILE, or set it to a profile that exists"


def test_config_validate_reports_an_unreadable_aws_config_instead_of_crashing(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic command reports a malformed AWS config file as a failed step.
    Mutation that must redden this: catch only MintdError in the profile step."""
    from mintd.config_ops import validate_config

    bad = tmp_path / "bad-aws-config"
    bad.write_text("this is not ini\n[profile x\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(bad))
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    # Named or not: boto3 parses the files either way, so every S3 command would fail.
    # Mutation that must redden the second case: check only when a profile is named.
    for named in ("present", None):
        if named:
            monkeypatch.setenv("AWS_PROFILE", named)
        else:
            monkeypatch.delenv("AWS_PROFILE", raising=False)
        step = next(s for s in validate_config(cfg) if s.name == "aws_profile")
        assert step.status == "fail", named
        assert step.message.startswith("could not read the AWS configuration: "), named


def _committed_and_local(tmp_path: Path, local: str) -> Path:
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text("['remote \"origin\"']\n    url = s3://b/p\n    profile = committed\n")
    (tmp_path / ".dvc" / "config.local").write_text(local)
    return tmp_path


def test_a_config_local_without_this_remotes_section_leaves_the_committed_profile(tmp_path: Path) -> None:
    """Mutation that must redden this: stop catching the missing section (KeyError)."""
    from mintd._fast_sync_ops import get_remote_config

    project = _committed_and_local(tmp_path, "['remote \"other\"']\n    profile = present\n")
    assert get_remote_config(project, "origin") == {"url": "s3://b/p", "profile": "committed"}


def test_a_percent_sign_in_config_local_keeps_the_local_profile(tmp_path: Path) -> None:
    """dvc's config has no %-interpolation; a `%` in another key must not drop the
    local profile. Mutation that must redden this: parse with configparser's default interpolation."""
    from mintd._fast_sync_ops import get_remote_config

    project = _committed_and_local(
        tmp_path, "['remote \"origin\"']\n    profile = present\n    sse_kms_key_id = alias/x%y\n"
    )
    assert get_remote_config(project, "origin") == {"url": "s3://b/p", "profile": "present"}


def test_an_unreadable_config_local_is_an_error_not_a_silent_fallback(tmp_path: Path) -> None:
    """dvc can't read it either; falling back to the committed profile would use other
    credentials. Mutation that must redden this: swallow the parse error again."""
    from mintd._fast_sync_ops import get_remote_config
    from mintd.errors import MintdError

    project = _committed_and_local(tmp_path, "not ini at all\n[unclosed\n")
    with pytest.raises(MintdError) as raised:
        get_remote_config(project, "origin")
    assert str(raised.value).startswith(f"can't read {project / '.dvc' / 'config.local'}")
    assert raised.value.hint == "fix or remove it; dvc can't read it either"
