"""``mintd cache`` policy tests (slices C1–C4).

C1 — pure core + preflight: the two-space key mapping, the skip truth tables,
the collision-guard oracle, remote resolution, and the lifecycle predicate.
Later slices append moto integration tests to this module.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from mintd import _cache_ops as c
from mintd._cache_ops import (
    CacheCollisionError,
    CacheError,
    CacheKeyError,
    decide_pull,
    decide_push,
    guard_no_dvc_outs_under_cache,
    lifecycle_covers_cache_tag,
    push_key,
    resolve_repo_remote,
    safe_cache_remainder,
)

# All three classification tiers + the bucket-root remote.
TIER_PREFIXES = ["lab/proj", "pub/proj", "slug/proj", ""]


def _enum(tmp_path: Path, paths: list[str], tracked: set[str] | None = None):
    """``enumerate_push_items`` with an empty tracked-set default, returning the
    ``_PushScan``. Tests reach into ``.items`` / ``.symlinks`` / ``.empty_args``
    and ``.refused_by(<reason>)``."""
    return c.enumerate_push_items(tmp_path, paths, tracked or set())


# ---------------------------------------------------------------------------
# §C — the full-key mapping (cache/ + full repo-relative path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", TIER_PREFIXES)
@pytest.mark.parametrize(
    "rel", ["blob.bin", "data/scratch/30min/x.geojson", "uniçode/näme.parquet"]
)
def test_push_key_maps_repo_path_under_cache_segment(prefix: str, rel: str) -> None:
    full = push_key(prefix, rel)
    # Full key = <prefix>/cache/<repo-relative-path>.
    expected = f"{prefix}/{c.CACHE_DIR_NAME}/{rel}" if prefix else f"{c.CACHE_DIR_NAME}/{rel}"
    assert full == expected
    # The list-remainder under <prefix>/cache IS the repo path, round-tripping.
    assert full.split(f"{c.CACHE_DIR_NAME}/", 1)[1] == rel


def test_push_key_empty_prefix_has_no_leading_slash() -> None:
    assert push_key("", "data/x") == "cache/data/x"


def test_push_key_doubles_cache_for_a_file_already_under_cache_dir() -> None:
    # Uniform rule: a file whose repo path starts with cache/ maps under the
    # lane's own cache/ segment, so the key deliberately doubles. Intentional —
    # the outer cache/ is the lane namespace, the inner is the repo path.
    assert push_key("lab/proj", "cache/foo") == "lab/proj/cache/cache/foo"


@pytest.mark.parametrize("bad", ["../evil", "/etc/passwd", "a\\b", "x/../../y", ".."])
def test_safe_cache_remainder_rejects_escapes(bad: str) -> None:
    with pytest.raises(CacheKeyError):
        safe_cache_remainder(bad)


@pytest.mark.parametrize("good", ["blob.bin", "isochrones/ct/x", "a/b/c/d.parquet"])
def test_safe_cache_remainder_accepts_clean(good: str) -> None:
    assert safe_cache_remainder(good) == good


@pytest.mark.parametrize(
    "good",
    ["v1..2.csv", "report..final.parquet", "range_2020..2024.geojson", "a/b..c/d.bin"],
)
def test_safe_cache_remainder_accepts_consecutive_dots_in_filenames(good: str) -> None:
    # Segment-scoped forbidding: a filename with a ``..`` substring is NOT
    # traversal; the substring match over-refused these (one such stored key
    # would fail the whole pull).
    assert safe_cache_remainder(good) == good


def test_is_forbidden_path_segment_scoped() -> None:
    # Real traversal is refused segment-wise…
    assert c._is_forbidden_path("../evil")
    assert c._is_forbidden_path("x/../../y")
    assert c._is_forbidden_path("..")
    assert c._is_forbidden_path("/abs")
    assert c._is_forbidden_path("a\\b")
    # …but consecutive dots inside a segment are not.
    assert not c._is_forbidden_path("v1..2.csv")
    assert not c._is_forbidden_path("a/b..c/d.bin")


@pytest.mark.parametrize("bad", [".", "sub/.", "a/./b", "", "cache/x//y"])
def test_is_forbidden_path_rejects_dot_and_empty_segments(bad: str) -> None:
    # A '.'-only or empty path segment must be refused: on pull, a planted key
    # `<prefix>/cache/.` lists back as remainder '.', which normalises (pathlib
    # drops the '.') back onto the cache/ directory location itself — slipping
    # BOTH the '..'-only string gate and the dest-containment gate. safe_cache_
    # remainder / the containment check would then let download_object write
    # attacker bytes AS A FILE onto the cache/ path.
    assert c._is_forbidden_path(bad)


@pytest.mark.parametrize("bad", [".", "sub/.", "a/./b", ""])
def test_safe_cache_remainder_rejects_dot_segments(bad: str) -> None:
    with pytest.raises(CacheKeyError):
        safe_cache_remainder(bad)


# ---------------------------------------------------------------------------
# §D/§E — skip truth tables
# ---------------------------------------------------------------------------


def test_decide_push_truth_table() -> None:
    # remote absent -> upload (new)
    assert decide_push(local_size=10, remote_size=None, local_sha256="a", remote_sha256=None) == "upload"
    # size differs -> upload
    assert decide_push(local_size=10, remote_size=11, local_sha256="a", remote_sha256="a") == "upload"
    # size matches, metadata absent -> upload (never skip an unverifiable object)
    assert decide_push(local_size=10, remote_size=10, local_sha256="a", remote_sha256=None) == "upload"
    # size matches, sha differs -> upload
    assert decide_push(local_size=10, remote_size=10, local_sha256="a", remote_sha256="b") == "upload"
    # size matches, sha equal -> skip (only a verified match skips)
    assert decide_push(local_size=10, remote_size=10, local_sha256="a", remote_sha256="a") == "skip"
    # size matches but local sha not computed -> upload (defensive)
    assert decide_push(local_size=10, remote_size=10, local_sha256=None, remote_sha256="a") == "upload"


def test_decide_pull_truth_table() -> None:
    # local missing -> download
    assert decide_pull(local_exists=False, local_size=None, remote_size=10, local_sha256=None, remote_sha256="a") == "download"
    # size differs -> download
    assert decide_pull(local_exists=True, local_size=9, remote_size=10, local_sha256="a", remote_sha256="a") == "download"
    # metadata absent -> download (cannot verify)
    assert decide_pull(local_exists=True, local_size=10, remote_size=10, local_sha256="a", remote_sha256=None) == "download"
    # sha mismatch -> download
    assert decide_pull(local_exists=True, local_size=10, remote_size=10, local_sha256="a", remote_sha256="b") == "download"
    # sha equal -> skip
    assert decide_pull(local_exists=True, local_size=10, remote_size=10, local_sha256="a", remote_sha256="a") == "skip"


# ---------------------------------------------------------------------------
# §F — collision guard (the key function is the oracle)
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _path_based_dvc(path: str) -> str:
    return (
        "outs:\n"
        f"- path: {path}\n"
        "  md5: d41d8cd98f00b204e9800998ecf8427e\n"
        "  cloud:\n"
        "    origin:\n"
        "      version_id: v-abc\n"
    )


def _md5_dvc(path: str) -> str:
    return (
        "outs:\n"
        f"- path: {path}\n"
        "  md5: 0123456789abcdef0123456789abcdef\n"
    )


def test_guard_trips_on_dvc_pointer_under_cache(tmp_path: Path) -> None:
    (tmp_path / ".dvc").mkdir()
    _write(tmp_path / "cache" / "model.dvc", _path_based_dvc("model"))
    # A .dvc file inside cache/ pinning path: model -> key cache/model.
    with pytest.raises(CacheCollisionError) as ei:
        guard_no_dvc_outs_under_cache(tmp_path, "origin")
    msg = str(ei.value)
    assert "overlaps the repo file cache (S3) namespace" in msg
    # Binding question: the error must NAME the offending out + its source so
    # a user can act (a regression dropping either from the message must fail).
    assert "'model'" in msg  # the out path
    assert "cache/model.dvc" in msg  # its .dvc source
    assert "dvc remove cache/model.dvc" in (ei.value.hint or "")


def test_guard_trips_on_root_pointer_pinning_cache_path(tmp_path: Path) -> None:
    # The pathological case: a root-level foo.dvc whose out path is cache/x.
    (tmp_path / ".dvc").mkdir()
    _write(tmp_path / "foo.dvc", _path_based_dvc("cache/x"))
    with pytest.raises(CacheCollisionError):
        guard_no_dvc_outs_under_cache(tmp_path, "origin")


def test_guard_trips_on_dvc_lock_stage_out_under_cache(tmp_path: Path) -> None:
    (tmp_path / ".dvc").mkdir()
    _write(
        tmp_path / "dvc.lock",
        "schema: '2.0'\n"
        "stages:\n"
        "  build:\n"
        "    cmd: run\n"
        "    outs:\n"
        "    - path: cache/pipe\n"
        "      md5: abc\n"
        "      cloud:\n"
        "        origin:\n"
        "          version_id: v1\n",
    )
    with pytest.raises(CacheCollisionError):
        guard_no_dvc_outs_under_cache(tmp_path, "origin")


def test_guard_ignores_md5_keyed_out_under_cache(tmp_path: Path) -> None:
    # md5-keyed outs map to files/md5/... — never cache/... — so no collision.
    (tmp_path / ".dvc").mkdir()
    _write(tmp_path / "cache" / "blob.dvc", _md5_dvc("blob"))
    guard_no_dvc_outs_under_cache(tmp_path, "origin")  # does not raise


def test_guard_silent_when_no_outs_under_cache(tmp_path: Path) -> None:
    (tmp_path / ".dvc").mkdir()
    _write(tmp_path / "data" / "final.dvc", _path_based_dvc("data/final"))
    guard_no_dvc_outs_under_cache(tmp_path, "origin")  # does not raise


def test_guard_trips_on_dvc_out_tracking_whole_cache_dir(tmp_path: Path) -> None:
    # The most on-the-nose collision: a version-aware directory out that tracks
    # the ENTIRE top-level cache/ dir (`dvc add cache` -> cache.dvc pinning
    # `path: cache`). s3_key_for_out returns the bare key `cache` (no trailing
    # slash, root-anchored), yet that dir out's version-aware child objects live
    # at <prefix>/cache/<child> — the exact keys cache push/pull use. The guard
    # must trip on the directory-root case, not only on `cache/<x>` sub-keys.
    (tmp_path / ".dvc").mkdir()
    _write(
        tmp_path / "cache.dvc",
        "outs:\n"
        "- path: cache\n"
        "  md5: d41d8cd98f00b204e9800998ecf8427e.dir\n"
        "  cloud:\n"
        "    origin:\n"
        "      version_id: v1\n",
    )
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "model.bin").write_bytes(b"m")
    with pytest.raises(CacheCollisionError) as ei:
        guard_no_dvc_outs_under_cache(tmp_path, "origin")
    msg = str(ei.value)
    assert "overlaps the repo file cache (S3) namespace" in msg
    assert "'cache'" in msg  # the out path is named
    assert "cache.dvc" in msg  # its source is named


# ---------------------------------------------------------------------------
# §B — remote resolution
# ---------------------------------------------------------------------------


def _dvc_config(tmp_path: Path, url: str, *, remote: str = "origin") -> None:
    cfg = tmp_path / ".dvc" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'[remote "{remote}"]\n    url = {url}\n')


@pytest.mark.parametrize("prefix", TIER_PREFIXES)
def test_resolve_repo_remote_reads_dvc_config(tmp_path: Path, prefix: str) -> None:
    url = f"s3://hcal-data/{prefix}" if prefix else "s3://hcal-data"
    _dvc_config(tmp_path, url)
    repo = resolve_repo_remote(tmp_path, None)
    assert repo.bucket == "hcal-data"
    assert repo.prefix == prefix
    assert repo.remote_name == "origin"


def test_resolve_repo_remote_missing_config_raises_cache_error(tmp_path: Path) -> None:
    (tmp_path / ".dvc").mkdir()
    with pytest.raises(CacheError) as ei:
        resolve_repo_remote(tmp_path, None)
    assert ei.value.hint is not None


def test_resolve_repo_remote_non_s3_raises_cache_error(tmp_path: Path) -> None:
    _dvc_config(tmp_path, "gdrive://folder")
    with pytest.raises(CacheError) as ei:
        resolve_repo_remote(tmp_path, None)
    assert "not an S3 remote" in str(ei.value)


# ---------------------------------------------------------------------------
# §G — lifecycle predicate (pure fixture matrix)
# ---------------------------------------------------------------------------


def _covering_rule() -> dict:
    return {
        "Status": "Enabled",
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        "Filter": {"Tag": {"Key": "mintd-lane", "Value": "cache"}},
    }


def test_lifecycle_covers_via_tag_filter() -> None:
    assert lifecycle_covers_cache_tag([_covering_rule()]) is True


def test_lifecycle_covers_via_and_tags_arm() -> None:
    rule = {
        "Status": "Enabled",
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        "Filter": {"And": {"Prefix": "lab/", "Tags": [{"Key": "mintd-lane", "Value": "cache"}]}},
    }
    assert lifecycle_covers_cache_tag([rule]) is True


def test_lifecycle_not_covered_when_disabled() -> None:
    rule = _covering_rule()
    rule["Status"] = "Disabled"
    assert lifecycle_covers_cache_tag([rule]) is False


def test_lifecycle_not_covered_without_noncurrent_expiration() -> None:
    rule = _covering_rule()
    del rule["NoncurrentVersionExpiration"]
    assert lifecycle_covers_cache_tag([rule]) is False


def test_lifecycle_not_covered_by_prefix_only_filter() -> None:
    rule = {
        "Status": "Enabled",
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        "Filter": {"Prefix": "lab/proj/cache/"},
    }
    assert lifecycle_covers_cache_tag([rule]) is False


def test_lifecycle_empty_rules_not_covered() -> None:
    assert lifecycle_covers_cache_tag([]) is False


# ---------------------------------------------------------------------------
# §D — push enumeration (pure, filesystem only)
# ---------------------------------------------------------------------------


def test_enumerate_collects_any_repo_files(tmp_path: Path) -> None:
    # The scope is the whole working tree now — not just cache/.
    _write(tmp_path / "data" / "scratch" / "a.bin", "a")
    _write(tmp_path / "data" / "scratch" / "sub" / "b.bin", "b")
    scan = _enum(tmp_path, ["data/scratch"])
    rels = sorted(i.rel for i in scan.items)
    assert rels == ["data/scratch/a.bin", "data/scratch/sub/b.bin"]
    assert scan.symlinks == [] and scan.refused == [] and scan.empty_args == []


def test_enumerate_collects_a_top_level_file(tmp_path: Path) -> None:
    _write(tmp_path / "notes.txt", "x")
    scan = _enum(tmp_path, ["notes.txt"])
    assert [i.rel for i in scan.items] == ["notes.txt"]
    assert scan.refused == []


def test_enumerate_refuses_dvc_tracked_path(tmp_path: Path) -> None:
    # A DVC-tracked out (or anything under a tracked directory out) belongs to
    # `mintd data push` — refused as one violation, naming the path.
    _write(tmp_path / "data" / "final.parquet", "x")
    scan = _enum(tmp_path, ["data/final.parquet"], tracked={"data/final.parquet"})
    assert scan.items == []
    assert scan.refused_by("dvc_tracked") == ["data/final.parquet"]


def test_enumerate_refuses_files_under_a_tracked_directory_out(tmp_path: Path) -> None:
    _write(tmp_path / "data" / "iso" / "a.bin", "a")
    scan = _enum(tmp_path, ["data/iso"], tracked={"data/iso"})
    assert scan.items == []
    # The whole tracked dir is one refusal, not one-per-file.
    assert scan.refused_by("dvc_tracked") == ["data/iso"]


def test_enumerate_refuses_git_and_dvc_internals(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "config", "x")
    _write(tmp_path / ".dvc" / "config", "y")
    scan = _enum(tmp_path, [".git/config", ".dvc/config"])
    assert scan.items == []
    assert sorted(scan.refused_by("protected")) == [".dvc/config", ".git/config"]


def test_enumerate_prunes_git_dvc_subtrees_from_a_broad_arg(tmp_path: Path) -> None:
    # Pushing the whole project root is refused (project_root), but a broad
    # subdir walk that would descend into .git/.dvc must prune them rather than
    # refuse thousands of internal files. Here a mid-tree dir holds a nested
    # .git (a submodule-like layout) alongside a real file.
    _write(tmp_path / "work" / "keep.bin", "k")
    _write(tmp_path / "work" / ".git" / "HEAD", "ref")
    scan = _enum(tmp_path, ["work"])
    assert [i.rel for i in scan.items] == ["work/keep.bin"]
    assert scan.refused == []  # .git pruned, not refused per-file


def test_enumerate_refuses_project_root_arg(tmp_path: Path) -> None:
    _write(tmp_path / "a.bin", "a")
    scan = _enum(tmp_path, ["."])
    assert scan.items == []
    assert scan.refused_by("project_root") == ["."]


def test_enumerate_refuses_outside_project(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere.bin"
    outside.write_text("x")
    scan = _enum(tmp_path, [str(outside)])
    assert scan.items == []
    assert scan.refused_by("outside_project") == [str(outside)]


def test_enumerate_reports_all_violations_in_one_pass(tmp_path: Path) -> None:
    _write(tmp_path / "data" / "ok.bin", "ok")
    _write(tmp_path / "data" / "tracked.parquet", "t")
    _write(tmp_path / ".git" / "config", "g")
    scan = _enum(
        tmp_path,
        ["data/ok.bin", "data/tracked.parquet", ".git/config"],
        tracked={"data/tracked.parquet"},
    )
    assert [i.rel for i in scan.items] == ["data/ok.bin"]
    assert scan.refused_by("dvc_tracked") == ["data/tracked.parquet"]
    assert scan.refused_by("protected") == [".git/config"]


def test_enumerate_empty_directory_reported(tmp_path: Path) -> None:
    (tmp_path / "data" / "empty").mkdir(parents=True)
    scan = _enum(tmp_path, ["data/empty"])
    assert scan.items == []
    assert scan.empty_args == ["data/empty"]


def test_enumerate_skips_symlink_file(tmp_path: Path) -> None:
    _write(tmp_path / "data" / "real.bin", "real")
    (tmp_path / "data" / "link.bin").symlink_to(tmp_path / "data" / "real.bin")
    scan = _enum(tmp_path, ["data"])
    assert [i.rel for i in scan.items] == ["data/real.bin"]
    # The skipped symlink is named by its OWN path, not the outer arg, so a
    # directory carrying several symlinks yields distinguishable entries the
    # user can actually act on.
    assert scan.symlinks == ["data/link.bin"]


def test_enumerate_names_each_symlinked_file_individually(tmp_path: Path) -> None:
    # Two symlinked files under one pushed dir must appear as their own paths,
    # not two indistinguishable copies of the arg.
    _write(tmp_path / "data" / "real.bin", "real")
    (tmp_path / "data" / "a_link.bin").symlink_to(tmp_path / "data" / "real.bin")
    (tmp_path / "data" / "b_link.bin").symlink_to(tmp_path / "data" / "real.bin")
    scan = _enum(tmp_path, ["data"])
    assert sorted(scan.symlinks) == ["data/a_link.bin", "data/b_link.bin"]


def test_enumerate_skips_symlinked_subdirectory_not_silently_dropped(
    tmp_path: Path,
) -> None:
    # A symlinked SUBDIRECTORY must not be silently omitted:
    # os.walk(followlinks=False) yields its name but never descends, so files
    # beneath it would vanish from the push with no error/warning/ledger entry.
    outside = tmp_path.parent / "outside"
    (outside).mkdir()
    (outside / "secret.bin").write_text("secret")
    _write(tmp_path / "data" / "plain.bin", "plain")
    (tmp_path / "data" / "linked_subdir").symlink_to(outside, target_is_directory=True)

    scan = _enum(tmp_path, ["data"])
    # The real file is enumerated; the symlinked subtree's contents are NOT…
    assert [i.rel for i in scan.items] == ["data/plain.bin"]
    assert all("secret" not in i.rel for i in scan.items)
    # …but the user is told the subtree was skipped, named by its own path.
    assert scan.symlinks == ["data/linked_subdir"]
    assert scan.refused == [] and scan.empty_args == []


def test_enumerate_top_level_symlink_arg_skipped_not_followed(tmp_path: Path) -> None:
    # A symlink passed DIRECTLY as a push arg (not merely discovered in a walk)
    # must be screened before resolve(); resolving first follows it to its
    # target, so is_symlink() reads False and the target's bytes upload silently.
    _write(tmp_path / "data" / "real.bin", "real")
    (tmp_path / "data" / "link.bin").symlink_to(tmp_path / "data" / "real.bin")
    scan = _enum(tmp_path, ["data/link.bin"])
    assert scan.items == []  # the target's bytes are NOT enumerated
    assert scan.symlinks == ["data/link.bin"]  # reported as a skipped symlink
    assert scan.refused == [] and scan.empty_args == []


def test_enumerate_accepts_consecutive_dots_in_filename(tmp_path: Path) -> None:
    # A legitimate filename with consecutive dots is clean; the old substring
    # '..' match refused it as traversal.
    _write(tmp_path / "data" / "range_2020..2024.parquet", "d")
    scan = _enum(tmp_path, ["data"])
    assert scan.refused == []
    assert [i.rel for i in scan.items] == ["data/range_2020..2024.parquet"]


def test_enumerate_derives_posix_rel_not_backslash(tmp_path: Path) -> None:
    # A2 regression: derivation must be relative_to(...).as_posix(), never a
    # bare str(Path) (which yields backslashes on Windows that §C then refuses).
    _write(tmp_path / "data" / "sub" / "deep.bin", "d")
    scan = _enum(tmp_path, ["data/sub/deep.bin"])
    assert scan.refused == []
    assert scan.items[0].rel == "data/sub/deep.bin"
    assert "\\" not in scan.items[0].rel


# ===========================================================================
# C2 / C3 — moto integration (versioned bucket matching _init_ops scaffolding)
# ===========================================================================

from mintd._config import Config  # noqa: E402
from mintd._console import Reporter  # noqa: E402


def _project(tmp_path: Path, bucket: str, prefix: str = "lab/proj") -> Path:
    (tmp_path / ".dvc").mkdir(parents=True, exist_ok=True)
    url = f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
    (tmp_path / ".dvc" / "config").write_text(f'[remote "origin"]\n    url = {url}\n')
    return tmp_path


class _CountingClient:
    """Proxy that forwards to a real client but tallies control/data calls, so
    the LIST-precheck economics can be asserted."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.calls = {"head_object": 0, "upload_file": 0, "download_file": 0, "get_paginator": 0}

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    def head_object(self, **k):
        self.calls["head_object"] += 1
        return self._real.head_object(**k)

    def upload_file(self, *a, **k):
        self.calls["upload_file"] += 1
        return self._real.upload_file(*a, **k)

    def download_file(self, *a, **k):
        self.calls["download_file"] += 1
        return self._real.download_file(*a, **k)

    def get_paginator(self, name):
        self.calls["get_paginator"] += 1
        return self._real.get_paginator(name)


def _factory(client):
    return lambda _cfg, _prof: client


def _cfg() -> Config:
    return Config()


def test_push_uploads_keys_tag_and_metadata(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "data" / "iso").mkdir(parents=True)
    (proj / "data" / "iso" / "a.bin").write_bytes(b"hello" * 100)
    (proj / "data" / "b.bin").write_bytes(b"world" * 50)

    summary = c.cache_push(
        project_path=proj, paths=["data"], config=_cfg(),
        reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3),
    )
    assert summary.uploaded == 2 and summary.unchanged == 0
    # The key is <prefix>/cache/<repo-relative-path>.
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["lab/proj/cache/data/b.bin", "lab/proj/cache/data/iso/a.bin"]
    # tag + metadata on every object.
    for key in keys:
        head = s3.head_object(Bucket=bucket, Key=key)
        assert head["Metadata"]["mintd-sha256"]
        tags = s3.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
        assert {"Key": "mintd-lane", "Value": "cache"} in tags


def test_push_incremental_skip_matrix(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    cd = proj / "cache"
    cd.mkdir()
    (cd / "a.bin").write_bytes(b"a" * 500)
    (cd / "b.bin").write_bytes(b"b" * 250)
    (cd / "c.bin").write_bytes(b"c" * 10)
    counter = _CountingClient(s3)

    s1 = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                      reporter=Reporter(json_mode=True), s3_client_factory=_factory(counter))
    assert s1.uploaded == 3
    # 1 paginated LIST, no HEAD (nothing matched by size on an empty remote).
    assert counter.calls["get_paginator"] == 1
    assert counter.calls["head_object"] == 0
    assert counter.calls["upload_file"] == 3

    # a: size-diff; b: identical; c: same-size sha-diff; d: brand new.
    (cd / "a.bin").write_bytes(b"a" * 501)
    (cd / "c.bin").write_bytes(b"C" * 10)
    (cd / "d.bin").write_bytes(b"d" * 999)
    counter2 = _CountingClient(s3)
    s2 = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                      reporter=Reporter(json_mode=True), s3_client_factory=_factory(counter2))
    assert s2.uploaded == 3 and s2.unchanged == 1  # a,c,d upload; b unchanged
    assert counter2.calls["get_paginator"] == 1
    # HEADs only for the size-matching candidates (b, c) — never for a (size
    # changed) or d (new).
    assert counter2.calls["head_object"] == 2
    assert counter2.calls["upload_file"] == 3


def test_push_metadata_stripped_object_is_never_skipped(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "x.bin").write_bytes(b"z" * 40)
    # Plant an object of the SAME size but with no mintd-sha256 metadata
    # (as an `aws s3 cp` would). It must never be skipped.
    s3.put_object(Bucket=bucket, Key="lab/proj/cache/x.bin", Body=b"q" * 40)
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.uploaded == 1 and summary.unchanged == 0


def test_push_arbitrary_untracked_path_uploads(s3_versioned, tmp_path: Path) -> None:
    # The headline new behavior: any untracked working-tree path can be cached,
    # not just a top-level cache/ dir. It lands at <prefix>/cache/<repo-path>.
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "results").mkdir()
    (proj / "results" / "x.bin").write_bytes(b"x" * 20)
    summary = c.cache_push(project_path=proj, paths=["results/x.bin"], config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.uploaded == 1 and not summary.failed
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]]
    assert keys == ["lab/proj/cache/results/x.bin"]


def test_push_dvc_tracked_path_refused_uploads_nothing(s3_versioned, tmp_path: Path) -> None:
    # A DVC-tracked out belongs to `mintd data push` — the whole push is refused
    # before any transfer, and the hint points at data push.
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    _write(proj / "data" / "final.parquet.dvc", _path_based_dvc("final.parquet"))
    (proj / "data" / "final.parquet").write_bytes(b"tracked" * 10)
    with pytest.raises(CacheError) as ei:
        c.cache_push(project_path=proj, paths=["data/final.parquet"], config=_cfg(),
                     reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert "tracked by DVC" in str(ei.value)
    assert "data/final.parquet" in str(ei.value)
    assert "mintd data push" in (ei.value.hint or "")
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)  # bucket empty


def test_push_git_dvc_internal_refused_uploads_nothing(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    _write(proj / ".git" / "config", "secret")
    with pytest.raises(CacheError) as ei:
        c.cache_push(project_path=proj, paths=[".git/config"], config=_cfg(),
                     reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert ".git" in str(ei.value)
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_push_nonexistent_sibling_path_fails_whole_push(s3_versioned, tmp_path: Path) -> None:
    # A typo'd/nonexistent sibling arg must fail the whole push (naming it) even
    # when another arg yields files — otherwise CI would report exit 0 as if the
    # missing target had been cached, and a teammate's pull would silently lack
    # that data.
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache" / "results").mkdir(parents=True)
    (proj / "cache" / "results" / "r.bin").write_bytes(b"r" * 10)
    with pytest.raises(CacheError) as ei:
        c.cache_push(
            project_path=proj,
            paths=["cache/results", "cache/isocrones"],  # second is a typo
            config=_cfg(), reporter=Reporter(json_mode=True),
            s3_client_factory=_factory(s3),
        )
    assert "cache/isocrones" in str(ei.value)
    # Nothing uploaded — the whole push was refused before any transfer.
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_push_empty_directory_sibling_fails_whole_push(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache" / "full").mkdir(parents=True)
    (proj / "cache" / "full" / "f.bin").write_bytes(b"f" * 10)
    (proj / "cache" / "empty").mkdir()
    with pytest.raises(CacheError) as ei:
        c.cache_push(
            project_path=proj, paths=["cache/full", "cache/empty"],
            config=_cfg(), reporter=Reporter(json_mode=True),
            s3_client_factory=_factory(s3),
        )
    assert "cache/empty" in str(ei.value)
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_push_symlink_skipped_warned_counted(s3_versioned, tmp_path: Path, capsys) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "real.bin").write_bytes(b"real" * 10)
    (proj / "cache" / "link.bin").symlink_to(proj / "cache" / "real.bin")
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(), s3_client_factory=_factory(s3))
    assert summary.uploaded == 1
    assert summary.skipped_symlink == 1
    assert "skipped symlink" in capsys.readouterr().err


def test_push_dry_run_moves_zero_bytes(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "a.bin").write_bytes(b"a" * 10)
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), dry_run=True,
                           s3_client_factory=_factory(s3))
    assert summary.dry_run is True
    assert summary.uploaded == 1  # would-upload
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)  # nothing uploaded


class _FailingUploadClient(_CountingClient):
    """Fails ``upload_file`` for keys containing ``boom`` (mapped like real
    boto3 to S3UploadFailedError wrapping a ClientError)."""

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Callback=None):  # noqa: N803
        if "boom" in key:
            from boto3.exceptions import S3UploadFailedError
            from botocore.exceptions import ClientError
            try:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
                    "PutObject",
                )
            except ClientError:
                raise S3UploadFailedError(f"Failed to upload {filename}")
        return super().upload_file(filename, bucket, key, ExtraArgs=ExtraArgs, Callback=Callback)


def test_push_partial_failure_ledger(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "ok.bin").write_bytes(b"ok" * 10)
    (proj / "cache" / "boom.bin").write_bytes(b"no" * 10)
    client = _FailingUploadClient(s3)
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), jobs=2,
                           s3_client_factory=_factory(client))
    assert summary.uploaded == 1
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert fail.rel == "cache/boom.bin"
    assert "mintd cache push cache/boom.bin" in (fail.hint or "")


def test_push_mixed_tracked_arg_fails_whole_push_uploads_nothing(
    s3_versioned, tmp_path: Path
) -> None:
    # A MIXED push (a valid untracked arg + a DVC-tracked arg) must fail the
    # whole push naming the offender, uploading NOTHING — otherwise the valid
    # sibling would upload, the tracked path would be silently dropped, and the
    # command would report success (exit 0), masking the refusal from CI.
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "scratch").mkdir()
    (proj / "scratch" / "ok.bin").write_bytes(b"ok" * 10)
    _write(proj / "data" / "final.dvc", _path_based_dvc("final"))
    (proj / "data" / "final").write_bytes(b"keep" * 10)
    with pytest.raises(CacheError) as ei:
        c.cache_push(
            project_path=proj,
            paths=["scratch/ok.bin", "data/final"],
            config=_cfg(), reporter=Reporter(json_mode=True),
            s3_client_factory=_factory(s3),
        )
    assert "data/final" in str(ei.value)
    # The valid sibling did NOT upload — the whole push was refused before bytes.
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


def test_push_vanished_file_becomes_failed_not_traceback(
    s3_versioned, tmp_path: Path, monkeypatch
) -> None:
    # A real TOCTOU: enumeration reports a file that no longer exists by the time
    # the executor hashes it (a producer still writing into cache/, or a file
    # removed mid-push). The local file_sha256 OSError must resolve to a per-file
    # `failed` ledger entry, never escape the executor as a raw traceback out of
    # cli.main() (house norm: no traceback on documented failure paths).
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "ok.bin").write_bytes(b"ok" * 10)

    real_enum = c.enumerate_push_items

    def _ghost_enum(pp, paths, tracked):
        scan = real_enum(pp, paths, tracked)
        ghost = c._PushItem(
            rel="cache/gone.bin", abs_path=pp / "cache" / "gone.bin", size=5
        )
        return c._PushScan(
            items=list(scan.items) + [ghost], symlinks=scan.symlinks,
            refused=scan.refused, empty_args=scan.empty_args,
        )

    monkeypatch.setattr(c, "enumerate_push_items", _ghost_enum)
    summary = c.cache_push(
        project_path=proj, paths=["cache"], config=_cfg(),
        reporter=Reporter(json_mode=True), jobs=2, s3_client_factory=_factory(s3),
    )
    assert summary.uploaded == 1  # ok.bin still uploaded
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert fail.rel == "cache/gone.bin"
    assert "mintd cache push cache/gone.bin" in (fail.hint or "")


def test_enumeration_stat_toctou_becomes_failed_not_traceback(
    s3_versioned, tmp_path: Path, monkeypatch
) -> None:
    # The REAL enumeration stat() TOCTOU (distinct from the test above, which
    # injects a pre-built _PushItem so the stat at _add_file never runs): os.walk
    # lists a directory's filenames as a batch, then _add_file stat()s them one
    # at a time. A file present at walk-time but gone by stat-time must resolve to
    # a per-file `failed` ledger entry, never a bare FileNotFoundError aborting
    # the whole push as a raw traceback out of cache_push -> cli.main().
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "ok.bin").write_bytes(b"ok" * 10)

    real_walk = c.os.walk

    def _ghost_walk(top, **kw):
        for dirpath, dirnames, filenames in real_walk(top, **kw):
            # A filename that never existed on disk (a producer removed it between
            # os.walk's directory listing and _add_file's stat()).
            yield dirpath, dirnames, filenames + ["ghost.bin"]

    monkeypatch.setattr(c.os, "walk", _ghost_walk)
    summary = c.cache_push(
        project_path=proj, paths=["cache"], config=_cfg(),
        reporter=Reporter(json_mode=True), jobs=2, s3_client_factory=_factory(s3),
    )
    assert summary.uploaded == 1  # ok.bin still uploaded — the rest completes
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert fail.rel == "cache/ghost.bin"
    assert "local read error" in (fail.reason or "")
    assert "mintd cache push cache/ghost.bin" in (fail.hint or "")


@pytest.mark.skipif(
    os.name == "nt", reason="'\\' is the path separator on Windows — a file "
    "literally named a\\b.bin cannot exist; the backslash-in-filename case is POSIX-only",
)
def test_enumerate_backslash_local_file_refused_not_silently_dropped(
    tmp_path: Path,
) -> None:
    # A backslash is a legal POSIX filename char but is forbidden in a cache key
    # (it would corrupt the S3 key / be re-interpreted as a separator). A real
    # file data/a\b.bin must be REFUSED LOUDLY (named under 'forbidden'), never
    # dropped by a bare `return` — with a valid sibling present a silent drop
    # would let the push report success while omitting the file (data loss, the
    # red-team P0).
    _write(tmp_path / "data" / "normal.bin", "ok")
    (tmp_path / "data" / "a\\b.bin").write_bytes(b"y" * 5)
    scan = _enum(tmp_path, ["data"])
    assert [i.rel for i in scan.items] == ["data/normal.bin"]
    assert scan.refused_by("forbidden") == ["data/a\\b.bin"]  # named, not dropped


@pytest.mark.skipif(
    os.name == "nt", reason="'\\' is the path separator on Windows — a file "
    "literally named a\\b.bin cannot exist; the backslash-in-filename case is POSIX-only",
)
def test_push_backslash_local_file_refuses_whole_push_uploads_nothing(
    s3_versioned, tmp_path: Path
) -> None:
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "normal.bin").write_bytes(b"x" * 100)
    (proj / "cache" / "a\\b.bin").write_bytes(b"y" * 50)
    with pytest.raises(CacheError) as ei:
        c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                     reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert "a\\b.bin" in str(ei.value)
    # The valid sibling did NOT upload — the whole push was refused before bytes.
    assert "Contents" not in s3.list_objects_v2(Bucket=bucket)


# ---------------------------------------------------------------------------
# C3 — cache pull + ls
# ---------------------------------------------------------------------------


def _seed_remote(s3, bucket: str, tmp_path: Path, files: dict[str, bytes], prefix: str = "lab/proj") -> Path:
    """Push ``files`` (keyed by repo-relative path) from a scratch project so the
    remote is populated exactly as cache_push would leave it. Each key maps to
    S3 key ``<prefix>/cache/<rel>``, so its list-remainder IS ``<rel>`` and a
    pull reconstructs it at ``<rel>`` in the working tree."""
    src = tmp_path / "_producer"
    _project(src, bucket, prefix)
    for rel, body in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    c.cache_push(project_path=src, paths=list(files), config=_cfg(),
                 reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    return src


def test_pull_prefix_scoped_lands_at_repo_paths(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {
        "iso/ct/a.bin": b"A" * 100,
        "iso/ct/b.bin": b"B" * 50,
        "iso/ma/c.bin": b"C" * 30,
    })
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           prefix="iso/ct/", s3_client_factory=_factory(s3))
    assert summary.pulled == 2 and not summary.failed
    # Objects reconstruct at their repo-relative paths (not under cache/).
    assert (dest / "iso" / "ct" / "a.bin").read_bytes() == b"A" * 100
    assert (dest / "iso" / "ct" / "b.bin").exists()
    # ma/ was outside the prefix — never fetched.
    assert not (dest / "iso" / "ma").exists()


def test_pull_skips_verified_local_files(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"x.bin": b"x" * 40})
    dest = _project(tmp_path / "clone", bucket)
    first = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                         s3_client_factory=_factory(s3))
    assert first.pulled == 1
    second = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                          s3_client_factory=_factory(s3))
    assert second.pulled == 0 and second.unchanged == 1


def test_pull_all_preamble_and_empty_listing_warn(s3_versioned, tmp_path: Path, capsys) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"a.bin": b"a" * 10, "b.bin": b"b" * 20})
    dest = _project(tmp_path / "clone", bucket)
    c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(),
                 s3_client_factory=_factory(s3))
    out = capsys.readouterr().err
    assert "pulling 2 file(s)" in out and "from the repo file cache (S3)" in out

    # An empty prefix warns and returns an empty summary (handler exits 0).
    empty = _project(tmp_path / "empty_clone", bucket)
    summary = c.cache_pull(project_path=empty, config=_cfg(), reporter=Reporter(),
                           prefix="does/not/exist/", s3_client_factory=_factory(s3))
    assert summary.pulled == 0 and summary.unchanged == 0
    assert "nothing pulled" in capsys.readouterr().err


def test_pull_rejects_hostile_planted_key(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"good.bin": b"g" * 10})
    # Plant a traversal key that lists back as remainder ../evil.
    s3.put_object(Bucket=bucket, Key="lab/proj/cache/../evil", Body=b"pwn")
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           s3_client_factory=_factory(s3))
    # good.bin pulled; hostile key refused (named) and never written anywhere.
    assert summary.pulled == 1
    assert len(summary.failed) == 1
    assert "unsafe key refused" in (summary.failed[0].reason or "")
    assert not (dest.parent / "evil").exists()
    assert not (dest / "evil").exists()
    # good.bin landed at the repo root; nothing escaped.
    assert sorted(p.name for p in dest.iterdir()) == [".dvc", "good.bin"]


@pytest.mark.parametrize("hostile_key", ["lab/proj/cache/.", "lab/proj/cache/sub/."])
def test_pull_rejects_planted_dot_segment_key(
    s3_versioned, tmp_path: Path, hostile_key: str
) -> None:
    # A planted key `<prefix>/cache/.` (or `.../sub/.`) lists back as remainder
    # '.' / 'sub/.'. Before the fix it slipped BOTH gates — safe_cache_remainder
    # (only rejected '..') and dest.resolve().relative_to(cache_root) (a '.'-
    # suffixed dest collapses onto cache_root, which is contained in itself) —
    # so download_object wrote the attacker bytes AS A FILE onto the cache/ path
    # (or, with a subdir, onto cache/sub) and the pull reported success (exit 0).
    # It must instead be refused, named, and fail the pull with nothing written.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"good.bin": b"g" * 10})
    s3.put_object(
        Bucket=bucket, Key=hostile_key, Body=b"HOSTILE",
        Metadata={"mintd-sha256": "0" * 64},
    )
    # A FRESH clone (the documented teammate scenario, where a bare
    # `tmp.replace(<clone>/.)` would otherwise write onto the project root).
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # good.bin
    assert len(summary.failed) == 1
    assert "unsafe key refused" in (summary.failed[0].reason or "")
    # The hostile '.'-segment write never landed as a file onto the project root
    # (or cache/sub), and nothing leaked as a stray tmp; good.bin landed normally.
    assert not any(p.name.endswith(".mintd-tmp") for p in dest.iterdir())
    assert (dest / "good.bin").read_bytes() == b"g" * 10


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows CI")
def test_pull_containment_gate_catches_symlinked_intermediate_dir(
    s3_versioned, tmp_path: Path
) -> None:
    """The SECOND (filesystem-containment) gate, independent of the string
    gate: a remainder that is string-clean (no ../, no leading /, no backslash)
    but whose on-disk dest resolves OUTSIDE the project root via a symlinked
    intermediate directory must be refused. Deleting the
    `dest.resolve().relative_to(project_root)` check makes this test fail (the
    string gate alone cannot catch it)."""
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"good.bin": b"g" * 10})
    # A perfectly clean-looking remainder key.
    s3.put_object(Bucket=bucket, Key="lab/proj/cache/evil/x", Body=b"pwn")
    dest = _project(tmp_path / "clone", bucket)
    outside = tmp_path / "outside"
    outside.mkdir()
    # Plant a symlinked intermediate dir so evil/x resolves to outside/x.
    (dest / "evil").symlink_to(outside)

    summary = c.cache_pull(project_path=dest, config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))

    assert summary.pulled == 1  # good.bin
    assert len(summary.failed) == 1
    assert "unsafe key refused" in (summary.failed[0].reason or "")
    assert not (outside / "x").exists()  # nothing written outside the project


def test_push_concurrency_smoke_many_files(s3_versioned, tmp_path: Path) -> None:
    """Concurrency path (ThreadPoolExecutor) exercised well past the ~4-file
    max of the other tests: N files with --jobs 8 all land, correct ledger.
    Wall-clock is a CI non-goal; this pins that the executor path works and no
    file is dropped/duplicated under concurrent submission."""
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    many_dir = proj / "scratch" / "many"
    many_dir.mkdir(parents=True)
    n = 128
    for i in range(n):
        (many_dir / f"f{i:03d}.bin").write_bytes(f"content-{i}".encode())

    summary = c.cache_push(project_path=proj, paths=["scratch/many"],
                           config=_cfg(), reporter=Reporter(json_mode=True), jobs=8,
                           s3_client_factory=_factory(s3))

    assert summary.uploaded == n and not summary.failed
    listed = s3.list_objects_v2(Bucket=bucket, Prefix="lab/proj/cache/scratch/many/")
    assert listed["KeyCount"] == n  # every file landed, none duplicated


def test_pull_does_not_clobber_user_tmp_scratch_file(s3_versioned, tmp_path: Path) -> None:
    # download_object's tmp path used to be a predictable <dest>.tmp; pulling
    # results.parquet would delete a user's own cache/results.parquet.tmp scratch
    # file of that name. The collision-proof per-download tmp suffix leaves it be.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"results.parquet": b"R" * 40})
    dest = _project(tmp_path / "clone", bucket)
    scratch = dest / "results.parquet.tmp"
    scratch.write_bytes(b"user-scratch-do-not-touch")
    summary = c.cache_pull(project_path=dest, config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.pulled == 1
    assert (dest / "results.parquet").read_bytes() == b"R" * 40
    # The user's scratch file survives untouched.
    assert scratch.read_bytes() == b"user-scratch-do-not-touch"


def test_pull_key_and_its_tmp_sibling_both_land(s3_versioned, tmp_path: Path) -> None:
    # The remote holds both "foo.bin" and "foo.bin.tmp". Under a predictable
    # <dest>.tmp scheme, foo.bin's tmp IS foo.bin.tmp's final dest — concurrent
    # pulls raced and one file went missing/corrupt. The uuid tmp suffix makes
    # both land deterministically regardless of interleaving.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"foo.bin": b"F" * 200, "foo.bin.tmp": b"T" * 150})
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(),
                           reporter=Reporter(json_mode=True), jobs=8,
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 2 and not summary.failed
    assert (dest / "foo.bin").read_bytes() == b"F" * 200
    assert (dest / "foo.bin.tmp").read_bytes() == b"T" * 150
    # No orphaned mintd-tmp files left behind.
    assert not any(p.name.endswith(".mintd-tmp") for p in dest.iterdir())


def test_pull_skips_existing_untracked_file_unless_force(s3_versioned, tmp_path: Path, capsys) -> None:
    # Under the repo-path model a pull can land anywhere in the tree, so it must
    # NOT silently clobber a user's own untracked file that differs. It is kept
    # (skipped_existing) and warned — retryable with --force.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"data/x.bin": b"REMOTE" * 10})
    dest = _project(tmp_path / "clone", bucket)
    local = dest / "data" / "x.bin"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"LOCAL-mine")  # different content + size

    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(),
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 0
    assert len(summary.skipped_existing) == 1
    assert summary.skipped_existing[0].rel == "data/x.bin"
    assert not summary.failed
    # The user's file is untouched.
    assert local.read_bytes() == b"LOCAL-mine"

    # --force overwrites it from the cache.
    forced = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(),
                          force=True, s3_client_factory=_factory(s3))
    assert forced.pulled == 1 and not forced.skipped_existing
    assert local.read_bytes() == b"REMOTE" * 10


def test_pull_refuses_object_mapping_to_dvc_tracked_path(s3_versioned, tmp_path: Path) -> None:
    # A planted key whose repo path is DVC-tracked must be refused (restore a
    # tracked out with `mintd data pull`, never let the cache clobber it).
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"scratch/ok.bin": b"o" * 10})
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/data/final.parquet", Body=b"pwn",
        Metadata={"mintd-sha256": "0" * 64},
    )
    dest = _project(tmp_path / "clone", bucket)
    # Make data/final.parquet a tracked out in the clone.
    _write(dest / "data" / "final.parquet.dvc", _path_based_dvc("final.parquet"))

    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # scratch/ok.bin
    assert len(summary.failed) == 1
    assert "DVC-tracked" in (summary.failed[0].reason or "")
    assert not (dest / "data" / "final.parquet").exists()  # never written


def test_pull_refuses_object_mapping_under_git_or_dvc(s3_versioned, tmp_path: Path) -> None:
    # A hostile key that maps under .git/ or .dvc/ must never be reconstructed —
    # it could clobber .git/config / .dvc/config.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"ok.bin": b"o" * 10})
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/.git/config", Body=b"[core] evil",
        Metadata={"mintd-sha256": "0" * 64},
    )
    dest = _project(tmp_path / "clone", bucket)
    git_cfg = dest / ".git" / "config"
    git_cfg.parent.mkdir(parents=True)
    git_cfg.write_bytes(b"[core] mine")

    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # ok.bin
    assert len(summary.failed) == 1
    assert ".git" in (summary.failed[0].reason or "")
    assert git_cfg.read_bytes() == b"[core] mine"  # untouched


@pytest.mark.parametrize("segment", [".git", ".GIT", ".Git", ".dvc", ".DVC"])
def test_is_protected_repo_path_is_case_insensitive(segment: str) -> None:
    # Reshape red-team P1 (RCE): on a case-insensitive FS (macOS/Windows) a key
    # '.GIT/config' names the SAME file as '.git/config'. A case-sensitive screen
    # let a pull write into git's internals with no --force. The compare must be
    # case-folded (resolve() does NOT canonicalize case on macOS).
    assert c._is_protected_repo_path(f"{segment}/hooks/pre-commit")
    assert c._is_protected_repo_path(f"a/{segment}/x")


def test_is_tracked_is_case_insensitive() -> None:
    # Same class on the DVC-tracked screen: data/FINAL.parquet == the tracked
    # data/final.parquet on a case-insensitive FS.
    tracked = {"data/final.parquet", "data/iso"}
    assert c._is_tracked("data/FINAL.parquet", tracked)
    assert c._is_tracked("DATA/final.parquet", tracked)
    assert c._is_tracked("data/ISO/a.bin", tracked)  # under a tracked dir out
    # A prefix that is NOT under the tracked out must still NOT match.
    assert not c._is_tracked("data/final.parquet2", tracked)
    assert not c._is_tracked("data/isolate.bin", tracked)


@pytest.mark.skipif(os.name == "nt", reason="case-fold write test is POSIX-oriented")
def test_pull_refuses_case_variant_git_key_no_clobber(s3_versioned, tmp_path: Path) -> None:
    # End-to-end reshape P1: a planted '.GIT/...' key must be refused, even with
    # no pre-existing target and no --force (the RCE path). Nothing written under
    # .git/.
    import hashlib

    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"ok.bin": b"o" * 10})
    body = b"#!/bin/sh\necho PWNED\n"
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/.GIT/hooks/pre-commit",
        Body=body, Metadata={"mintd-sha256": hashlib.sha256(body).hexdigest()},
    )
    dest = _project(tmp_path / "clone", bucket)
    (dest / ".git" / "hooks").mkdir(parents=True)
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           force=True, s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # ok.bin
    assert len(summary.failed) == 1
    assert ".git" in (summary.failed[0].reason or "")
    assert not (dest / ".git" / "hooks" / "pre-commit").exists()  # never written


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows CI")
def test_pull_refuses_symlinked_intermediate_into_git(s3_versioned, tmp_path: Path) -> None:
    # Reshape red-team P2: an intra-project symlink 'linkdir -> .git' plus a
    # planted 'linkdir/config' key resolves onto .git/config. The screens must
    # run on the RESOLVED relative path, not just the raw remainder.
    import hashlib

    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"ok.bin": b"o" * 10})
    body = b"[core] evil"
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/linkdir/config",
        Body=body, Metadata={"mintd-sha256": hashlib.sha256(body).hexdigest()},
    )
    dest = _project(tmp_path / "clone", bucket)
    (dest / ".git").mkdir()
    (dest / "linkdir").symlink_to(dest / ".git")
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           force=True, s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # ok.bin
    assert len(summary.failed) == 1
    assert not (dest / ".git" / "config").exists()  # never written via the symlink


def test_enumerate_overlapping_dir_args_not_flagged_empty(tmp_path: Path) -> None:
    # Reshape red-team P2: `cache push data data/sub` re-walks already-`seen`
    # files under data/sub; those dedup to nothing NEW, but the arg is NOT empty
    # (it has files) and must not abort the whole push as 'no files under'.
    _write(tmp_path / "data" / "sub" / "a.bin", "a")
    _write(tmp_path / "data" / "top.bin", "t")
    scan = _enum(tmp_path, ["data", "data/sub"])
    assert sorted(i.rel for i in scan.items) == ["data/sub/a.bin", "data/top.bin"]
    assert scan.empty_args == []  # data/sub NOT falsely flagged empty
    assert scan.refused == []


def test_enumerate_duplicate_same_dir_arg_not_flagged_empty(tmp_path: Path) -> None:
    _write(tmp_path / "data" / "a.bin", "a")
    scan = _enum(tmp_path, ["data", "data"])
    assert [i.rel for i in scan.items] == ["data/a.bin"]  # deduped, once
    assert scan.empty_args == []


@pytest.mark.parametrize("bad", ["../etc", "/abs", "a\\b"])
def test_pull_bad_prefix_raises_value_error(s3_versioned, tmp_path: Path, bad: str) -> None:
    s3, bucket = s3_versioned
    dest = _project(tmp_path / "clone", bucket)
    with pytest.raises(ValueError):
        c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                     prefix=bad, s3_client_factory=_factory(s3))


def test_pull_tampered_object_leaves_no_final_file(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    from mintd._share_ops import file_sha256

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.bin").write_bytes(b"a" * 40)
    stale_sha = file_sha256(src / "a.bin")
    # Object body differs from its (stale) metadata sha — a corrupted/tampered
    # object. Full-file metadata verify must catch it after download.
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/a.bin", Body=b"b" * 40,
        Metadata={"mintd-sha256": stale_sha},
    )
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                           s3_client_factory=_factory(s3))
    assert summary.pulled == 0 and len(summary.failed) == 1
    assert "sha256 mismatch" in (summary.failed[0].reason or "")
    assert not (dest / "cache" / "a.bin").exists()
    assert not (dest / "cache" / "a.bin.tmp").exists()


def test_pull_metadata_governs_over_native_checksum(s3_versioned, tmp_path: Path) -> None:
    # A raw put_object (no ChecksumSHA256) with a CORRECT mintd-sha256 metadata
    # is trusted by the full-file metadata verify — and skipped on re-pull.
    s3, bucket = s3_versioned
    from mintd._share_ops import file_sha256

    body = b"m" * (1024 * 300)
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(body)
    s3.put_object(
        Bucket=bucket, Key="lab/proj/cache/big.bin", Body=body,
        Metadata={"mintd-sha256": file_sha256(src / "big.bin")},
    )
    dest = _project(tmp_path / "clone", bucket)
    first = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                         s3_client_factory=_factory(s3))
    assert first.pulled == 1
    assert (dest / "big.bin").read_bytes() == body
    second = c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                          s3_client_factory=_factory(s3))
    assert second.unchanged == 1 and second.pulled == 0


def test_pull_guard_blocks_when_out_under_cache(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"a.bin": b"a" * 10})
    dest = _project(tmp_path / "clone", bucket)
    _write(dest / "cache" / "model.dvc", _path_based_dvc("model"))
    with pytest.raises(CacheCollisionError):
        c.cache_pull(project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
                     s3_client_factory=_factory(s3))


def test_pull_ignores_folder_marker_object(s3_versioned, tmp_path: Path) -> None:
    # The S3 console and `aws s3` create 0-byte "folder marker" objects whose key
    # ends in '/'. list_product_objects returns them as non-dir objects, so the
    # trailing-slash filter (belt-and-suspenders in the same untrusted-namespace
    # threat model as the hostile-key defense) must drop them: otherwise a marker
    # `<prefix>/cache/iso/` lists back as remainder `iso/`, passes the string and
    # containment gates, and download_object tries to write onto the DIRECTORY
    # path cache/iso -> a failed outcome that exits the whole pull 1.
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"iso/real.bin": b"r" * 10})
    s3.put_object(Bucket=bucket, Key="lab/proj/cache/iso/", Body=b"")
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(
        project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
        s3_client_factory=_factory(s3),
    )
    # The marker is ignored (not pulled, not a failure); the real file lands.
    assert summary.pulled == 1 and not summary.failed
    assert (dest / "iso" / "real.bin").read_bytes() == b"r" * 10


def test_pull_intermediate_path_is_file_becomes_failed_not_traceback(
    s3_versioned, tmp_path: Path
) -> None:
    # A cache/ path component already exists as a plain FILE where the download
    # needs a directory. download_object's tmp.parent.mkdir raises FileExistsError
    # BEFORE any transport-error mapping (it is outside the mapped try block), so
    # without an OSError guard in _pull_one it escapes as a raw traceback out of
    # cli.main(). It must instead resolve to a per-file `failed` entry while the
    # rest of the pull completes.
    s3, bucket = s3_versioned
    _seed_remote(
        s3, bucket, tmp_path, {"sub/x.bin": b"x" * 10, "other.bin": b"o" * 5}
    )
    dest = _project(tmp_path / "clone", bucket)
    (dest / "sub").write_bytes(b"stray file, not a dir")
    summary = c.cache_pull(
        project_path=dest, config=_cfg(), reporter=Reporter(json_mode=True),
        s3_client_factory=_factory(s3),
    )
    assert summary.pulled == 1  # other.bin still landed
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert fail.rel == "sub/x.bin"
    assert "local filesystem error" in (fail.reason or "")
    # The stray file was not clobbered; nothing partial written.
    assert (dest / "sub").read_bytes() == b"stray file, not a dir"


def test_ls_returns_remainders_via_shared_listing(s3_versioned, tmp_path: Path) -> None:
    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"iso/a.bin": b"a" * 10, "b.bin": b"b" * 5})
    repo = resolve_repo_remote(_project(tmp_path / "clone", bucket), None)
    result = c.list_cache_objects(
        repo, sub_path=None, aws_profile_name=None, factory=_factory(s3)
    )
    keys = sorted(o.key for o in result.objects if not o.is_dir)
    assert keys == ["b.bin", "iso/a.bin"]  # relative-remainder space, no prefix


def test_ls_hint_prefix_actually_pulls_the_named_file(
    s3_versioned, tmp_path: Path, monkeypatch, capsys
) -> None:
    # The `cache ls` hint must name a PREFIX cache_pull can list under. A full
    # file key + normalise's trailing '/' matches nothing; the dirname does.
    import argparse
    import json

    from mintd import cli

    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"iso/deep/a.bin": b"a" * 20})
    clone = _project(tmp_path / "clone", bucket)
    monkeypatch.setattr(cli, "_resolve_cache_ops", lambda _cfg: _factory(s3))

    args = argparse.Namespace(
        _reporter=Reporter(json_mode=True), path=clone, remote=None,
        prefix=None, no_truncate=False,
    )
    assert cli._handle_cache_ls(args) == 0
    payload = json.loads(capsys.readouterr().out)
    hint = payload["hint"]
    assert hint.startswith("mintd cache pull")

    # Execute the hint's --prefix through cache_pull; the named file must land.
    prefix = None
    if "--prefix" in hint:
        prefix = hint.split("--prefix", 1)[1].strip()
    summary = c.cache_pull(
        project_path=clone, config=_cfg(), reporter=Reporter(json_mode=True),
        prefix=prefix, s3_client_factory=_factory(s3),
    )
    assert summary.pulled == 1
    assert (clone / "iso" / "deep" / "a.bin").read_bytes() == b"a" * 20


# ---------------------------------------------------------------------------
# C4 — lifecycle warn + naming discipline
# ---------------------------------------------------------------------------

import inspect  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402


class _LifecycleStub:
    def __init__(self, *, versioning="Enabled", rules=None, lifecycle_error=None):
        self.versioning = versioning
        self.rules = rules
        self.lifecycle_error = lifecycle_error
        self.put_called = False

    def get_bucket_versioning(self, Bucket):  # noqa: N803
        return {"Status": self.versioning} if self.versioning else {}

    def get_bucket_lifecycle_configuration(self, Bucket):  # noqa: N803
        if self.lifecycle_error is not None:
            raise self.lifecycle_error
        return {"Rules": self.rules or []}

    def put_bucket_lifecycle_configuration(self, **k):
        self.put_called = True
        raise AssertionError("mintd must never write bucket lifecycle config")


def _lifecycle_err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "GetBucketLifecycleConfiguration")


def test_lifecycle_warn_when_no_covering_rule(capsys) -> None:
    c._maybe_warn_lifecycle(_LifecycleStub(rules=[]), "b", Reporter())
    assert "never expire" in capsys.readouterr().err


def test_lifecycle_warn_on_no_such_lifecycle_config(capsys) -> None:
    stub = _LifecycleStub(lifecycle_error=_lifecycle_err("NoSuchLifecycleConfiguration"))
    c._maybe_warn_lifecycle(stub, "b", Reporter())
    assert "never expire" in capsys.readouterr().err


def test_lifecycle_silent_when_rule_covers_tag(capsys) -> None:
    stub = _LifecycleStub(rules=[_covering_rule()])
    c._maybe_warn_lifecycle(stub, "b", Reporter())
    assert capsys.readouterr().err == ""


def test_lifecycle_distinct_warn_on_access_denied(capsys) -> None:
    stub = _LifecycleStub(lifecycle_error=_lifecycle_err("AccessDenied"))
    c._maybe_warn_lifecycle(stub, "b", Reporter())
    err = capsys.readouterr().err
    assert "AccessDenied" in err and "never expire" not in err


def test_lifecycle_silent_on_unversioned_bucket(capsys) -> None:
    stub = _LifecycleStub(versioning=None, lifecycle_error=_lifecycle_err("AccessDenied"))
    c._maybe_warn_lifecycle(stub, "b", Reporter())
    assert capsys.readouterr().err == ""  # no noncurrent bill => no warn, no read


def test_lifecycle_network_error_never_fails_push(capsys) -> None:
    # A persistent botocore network error (survives retry_transient's budget) is
    # NOT a ClientError; on this read-only, post-push, best-effort probe it must
    # never propagate out and crash an already-successful push (§G).
    from botocore.exceptions import EndpointConnectionError

    stub = _LifecycleStub(
        lifecycle_error=EndpointConnectionError(endpoint_url="https://s3.example")
    )
    c._maybe_warn_lifecycle(stub, "b", Reporter())  # must not raise
    # Best-effort: swallowed silently (no misleading "never expire" warn).
    assert "never expire" not in capsys.readouterr().err


def test_mintd_never_writes_bucket_lifecycle_configuration() -> None:
    # Spy: the stub raises if put is ever called; a full warn pass must not.
    stub = _LifecycleStub(rules=[])
    c._maybe_warn_lifecycle(stub, "b", Reporter())
    assert stub.put_called is False
    # Grep: the string must not appear anywhere in the cache module.
    src = Path(c.__file__).read_text(encoding="utf-8")
    assert "put_bucket_lifecycle_configuration" not in src.lower()


# --- boundary + naming discipline (grep-enforced) ---


def _cache_surface_source() -> str:
    from mintd import cli

    src = Path(c.__file__).read_text(encoding="utf-8")
    for name in (
        "_handle_cache_push", "_handle_cache_pull", "_handle_cache_ls",
        "_render_cache_push", "_render_cache_pull", "_cache_project_preflight",
        "_resolve_cache_ops",
        # The cache subparsers' --help strings are the user-facing surface the
        # SLICE-37 retro said rots under prose-only norms; grep them too.
        "_add_cache_parser",
    ):
        src += inspect.getsource(getattr(cli, name))
    return src


def test_no_transport_calls_in_cache_module() -> None:
    # Every byte moves through S1's transport; the data plane must be empty here.
    # Match call syntax (``.name(``) so the boundary docstring may still name
    # the primitives it forbids calling.
    src = Path(c.__file__).read_text(encoding="utf-8").lower()
    for banned in (".upload_file(", ".download_file(", ".put_object("):
        assert banned not in src, f"{banned} — widen S1's core, never fork transport"


# --- naming discipline: allowlist over EVERY user-facing string literal ---
#
# The prior guard was a fixed denylist of ~8 bad substrings, so any fresh
# bare-"cache" phrasing that was not one of those substrings shipped silently
# (a mutation of the durability hint "the repo file cache (S3) is durable" ->
# "the cache is durable" stayed green). This replaces it with a POSITIVE
# allowlist: parse the cache surface, extract every string literal that reaches
# a user-facing sink (reporter.success/error/warn/info/result/status/progress,
# the CacheError family's msg+hint, and argparse help=/hint=/desc=), and for any
# literal that still names "cache" after the approved uses are stripped, fail.

_SINK_ATTRS = {"success", "error", "warn", "info", "result", "status", "progress"}
_SINK_FUNCS = {"CacheError", "CacheKeyError", "CacheCollisionError", "TransferError"}
_KW_SINKS = {"help", "hint", "desc"}
# The approved appearances of the word "cache" in user-facing text: the full
# store name, the local `cache/` directory, the `mintd cache` command, and the
# per-object tag token. Everything else is a bare-noun regression.
_ALLOWED_CACHE_USES = ["repo file cache (s3)", "cache/", "mintd cache", "mintd-lane=cache"]


def _static_text(node: ast.AST) -> str | None:
    """Static text of a string literal / f-string / adjacent-or-``+`` concat;
    ``{expr}`` and non-literal ``+`` operands collapse to a neutral placeholder
    so code identifiers (``cache_list_prefix``, ``CACHE_DIR_NAME``, …) inside an
    f-string can never inject a spurious "cache". ``None`` if not text-shaped."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left), _static_text(node.right)
        if left is None and right is None:
            return None
        return (left or "{}") + (right or "{}")
    return None


def _sink_literals(scopes: list[ast.AST]) -> list[str]:
    lits: list[str] = []
    for scope in scopes:
        for call in ast.walk(scope):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            candidates: list[ast.AST] = []
            if (isinstance(f, ast.Attribute) and f.attr in _SINK_ATTRS) or (
                isinstance(f, ast.Name) and f.id in _SINK_FUNCS
            ):
                candidates.extend(call.args)  # positional msg / desc
            candidates.extend(kw.value for kw in call.keywords if kw.arg in _KW_SINKS)
            for node in candidates:
                text = _static_text(node)
                if text is not None:
                    lits.append(text)
    return lits


def _cache_user_facing_literals() -> list[str]:
    from mintd import cli

    ops_tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))  # whole module is cache
    scopes: list[ast.AST] = [ops_tree]
    cli_tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    cache_fns = {
        "_handle_cache_push", "_render_cache_push", "_handle_cache_pull",
        "_render_cache_pull", "_handle_cache_ls", "_cache_project_preflight",
        "_resolve_cache_ops", "_add_cache_parser",
    }
    scopes.extend(
        n for n in ast.walk(cli_tree)
        if isinstance(n, ast.FunctionDef) and n.name in cache_fns
    )
    return _sink_literals(scopes)


def _strip_allowed_cache_uses(text: str) -> str:
    lowered = text.lower()
    for allowed in _ALLOWED_CACHE_USES:
        lowered = lowered.replace(allowed, "")
    return lowered


def test_user_facing_text_uses_repo_file_cache_phrase() -> None:
    lits = _cache_user_facing_literals()
    # Sanity: the extraction actually found the surface (guards against a silent
    # refactor that renames the sinks and makes this test vacuous).
    assert any("repo file cache (s3)" in t.lower() for t in lits)
    # Allowlist: after the approved uses are stripped, no literal may still name
    # "cache". A fresh bare-"cache" reporter string now fails instead of shipping.
    offenders = [t for t in lits if "cache" in _strip_allowed_cache_uses(t)]
    assert not offenders, (
        "user-facing text names a bare 'cache' outside the approved phrase "
        f"'repo file cache (S3)': {offenders}"
    )


def test_progress_desc_strings_name_the_full_phrase() -> None:
    # Positive (allowlist) check, not just a denylist: EVERY user-visible rich
    # progress `desc=` label on the cache surface must carry the full "repo file
    # cache (S3)" phrase. The denylist above cannot catch a fresh bare-"cache"
    # progress label like `desc="Pushing cache"` (SLICE-37: prose-only naming
    # conventions rot), so pin the progress labels explicitly.
    descs = re.findall(r'desc="([^"]*)"', Path(c.__file__).read_text(encoding="utf-8"))
    assert descs, "expected the cache surface to have progress desc labels"
    for d in descs:
        assert "repo file cache (S3)" in d, (
            f"progress desc {d!r} must name the full 'repo file cache (S3)' phrase"
        )


def test_push_missing_bucket_maps_to_cache_error_not_traceback(s3_versioned, tmp_path: Path) -> None:
    s3, _bucket = s3_versioned
    proj = _project(tmp_path, "no-such-bucket")  # bucket never created
    (proj / "cache").mkdir()
    (proj / "cache" / "a.bin").write_bytes(b"a")
    with pytest.raises(CacheError) as ei:
        c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                     reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert "could not list the repo file cache (S3)" in str(ei.value)


def test_pull_missing_bucket_maps_to_cache_error_not_traceback(s3_versioned, tmp_path: Path) -> None:
    s3, _bucket = s3_versioned
    proj = _project(tmp_path, "no-such-bucket")
    with pytest.raises(CacheError):
        c.cache_pull(project_path=proj, config=_cfg(), reporter=Reporter(json_mode=True),
                     s3_client_factory=_factory(s3))


# ---------------------------------------------------------------------------
# Red-team regressions
# ---------------------------------------------------------------------------


def test_enumerate_root_file_named_cache_is_cacheable(tmp_path: Path) -> None:
    # Under the repo-path model a regular file literally named `cache` at the
    # root is just another untracked file — cacheable (it maps to the doubled key
    # cache/cache). No longer a boundary-key hazard: it round-trips on pull.
    (tmp_path / "cache").write_bytes(b"i am a file")
    scan = _enum(tmp_path, ["cache"])
    assert [i.rel for i in scan.items] == ["cache"]
    assert scan.refused == []


def test_push_pull_file_named_cache_round_trips_via_doubled_key(
    s3_versioned, tmp_path: Path
) -> None:
    # End-to-end: a root file named `cache` uploads to <prefix>/cache/cache and a
    # teammate's pull reconstructs it at `cache` — no data loss, fully retrievable
    # (the old model refused it as an unretrievable boundary key).
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").write_bytes(b"payload-42")
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.uploaded == 1
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"]]
    assert keys == ["lab/proj/cache/cache"]
    clone = _project(tmp_path / "clone", bucket)
    pulled = c.cache_pull(project_path=clone, config=_cfg(),
                          reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert pulled.pulled == 1
    assert (clone / "cache").read_bytes() == b"payload-42"


def test_push_cache_directory_maps_under_lane_segment(
    s3_versioned, tmp_path: Path
) -> None:
    # Pushing a top-level cache/ DIRECTORY still enumerates its children; each
    # maps under the lane's own cache/ segment (the deliberate doubling).
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache" / "sub").mkdir(parents=True)
    (proj / "cache" / "a.bin").write_bytes(b"a" * 10)
    (proj / "cache" / "sub" / "b.bin").write_bytes(b"b" * 10)
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.uploaded == 2
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["lab/proj/cache/cache/a.bin", "lab/proj/cache/cache/sub/b.bin"]


@pytest.mark.parametrize(
    "bad",
    [
        "legit.bin\n✓ pulled 999 file(s) from the repo file cache (S3)",
        "a\tb",
        "x\rY",
        "esc\x1b[32mgreen\x1b[0m.bin",
        "nul\x00byte",
    ],
)
def test_is_forbidden_path_rejects_control_chars(bad: str) -> None:
    # C0 controls / DEL / ESC never belong in a cache key; they enable log
    # injection and forged status lines on the pull path.
    assert c._is_forbidden_path(bad)
    with pytest.raises(CacheKeyError):
        safe_cache_remainder(bad)


def test_pull_rejects_control_char_planted_key_no_file_written(
    s3_versioned, tmp_path: Path
) -> None:
    # A key with an embedded newline + a forged '✓ pulled …' success line and a
    # CORRECT mintd-sha256 (so decide_pull would download+write it). It must be
    # refused at the string gate: no file is written whose NAME is the forged
    # status line, and the failed reason carries no raw control char (so it
    # cannot forge lines or inject ANSI when echoed to stderr).
    import hashlib

    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"good.bin": b"g" * 10})
    forged = "pwned.bin\n✓ pulled 999999 file(s) (999.0 TB) from the repo file cache (S3)"
    hostile_key = f"lab/proj/cache/{forged}"
    body = b"pwn"
    s3.put_object(
        Bucket=bucket, Key=hostile_key, Body=body,
        Metadata={"mintd-sha256": hashlib.sha256(body).hexdigest()},
    )
    dest = _project(tmp_path / "clone", bucket)
    summary = c.cache_pull(project_path=dest, config=_cfg(),
                           reporter=Reporter(json_mode=True), s3_client_factory=_factory(s3))
    assert summary.pulled == 1  # good.bin only
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert "unsafe key refused" in (fail.reason or "")
    # No raw control chars leaked into the message that gets echoed to stderr.
    assert "\n" not in (fail.reason or "") and "\x1b" not in (fail.reason or "")
    assert "\n" not in fail.rel and "\x1b" not in fail.rel
    # Nothing named after the forged line exists anywhere under the clone.
    assert not any("pulled 999999" in p.name for p in (dest / "cache").rglob("*"))
    assert not any("pulled 999999" in p.name for p in dest.rglob("*"))


def test_pull_control_char_key_reason_reaches_stderr_escaped(
    s3_versioned, tmp_path: Path, monkeypatch, capsys
) -> None:
    # Drive the full CLI render: the forged newline must NOT create an extra
    # stderr line — the reporter.error text stays single-line (escaped).
    import argparse
    import hashlib

    from mintd import cli

    s3, bucket = s3_versioned
    _seed_remote(s3, bucket, tmp_path, {"good.bin": b"g" * 10})
    body = b"pwn"
    s3.put_object(
        Bucket=bucket,
        Key="lab/proj/cache/x.bin\n✓ pulled 5 file(s) from the repo file cache (S3)",
        Body=body, Metadata={"mintd-sha256": hashlib.sha256(body).hexdigest()},
    )
    clone = _project(tmp_path / "clone", bucket)
    monkeypatch.setattr(cli, "_resolve_cache_ops", lambda _cfg: _factory(s3))

    args = argparse.Namespace(
        _reporter=Reporter(), path=clone, remote=None, prefix=None, jobs=8,
        force=False,
    )
    rc = cli._handle_cache_pull(args)
    assert rc == 1  # a refused key fails the pull
    err = capsys.readouterr().err
    # The forged '✓ pulled 5 …' text must not appear as its own status-looking
    # line: it is escaped inline, so there is no bare line starting with '✓'.
    assert not any(line.lstrip().startswith("✓") for line in err.splitlines())


class _VanishBeforeUploadClient(_CountingClient):
    """Simulates the producer-TOCTOU: the local file is present when hashed but
    a producer removes it just before boto3's ``upload_file`` opens it. Deleting
    the file and delegating to the real client makes boto3 raise the genuine
    ``FileNotFoundError`` (an OSError), exactly as a real rotation would."""

    def __init__(self, real: object, victim_key_fragment: str) -> None:
        super().__init__(real)
        self.victim = victim_key_fragment

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Callback=None):  # noqa: N803
        if self.victim in key:
            os.unlink(filename)  # producer removed it between hash and transfer
        return super().upload_file(filename, bucket, key, ExtraArgs=ExtraArgs, Callback=Callback)


def test_push_file_vanishing_before_upload_becomes_failed_not_traceback(
    s3_versioned, tmp_path: Path
) -> None:
    # The producer-TOCTOU window the file_sha256 guards do NOT cover: boto3's
    # upload_file re-opens the path itself, so a file removed AFTER hashing but
    # BEFORE the transfer opens it raises FileNotFoundError out of upload_object
    # (which does not map OSError). Without the except OSError on the upload leg
    # this escapes the executor as a raw traceback out of cli.main(). It must
    # instead be a per-file `failed` ledger entry while the rest of the push
    # completes and the command exits 1 with a hinted retry.
    s3, bucket = s3_versioned
    proj = _project(tmp_path, bucket)
    (proj / "cache").mkdir()
    (proj / "cache" / "ok.bin").write_bytes(b"ok" * 10)
    (proj / "cache" / "vanish.bin").write_bytes(b"v" * 20)
    client = _VanishBeforeUploadClient(s3, "vanish.bin")
    summary = c.cache_push(project_path=proj, paths=["cache"], config=_cfg(),
                           reporter=Reporter(json_mode=True), jobs=2,
                           s3_client_factory=_factory(client))
    assert summary.uploaded == 1  # ok.bin still landed
    assert len(summary.failed) == 1
    fail = summary.failed[0]
    assert fail.rel == "cache/vanish.bin"
    assert "local read error" in (fail.reason or "")
    assert "mintd cache push cache/vanish.bin" in (fail.hint or "")
