"""`mintd config validate` checks what every S3 command needs: credentials that load,
and the configured bucket. Made-up keys and buckets only; nothing reaches AWS
(moto stands in for S3, and the instance metadata service is off)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from mintd import _s3, cli
from mintd.config_ops import ValidationStep, render_validation, validate_config
from mintd.errors import MintdError

_KEY, _SECRET, _TOKEN = "AKIAEXAMPLEKEY", "example-secret-value", "example-session-token"


@pytest.fixture
def aws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No credentials anywhere: no AWS variables, AWS files at paths of the test's own,
    no instance metadata service, and a HOME of its own (the SSO token cache)."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    aws_dir = tmp_path / "aws"
    aws_dir.mkdir()
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_dir / "credentials"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_dir / "config"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    return aws_dir


def _env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _SECRET)
    monkeypatch.setenv("AWS_SESSION_TOKEN", _TOKEN)


def _config(tmp_path: Path, text: str = "registry_url: https://example.org/catalog.git\n") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def _steps(path: Path, **kw: object) -> dict[str, ValidationStep]:
    return {s.name: s for s in validate_config(path, **kw)}  # type: ignore[arg-type]


# --- loading the credentials ---------------------------------------------------


def test_no_credentials_are_found(aws: Path) -> None:
    """Mutation: take get_credentials() returning None as found -> red."""
    with pytest.raises(MintdError, match="no AWS credentials were found") as raised:
        _s3.load_credentials({}, None)
    assert "AWS_ACCESS_KEY_ID" in (raised.value.hint or "")


@pytest.mark.parametrize("section", [
    pytest.param(f"aws_access_key_id = {_KEY}\naws_secret_access_key =\n", id="blank-secret"),
    pytest.param("aws_access_key_id =\naws_secret_access_key =\n", id="both-blank"),
    pytest.param(f"aws_access_key_id = {_KEY}\n", id="no-secret-line"),
])
def test_a_mintd_profile_without_both_keys_fails(aws: Path, section: str) -> None:
    """botocore returns blank keys without raising; a key with no secret is partial.
    Mutation: drop the blank check -> the blank cases pass, red."""
    (aws / "credentials").write_text(f"[mintd]\n{section}")
    with pytest.raises(MintdError) as raised:
        _s3.load_credentials({}, "mintd")
    assert _KEY not in str(raised.value)


def test_env_keys_load_and_say_so(aws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env_keys(monkeypatch)
    _, method = _s3.load_credentials({}, None)
    assert method == "env"


def test_an_sso_profile_with_no_token_fails_when_its_credentials_are_read(aws: Path) -> None:
    """SSO credentials are deferred: get_credentials() returns an object; only reading them fails.
    Mutation: skip get_frozen_credentials() -> nothing raises, red."""
    (aws / "config").write_text(
        "[profile lab]\nsso_start_url = https://example.awsapps.com/start\nsso_region = us-east-1\n"
        "sso_account_id = 111111111111\nsso_role_name = Example\nregion = us-east-1\n"
    )
    with pytest.raises(MintdError, match="AWS credentials could not be loaded"):
        _s3.load_credentials({"profile": "lab"}, None)


def test_a_failing_credential_process_is_named_without_its_output(aws: Path, tmp_path: Path) -> None:
    """A credential_process's stderr may hold anything; botocore puts it in its error.
    Mutation: put str(exc) back into the message -> STDERR-SECRET shows, red."""
    code = "import sys; sys.stderr.write('STDERR-SECRET'); sys.exit(1)"  # Python, so it runs on Windows too
    (aws / "config").write_text(f'[profile lab]\ncredential_process = "{sys.executable}" -c "{code}"\n')
    with pytest.raises(MintdError) as raised:
        _s3.load_credentials({"profile": "lab"}, None)
    text = f"{raised.value} {raised.value.hint}"
    assert "custom-process provider failed" in text and "credential_process yourself" in text
    assert "STDERR-SECRET" not in text


def test_a_credential_process_that_cannot_start_is_named_and_points_at_the_process(aws: Path, tmp_path: Path) -> None:
    """botocore lets the OSError through; on Windows a script with a shebang raises one too.
    Mutation: drop the OSError branch -> the hint about a [mintd] section, red."""
    missing = tmp_path / "no-such-program"
    (aws / "config").write_text(f'[profile lab]\ncredential_process = "{missing}" --token ARG-SECRET\n')
    with pytest.raises(MintdError) as raised:
        _s3.load_credentials({"profile": "lab"}, None)
    assert "the profile's credential_process couldn't start" in str(raised.value)
    assert raised.value.hint == _PROCESS_HINT
    assert "ARG-SECRET" not in f"{raised.value} {raised.value.hint}"


_PROCESS_HINT = "run the profile's credential_process yourself to see what it says"


def _process_prints(aws: Path, code: str) -> None:
    """A credential_process that runs ``code`` in Python, so it runs on Windows too."""
    (aws / "config").write_text(f'[profile lab]\ncredential_process = "{sys.executable}" -c "{code}"\n')


def _load_failure(remote: dict[str, str] | None = None) -> MintdError:
    with pytest.raises(MintdError) as raised:
        _s3.load_credentials({"profile": "lab"} if remote is None else remote, None)
    return raised.value


def test_a_credential_process_whose_expiration_does_not_parse_never_prints_its_output(aws: Path) -> None:
    """dateutil's error quotes the value, which came from the process's stdout.
    Mutation: print every load error's text again -> SEKRET_STDOUT shows, red."""
    _process_prints(aws, "import json; print(json.dumps(dict(Version=1, AccessKeyId='AKIAEXAMPLE', "
                         "SecretAccessKey='x', Expiration='SEKRET_STDOUT')))")
    error = _load_failure()
    assert "SEKRET_STDOUT" not in f"{error} {error.hint}"
    assert error.hint == _PROCESS_HINT


@pytest.mark.parametrize("code", [
    pytest.param("print('not json SEKRET_STDOUT')", id="bad-json"),
    pytest.param("import json; print(json.dumps(dict(Version=1, AccessKeyId='AKIAEXAMPLE', SecretAccessKey='x', "
                 "Expiration='2000-01-01T00:00:00Z')))", id="expired"),
])
def test_a_credential_process_that_gives_unusable_keys_points_at_the_process(aws: Path, code: str) -> None:
    """Mutation: the [mintd]-section hint for a profile with a credential_process -> red."""
    _process_prints(aws, code)
    error = _load_failure()
    assert error.hint == _PROCESS_HINT
    assert "SEKRET_STDOUT" not in f"{error} {error.hint}"


def test_a_missing_token_file_is_named_without_blaming_a_credential_process(
    aws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No profile runs a program here. Mutation: say "credential_process" for any OSError -> red."""
    missing = tmp_path / "no-token"
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(missing))
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/example")
    error = _load_failure({})
    text = f"{error} {error.hint}"
    assert str(missing) in str(error) and "credential_process" not in text
    assert "AWS_*_FILE" in error.hint


def test_an_aws_config_that_does_not_parse_is_a_mintd_error(aws: Path) -> None:
    """Mutation: no ConfigParseError mapping in profile_session -> botocore's own error, red."""
    (aws / "config").write_text("[profile lab\nregion = us-east-1\n")
    error = _load_failure()
    assert "could not read the AWS configuration" in str(error) and str(aws / "config") in str(error)


# --- validate ----------------------------------------------------------------------


def test_validate_fails_with_no_credentials_and_skips_s3(aws: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    path = _config(tmp_path, "registry_url: x\nstorage_bucket_prefix: example-lab-data\n")
    steps = _steps(path)
    assert steps["aws_profile"].status == "fail"
    assert "no AWS credentials were found" in steps["aws_profile"].message
    assert (steps["s3"].status, steps["s3"].message) == ("skipped", "aws_profile failed")
    assert render_validation(list(steps.values()), json_out=False)[1] == 1
    assert cli.main(["config", "validate", "--path", str(path)]) == 1


def test_validate_reads_the_credentials_once(aws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bucket check reuses the profile step's session: one credential fetch, one IMDS wait.
    Mutation: build the client without the session -> two sessions, red."""
    _env_keys(monkeypatch)
    made = []
    real = _s3.profile_session
    monkeypatch.setattr(_s3, "profile_session", lambda *a: made.append(a) or real(*a))
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="example-lab-data")
        steps = _steps(_config(tmp_path, "registry_url: x\nstorage_bucket_prefix: example-lab-data\n"))
    assert steps["s3"].status == "ok", steps["s3"].message
    assert len(made) == 1


def test_validate_with_no_boto3_skips_both_s3_steps(aws: Path, tmp_path: Path, monkeypatch) -> None:
    """Mutation: report the profile ok when boto3 is missing -> red."""
    monkeypatch.setattr(_s3, "available", lambda: False)
    steps = _steps(_config(tmp_path))
    assert [(steps[n].status, steps[n].message) for n in ("aws_profile", "s3")] == [
        ("skipped", "boto3 not installed"), ("skipped", "boto3 not installed")]


@pytest.mark.parametrize(("prefix", "flag", "status", "checked"), [
    pytest.param("example-lab-data", None, "ok", "example-lab-data", id="the-config-bucket"),
    pytest.param("example-missing", None, "fail", "example-missing", id="the-config-bucket-missing"),
    pytest.param("example-missing", "example-lab-data", "ok", "example-lab-data", id="--bucket-wins"),
])
def test_validate_checks_the_configured_bucket(
    aws: Path, tmp_path: Path, monkeypatch, prefix: str, flag: str | None, status: str, checked: str,
) -> None:
    """Mutation: drop the fallback to storage_bucket_prefix -> the config cases skip, red."""
    _env_keys(monkeypatch)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="example-lab-data")
        s3 = _steps(_config(tmp_path, f"registry_url: x\nstorage_bucket_prefix: {prefix}\n"), bucket=flag)["s3"]
    assert (s3.status, s3.bucket) == (status, checked), s3.message
    assert f"s3://{checked}" in s3.message


@pytest.mark.parametrize("prefix", ['""', '"  "', None])
def test_validate_with_no_bucket_says_it_checked_none(aws: Path, tmp_path: Path, monkeypatch, prefix) -> None:
    """A blank prefix is no prefix. Mutation: drop `.strip() or None` -> head_bucket(""), red."""
    _env_keys(monkeypatch)
    text = "registry_url: x\n" + (f"storage_bucket_prefix: {prefix}\n" if prefix else "")
    s3 = _steps(_config(tmp_path, text))["s3"]
    assert (s3.status, s3.message) == (
        "skipped", "no bucket to check: storage_bucket_prefix isn't set and no --bucket was given")


@pytest.mark.parametrize("prefix", ["s3://example-lab-data", "../example-lab-data", "example/lab"])
def test_validate_never_passes_a_malformed_bucket(aws: Path, tmp_path: Path, monkeypatch, prefix: str) -> None:
    _env_keys(monkeypatch)
    with mock_aws():
        s3 = _steps(_config(tmp_path, f"registry_url: x\nstorage_bucket_prefix: '{prefix}'\n"))["s3"]
    assert s3.status == "fail", s3.message


@pytest.mark.parametrize("line", ["", 'registry_url: ""\n', 'registry_url: "  "\n'])
def test_a_missing_registry_url_warns_and_still_exits_0(aws: Path, tmp_path: Path, monkeypatch, line: str) -> None:
    """Mutations: count `warning` toward the exit code -> red; test `is None` -> the blank cases pass, red."""
    _env_keys(monkeypatch)
    steps = list(validate_config(_config(tmp_path, line or "author: Example\n")))
    schema = steps[0]
    assert schema.status == "warning" and "registry_url isn't set" in schema.message
    text, code = render_validation(steps, json_out=False)
    assert code == 0 and text.splitlines()[0].startswith("⚠ schema:")
    assert set(json.loads(render_validation(steps, json_out=True)[0])) == {"schema", "aws_profile", "s3"}


def test_a_set_registry_url_is_not_mentioned(aws: Path, tmp_path: Path, monkeypatch) -> None:
    _env_keys(monkeypatch)
    schema = validate_config(_config(tmp_path))[0]
    assert schema.status == "ok" and "registry_url" not in schema.message


def test_a_warning_never_changes_the_exit_code() -> None:
    steps = [ValidationStep(name="schema", status="warning", message="m"),
             ValidationStep(name="aws_profile", status="ok", message="m"),
             ValidationStep(name="s3", status="skipped", message="m")]
    assert render_validation(steps, json_out=False)[1] == 0
    assert render_validation(steps, json_out=True)[1] == 0


# --- what the CLI prints -------------------------------------------------------------


@pytest.mark.parametrize("json_out", [False, True])
def test_no_key_secret_or_token_is_printed(aws: Path, tmp_path: Path, monkeypatch, capsys, json_out: bool) -> None:
    _env_keys(monkeypatch)
    path = _config(tmp_path, "registry_url: x\nstorage_bucket_prefix: example-lab-data\n")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="example-lab-data")
        assert cli.main([*(["--json"] if json_out else []), "config", "validate", "--path", str(path)]) == 0
    out = capsys.readouterr()
    for secret in (_KEY, _SECRET, _TOKEN):
        assert secret not in out.out + out.err


@pytest.mark.parametrize("json_out", [False, True])
def test_a_url_with_credentials_in_a_step_is_printed_without_them(
    aws: Path, tmp_path: Path, monkeypatch, capsys, s3_server, json_out: bool,
) -> None:
    """An endpoint written with a user and password: the s3 step's message
    names it. Mutation: print the text without strip_userinfo -> red."""
    _env_keys(monkeypatch)
    host = s3_server.endpoint.removeprefix("http://")
    path = _config(tmp_path, f"storage_endpoint: http://user:pass@{host}\nstorage_bucket_prefix: no-such-bucket\n")
    cli.main([*(["--json"] if json_out else []), "config", "validate", "--path", str(path)])
    out = capsys.readouterr().out
    assert "user:pass" not in out and host in out


@pytest.mark.parametrize(("prefix", "flag"), [
    pytest.param("journey-bucket", None, id="the-config-bucket"),
    pytest.param(None, "journey-bucket", id="the-flag"),
])
def test_the_success_line_names_the_bucket_checked(
    aws: Path, tmp_path: Path, monkeypatch, capsys, s3_server, prefix, flag,
) -> None:
    """Mutation: name args.bucket only -> s3://None for a config bucket, red."""
    _env_keys(monkeypatch)
    text = f"storage_endpoint: {s3_server.endpoint}\n" + (f"storage_bucket_prefix: {prefix}\n" if prefix else "")
    path = _config(tmp_path, text)
    assert cli.main(["config", "validate", "--path", str(path), *(["--bucket", flag] if flag else [])]) == 0
    line = next(ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("✓ s3://"))
    assert "s3://journey-bucket" in line and "s3://None" not in line
    assert s3_server.endpoint in line  # the endpoint comes from --path's config
