"""`mintd data push` with a real dvc subprocess against an S3 server.

S3 is moto's server app behind a small WSGI layer that refuses
ListObjectVersions with 403, as the lab's `mintd` key is refused, and counts
PUTs. dvc runs for real, so these tests see what dvc does to the remote and
to the pointer files, which the fakes elsewhere only describe.
"""

from __future__ import annotations

import os
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs

import boto3
import pytest
import yaml

from mintd import cli
from tests._harness.dvc import real_dvc  # noqa: F401  (fixture)
from tests._harness.git import _git

BUCKET = "journey-bucket"


@pytest.fixture
def s3_server():
    from moto.moto_server.werkzeug_app import DomainDispatcherApplication, create_backend_app
    from werkzeug.serving import make_server

    app = DomainDispatcherApplication(create_backend_app)
    puts: list[str] = []

    def guarded(environ, start_response):
        method, query = environ["REQUEST_METHOD"], parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        if method == "GET" and "versions" in query:
            start_response("403 Forbidden", [("Content-Type", "application/xml")])
            return [b'<?xml version="1.0"?><Error><Code>AccessDenied</Code><Message>no</Message></Error>']
        if method == "PUT" and environ.get("PATH_INFO", "/").strip("/").count("/") >= 1:
            puts.append(environ["PATH_INFO"])
        return app(environ, start_response)

    server = make_server("127.0.0.1", 0, guarded, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id="test", aws_secret_access_key="test")
    # moto's backends are process-wide: start each test from an empty server.
    urllib.request.urlopen(urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")).close()
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    puts.clear()
    try:
        yield endpoint, s3, puts
    finally:
        server.shutdown()


@pytest.fixture
def producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_dvc, s3_server):  # noqa: F811
    """A git repo with a dvc project on a version-aware remote at the server:
    `data/raw/` (two files) tracked by `data/raw.dvc`, and stage `build`
    (wdir `code`) writing `data/final/a.csv`; committed and pushed to a bare
    upstream, no data pushed yet."""
    endpoint, s3, puts = s3_server
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
                 "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
        real_dvc.env[k] = v  # the harness copied the environment before these were set
    # mintd's own dvc subprocesses share the harness's redirected dvc dirs, so
    # a stale site cache left by a raw `dvc push` is the one mintd would read.
    for k in ("HOME", "USERPROFILE", "DVC_GLOBAL_CONFIG_DIR", "DVC_SYSTEM_CONFIG_DIR", "DVC_SITE_CACHE_DIR"):
        monkeypatch.setenv(k, real_dvc.env[k])
    config_dir = tmp_path / "mintd-config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(f"cache_dir: {tmp_path / 'mintd-cache'}\n", encoding="utf-8")
    monkeypatch.setenv("MINTD_CONFIG_DIR", str(config_dir))

    proj = tmp_path / "proj"
    proj.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=proj)
    _git(["config", "user.email", "t@t"], cwd=proj)
    _git(["config", "user.name", "T"], cwd=proj)
    real_dvc(["init", "-q"], cwd=proj, check=True)
    _remote(proj, real_dvc, endpoint, "lab/a")
    (proj / "data" / "raw").mkdir(parents=True)
    (proj / "data" / "raw" / "x.csv").write_text("x\n")
    (proj / "data" / "raw" / "y.csv").write_text("y\n")
    real_dvc(["add", "-q", "data/raw"], cwd=proj, check=True)
    (proj / "code").mkdir()
    write = "import pathlib; p=pathlib.Path('../data/final'); p.mkdir(parents=True, exist_ok=True); (p/'a.csv').write_text('a\\n')"
    (proj / "dvc.yaml").write_text(yaml.safe_dump({"stages": {"build": {
        "wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": ["../data/final/a.csv"],
    }}}, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=proj, check=True)
    _git(["add", "-A"], cwd=proj)
    _git(["commit", "-q", "-m", "track"], cwd=proj)
    origin = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)])
    _git(["remote", "add", "origin", str(origin)], cwd=proj)
    _git(["push", "-q", "-u", "origin", "main"], cwd=proj)
    monkeypatch.chdir(proj)
    return proj


def _remote(proj: Path, real_dvc, endpoint: str, prefix: str) -> None:  # noqa: F811
    real_dvc(["remote", "add", "-d", "-f", "storage", f"s3://{BUCKET}/{prefix}"], cwd=proj, check=True)
    real_dvc(["remote", "modify", "storage", "endpointurl", endpoint], cwd=proj, check=True)
    real_dvc(["remote", "modify", "storage", "version_aware", "true"], cwd=proj, check=True)


def _ids(proj: Path) -> dict[str, str]:
    """path -> version ID, from data/raw.dvc and dvc.lock."""
    got: dict[str, str] = {}
    raw = yaml.safe_load((proj / "data" / "raw.dvc").read_text())["outs"][0]
    for f in raw.get("files") or []:
        if (f.get("cloud") or {}).get("storage"):
            got[f"data/raw/{f['relpath']}"] = f["cloud"]["storage"]["version_id"]
    out = yaml.safe_load((proj / "dvc.lock").read_text())["stages"]["build"]["outs"][0]
    if (out.get("cloud") or {}).get("storage"):
        got["data/final/a.csv"] = out["cloud"]["storage"]["version_id"]
    return got


def _imports_everything(tmp_path: Path, real_dvc, name: str) -> None:  # noqa: F811
    """From an empty directory, `dvc import` of the pushed commit lands every
    file with its bytes: the check that catches a wrong S3 key, which a key
    bug shared by the repair and the gate would not."""
    consumer = tmp_path / name
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    origin = str(tmp_path / "origin.git")
    for path, body in (("data/raw", None), ("data/final/a.csv", "a\n")):
        r = real_dvc(["import", "-q", origin, path, "-o", Path(path).name], cwd=consumer)
        assert r.returncode == 0, r.stderr
        if body is not None:
            assert (consumer / Path(path).name).read_text() == body
    assert (consumer / "raw" / "x.csv").read_text() == "x\n"
    assert (consumer / "raw" / "y.csv").read_text() == "y\n"


def _out(capsys) -> str:
    return " ".join(capsys.readouterr().err.split())


def test_push_pins_verifies_commits_and_an_import_lands_everything(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    _, _, puts = s3_server
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert "✓ 3 file(s) verified on s3://journey-bucket/lab/a" in err
    assert set(_ids(producer)) == {"data/raw/x.csv", "data/raw/y.csv", "data/final/a.csv"}
    assert len(puts) == 3
    _imports_everything(tmp_path, real_dvc, "consumer")


def _drop_ids(proj: Path) -> None:
    """What `dvc commit -f clean` did to cms-pdc's dvc.lock: same md5s, no
    cloud blocks. (dvc 3.67.1's `dvc commit -f` keeps them on these simple
    stages, so the rewrite is applied directly.)"""
    for rel in ("data/raw.dvc", "dvc.lock"):
        doc = yaml.safe_load((proj / rel).read_text())
        bodies = [doc] if rel.endswith(".dvc") else list(doc["stages"].values())
        for body in bodies:
            for out in body["outs"]:
                out.pop("cloud", None)
                for f in out.get("files") or []:
                    f.pop("cloud", None)
        (proj / rel).write_text(yaml.safe_dump(doc, sort_keys=False))


def test_a_rewrite_that_drops_the_ids_costs_no_upload(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    _, _, puts = s3_server
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()
    first = _ids(producer)
    _drop_ids(producer)
    _git(["commit", "-q", "-am", "dvc commit -f"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert _ids(producer) == first
    assert all(f"re-attached {p} (no ID)" in err for p in first)
    assert "dvc.lock lost these IDs" in err
    _imports_everything(tmp_path, real_dvc, "consumer")


def test_a_folder_stage_dvc_commit_f_rewrote_costs_no_upload(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    """`dvc commit -f` (and `dvc repro` of an unchanged output) writes a folder
    out back as `md5: <tree>.dir`, with no files list and no IDs. mintd
    re-attaches every file from S3 as the files list DVC writes after a push,
    so nothing is uploaded again and DVC still sees the stage as up to date."""
    _, _, puts = s3_server
    write = ("import pathlib; p=pathlib.Path('../data/out'); p.mkdir(parents=True, exist_ok=True); "
             "[(p/n).write_text(n) for n in ('a.csv', 'b.csv', 'c.csv')]")
    doc = yaml.safe_load((producer / "dvc.yaml").read_text())
    doc["stages"]["many"] = {"wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": ["../data/out"]}
    (producer / "dvc.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "folder stage"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()

    real_dvc(["commit", "-f", "-q", "dvc.yaml:many"], cwd=producer, check=True)
    (rec,) = yaml.safe_load((producer / "dvc.lock").read_text())["stages"]["many"]["outs"]
    assert "files" not in rec and rec["md5"].endswith(".dir")  # what DVC does to a folder out
    _git(["commit", "-q", "-am", "dvc commit -f many"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert all(f"re-attached data/out/{n} (no ID)" in err for n in ("a.csv", "b.csv", "c.csv"))
    assert "dvc.lock lost these IDs" in err
    assert "up to date" in real_dvc(["status"], cwd=producer).stdout
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    r = real_dvc(["import", "-q", str(tmp_path / "origin.git"), "data/out", "-o", "out"], cwd=consumer)
    assert r.returncode == 0, r.stderr
    assert sorted(f.read_text() for f in (consumer / "out").iterdir()) == ["a.csv", "b.csv", "c.csv"]


def test_a_moved_remote_is_reuploaded_despite_a_stale_dvc_site_cache(
    producer, s3_server, real_dvc, tmp_path, capsys,  # noqa: F811
) -> None:
    """The cms-pdc case. The files are pinned at prefix A, then the remote
    moves to B. A raw `dvc push` keeps the IDs that point at A, can't list
    versions, uploads nothing, and leaves its md5-keyed record of what is on
    B in the site cache (it also writes data/raw.dvc back with an empty file
    list, undone here from git). `mintd data push` strips the stale IDs; with
    that site cache DVC would still upload nothing, since no md5 changed, so
    mintd's push runs with a fresh one, uploads to B and verifies."""
    endpoint, _, puts = s3_server
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    capsys.readouterr()
    _remote(producer, real_dvc, endpoint, "lab/b")
    _git(["commit", "-q", "-am", "move the remote"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()
    real_dvc(["push", "-q"], cwd=producer)
    assert puts == []  # the defect: nothing reached the new prefix
    _git(["checkout", "-q", "--", "data/raw.dvc", "dvc.lock"], cwd=producer)

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert "the remote has probably moved" in err
    assert "✓ 3 file(s) verified on s3://journey-bucket/lab/b (3 uploaded and verified)" in err
    assert sorted(p.split("/", 2)[2] for p in puts) == ["lab/b/data/final/a.csv", "lab/b/data/raw/x.csv", "lab/b/data/raw/y.csv"]
    _imports_everything(tmp_path, real_dvc, "consumer")


@pytest.mark.skipif(os.name == "nt", reason="the scope census is platform-independent")
def test_push_false_and_cache_false_outs_are_outside_what_push_checks(producer, s3_server, real_dvc, capsys):  # noqa: F811
    """mintd's scope is DVC's own: what a real `dvc push` uploads."""
    _, _, puts = s3_server
    write = "import pathlib; [pathlib.Path(n).write_text(n) for n in ('../data/keep.csv', '../data/tmp.csv')]"
    doc = yaml.safe_load((producer / "dvc.yaml").read_text())
    doc["stages"]["side"] = {"wdir": "code", "cmd": f'"{sys.executable}" -c "{write}"', "outs": [
        {"../data/keep.csv": {"push": False}}, {"../data/tmp.csv": {"cache": False}},
    ]}
    (producer / "dvc.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    real_dvc(["repro", "-q"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "side outputs"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    assert cli.main(["data", "push"]) == 0, _out(capsys)
    assert "✓ 3 file(s) verified" in _out(capsys)
    assert not any("keep.csv" in p or "tmp.csv" in p for p in puts)


def test_a_file_sent_in_parts_is_proven_reattached_and_imported(producer, s3_server, real_dvc, tmp_path, capsys):  # noqa: F811
    """A file of 100 MiB or more goes up in 50 MiB parts, so its ETag isn't its
    md5. mintd proves it by recomputing that ETag from the cache, both after
    the first push and when a rewrite loses the ID, which costs no upload."""
    _, s3, puts = s3_server
    big = os.urandom(100 * 2**20 + 7)
    (producer / "data" / "big.bin").write_bytes(big)
    real_dvc(["add", "-q", "data/big.bin"], cwd=producer, check=True)
    _git(["add", "-A"], cwd=producer)
    _git(["commit", "-q", "-m", "big"], cwd=producer)
    _git(["push", "-q"], cwd=producer)

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    assert "✓ 4 file(s) verified" in _out(capsys)
    pointer = producer / "data" / "big.bin.dvc"
    pin = yaml.safe_load(pointer.read_text())["outs"][0]["cloud"]["storage"]
    etag = s3.head_object(Bucket=BUCKET, Key="lab/a/data/big.bin", VersionId=pin["version_id"])["ETag"]
    assert etag.strip('"').endswith("-3")  # 50 MiB + 50 MiB + 7 bytes: really sent in parts

    doc = yaml.safe_load(pointer.read_text())
    doc["outs"][0].pop("cloud")
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False))
    _git(["commit", "-q", "-am", "dvc add again, without the ID"], cwd=producer)
    _git(["push", "-q"], cwd=producer)
    puts.clear()

    assert cli.main(["data", "push"]) == 0, _out(capsys)
    err = _out(capsys)
    assert puts == []
    assert "re-attached data/big.bin (no ID)" in err
    assert yaml.safe_load(pointer.read_text())["outs"][0]["cloud"]["storage"]["version_id"] == pin["version_id"]
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=consumer)
    real_dvc(["init", "-q"], cwd=consumer, check=True)
    r = real_dvc(["import", "-q", str(tmp_path / "origin.git"), "data/big.bin", "-o", "big.bin"], cwd=consumer)
    assert r.returncode == 0, r.stderr
    assert (consumer / "big.bin").read_bytes() == big
