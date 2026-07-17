"""Consumer-side import-rescue lane (``_import_rescue_ops``).

Represents the field-incident shape the old suite could not: a ``version_aware``
producer bucket holding worktree-layout dvc-tracked objects with NO recorded
version_id and NO ``mintd-sha256`` metadata — one via a REAL moto multipart
upload (composite ``-N`` ETag != md5(body), the original field suspicion) and
one via a plain single-part ``put_object`` (ETag == md5, the issue-CORRECTION's
harsher shape). Serving BOTH pins that the fix keys on "no version_id recorded",
not on ETag shape.

The end-to-end test drives ``data_pull`` — the same function ``mintd data
clone`` / ``mintd data pull`` call — with a scripted ``_FakeDvcOps`` reproducing
dvc's field behavior and asserts both files land byte-correct.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from mintd import imports
from mintd._dvc_ops import DvcPullError
from mintd._fast_sync_ops import (
    DvcFileEntry,
    ensure_dir_manifest,
    is_cached,
)
from mintd._import_rescue_ops import (
    _entries_from_producer_pointer,
    _match_out_files,
    _resolve_producer,
)
from mintd._producer_git_ops import FetchError
from mintd.data_ops import data_pull
from mintd.model import FastPullResult
from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.fast_sync_ops import _FakeFastSyncOps
from tests._fakes.reporter import RecordingReporter

# The producer's committed .dvc/config: one version_aware S3 remote named
# "storage" at a worktree-layout prefix. url prefix "data_producer" + the
# import's dep path + the file relpath is the object key the rescue fetches.
PRODUCER_CONFIG = (
    "['remote \"storage\"']\n"
    "    url = s3://lab/data_producer\n"
    "    version_aware = true\n"
)

# dvc's field failure signature for the unpullable import (issue Symptom).
FIELD_SIG = (
    "Everything is up to date.\n"
    "WARNING: No file hash info found for "
    "'data/imports/producer/final/big.dta'. It won't be created.\n"
    "ERROR: failed to pull data from the cloud - Checkout failed for "
    "following targets:\ndata/imports/producer/final"
)

TARGET = "data/imports/producer/final.dvc"


# --------------------------------------------------------------------------
# Multipart helper — REAL moto multipart, self-asserting the composite shape.
# --------------------------------------------------------------------------

def _put_multipart(s3, bucket, key, body, part_size=5 * 1024 * 1024):
    """Upload ``body`` as a real multipart object. Self-asserts (for EVERY
    caller, not one test) that the resulting ETag is a composite ``-N`` form
    that does NOT equal ``md5(body)`` — any refactor back to ``put_object``
    then fails every dependent test loudly."""
    upload_id = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    parts = []
    for i, off in enumerate(range(0, len(body), part_size), start=1):
        chunk = body[off:off + part_size]
        etag = s3.upload_part(
            Bucket=bucket, Key=key, PartNumber=i, UploadId=upload_id, Body=chunk,
        )["ETag"]
        parts.append({"ETag": etag, "PartNumber": i})
    s3.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )
    etag = s3.head_object(Bucket=bucket, Key=key)["ETag"].strip('"')
    body_md5 = hashlib.md5(body).hexdigest()
    assert "-" in etag, f"expected composite multipart ETag, got {etag!r}"
    assert etag != body_md5, "multipart ETag must not equal md5(body)"


# --------------------------------------------------------------------------
# Fixtures / builders.
# --------------------------------------------------------------------------

@pytest.fixture
def producer_bucket():
    """A moto bucket ``lab`` (no versioning) held open for the whole test so
    the rescue's own ``_create_s3_client`` picks up the mocked endpoint."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="lab")
        yield s3, "lab"


def _seed_objects(s3, bucket, prefix="data_producer", output_path="data/final"):
    """Seed the two worktree-layout objects with NO version_id and NO
    mintd-sha256 metadata: big.dta via real multipart (composite ETag),
    small.dta via plain put_object (ETag == md5)."""
    big = b"BIG-" + b"x" * (10 * 1024 * 1024)
    small = b"small-data-payload-" * 200
    _put_multipart(s3, bucket, f"{prefix}/{output_path}/big.dta", big)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/{output_path}/small.dta", Body=small)
    return {
        "big": (big, hashlib.md5(big).hexdigest()),
        "small": (small, hashlib.md5(small).hexdigest()),
    }


def _entries(objs):
    return [
        DvcFileEntry(md5=objs["big"][1], relpath="big.dta", size=len(objs["big"][0])),
        DvcFileEntry(md5=objs["small"][1], relpath="small.dta", size=len(objs["small"][0])),
    ]


def _git_repo(root: Path, files: dict[str, str]) -> str:
    """Init a real on-disk git repo committing ``files`` (rel path -> text).
    Returns HEAD sha (the consumer import's rev_lock)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _dir_md5(entries, scratch: Path) -> str:
    """Compute the ``.dir`` md5 for ``entries`` via production's own writer,
    against a throwaway cache dir (does NOT plant into the consumer cache)."""
    return ensure_dir_manifest(scratch / ".dvc" / "cache", entries)


def _consumer(root: Path, producer_repo: str, sha: str, dir_md5: str, total: int) -> str:
    """Build a consumer project: own .dvc/config + a dvc-import .dvc pinning
    the producer at ``sha``. Returns the import target path."""
    (root / ".dvc").mkdir(parents=True, exist_ok=True)
    (root / ".dvc" / "config").write_text(
        "['remote \"origin\"']\n    url = s3://consumer/foo\n"
    )
    imp = root / "data" / "imports" / "producer"
    imp.mkdir(parents=True, exist_ok=True)
    (imp / "final.dvc").write_text(
        "outs:\n"
        f"  - md5: {dir_md5}\n"
        f"    size: {total}\n"
        "    path: final\n"
        "deps:\n"
        "  - path: data/final\n"
        "    repo:\n"
        f"      url: {producer_repo}\n"
        "      rev: main\n"
        f"      rev_lock: {sha}\n"
    )
    return TARGET


def _pointer_dvc(entries, *, remote_name="storage", version_ids=None) -> str:
    """Render a producer pointer ``.dvc`` (``data/final.dvc``) whose out lists
    ``files:`` entries — optionally with pinned cloud version_ids."""
    version_ids = version_ids or {}
    lines = ["outs:", "  - path: final", "    files:"]
    for e in entries:
        lines.append(f"      - relpath: {e.relpath}")
        lines.append(f"        md5: {e.md5}")
        lines.append(f"        size: {e.size}")
        vid = version_ids.get(e.relpath)
        if vid:
            lines.append("        cloud:")
            lines.append(f"          {remote_name}:")
            lines.append(f"            version_id: {vid}")
    return "\n".join(lines) + "\n"


def _lock_md5_only(dir_md5, total) -> str:
    """A producer ``dvc.lock`` in the field-incident shape (issue lines 47-49):
    the dir out records only ``md5: <hash>.dir`` — NO ``files:``/``cloud:``/
    version_id. The ``.dir`` md5 is the manifest's hash, not a blob's, so this
    pointer (source b) must not be keyed on for fetching; resolution has to
    fall through to the cached ``.dir`` manifest (source c)."""
    return (
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    outs:\n"
        "    - path: final\n"
        f"      md5: {dir_md5}\n"
        f"      size: {total}\n"
    )


def _run_pull(consumer, fake, target=TARGET, fallback=None, **kw):
    fast = _FakeFastSyncOps()
    fast.result = FastPullResult(
        success=False, synced_count=0,
        fallback_targets=fallback if fallback is not None else [target],
    )
    rep = RecordingReporter()
    summary = data_pull(
        consumer, targets=[target] if fallback is None else fallback,
        dvc_ops=fake, fast_sync_ops=fast, aws_profile_name=None, reporter=rep, **kw,
    )
    return summary, rep


# --------------------------------------------------------------------------
# Step 1 — representability (shape-pinning).
# --------------------------------------------------------------------------

def test_multipart_object_has_composite_etag_not_md5(producer_bucket):
    s3, bucket = producer_bucket
    body = b"y" * (10 * 1024 * 1024)
    _put_multipart(s3, bucket, "k/big", body)
    etag = s3.head_object(Bucket=bucket, Key="k/big")["ETag"].strip('"')
    assert etag.endswith("-2")
    assert etag != hashlib.md5(body).hexdigest()


def test_single_part_object_etag_equals_md5(producer_bucket):
    s3, bucket = producer_bucket
    body = b"small-body"
    s3.put_object(Bucket=bucket, Key="k/small", Body=body)
    etag = s3.head_object(Bucket=bucket, Key="k/small")["ETag"].strip('"')
    assert etag == hashlib.md5(body).hexdigest()


# --------------------------------------------------------------------------
# Step 2 — end-to-end researcher journey.
# --------------------------------------------------------------------------

def test_rescue_end_to_end_materializes_import(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    # Field state: a failed dvc pull left the .dir manifest cached (source c),
    # but none of the blobs.
    dir_md5 = ensure_dir_manifest(cache, entries)
    total = len(objs["big"][0]) + len(objs["small"][0])
    _consumer(consumer, str(producer), sha, dir_md5, total)

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    big_ws = consumer / "data/imports/producer/final/big.dta"
    small_ws = consumer / "data/imports/producer/final/small.dta"
    assert big_ws.read_bytes() == objs["big"][0]
    assert small_ws.read_bytes() == objs["small"][0]
    assert is_cached(cache, objs["big"][1])
    assert is_cached(cache, objs["small"][1])
    assert summary.error_count == 0
    infos = [e[1] for e in rep.events_of("info")]
    assert any(str(producer) in m and sha[:7] in m for m in infos)
    assert rep.events_of("error") == []


def test_rescue_falls_through_md5_only_producer_lock(producer_bucket, tmp_path):
    """The exact field-incident producer shape (issue lines 47-49): a committed
    ``dvc.lock`` whose dir out records only ``md5: <hash>.dir`` with NO
    ``files:``/``cloud:``/version_id. That pointer (source b) outranks the
    cached ``.dir`` manifest (source c); if the no-files branch returned the
    ``.dir`` md5 as a single blob entry it would shadow the manifest, ``is_cached``
    on the already-present ``.dir`` file would skip every fetch, and the rescue
    would seed no blobs. The fix skips dir outs so resolution falls through to
    the manifest, which carries the real per-file hashes."""
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    # Field state after the failed dvc pull: the .dir manifest (source c) is
    # cached, but none of the blobs.
    dir_md5 = ensure_dir_manifest(cache, entries)
    total = len(objs["big"][0]) + len(objs["small"][0])
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {
        ".dvc/config": PRODUCER_CONFIG,
        "dvc.lock": _lock_md5_only(dir_md5, total),
    })
    _consumer(consumer, str(producer), sha, dir_md5, total)

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    big_ws = consumer / "data/imports/producer/final/big.dta"
    small_ws = consumer / "data/imports/producer/final/small.dta"
    assert big_ws.read_bytes() == objs["big"][0]
    assert small_ws.read_bytes() == objs["small"][0]
    assert is_cached(cache, objs["big"][1])
    assert is_cached(cache, objs["small"][1])
    assert summary.error_count == 0
    assert rep.events_of("error") == []


def test_partial_materialized_git_rider_still_rescues(producer_bucket, tmp_path):
    """A legacy producer's git-tracked riders (readme, .gitkeep) ride in via
    the erepo git clone and land in the import dir even when every dvc-tracked
    file fails to pull (issue Symptom). The stat-only probe sees a non-empty
    dir and would call the import materialized — skipping the rescue and
    swallowing the absorbed DvcPullError into a silent exit-0 with the payload
    missing. Because the pull raised, the rescue must run regardless of the
    stat probe."""
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    dir_md5 = ensure_dir_manifest(cache, entries)
    total = len(objs["big"][0]) + len(objs["small"][0])
    _consumer(consumer, str(producer), sha, dir_md5, total)
    # Field state: only the git-tracked rider materialized (erepo clone); every
    # dvc-tracked file is still missing.
    ws = consumer / "data" / "imports" / "producer" / "final"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "readme.md").write_text("git-tracked rider\n")

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert (ws / "big.dta").read_bytes() == objs["big"][0]
    assert (ws / "small.dta").read_bytes() == objs["small"][0]
    assert is_cached(cache, objs["big"][1])
    assert is_cached(cache, objs["small"][1])
    assert summary.error_count == 0
    assert summary.targets_pulled == 1
    # The rescue actually fired (not a silent stat-probe skip).
    infos = [e[1] for e in rep.events_of("info")]
    assert any(str(producer) in m and sha[:7] in m for m in infos)
    assert rep.events_of("error") == []


# --------------------------------------------------------------------------
# Step 6 — error and variant paths.
# --------------------------------------------------------------------------

def test_data_loss_404_names_producer(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    dir_md5 = ensure_dir_manifest(cache, entries)
    _consumer(consumer, str(producer), sha, dir_md5, 1)
    # Pinned bytes overwritten/gone: delete the object.
    s3.delete_object(Bucket=bucket, Key="data_producer/data/final/big.dta")

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 1
    assert summary.targets_pulled == 0
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "no longer in the producer's bucket" in msg
    assert str(producer) in msg
    assert hint  # actionable, not a traceback
    assert not is_cached(cache, objs["big"][1])


def test_drift_md5_mismatch_leaves_cache_unseeded(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    dir_md5 = ensure_dir_manifest(cache, entries)
    _consumer(consumer, str(producer), sha, dir_md5, 1)
    # Producer replaced the bytes: the object drifts from the pinned md5.
    _put_multipart(s3, bucket, "data_producer/data/final/big.dta",
                   b"DRIFTED-" + b"z" * (10 * 1024 * 1024))

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "drift" in msg.lower() or "pinned hash" in msg.lower()
    assert hint
    # Cache NOT seeded — fetch_to_cache unlinks the tmp on md5 mismatch.
    assert not is_cached(cache, objs["big"][1])


def test_pinned_version_id_honored(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    good_big = b"GOODBIG-" + b"g" * (10 * 1024 * 1024)
    good_small = b"good-small-" * 300
    key_big = "data_producer/data/final/big.dta"
    key_small = "data_producer/data/final/small.dta"
    v_big = s3.put_object(Bucket=bucket, Key=key_big, Body=good_big)["VersionId"]
    v_small = s3.put_object(Bucket=bucket, Key=key_small, Body=good_small)["VersionId"]
    # Overwrite the current version with junk AFTER recording the pin.
    s3.put_object(Bucket=bucket, Key=key_big, Body=b"JUNK-OVERWRITE")

    objs = {
        "big": (good_big, hashlib.md5(good_big).hexdigest()),
        "small": (good_small, hashlib.md5(good_small).hexdigest()),
    }
    entries = _entries(objs)
    consumer = tmp_path / "consumer"
    # Fresh clone: no local manifest; hashes + version_ids come from the
    # producer's pointer file at the pin (source b).
    dir_md5 = _dir_md5(entries, tmp_path / "scratch")
    pointer = _pointer_dvc(
        entries, version_ids={"big.dta": v_big, "small.dta": v_small}
    )
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {
        ".dvc/config": PRODUCER_CONFIG, "data/final.dvc": pointer,
    })
    _consumer(consumer, str(producer), sha, dir_md5,
              len(good_big) + len(good_small))

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 0
    big_ws = consumer / "data/imports/producer/final/big.dta"
    assert big_ws.read_bytes() == good_big  # the pinned version, not the junk
    assert rep.events_of("error") == []


def test_fresh_clone_uses_producer_pointer_files(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    entries = _entries(objs)
    pointer = _pointer_dvc(entries)  # no version_ids, unversioned bucket
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {
        ".dvc/config": PRODUCER_CONFIG, "data/final.dvc": pointer,
    })
    consumer = tmp_path / "consumer"
    dir_md5 = _dir_md5(entries, tmp_path / "scratch")  # cache stays EMPTY
    _consumer(consumer, str(producer), sha, dir_md5,
              len(objs["big"][0]) + len(objs["small"][0]))

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 0
    assert (consumer / "data/imports/producer/final/big.dta").read_bytes() == objs["big"][0]
    assert rep.events_of("error") == []


def test_no_hash_source_anywhere_actionable_error(producer_bucket, tmp_path):
    s3, bucket = producer_bucket  # noqa: F841 — bucket exists but no manifest
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})  # no pointer file
    consumer = tmp_path / "consumer"
    # A .dir out.md5, but nothing cached and no producer pointer -> exhausted.
    _consumer(consumer, str(producer), sha, "deadbeef" * 4 + ".dir", 1)

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, msg, hint = errors[0]
    assert "cannot determine expected file hashes" in msg
    assert hint


def test_unreachable_producer_maps_to_actionable_hint(producer_bucket, tmp_path):
    s3, bucket = producer_bucket  # noqa: F841
    producer = tmp_path / "producer"
    _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    bad_sha = "0" * 40  # a well-formed sha that does not exist in the repo
    _consumer(consumer, str(producer), bad_sha, "cafef00d" * 4 + ".dir", 1)

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(consumer, fake)

    assert summary.error_count == 1
    errors = rep.events_of("error")
    assert len(errors) == 1
    _, _msg, hint = errors[0]
    assert hint  # actionable, not a traceback


def test_mixed_pull_non_import_batched_and_import_rescued(producer_bucket, tmp_path):
    s3, bucket = producer_bucket
    objs = _seed_objects(s3, bucket)
    producer = tmp_path / "producer"
    sha = _git_repo(producer, {".dvc/config": PRODUCER_CONFIG})
    consumer = tmp_path / "consumer"
    entries = _entries(objs)
    cache = consumer / ".dvc" / "cache"
    dir_md5 = ensure_dir_manifest(cache, entries)
    _consumer(consumer, str(producer), sha, dir_md5,
              len(objs["big"][0]) + len(objs["small"][0]))
    # A plain (non-import) dvc-tracked target alongside the import.
    own = consumer / "data" / "own.csv.dvc"
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("outs:\n  - md5: abc\n    size: 3\n    path: own.csv\n")
    non_import = "data/own.csv.dvc"

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fake.pull_raises_for = {TARGET: DvcPullError(FIELD_SIG)}
    summary, rep = _run_pull(
        consumer, fake, fallback=[non_import, TARGET],
    )

    # The non-import went through a single batched dvc pull; the import's
    # per-target pull raised (before recording) then rescued.
    assert [c.targets for c in fake.pull_calls] == [[non_import]]
    assert summary.error_count == 0
    assert summary.targets_pulled == 2
    assert (consumer / "data/imports/producer/final/big.dta").read_bytes() == objs["big"][0]
    assert rep.events_of("error") == []


def test_healthy_import_never_invokes_rescue(tmp_path):
    consumer = tmp_path / "consumer"
    entries = [DvcFileEntry(md5="m1", relpath="big.dta", size=3)]
    dir_md5 = _dir_md5(entries, tmp_path / "scratch")
    _consumer(consumer, "s3://irrelevant", "a" * 40, dir_md5, 3)
    # Simulate a dvc pull that already materialized the import: the workspace
    # dir exists and is non-empty, so outs_materialized is True and the rescue
    # is never consulted.
    ws = consumer / "data" / "imports" / "producer" / "final"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "big.dta").write_text("materialized")

    def _never(*_a, **_k):
        raise AssertionError("rescue must not fire on a healthy import")

    fake = _FakeDvcOps()
    fake.workspace = consumer
    fast = _FakeFastSyncOps()
    fast.result = FastPullResult(
        success=False, synced_count=0, fallback_targets=[TARGET],
    )
    rep = RecordingReporter()
    summary = data_pull(
        consumer, targets=[TARGET], dvc_ops=fake, fast_sync_ops=fast,
        aws_profile_name=None, reporter=rep, import_rescue=_never,
    )
    assert summary.error_count == 0
    assert rep.events_of("error") == []


# --------------------------------------------------------------------------
# Unit tests — pointer parsing, memoization.
# --------------------------------------------------------------------------

def test_match_out_files_reads_files_and_version_ids():
    data = {
        "outs": [
            {
                "path": "final",
                "md5": "abc.dir",
                "files": [
                    {"relpath": "a.dta", "md5": "m1", "size": 3,
                     "cloud": {"storage": {"version_id": "v1"}}},
                    {"relpath": "b.dta", "md5": "m2", "size": 4},
                ],
            }
        ]
    }
    ents = _match_out_files(data, "data/final", "storage")
    assert ents is not None
    by = {e.relpath: e for e in ents}
    assert set(by) == {"a.dta", "b.dta"}
    assert by["a.dta"].version_id == "v1"
    assert by["a.dta"].md5 == "m1"
    assert by["b.dta"].version_id is None


def test_match_out_files_skips_md5_only_dir_out():
    """A dir out carrying only ``md5: <hash>.dir`` and no ``files:`` yields
    None — the ``.dir`` md5 is the manifest's hash, not a blob's — so the
    caller falls through to the locally-cached ``.dir`` manifest (source c)."""
    data = {"outs": [{"path": "final", "md5": "a" * 32 + ".dir", "size": 10}]}
    assert _match_out_files(data, "data/final", "storage") is None


def test_match_out_files_from_dvc_lock_stage():
    data = {
        "stages": {
            "build": {
                "outs": [
                    {"path": "final", "files": [
                        {"relpath": "x", "md5": "mx", "size": 1},
                    ]},
                ]
            }
        }
    }
    ents = _match_out_files(data, "data/final", "storage")
    assert ents is not None
    assert ents[0].relpath == "x"


def _dep(repo="r", pin="p", output_path="data/final"):
    return imports.DataDependency(
        source=Path("x.dvc"), kind="dvc_file", producer_repo=repo,
        contract_pin=pin, output_path=output_path, local_path="final",
    )


class _CountingFetcher:
    def __init__(self, config_bytes):
        self.config = config_bytes
        self.calls = 0

    def fetch_path_at(self, repo, pin, path):
        if path == ".dvc/config":
            self.calls += 1
            return self.config
        raise FetchError.path_missing(repo, pin)


def test_resolve_producer_memoized_by_repo_and_pin():
    dep = _dep()
    f = _CountingFetcher(PRODUCER_CONFIG.encode())
    cache: dict = {}
    p1, e1 = _resolve_producer(dep, fetcher=f, aws_profile_name=None, cache=cache)
    p2, e2 = _resolve_producer(dep, fetcher=f, aws_profile_name=None, cache=cache)
    assert e1 is None and e2 is None
    assert p1 is p2
    assert p1.bucket == "lab"
    assert p1.prefix == "data_producer"
    assert p1.remote_name == "storage"
    assert f.calls == 1  # config fetched once, then memoized


def test_entries_from_producer_pointer_reads_dvc_file():
    dep = _dep()
    entries = [
        DvcFileEntry(md5="m1", relpath="big.dta", size=3),
        DvcFileEntry(md5="m2", relpath="small.dta", size=4),
    ]
    pointer = _pointer_dvc(entries)

    class _F:
        def fetch_path_at(self, repo, pin, path):
            if path == "data/final.dvc":
                return pointer.encode()
            raise FetchError.path_missing(repo, pin)

    from mintd._import_rescue_ops import _Producer
    producer = _Producer(bucket="lab", prefix="data_producer",
                         remote_cfg={"url": "s3://lab/data_producer"},
                         remote_name="storage")
    got = _entries_from_producer_pointer(dep, producer, fetcher=_F())
    assert got is not None
    assert {e.relpath for e in got} == {"big.dta", "small.dta"}
