"""Consumer-side import rescue lane.

Automates the field-verified manual workaround for the case where a
``dvc import``'s producer bucket holds worktree-layout (``version_aware``)
dvc-tracked objects with **no recorded version_id** in the producer's
committed lock. On that shape plain ``dvc pull`` cannot materialize the
import at all (dvc 3.66.x builds storage for an import only from recorded
version info — there is no ETag-vs-md5 fallback on the erepo path), so the
import is unpullable by any dvc invocation even though the bytes on S3 are
fine and md5-verifiable. See
``notes/issues/issue-import-pull-version-aware-no-version-ids.md``.

This is NOT an extension of fast-sync (SLICE-29 correctly rejected teaching
fast-sync about second buckets). It fires only *after* a per-import
``dvc pull`` has already failed to materialize the import — healthy imports
behave byte-identically to today. Given a failed import, it resolves the
producer's S3 remote from the import's pinned rev, downloads each
worktree-layout object, stream-verifies its md5 against the pinned manifest,
seeds the consumer's DVC cache, and ``dvc checkout``s the import.

Boundaries:
  - The consumer's AWS profile is reused for the producer's bucket (the lab
    is a single-account layout); a cross-account bucket the consumer can't
    read lands on the AccessDenied path with an actionable hint, not data.
  - Enclave / network-restricted consumers that cannot reach the producer's
    git repo land on the ``FetchError`` path with an actionable error, not
    data — the rescue reads the producer's ``.dvc/config`` (and, when needed,
    its pointer files) at pull time via git.
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import imports
from ._fast_sync_ops import (
    _DEFAULT_DVC_CACHE_REL,
    _DRIFT_404_CODES,
    _default_remote_name_from_config,
    _extract_version_id_from_file_entry,
    ClientError,
    DvcFileEntry,
    boto3,
    cache_path_for,
    ensure_dir_manifest,
    is_cached,
    _create_s3_client,
    outs_for_target,
    outs_materialized,
    parse_remote_config_text,
    parse_s3_url,
    read_cached_dir_manifest,
    fetch_to_cache,
)
from ._producer_git_ops import FetchError, GitArchiveFetcher

if TYPE_CHECKING:
    from ._console import Reporter
    from ._dvc_ops import DvcOps
    from ._producer_git_ops import Fetcher

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RescueResult:
    ok: bool
    files_fetched: int = 0
    reason: str = ""  # one-line, user-facing
    hint: str | None = None


@dataclass(frozen=True)
class _Expected:
    """One file the rescue must land: its dir-relative path (``""`` for a
    single-file out), its pinned md5, an optional pinned S3 version_id, and
    its size (for the .dir manifest)."""
    relpath: str
    md5: str
    version_id: str | None = None
    size: int = 0


@dataclass(frozen=True)
class _Producer:
    """Resolved producer S3 location (memoized per (repo, pin) per pull run)."""
    bucket: str
    prefix: str
    remote_cfg: dict[str, str]
    remote_name: str


def _consumer_remote_name(project_path: Path) -> str:
    """Best-effort default DVC remote name for the consumer project."""
    cfg = project_path / ".dvc" / "config"
    try:
        cp = configparser.ConfigParser()
        cp.read(cfg)
        return _default_remote_name_from_config(cp) or "origin"
    except Exception:
        return "origin"


def _fetch_error_reason(exc: FetchError, repo: str) -> tuple[str, str]:
    """Map a producer ``FetchError`` to (reason, hint)."""
    if exc.reason is FetchError.Reason.PIN_MISSING:
        return (
            f"the pinned rev no longer exists in the producer repo ({repo})",
            "ask the producer to restore the pinned revision, or re-pin the import",
        )
    if exc.reason is FetchError.Reason.UNREACHABLE:
        return (
            f"could not reach the producer repo ({repo})",
            f"check git access to {repo}",
        )
    return (
        f"could not read the producer repo ({repo}): {exc.detail or exc.reason}",
        f"check git access to {repo}",
    )


def _resolve_producer(
    dep: "imports.DataDependency",
    *,
    fetcher: "Fetcher",
    aws_profile_name: str | None,
    cache: dict[tuple[str, str], _Producer],
) -> tuple[_Producer | None, RescueResult | None]:
    """Fetch + parse the producer's ``.dvc/config`` at the pin, resolve the
    S3 bucket/prefix. Memoized by (repo, pin) so several imports from one
    producer fetch its config once. Returns ``(_Producer, None)`` on success
    or ``(None, RescueResult)`` with the failure to surface."""
    key = (dep.producer_repo, dep.contract_pin)
    if key in cache:
        return cache[key], None

    try:
        config_bytes = fetcher.fetch_path_at(
            dep.producer_repo, dep.contract_pin, ".dvc/config"
        )
    except FetchError as exc:
        if exc.reason is FetchError.Reason.PATH_MISSING:
            return None, RescueResult(
                ok=False,
                reason=(
                    f"the producer repo ({dep.producer_repo}) has no .dvc/config "
                    f"at the pinned rev"
                ),
                hint="ask the producer to commit a DVC remote config",
            )
        reason, hint = _fetch_error_reason(exc, dep.producer_repo)
        return None, RescueResult(ok=False, reason=reason, hint=hint)

    config_text = config_bytes.decode("utf-8", errors="replace")
    cp = configparser.ConfigParser()
    try:
        cp.read_string(config_text)
    except configparser.Error as exc:
        return None, RescueResult(
            ok=False,
            reason=f"could not parse the producer's .dvc/config ({exc})",
            hint="ask the producer to check their .dvc/config",
        )
    remote_name = _default_remote_name_from_config(cp)
    if remote_name is None:
        return None, RescueResult(
            ok=False,
            reason=(
                f"the producer's .dvc/config ({dep.producer_repo}) has no single "
                f"default remote"
            ),
            hint="ask the producer to set a [core] remote in .dvc/config",
        )
    try:
        remote_cfg = parse_remote_config_text(config_text, remote_name)
    except KeyError as exc:
        return None, RescueResult(
            ok=False,
            reason=f"could not read the producer's remote config: {exc}",
            hint="ask the producer to check their .dvc/config",
        )
    try:
        bucket, prefix = parse_s3_url(remote_cfg.get("url", ""))
    except ValueError:
        return None, RescueResult(
            ok=False,
            reason=(
                f"the producer's remote is not an S3 remote "
                f"({remote_cfg.get('url', '')!r})"
            ),
            hint="the rescue lane only supports S3-backed producers",
        )

    producer = _Producer(
        bucket=bucket, prefix=prefix, remote_cfg=remote_cfg, remote_name=remote_name
    )
    cache[key] = producer
    return producer, None


def _entries_from_consumer_out(out: Any) -> list[_Expected] | None:
    """Source (a): files-format entries already on the consumer's parsed out."""
    if not out.files:
        return None
    return [
        _Expected(relpath=fe.relpath, md5=fe.md5, version_id=fe.version_id, size=fe.size)
        for fe in out.files
    ]


def _entries_from_producer_pointer(
    dep: "imports.DataDependency",
    producer: _Producer,
    *,
    fetcher: "Fetcher",
) -> list[_Expected] | None:
    """Source (b): the producer's pointer file at the pin.

    Tries ``<output_path>.dvc``, then ``dvc.lock`` on PATH_MISSING. Reads the
    matching out's ``files:`` list (md5/relpath/size, and version_id when the
    pinned rev recorded one). Best-effort: any fetch/parse failure returns
    ``None`` so the caller falls through to the locally-cached manifest."""
    output_path = dep.output_path
    for candidate in (f"{output_path}.dvc", "dvc.lock"):
        try:
            raw = fetcher.fetch_path_at(dep.producer_repo, dep.contract_pin, candidate)
        except FetchError:
            continue
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        entries = _match_out_files(data, output_path, producer.remote_name)
        if entries is not None:
            return entries
    return None


def _match_out_files(
    data: Any, output_path: str, remote_name: str
) -> list[_Expected] | None:
    """Find the out matching ``output_path`` in a parsed ``.dvc``/``dvc.lock``
    document and return its ``files:`` entries, or ``None``."""
    if not isinstance(data, dict):
        return None
    out_lists: list[list[Any]] = []
    if "outs" in data and isinstance(data["outs"], list):
        out_lists.append(data["outs"])
    stages = data.get("stages")
    if isinstance(stages, dict):
        for stage in stages.values():
            if isinstance(stage, dict) and isinstance(stage.get("outs"), list):
                out_lists.append(stage["outs"])

    base = output_path.rsplit("/", 1)[-1]
    for outs in out_lists:
        # Prefer an out whose path matches the whole output_path or its
        # basename; else fall back to the sole out.
        chosen = None
        for out in outs:
            if not isinstance(out, dict):
                continue
            path = str(out.get("path", ""))
            if path == output_path or path.rsplit("/", 1)[-1] == base:
                chosen = out
                break
        if chosen is None and len(outs) == 1 and isinstance(outs[0], dict):
            chosen = outs[0]
        if chosen is None:
            continue
        files = chosen.get("files")
        if not isinstance(files, list):
            # Single-file out with a version_id recorded.
            md5 = str(chosen.get("md5", ""))
            # A dir out whose committed lock recorded only ``md5: <hash>.dir``
            # (no per-file ``files:`` list — the exact field-incident producer
            # shape) carries no usable per-file hashes here: the ``.dir`` md5 is
            # the manifest's hash, not a blob's. Skip it so entry resolution
            # falls through to the locally-cached ``.dir`` manifest (source c),
            # which holds the real per-file hashes.
            if not md5 or md5.endswith(".dir"):
                continue
            return [
                _Expected(
                    relpath="",
                    md5=md5,
                    version_id=_extract_version_id_from_file_entry(chosen, remote_name),
                    size=int(chosen.get("size", 0) or 0),
                )
            ]
        result: list[_Expected] = []
        for fe in files:
            if not isinstance(fe, dict):
                continue
            result.append(
                _Expected(
                    relpath=str(fe.get("relpath", "")),
                    md5=str(fe.get("md5", "")),
                    version_id=_extract_version_id_from_file_entry(fe, remote_name),
                    size=int(fe.get("size", 0) or 0),
                )
            )
        if result:
            return result
    return None


def _entries_from_cached_manifest(
    cache_dir: Path, out: Any
) -> list[_Expected] | None:
    """Source (c): the locally-cached ``.dir`` manifest (the field state a
    failed ``dvc pull`` leaves behind). Single-file outs get their one
    pinned md5 directly."""
    if out.is_dir:
        cached = read_cached_dir_manifest(cache_dir, out.md5)
        if cached is None:
            return None
        return [
            _Expected(relpath=fe.relpath, md5=fe.md5, version_id=None, size=fe.size)
            for fe in cached
        ]
    if out.md5:
        return [_Expected(relpath="", md5=out.md5, version_id=out.version_id, size=out.size)]
    return None


def _s3_key(prefix: str, output_path: str, relpath: str) -> str:
    return "/".join(seg for seg in (prefix, output_path, relpath) if seg)


def rescue_import_pull(
    project_path: Path,
    target: str,
    *,
    dvc_ops: "DvcOps",
    aws_profile_name: str | None = None,
    reporter: "Reporter | None" = None,
    fetcher: "Fetcher | None" = None,
    _producer_cache: dict[tuple[str, str], _Producer] | None = None,
) -> RescueResult:
    """Materialize a dvc-import that plain ``dvc pull`` could not, by fetching
    the producer's worktree-layout objects directly and seeding the consumer
    cache. Returns a ``RescueResult`` — never raises on a documented failure
    path.
    """
    if fetcher is None:
        fetcher = GitArchiveFetcher()
    cache = _producer_cache if _producer_cache is not None else {}

    # 1. Parse the import.
    dvc_path = project_path / target
    try:
        dep = imports.DataDependency.from_dvc_file(dvc_path)
    except imports.NotAnImportError:
        return RescueResult(ok=False, reason=f"{target} is not a dvc-import")
    except Exception as exc:
        return RescueResult(ok=False, reason=f"could not parse import {target}: {exc}")

    remote_name = _consumer_remote_name(project_path)
    outs = outs_for_target(project_path, target, remote_name)
    if not outs:
        return RescueResult(
            ok=False, reason=f"could not parse the tracked output of {target}"
        )
    out = outs[0]

    # 2. Status line.
    if reporter is not None:
        reporter.info(
            f"dvc could not materialize {target} — fetching directly from the "
            f"producer's bucket ({dep.producer_repo}@{dep.contract_pin[:7]})"
        )

    if boto3 is None:
        return RescueResult(
            ok=False,
            reason="boto3 is not installed, so the producer's bucket cannot be reached",
            hint="install DVC's S3 extra: pip install 'dvc[s3]'",
        )

    # 3. Resolve the producer's S3 remote (memoized).
    producer, err = _resolve_producer(
        dep, fetcher=fetcher, aws_profile_name=aws_profile_name, cache=cache
    )
    if err is not None:
        return err
    assert producer is not None

    cache_dir = project_path / _DEFAULT_DVC_CACHE_REL

    # 4. Resolve expected per-file entries in priority order.
    entries = _entries_from_consumer_out(out)
    source = "consumer files"
    if entries is None:
        entries = _entries_from_producer_pointer(dep, producer, fetcher=fetcher)
        source = "producer pointer"
    if entries is None:
        entries = _entries_from_cached_manifest(cache_dir, out)
        source = "cached manifest"
    if not entries:
        return RescueResult(
            ok=False,
            reason=f"cannot determine expected file hashes for {target}",
            hint=(
                f"run `dvc fetch {target}` then retry `mintd data pull {target}`, "
                "or ask the producer to re-push"
            ),
        )
    logger.info("rescue: %s expected via %s (%d file(s))", target, source, len(entries))

    # 5. Build the S3 client and fetch each uncached blob.
    try:
        s3 = _create_s3_client(producer.remote_cfg, aws_profile_name)
    except Exception as exc:
        return RescueResult(
            ok=False,
            reason=f"could not build an S3 client for the producer's bucket: {exc}",
            hint="check AWS credentials / the [mintd] profile",
        )

    fetched = 0
    for entry in entries:
        if not entry.md5:
            return RescueResult(
                ok=False,
                reason=f"the pinned manifest for {target} is missing a file hash",
                hint="ask the producer to re-push so the lock records file hashes",
            )
        if is_cached(cache_dir, entry.md5):
            continue
        key = _s3_key(producer.prefix, dep.output_path, entry.relpath)
        try:
            fetch_to_cache(
                s3,
                producer.bucket,
                key,
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                version_id=entry.version_id,
            )
        except ValueError as exc:
            # md5 mismatch — the cache is NOT seeded (fetch_to_cache unlinks
            # the tmp file). Drift/corruption at the producer.
            return RescueResult(
                ok=False,
                reason=(
                    f"the producer's data drifted from the pinned hash "
                    f"(md5 {entry.md5[:8]}…) at s3://{producer.bucket}/{key} "
                    f"in {dep.producer_repo}: {exc}"
                ),
                hint="ask the producer to re-push the pinned revision",
            )
        except ClientError as exc:
            result = _client_error_result(exc, dep, producer.bucket, key, entry.md5)
            if result is not None:
                return result
            return RescueResult(
                ok=False,
                reason=(
                    f"could not fetch pinned data (md5 {entry.md5[:8]}…) from "
                    f"the producer's bucket (s3://{producer.bucket}/{key}): {exc}"
                ),
                hint=f"check access to the producer's bucket for {dep.producer_repo}",
            )
        except Exception as exc:
            return RescueResult(
                ok=False,
                reason=(
                    f"could not fetch pinned data (md5 {entry.md5[:8]}…) from "
                    f"the producer's bucket (s3://{producer.bucket}/{key}): {exc}"
                ),
                hint=f"check access to the producer's bucket for {dep.producer_repo}",
            )
        fetched += 1

    # 6. Seed the .dir manifest for dir outs when it isn't already cached.
    if out.is_dir and read_cached_dir_manifest(cache_dir, out.md5) is None:
        manifest_entries = [
            DvcFileEntry(md5=e.md5, relpath=e.relpath, size=e.size) for e in entries
        ]
        ensure_dir_manifest(cache_dir, manifest_entries)

    # 7. Check the import out into the workspace and confirm it materialized.
    try:
        dvc_ops.checkout(targets=[target])
    except Exception as exc:
        return RescueResult(
            ok=False,
            reason=f"dvc checkout of {target} failed after the rescue fetch: {exc}",
            hint=f"retry just this target: mintd data pull {target}",
        )
    if not outs_materialized(project_path, outs):
        return RescueResult(
            ok=False,
            reason=(
                f"{target} was not materialized by dvc checkout even after the "
                f"rescue seeded its cache"
            ),
            hint=f"retry just this target: mintd data pull {target}",
        )

    if reporter is not None:
        reporter.info(
            f"✓ rescued {target} — fetched {fetched} file(s) from the producer's bucket"
        )
    return RescueResult(ok=True, files_fetched=fetched)


def _client_error_result(
    exc: "ClientError",
    dep: "imports.DataDependency",
    bucket: str,
    key: str,
    md5: str,
) -> RescueResult | None:
    """Map a boto3 ``ClientError`` from the blob fetch to a named
    ``RescueResult``, or ``None`` for the generic caller-handled case."""
    error = getattr(exc, "response", {}).get("Error", {}) if hasattr(exc, "response") else {}
    code = str(error.get("Code", ""))
    status = (
        getattr(exc, "response", {})
        .get("ResponseMetadata", {})
        .get("HTTPStatusCode")
        if hasattr(exc, "response")
        else None
    )
    if code in _DRIFT_404_CODES or status == 404:
        return RescueResult(
            ok=False,
            reason=(
                f"pinned data (md5 {md5[:8]}…) is no longer in the producer's "
                f"bucket (s3://{bucket}/{key}); only the producer "
                f"({dep.producer_repo}) can restore it — ask them to re-push"
            ),
            hint="ask the producer to re-push the pinned revision",
        )
    if code in ("AccessDenied", "403") or status == 403:
        return RescueResult(
            ok=False,
            reason="cannot read the producer's bucket with the current AWS profile",
            hint=(
                f"get read access to the producer's bucket (s3://{bucket}) for "
                f"{dep.producer_repo}"
            ),
        )
    return None
