"""Story 3: I push, and a teammate gets my data (Holds).

- Given: a producer with new data, and a teammate with a fresh project.
- When: I run `mintd data push`, then the teammate imports.
- Then: the teammate's bytes equal mine.
- Pass:
  - storage holds every version the pointers name before ✓ prints, both for
    `.dvc` pointers and for outs in `dvc.lock`;
  - a push where an upload failed prints no ✓ and exits non-zero.
- Catches: ✓ printed while storage was missing a pinned version (the bug
  fixed in #70).

Notes:
- Pass line 1 is checked once the push has exited, from the pins git holds.
  That is equivalent: nothing uploads after the ✓.
- Three cases extend the Given beyond the story's listed failure injection
  (the maintainer's call, 2026-11-19), so the Catches line can go red: the
  remote moved since the last push; a stage group pushed by name; a pin whose
  bytes only their size vouches for.
- Mutations that turn it red: undoing #70's storage gate (`prepared = None`
  in `data_push`) turns `remote-moved` red; on main the commit's pointer check
  then refuses the push, so no false ✓ prints either way. `_push_verify`
  matching a foreach group only by its plain name turns the by-name case red;
  a size-only pin judged proven turns that case red; a push that leaves its
  pointers uncommitted, or that reads every file's pinned version id as absent
  (`_fast_sync_ops._extract_version_id_from_file_entry`), turns the
  second-release case red; the failed upload, see its test.
"""

from __future__ import annotations

import hashlib
import sys

import pytest
import yaml

from tests._harness import HarnessError
from tests._harness.expect import assert_landed_match_pointers, assert_storage_holds_pins, md5
from tests._harness.payload import publish_payload, publish_pipeline_payload
from tests._harness.s3 import BUCKET

MOVED = "lab/harness-demo-moved"
WRITE = "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
STAGE = yaml.safe_dump({"stages": {"build": {
    "cmd": f'"{sys.executable}" -c "{WRITE}" data/built/out.csv built', "outs": ["data/built"],
}}})
FAN = yaml.safe_dump({"stages": {"fan": {"foreach": ["x", "y"], "do": {
    "cmd": f'"{sys.executable}" -c "{WRITE}" data/fan/${{item}}.csv fan-${{item}}', "outs": ["data/fan/${item}.csv"],
}}}})


def _new_data(product, s3_server, *, moved: bool = False) -> str:
    """New data, tracked but not pushed: a new file in the primary and a
    pipeline output. With `moved`, the remote has moved to another prefix
    since the last push. Returns the prefix the push should land under."""
    producer = product.producer
    if moved:
        producer.remote = {**producer.remote, "url": f"s3://{BUCKET}/{MOVED}"}
    publish_pipeline_payload(producer.work, producer.remote, STAGE, push=False)  # writes .dvc/config's url too
    publish_payload(producer.work, producer.remote, {"data/final": {**product.outs["data/final"], "d.csv": b"delta\n"}},
                    push=False)
    producer.add_output("data/built/")  # commits and pushes dvc.yaml, dvc.lock, the pointers, .dvc/config
    if s3_server.puts:  # the product fixture cleared them: nothing new may be on S3 before mintd pushes it
        raise HarnessError(f"the Given uploaded {s3_server.puts}: the new data must start unpushed")
    return MOVED if moved else product.prefix


def _push(mintd, product, *args: str):
    run = mintd("data", "push", *args, cwd=product.producer.work, json=False)
    return run, "✓" in run.stdout + run.stderr


@pytest.mark.parametrize("moved", [False, True], ids=["new-data", "remote-moved"])
def test_push_then_a_teammate_imports_my_bytes(product, mintd, s3_server, fresh_project, moved: bool) -> None:
    prefix = _new_data(product, s3_server, moved=moved)

    run, ticked = _push(mintd, product)

    assert run.returncode == 0 and ticked, run.stderr
    # every file of data/final, data/extra and data/raw (.dvc pointers) and data/built (dvc.lock)
    assert assert_storage_holds_pins(s3_server, product.producer.bare, prefix) == 6
    project = fresh_project()
    got = mintd("data", "import", product.name, "--all", cwd=project)
    assert got.returncode == 0, got.stderr
    mine = {**{f"data/final/{k}": v for k, v in {**product.outs["data/final"], "d.csv": b"delta\n"}.items()},
            **{f"data/extra/{k}": v for k, v in product.outs["data/extra"].items()}, "data/built/out.csv": b"built"}
    root = f"data/imports/data_{product.name}"
    # data/raw, tracked but declared by no output, does not land: --all brings declared outputs today; add it
    # when story 1's `[all]` Target passes
    assert assert_landed_match_pointers(project) == {f"{root}/{k}": md5(v) for k, v in mine.items()}


def test_each_release_brings_the_bytes_it_pinned(product, mintd, s3_server, fresh_project) -> None:
    """A file changed since the last release: its key holds two versions, and
    each release's pins bring that release's bytes, the old one by `--rev`.
    The imports run `dvc import`, so dvc's own transfer fetches each pinned
    version; mintd's fast lane (clone and pull) is not what serves them."""
    producer = product.producer
    first, old = producer.head_sha, product.outs["data/final"]["a.csv"]  # the product fixture's release
    new = b"alpha, revised\n"
    publish_payload(producer.work, producer.remote, {"data/final": {**product.outs["data/final"], "a.csv": new}},
                    push=False)
    producer._commit_and_push("revise a.csv")
    if s3_server.puts:  # the product fixture cleared them: mintd must be the one to upload the revision
        raise HarnessError(f"the Given uploaded {s3_server.puts}: the revision must start unpushed")

    run, ticked = _push(mintd, product)

    assert run.returncode == 0 and ticked, run.stderr
    for rev in (first, "main"):  # storage still holds the first release's version too
        assert assert_storage_holds_pins(s3_server, producer.bare, product.prefix, rev=rev) == 4
    root = f"data/imports/data_{product.name}/data/final"
    for name, args, body in (("teammate-new", (), new), ("teammate-old", ("--rev", first), old)):
        project = fresh_project(name)
        got = mintd("data", "import", product.name, *args, cwd=project)
        assert got.returncode == 0, got.stderr
        landed = assert_landed_match_pointers(project)
        assert landed[f"{root}/a.csv"] == md5(body), (name, landed)


def test_a_stage_pushed_by_name_is_held_and_reaches_the_teammate(product, mintd, s3_server, fresh_project) -> None:
    producer = product.producer
    publish_pipeline_payload(producer.work, producer.remote, FAN, push=False)
    producer._commit_and_push("a foreach group")  # dvc.yaml committed, or the stage is out of push's scope

    run, ticked = _push(mintd, product, "fan")

    assert run.returncode == 0 and ticked, run.stderr
    # data/final, data/extra and data/raw's files (.dvc pointers), and fan@x and fan@y (dvc.lock)
    assert assert_storage_holds_pins(s3_server, producer.bare, product.prefix) == 6
    for item in ("x", "y"):
        project = fresh_project(f"teammate-{item}")
        got = mintd("data", "import", product.name, "--path", f"data/fan/{item}.csv", cwd=project)
        assert got.returncode == 0, got.stderr
        landed = assert_landed_match_pointers(project)
        assert landed == {f"data/imports/data_{product.name}/data/fan/{item}.csv": md5(f"fan-{item}".encode())}


def test_no_tick_over_a_pin_whose_bytes_nobody_checked(product, mintd, s3_server) -> None:
    """A pin to an object sent in parts, whose md5 names other bytes of the
    same size, with the file in neither the work tree nor the cache: only its
    size vouches for it, so the push must not print ✓."""
    work, size = product.producer.work, 5 * 2**20 + 1
    stored, claimed = b"w" * size, b"b" * size
    s3, key = s3_server.client, f"{product.prefix}/data/big.bin"
    upload = s3.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
    parts = [{"PartNumber": n, "ETag": s3.upload_part(Bucket=BUCKET, Key=key, UploadId=upload, PartNumber=n,
                                                      Body=chunk)["ETag"]}
             for n, chunk in ((1, stored[: 5 * 2**20]), (2, stored[5 * 2**20:]))]
    done = s3.complete_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload, MultipartUpload={"Parts": parts})
    (work / "data" / "big.bin.dvc").write_text(yaml.safe_dump({"outs": [{
        "md5": hashlib.md5(claimed, usedforsecurity=False).hexdigest(), "size": size, "hash": "md5", "path": "big.bin",
        "cloud": {"storage": {"etag": done["ETag"].strip('"'), "version_id": done["VersionId"]}},
    }]}), encoding="utf-8")

    run, ticked = _push(mintd, product)

    assert run.returncode != 0 and not ticked, run.stdout + run.stderr


def test_a_push_whose_upload_failed_prints_no_tick(product, mintd, s3_server) -> None:
    """Every PUT after the first is refused. Several guards stop the ✓ on
    main: dvc's failed exit (`DvcPushError`), the gate's verdicts and its check
    that every pointer kept its files, and the commit's pointer check. Undoing
    any one leaves this green; which upload is refused varies, so what two
    undone guards leave varies too. Mutation that turns it red: a push that
    reports ✓ and exits 0 whatever failed."""
    _new_data(product, s3_server)
    s3_server.release.set()  # a refused PUT answers 503 at once
    s3_server.hold_puts_after = 1
    mintd.env["AWS_MAX_ATTEMPTS"] = "1"

    run, ticked = _push(mintd, product)

    if not s3_server.holding.is_set():
        raise HarnessError("no upload was refused: the push did not reach a second PUT")
    assert run.returncode != 0 and not ticked, run.stdout + run.stderr
