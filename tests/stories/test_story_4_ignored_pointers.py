"""Story 4: Ignored pointers stay ignored (Holds).

- Given: a `.dvc` pointer inside a folder that `.gitignore` excludes.
- When: I run push, pull or check.
- Then: all three skip it, the same way DVC does.
- Pass: no upload, download or finding for it; every other pointer is handled
  normally.
- Catches: ignored pointers pushed, pulled or flagged (the bug fixed in #74).
"""

from __future__ import annotations

import yaml

from tests._harness.expect import md5, s3_state
from tests._harness.git import _git
from tests._harness.payload import publish_payload
from tests._harness.s3 import BUCKET

IGNORED = "data/ext/vendor"
CONFLICTED = "<<<<<<< HEAD\nouts:\n=======\n>>>>>>> theirs\n"  # a pointer a bad merge left unreadable


def _ignore_vendor(product, s3_server, unpushed: dict[str, bytes]) -> None:
    """Give the product an ignored folder holding one bait pointer per verb,
    plus `unpushed`, tracked but not uploaded. z.csv is pushed and pinned
    before the folder is ignored, so a pull that read it would GET it; y.csv
    is never pushed, so a push that read it would upload it or fail; w.csv.dvc
    is unreadable, so a check that read it would name it."""
    producer = product.producer
    producer.publish({f"{IGNORED}/z.csv": b"zeta\n"})
    # dvc refuses to add a path .gitignore already excludes: add first, ignore after.
    publish_payload(producer.work, producer.remote, {f"{IGNORED}/y.csv": b"why\n", **unpushed}, push=False)
    (producer.work / IGNORED / "w.csv.dvc").write_text(CONFLICTED, encoding="utf-8")
    meta = producer.metadata()
    # Without a full storage block, check exits 1 on storage_partial_dvc_only.
    meta["storage"] = {
        "provider": "s3", "bucket": BUCKET, "prefix": product.prefix, "endpoint": s3_server.endpoint,
        "versioning": True, "dvc": {"remote_name": "storage"},
    }
    producer._write_metadata(meta)
    (producer.work / "data" / "ext" / ".gitignore").write_text("vendor\n", encoding="utf-8")
    _git(["add", "-f", f"{IGNORED}/y.csv.dvc", f"{IGNORED}/w.csv.dvc"], cwd=producer.work)
    producer._commit_and_push("ignore data/ext/vendor")
    s3_server.puts.clear()
    s3_server.gets.clear()


def test_push_uploads_and_pins_all_but_the_ignored_pointers(product, mintd, s3_server) -> None:
    _ignore_vendor(product, s3_server, {"data/more.csv": b"more\n"})

    run = mintd("data", "push", cwd=product.producer.work)

    assert run.returncode == 0, run.stderr
    assert set(s3_state(s3_server, product.prefix).puts) == {"data/more.csv"}, "no upload for the ignored pointer"
    out = yaml.safe_load(_git(["show", "main:data/more.csv.dvc"], cwd=product.producer.bare))["outs"][0]
    pinned = s3_server.client.get_object(
        Bucket=BUCKET, Key=f"{product.prefix}/data/more.csv", VersionId=out["cloud"]["storage"]["version_id"],
    )
    assert pinned["Body"].read() == b"more\n"


def test_pull_lands_all_but_the_ignored_pointers(product, mintd, s3_server, tmp_path) -> None:
    _ignore_vendor(product, s3_server, {})
    clone = tmp_path / "teammate"
    _git(["clone", product.producer.url, str(clone)])

    run = mintd("data", "pull", cwd=clone)

    assert run.returncode == 0, run.stderr
    assert set(s3_state(s3_server, product.prefix).gets) == set(product.published()), "no download for z.csv"
    landed = {
        p.relative_to(clone).as_posix(): md5(p.read_bytes()) for p in (clone / "data").rglob("*")
        if p.is_file() and p.suffix != ".dvc" and p.name != ".gitignore"
    }
    assert landed == {k: md5(b) for k, b in product.published().items()}


def test_check_names_no_ignored_pointer(product, mintd, s3_server) -> None:
    _ignore_vendor(product, s3_server, {})
    work = product.producer.work

    run = mintd("check", cwd=work)

    assert run.returncode == 0, run.stdout + run.stderr
    assert IGNORED not in "".join((run.stdout + run.stderr).split())  # no wrap can hide the path
    # The other pointers are still read: an unreadable one outside the ignored folder is named.
    (work / "data" / "broken.csv.dvc").write_text(CONFLICTED, encoding="utf-8")
    again = mintd("check", cwd=work)
    said = "".join((again.stdout + again.stderr).split())
    assert "data/broken.csv.dvc" in said, again.stdout + again.stderr
    assert IGNORED not in said
