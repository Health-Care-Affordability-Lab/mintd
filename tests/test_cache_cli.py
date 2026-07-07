"""End-to-end ``mintd cache`` CLI tests via ``cli.main``.

Covers the ✓-line grammar, ``--json`` single-object stdout (with the ``failed``
key), exit codes (1 runtime / 2 boto3-missing), the not-inside-a-project and
collision-guard failure paths (``error: … / hint: …``, no traceback), the
hostile-key refusal, and the ``ls`` render through the shared data-ls renderer
with the ``mintd cache pull --prefix`` hint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from mintd import cli
from mintd._config import Config


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _load_config(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr("mintd.cli.Config.load", classmethod(lambda cls, path=None: cfg))


def _use_factory(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setattr("mintd.cli._resolve_cache_ops", lambda config: (lambda _c, _p: client))


def _project(tmp_path: Path, bucket: str, prefix: str = "lab/proj") -> Path:
    proj = tmp_path / "proj"
    (proj / ".dvc").mkdir(parents=True)
    (proj / ".dvc" / "config").write_text(f'[remote "origin"]\n    url = s3://{bucket}/{prefix}\n')
    return proj


def _versioned_bucket():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="test-bucket")
    client.put_bucket_versioning(
        Bucket="test-bucket", VersioningConfiguration={"Status": "Enabled"}
    )
    return client, "test-bucket"


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


def test_cache_push_happy_line_and_durability_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        proj = _project(tmp_path, bucket)
        (proj / "cache" / "iso").mkdir(parents=True)
        (proj / "cache" / "iso" / "a.bin").write_bytes(b"a" * 500)
        _use_factory(monkeypatch, client)
        _load_config(monkeypatch, Config())

        rc = cli.main(["cache", "push", "cache/", "--path", str(proj)])
        err = _norm(capsys.readouterr().err)
        assert rc == 0
        assert "✓ pushed 1 file(s) to the repo file cache (S3)" in err
        assert "1 uploaded, 0 unchanged" in err
        assert "s3://test-bucket/lab/proj/cache/" in err
        assert "for versioned, citable outputs use: mintd data push" in err


def test_cache_push_json_has_failed_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        proj = _project(tmp_path, bucket)
        (proj / "cache").mkdir()
        (proj / "cache" / "a.bin").write_bytes(b"a" * 20)
        _use_factory(monkeypatch, client)
        _load_config(monkeypatch, Config())

        rc = cli.main(["--json", "cache", "push", "cache/", "--path", str(proj)])
        out = capsys.readouterr().out.strip()
        assert rc == 0
        payload = json.loads(out)  # exactly one JSON object
        assert payload["cached"] == 1
        assert payload["uploaded"] == 1
        assert payload["unchanged"] == 0
        assert payload["failed"] == 0
        assert payload["prefix"] == "lab/proj/cache"


def test_cache_push_outside_project_errors_with_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    bare = tmp_path / "not-a-project"
    bare.mkdir()
    _load_config(monkeypatch, Config())
    rc = cli.main(["cache", "push", "cache/", "--path", str(bare)])
    err = _norm(capsys.readouterr().err)
    assert rc == 1
    assert "not inside a mintd project" in err
    assert "repo file cache (S3)" in err
    assert "traceback" not in err.lower()


def test_cache_push_boto3_missing_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        _client, bucket = _versioned_bucket()
        proj = _project(tmp_path, bucket)
        (proj / "cache").mkdir()
        (proj / "cache" / "a.bin").write_bytes(b"a")
        monkeypatch.setattr("mintd.cli._resolve_cache_ops", lambda config: None)
        _load_config(monkeypatch, Config())
        rc = cli.main(["cache", "push", "cache/", "--path", str(proj)])
        err = _norm(capsys.readouterr().err)
        assert rc == 2
        assert "requires boto3" in err


def test_cache_push_collision_guard_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        proj = _project(tmp_path, bucket)
        (proj / "cache").mkdir()
        (proj / "cache" / "a.bin").write_bytes(b"a")
        (proj / "cache" / "model.dvc").write_text(
            "outs:\n- path: model\n  md5: d41d8cd98f00b204e9800998ecf8427e\n"
            "  cloud:\n    origin:\n      version_id: v1\n"
        )
        _use_factory(monkeypatch, client)
        _load_config(monkeypatch, Config())
        rc = cli.main(["cache", "push", "cache/", "--path", str(proj)])
        err = _norm(capsys.readouterr().err)
        assert rc == 1
        assert "overlaps the repo file cache (S3) namespace" in err


def test_cache_push_partial_failure_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from boto3.exceptions import S3UploadFailedError
    from botocore.exceptions import ClientError

    class _Failing:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, n):
            return getattr(self._real, n)

        def upload_file(self, filename, bucket, key, ExtraArgs=None, Callback=None):  # noqa: N803
            if "boom" in key:
                try:
                    raise ClientError(
                        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                        "PutObject",
                    )
                except ClientError:
                    raise S3UploadFailedError("fail")
            return self._real.upload_file(filename, bucket, key, ExtraArgs=ExtraArgs, Callback=Callback)

    with mock_aws():
        client, bucket = _versioned_bucket()
        proj = _project(tmp_path, bucket)
        (proj / "cache").mkdir()
        (proj / "cache" / "ok.bin").write_bytes(b"ok")
        (proj / "cache" / "boom.bin").write_bytes(b"no")
        _use_factory(monkeypatch, _Failing(client))
        _load_config(monkeypatch, Config())
        rc = cli.main(["cache", "push", "cache/", "--path", str(proj)])
        err = _norm(capsys.readouterr().err)
        assert rc == 1
        assert "push incomplete" in err
        assert "mintd cache push cache/boom.bin" in err


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def _seed(client, bucket: str, tmp_path: Path, files: dict[str, bytes], monkeypatch) -> None:
    # Push each file at its literal repo-relative path, so its S3 key is
    # <prefix>/cache/<rel> and a pull reconstructs it at <rel>.
    src = _project(tmp_path / "producer", bucket)
    for rel, body in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    _use_factory(monkeypatch, client)
    _load_config(monkeypatch, Config())
    assert cli.main(["--json", "cache", "push", *files, "--path", str(src)]) == 0


def test_cache_pull_happy_line_and_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"iso/ct/a.bin": b"A" * 100}, monkeypatch)
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["cache", "pull", "--prefix", "iso/ct/", "--path", str(clone)])
        err = _norm(capsys.readouterr().err)
        assert rc == 0
        assert "✓ pulled 1 file(s) from the repo file cache (S3)" in err
        assert "to their repo paths" in err
        assert "'mintd data pull' handles the versioned outputs" in err
        # Reconstructed at its repo-relative path (not under cache/).
        assert (clone / "iso" / "ct" / "a.bin").read_bytes() == b"A" * 100


def test_cache_pull_hostile_key_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"good.bin": b"g" * 5}, monkeypatch)
        client.put_object(Bucket=bucket, Key="lab/proj/cache/../evil", Body=b"pwn")
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["cache", "pull", "--path", str(clone)])
        err = _norm(capsys.readouterr().err)
        assert rc == 1
        assert "unsafe key refused" in err
        assert not (clone.parent / "evil").exists()


def test_cache_pull_empty_listing_warns_exit_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _use_factory(monkeypatch, client)
        _load_config(monkeypatch, Config())
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["cache", "pull", "--path", str(clone)])
        err = _norm(capsys.readouterr().err)
        assert rc == 0
        assert "nothing pulled" in err


def test_cache_pull_local_fs_error_exit_1_no_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    # A repo path component that already exists as a plain FILE makes
    # download_object's tmp.parent.mkdir raise FileExistsError before any
    # transport-error mapping. It must surface as a clean exit-1 error line
    # (not a raw Python traceback) out of cli.main().
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"sub/x.bin": b"x" * 10}, monkeypatch)
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        (clone / "sub").write_bytes(b"stray file, not a dir")
        rc = cli.main(["cache", "pull", "--path", str(clone)])
        err = _norm(capsys.readouterr().err)
        assert rc == 1
        assert "pull incomplete" in err
        assert "local filesystem error" in err
        assert "traceback" not in err.lower()


def test_cache_pull_json_has_failed_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"a.bin": b"a" * 12}, monkeypatch)
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["--json", "cache", "pull", "--path", str(clone)])
        payload = json.loads(capsys.readouterr().out.strip())
        assert rc == 0
        assert payload["pulled"] == 1
        assert payload["failed"] == 0
        assert "bytes" in payload


def test_cache_pull_skip_existing_then_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    # A local untracked file that differs is kept-and-warned (exit 0); --force
    # then overwrites it. Drives the full CLI render + flag wiring.
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"data/x.bin": b"REMOTE" * 8}, monkeypatch)
        clone = _project(tmp_path / "cloneroot", bucket)
        local = clone / "data" / "x.bin"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"LOCAL")
        capsys.readouterr()

        rc = cli.main(["cache", "pull", "--path", str(clone)])
        err = _norm(capsys.readouterr().err)
        assert rc == 0
        assert "kept local" in err and "data/x.bin" in err
        assert local.read_bytes() == b"LOCAL"

        rc = cli.main(["cache", "pull", "--force", "--path", str(clone)])
        assert rc == 0
        assert local.read_bytes() == b"REMOTE" * 8


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def test_cache_ls_renders_with_cache_pull_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"iso/a.bin": b"a" * 10, "iso/b.bin": b"b" * 5}, monkeypatch)
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["cache", "ls", "--path", str(clone)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "s3://test-bucket/lab/proj/cache/" in out
        # The hint names the file's containing DIRECTORY (a listable prefix,
        # trailing '/'), never the full file key (which normalise's trailing
        # '/' turns into a no-op prefix that matches nothing).
        assert "mintd cache pull --prefix iso/" in out
        assert "--prefix iso/a.bin" not in out
        # No ✓-line for a listing.
        assert "✓" not in out


def test_cache_ls_neutralizes_control_chars_in_planted_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """A remote key with an embedded newline + fake ✓-line (planted directly,
    bypassing the push guard, as any teammate / `aws s3 cp` could) must not
    forge a status row in the pretty listing: the control char is escaped and
    the raw byte never reaches stdout. Cache-redteam confirming-round P2."""
    with mock_aws():
        client, bucket = _versioned_bucket()
        hostile = "iso/a.bin\n  ✓ pulled 9999 file(s) \x1b[32mOK\x1b[0m"
        client.put_object(
            Bucket=bucket, Key=f"lab/proj/cache/{hostile}", Body=b"x" * 3,
        )
        _use_factory(monkeypatch, client)
        _load_config(monkeypatch, Config())
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["cache", "ls", "--path", str(clone)])
        out = capsys.readouterr().out
        assert rc == 0
        # The raw forged status line must NOT appear as its own rendered row.
        assert "\n  ✓ pulled 9999" not in out
        # No raw ESC byte reaches the terminal.
        assert "\x1b" not in out
        # The key still renders — with its control bytes inertly escaped.
        assert "iso/a.bin\\n" in out
        assert "\\x1b[32m" in out


def test_cache_ls_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    with mock_aws():
        client, bucket = _versioned_bucket()
        _seed(client, bucket, tmp_path, {"b.bin": b"b" * 5}, monkeypatch)
        capsys.readouterr()
        clone = _project(tmp_path / "cloneroot", bucket)
        rc = cli.main(["--json", "cache", "ls", "--path", str(clone)])
        payload = json.loads(capsys.readouterr().out.strip())
        assert rc == 0
        assert payload["name"] == "cache"
        assert any(o["key"] == "b.bin" for o in payload["objects"])
