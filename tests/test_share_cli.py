"""End-to-end ``mintd share`` CLI tests via ``cli.main``.

Covers the ✓-line grammar, the identity nudge, ``--json`` single-object stdout,
exit codes (1 runtime / 64 usage / 130 Ctrl-C), and the journey-transcript
failure paths pinned verbatim (whitespace-normalized — Reporter wraps). No bare
``print()``; every documented failure ends in ``error: … / hint: …`` with no
traceback.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from mintd import cli
from mintd._config import Config


def _norm(text: str) -> str:
    """Collapse Reporter's line-wrapping whitespace for stable assertions."""
    return re.sub(r"\s+", " ", text).strip()


def _load_config(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cfg))


def _use_factory(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    monkeypatch.setattr("mintd._share_ops._create_s3_client", factory)


def _configured() -> Config:
    return Config(
        storage_bucket_prefix="test-bucket",
        storage_endpoint="https://s3",
        author="Maurice Dalton",
    )


class _AssertNotCalledFactory:
    def __call__(self, *a, **k):
        raise AssertionError("s3_client_factory must not be called")


# ---------- happy path: moto round-trip through the CLI ----------


def test_share_put_then_get_roundtrip_via_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "model.parquet"
    src.write_bytes(b"payload" * 4000)

    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _use_factory(monkeypatch, lambda _c, _p: client)
        _load_config(monkeypatch, _configured())

        rc = cli.main(["share", "put", str(src), "--as", "grant-2026/"])
        put_err = _norm(capsys.readouterr().err)
        assert rc == 0
        # nudge (author fallback) + ✓-line grammar
        assert "sharing as 'maurice-dalton'" in put_err
        assert (
            "✓ shared model.parquet" in put_err
            and "share/maurice-dalton/grant-2026/model.parquet" in put_err
        )
        # the ready-to-run fetch command a teammate pastes (the get-ref, no
        # "share/" prefix) — this is the exact ref the get below uses
        assert (
            "fetch it with: mintd share get maurice-dalton/grant-2026/model.parquet"
            in put_err
        )

        out_dir = tmp_path / "inbox"
        out_dir.mkdir()
        rc = cli.main(
            [
                "share",
                "get",
                "maurice-dalton/grant-2026/model.parquet",
                "--out",
                str(out_dir),
            ]
        )
        get_err = _norm(capsys.readouterr().err)
        assert rc == 0
        assert "✓ got model.parquet" in get_err
        dest = out_dir / "model.parquet"
        assert dest.read_bytes() == src.read_bytes()


def test_share_get_out_trailing_slash_creates_dir_via_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `share get <ref> --out results/` where results/ does not yet exist must
    # create the directory and land the file INSIDE it — not write a file named
    # "results". Regression for argparse type=Path stripping the trailing '/'.
    src = tmp_path / "model.parquet"
    src.write_bytes(b"payload" * 100)
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _use_factory(monkeypatch, lambda _c, _p: client)
        _load_config(monkeypatch, _configured())
        cli.main(["share", "put", str(src), "--as", "g/"])
        capsys.readouterr()

        results = tmp_path / "results"
        assert not results.exists()
        rc = cli.main(
            [
                "share",
                "get",
                "maurice-dalton/g/model.parquet",
                "--out",
                f"{results}/",
            ]
        )
    err = _norm(capsys.readouterr().err)
    assert rc == 0
    assert results.is_dir()
    assert (results / "model.parquet").read_bytes() == src.read_bytes()
    assert not (tmp_path / "results").is_file()  # never a bare file named results
    assert "✓ got model.parquet" in err


def test_share_put_json_single_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "model.parquet"
    src.write_bytes(b"x" * 100)
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _use_factory(monkeypatch, lambda _c, _p: client)
        _load_config(monkeypatch, _configured())

        rc = cli.main(["--json", "share", "put", str(src), "--as", "g/"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    payload = json.loads(out)  # exactly one parseable object, nothing else
    assert payload["shared"] == "model.parquet"
    assert payload["key"] == "share/maurice-dalton/g/model.parquet"
    assert payload["ref"] == "maurice-dalton/g/model.parquet"  # the get-ref
    assert payload["bytes"] == 100
    assert set(payload) == {"shared", "key", "ref", "bytes", "elapsed_s"}


def test_share_get_json_single_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "model.parquet"
    src.write_bytes(b"y" * 50)
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _use_factory(monkeypatch, lambda _c, _p: client)
        _load_config(monkeypatch, _configured())
        cli.main(["--json", "share", "put", str(src), "--as", "g/"])
        capsys.readouterr()

        dest = tmp_path / "inbox" / "model.parquet"
        rc = cli.main(
            ["--json", "share", "get", "maurice-dalton/g/model.parquet", "--out", str(dest)]
        )
    out = capsys.readouterr().out.strip()
    assert rc == 0
    payload = json.loads(out)
    assert payload["got"] == "model.parquet"
    assert payload["ref"] == "maurice-dalton/g/model.parquet"
    assert payload["dest"] == str(dest)
    assert set(payload) == {"got", "ref", "dest", "bytes", "elapsed_s"}


def test_share_put_pinned_user_no_nudge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "f.parquet"
    src.write_bytes(b"z" * 10)
    cfg = Config(
        storage_bucket_prefix="test-bucket",
        storage_endpoint="https://s3",
        share_user="pinned",
    )
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        _use_factory(monkeypatch, lambda _c, _p: client)
        _load_config(monkeypatch, cfg)
        rc = cli.main(["-v", "share", "put", str(src)])
    err = _norm(capsys.readouterr().err)
    assert rc == 0
    assert "sharing as" not in err  # pinned share_user → no nudge
    assert "share/pinned/f.parquet" in err


# ---------- failure transcripts (R4 / R5 / R6) ----------


def test_share_put_no_identity_prints_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "draft.parquet"
    src.write_bytes(b"x")
    cfg = Config(storage_bucket_prefix="test-bucket", storage_endpoint="https://s3")
    _load_config(monkeypatch, cfg)
    _use_factory(monkeypatch, _AssertNotCalledFactory())

    rc = cli.main(["share", "put", str(src)])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: cannot determine share user" in err
    assert "hint: set share_user in ~/.config/mintd/config.yaml" in err
    assert "Traceback" not in err


def test_share_get_missing_remote_key_prints_verbatim_transcript(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Stub404:
        def head_object(self, **k):
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )

    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, lambda _c, _p: _Stub404())

    rc = cli.main(["share", "get", "alice/nope.parquet"])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: no share object at alice/nope.parquet" in err
    assert (
        "hint: check the ref with the sender; share objects may also have expired"
        in err
    )
    assert "Traceback" not in err


def test_share_get_missing_config_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _load_config(monkeypatch, Config())  # no bucket/endpoint
    _use_factory(monkeypatch, _AssertNotCalledFactory())
    rc = cli.main(["share", "get", "alice/x.parquet"])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: storage is not configured" in err
    assert "hint:" in err
    assert "Traceback" not in err


def test_share_put_missing_local_file_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, _AssertNotCalledFactory())
    rc = cli.main(["share", "put", str(tmp_path / "nope.parquet")])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: no such file" in err
    assert "Traceback" not in err


def test_share_put_absent_credentials_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "f.parquet"
    src.write_bytes(b"x")

    class _NoCreds:
        def upload_file(self, *a, **k):
            raise NoCredentialsError()

    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, lambda _c, _p: _NoCreds())
    rc = cli.main(["share", "put", str(src)])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: AWS credentials unavailable (not retried)" in err
    assert "hint: check AWS credentials: mintd config validate" in err
    assert "Traceback" not in err


def test_share_get_refuses_existing_dest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "model.parquet"
    existing.write_bytes(b"keep me")
    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, _AssertNotCalledFactory())  # refusal is pre-S3
    rc = cli.main(["share", "get", "alice/model.parquet", "--out", str(existing)])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "error: refusing to overwrite" in err
    assert existing.read_bytes() == b"keep me"


# ---------- boto3 unavailable (R3: probe-and-exit-2) ----------


def test_share_put_boto3_unavailable_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # R3: when boto3/dvc[s3] is not installed, the handler probes via
    # _share_boto3_available and exits 2 with a clean error + hint — never a
    # traceback from a later _create_s3_client whose boto3 is None. Simulate the
    # missing dependency by making `import boto3` raise ImportError (the guard's
    # early return fires before identity resolution or any S3 call).
    src = tmp_path / "f.parquet"
    src.write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "boto3", None)
    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, _AssertNotCalledFactory())

    rc = cli.main(["share", "put", str(src), "--as", "g/"])
    err = _norm(capsys.readouterr().err)
    assert rc == 2
    assert "error: share requires boto3, which is not installed" in err
    assert "hint: install it: pip install 'dvc" in err
    assert "Traceback" not in err


def test_share_get_boto3_unavailable_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # R3: same probe-and-exit-2 posture for the receive verb (needs no identity).
    monkeypatch.setitem(sys.modules, "boto3", None)
    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, _AssertNotCalledFactory())

    rc = cli.main(["share", "get", "alice/model.parquet", "--out", "inbox/"])
    err = _norm(capsys.readouterr().err)
    assert rc == 2
    assert "error: share requires boto3, which is not installed" in err
    assert "hint: install it: pip install 'dvc" in err
    assert "Traceback" not in err


# ---------- exit codes ----------


def test_share_bad_usage_exits_64(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["share", "put"])  # missing required positional 'file'
    assert ei.value.code == 64


def test_share_ctrl_c_exits_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "f.parquet"
    src.write_bytes(b"x")

    class _InterruptClient:
        def upload_file(self, *a, **k):
            raise KeyboardInterrupt()

    _load_config(monkeypatch, _configured())
    _use_factory(monkeypatch, lambda _c, _p: _InterruptClient())
    rc = cli.main(["share", "put", str(src), "--as", "g/"])
    err = _norm(capsys.readouterr().err)
    assert rc == 130
    assert "interrupted by user" in err
    assert "Traceback" not in err
