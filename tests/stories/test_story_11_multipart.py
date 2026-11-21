"""Story 11: A large file pushes in parts and imports byte for byte (Holds).

First case:

- Given: a producer with a file past the multipart threshold (100 MiB + 7
  bytes of random data).
- When: I run `mintd data push`, then a teammate runs `mintd data import`.
- Then: the file uploads in parts, is verified, and imports byte for byte.
- Pass: storage holds the pinned version, with the ETag of dvc's 50 MiB parts,
  when ✓ prints; the teammate's landed bytes hash to the pointer's md5; exit 0.
- Catches: version-id or checksum bugs only a multipart object shows.

Second case, the same bytes sent in other parts:

- Given: the producer's cache holds the file, and the object at its path holds
  the same bytes, uploaded in boto3's 8 MiB parts, so its ETag (`-13`) is not
  the one mintd recomputes from the cache (`-3`). Twice: the pointer has no
  version id, and the pointer is pinned to that 8 MiB version.
- When: I run `mintd data push`, then a teammate imports.
- Then: mintd cannot prove the object from its ETag, so it uploads the file
  again before it prints ✓.
- Pass: the version the pointer then pins is a new one, with the 50 MiB-part
  ETag; exit 0; the teammate's landed bytes are the producer's.
- Catches: a push that takes an object with the right bytes but other parts as
  proven, leaving a pin mintd never proved.

Where it runs: on the emulator by default, and in CI. On the lab's test bucket
only by hand:
`MINTD_STORY_BUCKET=cooper-mintd-test AWS_SHARED_CREDENTIALS_FILE=~/.aws/mintd-test-credentials AWS_CONFIG_FILE=~/.aws/mintd-test-config uv run pytest tests/stories/test_story_11_multipart.py -rA`.
The key is checked the way the live tier checks its own, only that bucket is
accepted, every write goes under `runs/<run id>/<test>/`, and nothing is ever
deleted or cleaned up: the bucket's lifecycle rule expires `runs/`. One run
writes 5 object versions under 3 keys, 524,288,035 bytes, in 35 part uploads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import boto3
import pytest
import yaml

from tests._harness import HarnessError
from tests._harness.expect import assert_landed_match_pointers, md5
from tests._harness.git import _git
from tests._harness.live import BUCKET_VAR, TEST_PROFILE, profile_endpoint, readonly_credentials, story_bucket
from tests._harness.payload import publish_payload
from tests._harness.producer import build_local_producer, register
from tests._harness.s3 import BUCKET

NAME = "harness-multipart"  # a made-up product
BIG = "data/final/big.bin"  # a per-file pointer under the default data/final/ primary: the ordinary lab shape
SIZE = 100 * 2**20 + 7  # 50 MiB + 50 MiB + 7 bytes in dvc's parts
DVC_PART, BOTO3_PART = 50 * 2**20, 8 * 2**20
# Read at import, before tests/conftest.py's autouse fixture points AWS_SHARED_CREDENTIALS_FILE at nothing.
_OPERATOR_ENV = dict(os.environ)
RUN_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


@dataclasses.dataclass
class Target:
    bucket: str
    key: str
    client: Any
    remote: dict[str, str]  # the producer's dvc remote


def _etag(data: bytes, part: int) -> str:
    """S3's ETag for `data` sent in `part`-byte parts: the md5 of the parts' md5s, then `-<count>`."""
    view = memoryview(data)
    digests = [hashlib.md5(view[i:i + part], usedforsecurity=False).digest() for i in range(0, len(data), part)]
    return f"{hashlib.md5(b''.join(digests), usedforsecurity=False).hexdigest()}-{len(digests)}"


def _send_in_parts(target: Target, data: bytes, part: int) -> dict:
    """Upload `data` in `part`-byte parts by hand: the transfer manager's failure path would send an abort."""
    mpu = target.client.create_multipart_upload(Bucket=target.bucket, Key=target.key)
    parts = [
        {"PartNumber": n, "ETag": target.client.upload_part(
            Bucket=target.bucket, Key=target.key, UploadId=mpu["UploadId"], PartNumber=n,
            Body=data[off:off + part])["ETag"]}
        for n, off in enumerate(range(0, len(data), part), start=1)
    ]
    return target.client.complete_multipart_upload(Bucket=target.bucket, Key=target.key, UploadId=mpu["UploadId"],
                                                   MultipartUpload={"Parts": parts})


def _push(mintd, producer) -> None:
    run = mintd("data", "push", cwd=producer.work, json=False)
    said = "".join(run.stderr.split())  # the console wraps at 80 columns, mid-word too
    assert run.returncode == 0 and "✓1file(s)verified" in said, run.stderr


def _pin(producer) -> dict:
    """The pin a teammate sees: the pointer as pushed to git."""
    return yaml.safe_load(_git(["show", f"main:{BIG}.dvc"], cwd=producer.bare))["outs"][0]["cloud"]["storage"]


def _holds_and_imports(target: Target, pin: dict, payload: bytes, mintd, fresh_project) -> None:
    head = target.client.head_object(Bucket=target.bucket, Key=target.key, VersionId=pin["version_id"])
    assert (head["ETag"].strip('"'), head["ContentLength"]) == (_etag(payload, DVC_PART), SIZE)
    project = fresh_project()
    run = mintd("data", "import", NAME, cwd=project)
    assert run.returncode == 0, run.stderr
    landed = assert_landed_match_pointers(project)  # the folder import brings dvc's .gitignore too: not data
    assert {k: v for k, v in landed.items() if not k.endswith(".gitignore")} == {
        f"data/imports/data_{NAME}/{BIG}": md5(payload)}


@pytest.fixture
def payload() -> bytes:
    return os.urandom(SIZE)


@pytest.fixture
def target(request, tmp_path, monkeypatch, mintd):
    folder = re.sub(r"\W", "_", request.node.name)  # the whole test id: tmp_path's name is cut to 30 characters
    prefix = f"runs/{RUN_ID}/{folder}"  # one folder per test, each param its own
    bucket = story_bucket(_OPERATOR_ENV.get(BUCKET_VAR))
    if bucket is None:  # the emulator
        s3 = request.getfixturevalue("s3_server")
        yield Target(BUCKET, f"{prefix}/{BIG}", s3.client,
                     {"url": f"s3://{BUCKET}/{prefix}", "endpointurl": s3.endpoint, "version_aware": "true"})
        # Every object PUT the emulator saw (mintd's, dvc's and the story's, parts included) is in this test's
        # folder. Spelled out again, not via `prefix`, so a mutation of `prefix` cannot also move the check.
        stray = [p for p in s3.puts if not p.startswith(f"/{BUCKET}/runs/{RUN_ID}/{folder}/")]
        if stray:
            raise HarnessError(f"PUTs outside runs/{RUN_ID}/{folder}/: {stray}")
        return
    creds = readonly_credentials(_OPERATOR_ENV, tmp_path / "aws", profile=TEST_PROFILE)
    request.addfinalizer(lambda: creds.unlink(missing_ok=True))
    endpoint = profile_endpoint(creds)
    for k in [k for k in os.environ if k.startswith(("AWS_", "BOTO_")) and k != "AWS_CA_BUNDLE"]:
        monkeypatch.delenv(k)  # in this process too, only the copy's key, region and endpoint count
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(creds.with_name("config")))
    (tmp_path / "boto").touch()  # BOTO_CONFIG: an empty file, as the live tier sets it
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"):
        del mintd.env[k]  # the fixture's emulator keys and region
    mintd.env.update(AWS_SHARED_CREDENTIALS_FILE=str(creds), AWS_CONFIG_FILE=str(creds.with_name("config")),
                     BOTO_CONFIG=str(tmp_path / "boto"), AWS_EC2_METADATA_DISABLED="true")
    if _OPERATOR_ENV.get("AWS_CA_BUNDLE"):  # trusted certificates, not a key: the live tier's one exception
        mintd.env["AWS_CA_BUNDLE"] = _OPERATOR_ENV["AWS_CA_BUNDLE"]
    mintd.timeout = None  # a real upload takes as long as the link does; Ctrl-C still stops it
    client = boto3.Session(profile_name="mintd").client("s3", endpoint_url=endpoint)
    print(f"story 11 writes under s3://{bucket}/{prefix}/")  # shown by -rA, for the PR's record
    yield Target(bucket, f"{prefix}/{BIG}", client,
                 {"url": f"s3://{bucket}/{prefix}", "endpointurl": endpoint, "version_aware": "true",
                  "profile": "mintd"})


@pytest.fixture
def producer(target, payload, mintd, remote_registry_empty, tmp_path):
    producer = build_local_producer(tmp_path / "prod", remote=target.remote)
    meta = producer.metadata()
    meta["project"].update(name=NAME, full_name=f"data_{NAME}")
    meta["repository"]["github_url"] = producer.url
    producer._write_metadata(meta)
    publish_payload(producer.work, producer.remote, {BIG: payload}, push=False)  # in dvc's cache; nothing on S3
    producer._commit_and_push(f"track {BIG}")
    register(remote_registry_empty, producer.metadata(), tmp_path / "registry-work")
    mintd.config.write_text(f"registry_url: {remote_registry_empty}\ncache_dir: {tmp_path / 'mintd-cache'}\n",
                            encoding="utf-8")  # a fresh cache_dir: an empty proven-versions memo
    return producer


def test_a_file_past_the_multipart_threshold_pushes_and_imports_byte_for_byte(
        target, producer, payload, mintd, fresh_project) -> None:
    """Mutation that turns it red: `_pin_proof._part_size` returning 8 MiB (the gate can't prove dvc's
    50 MiB-part upload: exit 1, no ✓)."""
    _push(mintd, producer)
    _holds_and_imports(target, _pin(producer), payload, mintd, fresh_project)


@pytest.mark.parametrize("pinned", [False, True], ids=["no-version-id", "pinned-to-8-mib-parts"])
def test_the_same_bytes_in_other_parts_are_uploaded_again_before_the_tick(
        target, producer, payload, mintd, fresh_project, pinned) -> None:
    """Mutation that turns both red: `_pin_proof.prove` returning "ok" for a multipart ETag whenever the
    cache holds the file, without comparing ETags (the 8 MiB version is kept or re-attached; nothing uploads)."""
    sent = _send_in_parts(target, payload, BOTO3_PART)
    if sent["ETag"].strip('"') != _etag(payload, BOTO3_PART) or not sent.get("VersionId"):
        raise HarnessError("the 8 MiB-part upload did not land as a -13 version")  # the harness broke, not mintd
    if pinned:
        pointer = producer.work / f"{BIG}.dvc"
        doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
        doc["outs"][0]["cloud"] = {"storage": {"etag": sent["ETag"].strip('"'), "version_id": sent["VersionId"]}}
        pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        producer._commit_and_push("pin the 8 MiB-part upload")
    _push(mintd, producer)
    pin = _pin(producer)
    assert pin["version_id"] != sent["VersionId"], "the push took the 8 MiB-part object as proven"
    _holds_and_imports(target, pin, payload, mintd, fresh_project)
