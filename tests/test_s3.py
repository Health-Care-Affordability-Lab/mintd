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
    """A shared credentials file holding `[present]`, `[other]` and `[committed]`, and no config file."""
    creds = tmp_path / "credentials"
    creds.write_text(
        "[present]\naws_access_key_id = AKIAPRESENT\naws_secret_access_key = s\n"
        "[other]\naws_access_key_id = AKIAOTHER\naws_secret_access_key = s\n"
        "[committed]\naws_access_key_id = AKIACOMMITTED\naws_secret_access_key = s\n"
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


@pytest.mark.parametrize(("remote", "env", "message", "hint"), [
    pytest.param({"profile": "nope"}, {}, "AWS profile 'nope' (named in .dvc/config)",
                 "add a [nope] section there, or remove `profile` from the remote in .dvc/config (or .dvc/config.local)",
                 id="the-remote"),
    pytest.param({}, {"AWS_PROFILE": "nope"}, "AWS profile 'nope' (named by AWS_PROFILE)",
                 "add a [nope] section there, or unset AWS_PROFILE", id="AWS_PROFILE"),
    pytest.param({}, {"AWS_DEFAULT_PROFILE": "nope"}, "AWS profile 'nope' (named by AWS_DEFAULT_PROFILE)",
                 "add a [nope] section there, or unset AWS_DEFAULT_PROFILE", id="AWS_DEFAULT_PROFILE"),
    pytest.param({}, {"AWS_PROFILE": ""}, "AWS profile '' (named by AWS_PROFILE)",
                 "unset AWS_PROFILE, or set it to a profile that exists", id="an-empty-AWS_PROFILE"),
    pytest.param({"profile": ""}, {}, "AWS profile '' (named in .dvc/config)",
                 "remove `profile` from the remote in .dvc/config (or .dvc/config.local), or set it to a profile that exists",
                 id="an-empty-remote-profile"),
])
def test_a_named_profile_that_is_missing_is_an_error_saying_what_named_it(
    credentials: Path, monkeypatch: pytest.MonkeyPatch, remote: dict, env: dict, message: str, hint: str,
) -> None:
    """A missing named profile is an error, never a fallback to other credentials, and it
    says which setting named it. An empty name still names a profile, as botocore and dvc
    read it (`dvc remote modify --local <remote> profile ""` writes `profile = ""`).
    Mutations that must redden this: fall back to the default chain on ProfileNotFound;
    stop reading AWS_DEFAULT_PROFILE; treat an empty variable, or an empty remote profile,
    as naming nothing. mintd's own profile here is `other`, which exists, so a fallback to it
    would succeed and fail the test."""
    from mintd import _s3
    from mintd.errors import MintdError

    for var, value in env.items():
        monkeypatch.setenv(var, value)
    with pytest.raises(MintdError) as raised:
        _s3.create_client(remote, "other")
    assert str(raised.value) == f"{message} isn't in {credentials}"
    assert raised.value.hint == hint


@pytest.mark.parametrize(("remote", "key"), [
    pytest.param({"profile": "present"}, "AKIAPRESENT", id="a-named-profile-that-exists"),
    pytest.param({}, "AKIAENV", id="nothing-named-the-default-chain"),
])
def test_the_client_signs_with_the_profile_it_resolves(credentials: Path, remote: dict, key: str) -> None:
    """A named profile that exists is used; with nothing named, boto3's default chain is,
    on purpose, so CI and sandboxes keep working."""
    from mintd import _s3

    assert _key(_s3.create_client(remote, None)) == key


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


@pytest.mark.parametrize(("keys", "message"), [
    pytest.param("", "no AWS credentials were found", id="none"),
    pytest.param("[default]\naws_access_key_id = AKIABLANK\naws_secret_access_key =\n",
                 "the AWS credentials from shared-credentials-file have an empty access key or secret", id="a-blank-secret"),
])
def test_every_s3_client_is_refused_without_usable_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys: str, message: str,
) -> None:
    """create_client is the one place every S3 command connects through: it loads the
    credentials first, so push, clone, import, cache, share and check --remote stop there,
    with config validate's message and hint (a pull then falls back to dvc: below).
    Mutation: create_client back to profile_session alone -> no refusal, red."""
    from mintd import _s3

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
                "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "credentials").write_text(keys)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")  # off EC2 the chain would wait 2 s there
    with pytest.raises(_s3.CredentialsUnavailable) as raised:
        _s3.create_client({"endpointurl": "http://127.0.0.1:9"}, None)
    assert str(raised.value) == message and "AKIABLANK" not in str(raised.value)
    assert "add a [mintd] section" in (raised.value.hint or "")


def test_a_pull_falls_back_to_dvc_when_only_dvc_has_the_keys(
    s3_server, real_dvc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Keys only in .dvc/config.local: mintd finds none, dvc does. A pull falls back to dvc
    and gets the file, as on main; other commands stop early. Mutation: let pull stop on
    CredentialsUnavailable like any other MintdError -> red."""
    from mintd import cli
    from tests._harness.git import _git

    project = tmp_path / "dvcproj"
    project.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=project)
    real_dvc(["init", "-q"], cwd=project, check=True)
    real_dvc(["remote", "add", "-d", "storage", "s3://journey-bucket/p"], cwd=project, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", s3_server.endpoint], cwd=project, check=True)
    real_dvc(["remote", "modify", "storage", "version_aware", "true"], cwd=project, check=True)
    real_dvc(["remote", "modify", "--local", "storage", "access_key_id", "test"], cwd=project, check=True)
    real_dvc(["remote", "modify", "--local", "storage", "secret_access_key", "test"], cwd=project, check=True)
    (project / "x.csv").write_text("x\n")
    real_dvc(["add", "-q", "x.csv"], cwd=project, check=True)
    real_dvc(["push", "-q"], cwd=project, check=True)
    (project / "x.csv").unlink()
    # Moved aside, not deleted: dvc makes its cache files read-only, and Windows won't
    # delete those; pytest's own tmp cleanup handles them.
    (project / ".dvc" / "cache").rename(tmp_path / "pushed-cache")

    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "credentials").write_text("")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(tmp_path / "mintd-config"))
    rc = cli.main(["data", "pull", "--path", str(project)])
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 0, err
    assert (project / "x.csv").read_text() == "x\n"


def test_config_validate_names_the_profile_it_checks(credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With AWS_PROFILE naming a profile that exists, the profile step is ok and says which
    profile it checked and what named it."""
    from mintd.config_ops import validate_config

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    monkeypatch.setenv("AWS_PROFILE", "other")
    step = next(s for s in validate_config(cfg) if s.name == "aws_profile")
    assert (step.status, step.message) == ("ok", "'other' profile, named by AWS_PROFILE; credentials from shared-credentials-file")


@pytest.mark.parametrize("bucket", [pytest.param(None, id="no-bucket"), pytest.param("b", id="with-a-bucket")])
def test_config_validate_fails_a_missing_named_profile_with_its_hint(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bucket: str | None,
) -> None:
    """Every S3 command fails on it, so validate can't call it ok, and the hint says how to fix it;
    the s3 step, which would fail the same way, is skipped rather than said twice.
    Mutations that must redden this: drop the existence check in the profile step; drop the hint;
    run the s3 step after the profile step failed."""
    from mintd.config_ops import validate_config

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    monkeypatch.setenv("AWS_PROFILE", "nope")
    steps = {s.name: s for s in validate_config(cfg, bucket=bucket)}
    step = steps["aws_profile"]
    assert step.status == "fail"
    assert "AWS profile 'nope' (named by AWS_PROFILE)" in step.message
    assert "add a [nope] section there, or unset AWS_PROFILE" in step.message
    assert (steps["s3"].status, steps["s3"].message) == ("skipped", "aws_profile failed")


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


@pytest.mark.parametrize("level", ["config", "config.local", "config.local-over-a-committed-profile"])
@pytest.mark.parametrize(("value", "dvc_reads"), [
    pytest.param('"mintd"', "mintd", id="double-quoted"),
    pytest.param("'mintd'", "mintd", id="single-quoted"),
    pytest.param("mintd # mine", "mintd", id="an-inline-comment"),
    pytest.param('"mintd" # mine', "mintd", id="quoted-with-a-comment"),
    pytest.param('""', "", id="empty-double-quotes"),
    pytest.param("''", "", id="empty-single-quotes"),
    pytest.param("", "", id="nothing-after-the-equals"),
])
def test_mintd_reads_a_hand_edited_profile_as_dvc_reads_it(real_dvc, tmp_path: Path, value: str, dvc_reads: str, level: str) -> None:
    """dvc's config reader (configobj) strips one pair of matching quotes and an unquoted inline
    comment, so `profile = "mintd"` and `profile = mintd # mine` name `mintd`, and `profile = ""`
    names the empty profile, which then fails as missing. Fast sync, the cache lane
    (`get_remote_config`) and the push check (`merged_remote`) read every form as dvc does, in
    `.dvc/config`, in `.dvc/config.local` alone (`dvc remote modify --local`), and in a
    `.dvc/config.local` laid over a committed profile.
    Mutations that must redden this: drop the stripping in get_remote_config or in merged_remote."""
    from mintd._fast_sync_ops import get_remote_config
    from mintd._push_verify import merged_remote

    real_dvc(["init", "-q", "--no-scm"], cwd=tmp_path, check=True)
    remote = "['remote \"storage\"']\n"
    committed = "    profile = committed\n" if level == "config.local-over-a-committed-profile" else ""
    (tmp_path / ".dvc" / "config").write_text(f"[core]\n    remote = storage\n{remote}    url = s3://b/p\n{committed}")
    target = "config" if level == "config" else "config.local"
    with (tmp_path / ".dvc" / target).open("a") as f:
        f.write(f"{remote if target == 'config.local' else ''}    profile = {value}\n")

    assert real_dvc(["config", "remote.storage.profile"], cwd=tmp_path, check=True).stdout.strip() == dvc_reads
    assert get_remote_config(tmp_path, "storage")["profile"] == dvc_reads
    assert merged_remote(tmp_path, "storage")["profile"] == dvc_reads


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


@pytest.mark.parametrize("named", ["present", None], ids=["a-named-profile", "nothing-named"])
def test_config_validate_reports_an_unreadable_aws_config_instead_of_crashing(
    credentials: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: str | None,
) -> None:
    """The diagnostic command reports a malformed AWS config file as a failed step. Named or
    not, boto3 parses the files, so every S3 command would fail.
    Mutations that must redden this: catch only MintdError in the profile step; check only
    when a profile is named (the nothing-named case)."""
    from mintd.config_ops import validate_config

    bad = tmp_path / "bad-aws-config"
    bad.write_text("this is not ini\n[profile x\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(bad))
    if named:
        monkeypatch.setenv("AWS_PROFILE", named)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("registry_url: x\n")
    step = next(s for s in validate_config(cfg) if s.name == "aws_profile")
    assert step.status == "fail"
    assert step.message.startswith("could not read the AWS configuration: ")


def _committed_and_local(tmp_path: Path, local: str) -> Path:
    (tmp_path / ".dvc").mkdir()
    (tmp_path / ".dvc" / "config").write_text("['remote \"origin\"']\n    url = s3://b/p\n    profile = committed\n")
    (tmp_path / ".dvc" / "config.local").write_text(local)
    return tmp_path


@pytest.mark.parametrize(("local", "profile", "key"), [
    pytest.param("['remote \"origin\"']\n    profile = present\n    url = s3://scratch/x\n    endpointurl = http://localhost:9\n",
                 "present", "AKIAPRESENT", id="its-profile-crosses-and-nothing-else"),
    pytest.param("['remote \"other\"']\n    profile = present\n", "committed", "AKIACOMMITTED", id="no-section-for-this-remote"),
    pytest.param("['remote \"origin\"']\n    profile = present\n    sse_kms_key_id = alias/x%y\n",
                 "present", "AKIAPRESENT", id="a-percent-sign-in-another-key"),
])
def test_config_local_lends_the_committed_remote_only_its_profile(
    credentials: Path, tmp_path: Path, local: str, profile: str, key: str,
) -> None:
    """dvc lays `.dvc/config.local` over `.dvc/config`, so a profile set there for this remote
    is the one this machine uses, and the client signs with it. Every other key stays the
    committed one: the committed config is the remote's location of record. A `config.local`
    with no section for this remote leaves the committed profile, and a `%` in it is literal,
    as in dvc.
    Mutations that must redden this: read .dvc/config alone; let every config.local key cross;
    raise when config.local has no section for this remote; parse with configparser's
    %-interpolation."""
    from mintd import _s3
    from mintd._fast_sync_ops import get_remote_config

    cfg = get_remote_config(_committed_and_local(tmp_path, local), "origin")
    assert cfg == {"url": "s3://b/p", "profile": profile}
    assert _key(_s3.create_client(cfg, None)) == key


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
