"""Stratum P (policy + orchestrator) tests for ``mintd._share_ops``.

Pure-unit matrices (identity precedence, ref grammar, key building, preflight)
plus moto round-trips through the orchestrators with a spy Reporter.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from mintd._config import Config
from mintd._share_ops import (
    GetResult,
    PutResult,
    ShareError,
    build_put_key,
    file_sha256,
    parse_share_ref,
    resolve_share_user,
    share_get,
    share_put,
)


class _SpyReporter:
    """Minimal Reporter double: records progress contexts, advances, warnings."""

    def __init__(self) -> None:
        self.progress_calls: list[tuple[int, str]] = []
        self.advances: list[list[int]] = []
        self.warnings: list[str] = []

    @contextmanager
    def progress(self, total: int, *, desc: str):
        self.progress_calls.append((total, desc))
        adv: list[int] = []
        self.advances.append(adv)
        yield adv.append

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _factory_never(_cfg, _prof):
    raise AssertionError("s3_client_factory must not be called (preflight breach)")


def _cfg(**kw) -> Config:
    base = {"storage_bucket_prefix": "test-bucket", "storage_endpoint": "https://s3"}
    base.update(kw)
    return Config(**base)


# ---------- resolve_share_user ----------


def test_resolve_share_user_prefers_explicit_share_user() -> None:
    assert resolve_share_user(_cfg(share_user="alice")) == ("alice", "share_user")


def test_resolve_share_user_invalid_share_user_raises() -> None:
    with pytest.raises(ShareError) as ei:
        resolve_share_user(_cfg(share_user="not a slug"))
    assert ei.value.hint is not None
    assert "share_user" in ei.value.hint


def test_resolve_share_user_falls_back_to_slugified_author() -> None:
    assert resolve_share_user(_cfg(author="Maurice Dalton")) == (
        "maurice-dalton",
        "author",
    )


def test_resolve_share_user_collapses_whitespace_runs() -> None:
    assert resolve_share_user(_cfg(author="Maurice   Dalton")) == (
        "maurice-dalton",
        "author",
    )


def test_resolve_share_user_share_user_wins_over_author() -> None:
    assert resolve_share_user(_cfg(share_user="pinned", author="Someone Else")) == (
        "pinned",
        "share_user",
    )


def test_resolve_share_user_whitespace_only_author_is_not_usable() -> None:
    with pytest.raises(ShareError) as ei:
        resolve_share_user(_cfg(author="   "))
    assert str(ei.value) == "cannot determine share user"


def test_resolve_share_user_neither_raises_with_config_setup_hint() -> None:
    with pytest.raises(ShareError) as ei:
        resolve_share_user(_cfg())
    assert str(ei.value) == "cannot determine share user"
    assert "config setup" in ei.value.hint


@pytest.mark.parametrize("bad", ["..", "."])
def test_resolve_share_user_rejects_dot_segments(bad: str) -> None:
    # SLUG_REGEX matches "." and "..", which would build keys like share/../f;
    # reject them so no share user can normalise outside share/<user>/.
    with pytest.raises(ShareError):
        resolve_share_user(_cfg(share_user=bad))


def test_resolve_share_user_non_slug_author_falls_through() -> None:
    # "José" slugifies to "josé" which fails SLUG_REGEX → not usable.
    with pytest.raises(ShareError):
        resolve_share_user(_cfg(author="José"))


# ---------- parse_share_ref ----------


def test_parse_ref_zero_sub_segments() -> None:
    assert parse_share_ref("alice/x.parquet") == ("alice", "", "x.parquet")


def test_parse_ref_one_sub_segment() -> None:
    assert parse_share_ref("alice/grant/x.parquet") == ("alice", "grant/", "x.parquet")


def test_parse_ref_many_sub_segments() -> None:
    assert parse_share_ref("alice/a/b/c/x.parquet") == ("alice", "a/b/c/", "x.parquet")


@pytest.mark.parametrize(
    "ref",
    [
        "/alice/x.parquet",  # leading slash
        "alice/../x.parquet",  # .. in sub
        "alice/..",  # .. filename
        "alice/sub\\x.parquet",  # backslash in filename
        "alice\\evil/x.parquet",  # backslash user rejected by SLUG_REGEX
        "not a slug/x.parquet",  # bad user slug
        "../secret.parquet",  # user segment ".." (SLUG_REGEX matches it)
        "./secret.parquet",  # user segment "."
        "alice/",  # trailing-slash folder ref
        "alice/sub/",  # trailing-slash folder ref
        "aliceonly",  # lone-user / missing filename
    ],
)
def test_parse_ref_rejects_escapes_and_folder_refs(ref: str) -> None:
    with pytest.raises(ShareError):
        parse_share_ref(ref)


def test_parse_ref_folder_ref_hint_mentions_s5() -> None:
    with pytest.raises(ShareError) as ei:
        parse_share_ref("alice/grant/")
    assert "S5" in ei.value.hint


# ---------- adversarial-QA regressions (red-team 2026-07-07) ----------


def test_resolve_share_user_rejects_trailing_newline_slug() -> None:
    """Python's regex `$` matches before a trailing newline; the guard uses
    fullmatch so a share_user ending in '\\n' cannot smuggle a control char
    into the S3 key."""
    with pytest.raises(ShareError, match="invalid share_user"):
        resolve_share_user(_cfg(share_user="alice\n"))


def test_parse_ref_rejects_trailing_newline_user() -> None:
    with pytest.raises(ShareError, match="invalid share user"):
        parse_share_ref("alice\n/grant/x.parquet")


@pytest.mark.parametrize("bad_ref", [
    "alice/grant/x\x00.parquet",  # NUL in filename
    "alice/grant/x\n.parquet",    # newline in filename
    "alice/gr\x00ant/x.parquet",  # NUL in sub-path
    "alice/gr\tant/x.parquet",    # tab in sub-path
])
def test_parse_ref_rejects_control_chars(bad_ref: str) -> None:
    with pytest.raises(ShareError):
        parse_share_ref(bad_ref)


@pytest.mark.parametrize("bad_as", ["x\x00.parquet", "sub\n/x.parquet", "x\t.parquet"])
def test_build_put_key_rejects_control_chars(bad_as: str) -> None:
    with pytest.raises(ShareError):
        build_put_key("alice", "model.parquet", bad_as)


# ---------- bare-ref "did you mean" (self-fetch nicety) ----------


def test_share_get_bare_filename_suggests_own_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare filename (sender segment dropped) with a resolvable identity
    errors with a ready-to-run suggestion — the self-fetch slip."""
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="uv.lock",
            config=_cfg(share_user="maurice-dalton"),
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,  # preflight breach if reached
        )
    assert "missing the sender" in str(ei.value)
    assert "did you mean: mintd share get maurice-dalton/uv.lock" in ei.value.hint


def test_share_get_bare_filename_no_identity_falls_back_to_base_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable identity → the generic base hint, never a suggestion
    (get stays zero-identity)."""
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="uv.lock",
            config=_cfg(),  # no share_user, no author
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "did you mean" not in (ei.value.hint or "")
    assert "the sender's share_user" in ei.value.hint


def test_share_get_ref_with_slash_error_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ref that has a '/' but fails for another reason keeps its specific
    error — the suggestion only fires for the bare-filename case."""
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="../secret.parquet",  # bad user segment '..'
            config=_cfg(share_user="maurice-dalton"),
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "did you mean" not in (ei.value.hint or "")


# ---------- build_put_key ----------


def test_build_put_key_no_as() -> None:
    assert build_put_key("alice", "model.parquet", None) == "share/alice/model.parquet"


def test_build_put_key_trailing_slash_into_folder() -> None:
    assert (
        build_put_key("alice", "model.parquet", "grant-2026/")
        == "share/alice/grant-2026/model.parquet"
    )


def test_build_put_key_full_path_renames() -> None:
    assert (
        build_put_key("alice", "model.parquet", "grant/final.parquet")
        == "share/alice/grant/final.parquet"
    )


@pytest.mark.parametrize("as_value", ["/grant/x", "../x", "a/../b/x", "sub\\x"])
def test_build_put_key_rejects_escapes(as_value: str) -> None:
    with pytest.raises(ShareError):
        build_put_key("alice", "model.parquet", as_value)


# ---------- preflight-before-bytes (R6) ----------


def test_share_put_missing_bucket_config_never_calls_s3(tmp_path: Path) -> None:
    src = tmp_path / "f.parquet"
    src.write_bytes(b"x")
    cfg = Config(storage_endpoint="https://s3")  # no bucket
    with pytest.raises(ShareError) as ei:
        share_put(
            local_path=src,
            user="alice",
            config=cfg,
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "config setup" in ei.value.hint


def test_share_get_missing_endpoint_config_never_calls_s3() -> None:
    cfg = Config(storage_bucket_prefix="test-bucket")  # no endpoint
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="alice/x.parquet",
            config=cfg,
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "config setup" in ei.value.hint


def test_share_put_missing_local_file_never_calls_s3(tmp_path: Path) -> None:
    with pytest.raises(ShareError) as ei:
        share_put(
            local_path=tmp_path / "nope.parquet",
            user="alice",
            config=_cfg(),
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "no such file" in str(ei.value)


def test_share_put_directory_as_file_never_calls_s3(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(ShareError) as ei:
        share_put(
            local_path=d,
            user="alice",
            config=_cfg(),
            reporter=_SpyReporter(),
            s3_client_factory=_factory_never,
        )
    assert "not a file" in str(ei.value)


# ---------- moto round-trips through the orchestrators ----------


def test_roundtrip_through_orchestrators_byte_identical(
    s3_versioned, tmp_path: Path
) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    cfg = _cfg(author="Maurice Dalton")
    src = tmp_path / "model.parquet"
    src.write_bytes(b"payload" * 5000)

    put_rep = _SpyReporter()
    put = share_put(
        local_path=src,
        user="maurice-dalton",
        config=cfg,
        reporter=put_rep,
        as_value="grant-2026/",
        s3_client_factory=factory,
    )
    assert isinstance(put, PutResult)
    assert put.key == "share/maurice-dalton/grant-2026/model.parquet"

    out_dir = tmp_path / "inbox"
    out_dir.mkdir()
    get_rep = _SpyReporter()
    got = share_get(
        ref="maurice-dalton/grant-2026/model.parquet",
        config=cfg,
        reporter=get_rep,
        out=out_dir,
        s3_client_factory=factory,
    )
    assert isinstance(got, GetResult)
    assert got.dest == out_dir / "model.parquet"
    assert file_sha256(src) == file_sha256(got.dest)

    # R7: progress entered once per verb, total == size, advances sum to bytes.
    assert put_rep.progress_calls == [(src.stat().st_size, "Uploading model.parquet")]
    assert sum(put_rep.advances[0]) == src.stat().st_size
    assert get_rep.progress_calls == [
        (src.stat().st_size, "Downloading model.parquet")
    ]
    assert sum(get_rep.advances[0]) == src.stat().st_size


def test_share_get_works_with_no_identity_configured(
    s3_versioned, tmp_path: Path
) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    # Seed an object as alice.
    src = tmp_path / "src.parquet"
    src.write_bytes(b"zero-setup-receive")
    share_put(
        local_path=src,
        user="alice",
        config=_cfg(share_user="alice"),
        reporter=_SpyReporter(),
        s3_client_factory=factory,
    )
    # Receiver has NO share_user and NO author. The --out value is a genuine
    # trailing-slash string for a directory that does NOT yet exist, exactly as
    # argparse (type=str) delivers it — the file must land INSIDE that new
    # directory, not become a file literally named "inbox".
    receiver_cfg = _cfg()
    inbox = tmp_path / "inbox"
    assert not inbox.exists()
    got = share_get(
        ref="alice/src.parquet",
        config=receiver_cfg,
        reporter=_SpyReporter(),
        out=f"{inbox}/",
        s3_client_factory=factory,
    )
    assert got.dest == inbox / "src.parquet"
    assert inbox.is_dir()
    assert file_sha256(got.dest) == file_sha256(src)


def test_share_get_missing_remote_key_raises_ref_based_share_error(
    s3_versioned, tmp_path: Path
) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="alice/nope.parquet",
            config=_cfg(),
            reporter=_SpyReporter(),
            out=tmp_path,
            s3_client_factory=factory,
        )
    assert str(ei.value) == "no share object at alice/nope.parquet"
    assert ei.value.hint == (
        "check the ref with the sender; share objects may also have expired"
    )


def test_share_get_refuses_existing_dest(s3_versioned, tmp_path: Path) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    src = tmp_path / "src.parquet"
    src.write_bytes(b"data")
    share_put(
        local_path=src,
        user="alice",
        config=_cfg(share_user="alice"),
        reporter=_SpyReporter(),
        s3_client_factory=factory,
    )
    existing = tmp_path / "dest.parquet"
    existing.write_bytes(b"do not clobber")
    with pytest.raises(ShareError) as ei:
        share_get(
            ref="alice/src.parquet",
            config=_cfg(),
            reporter=_SpyReporter(),
            out=existing,
            s3_client_factory=factory,
        )
    assert "refusing to overwrite" in str(ei.value)
    assert existing.read_bytes() == b"do not clobber"


def test_share_get_no_stored_checksum_warns(tmp_path: Path) -> None:
    # Stub head returns no ChecksumSHA256 → reporter.warn recorded, download
    # still proceeds. Uses a stub client (moto checksum fidelity not trusted).
    class _StubClient:
        def head_object(self, **k):
            return {"ContentLength": 4}

        def download_file(self, Bucket, Key, Filename, ExtraArgs=None, Callback=None):  # noqa: N803
            Path(Filename).write_bytes(b"data")
            if Callback is not None:
                Callback(4)

    rep = _SpyReporter()
    got = share_get(
        ref="alice/x.parquet",
        config=_cfg(),
        reporter=rep,
        out=tmp_path / "x.parquet",
        s3_client_factory=lambda _c, _p: _StubClient(),
    )
    assert got.bytes == 4
    assert any("no stored SHA256" in w for w in rep.warnings)


# ---------- dest resolution ----------


def test_get_dest_default_is_cwd_filename(s3_versioned, tmp_path: Path, monkeypatch) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    src = tmp_path / "src.parquet"
    src.write_bytes(b"data")
    share_put(
        local_path=src,
        user="alice",
        config=_cfg(share_user="alice"),
        reporter=_SpyReporter(),
        s3_client_factory=factory,
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    got = share_get(
        ref="alice/src.parquet",
        config=_cfg(),
        reporter=_SpyReporter(),
        out=None,
        s3_client_factory=factory,
    )
    assert got.dest == Path("src.parquet")
    assert (workdir / "src.parquet").exists()


def test_get_dest_explicit_file_path_used_verbatim(
    s3_versioned, tmp_path: Path
) -> None:
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    src = tmp_path / "src.parquet"
    src.write_bytes(b"data")
    share_put(
        local_path=src,
        user="alice",
        config=_cfg(share_user="alice"),
        reporter=_SpyReporter(),
        s3_client_factory=factory,
    )
    target = tmp_path / "renamed.parquet"
    got = share_get(
        ref="alice/src.parquet",
        config=_cfg(),
        reporter=_SpyReporter(),
        out=target,
        s3_client_factory=factory,
    )
    assert got.dest == target
    assert target.exists()


def test_get_dest_trailing_slash_into_nonexistent_dir(
    s3_versioned, tmp_path: Path
) -> None:
    # --out results/ where results/ does not exist yet: the file must land at
    # results/<filename>, and the directory is created — NOT a file named
    # "results". Regression for the type=Path trailing-separator loss.
    s3, _bucket = s3_versioned
    factory = lambda _cfg, _prof: s3  # noqa: E731
    src = tmp_path / "model.parquet"
    src.write_bytes(b"data")
    share_put(
        local_path=src,
        user="alice",
        config=_cfg(share_user="alice"),
        reporter=_SpyReporter(),
        s3_client_factory=factory,
    )
    results = tmp_path / "results"
    assert not results.exists()
    got = share_get(
        ref="alice/model.parquet",
        config=_cfg(),
        reporter=_SpyReporter(),
        out=f"{results}/",
        s3_client_factory=factory,
    )
    assert got.dest == results / "model.parquet"
    assert results.is_dir()
    assert (results / "model.parquet").is_file()
