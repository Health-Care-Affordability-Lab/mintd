"""DVC FastSync implementation.

Coupled to DVC cache layout (3.66.x).
"""

import configparser
import dataclasses
import hashlib
import json
import logging
import os
import random
import shlex
import subprocess
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, TypeVar

import yaml

try:
    import boto3
    from botocore.exceptions import (
        ClientError,
        ConnectionClosedError,
        EndpointConnectionError,
        NoCredentialsError,
        ProfileNotFound,
        ReadTimeoutError,
        SSLError,
    )
except ImportError:
    boto3 = None  # type: ignore[assignment]

    class _BotocoreMissingError(Exception):
        """Placeholder when botocore is absent; never raised, so
        ``isinstance`` checks against it are always False and
        ``except`` clauses naming it never fire."""

    ClientError = _BotocoreMissingError  # type: ignore[assignment,misc]
    ConnectionClosedError = _BotocoreMissingError  # type: ignore[assignment,misc]
    EndpointConnectionError = _BotocoreMissingError  # type: ignore[assignment,misc]
    NoCredentialsError = _BotocoreMissingError  # type: ignore[assignment,misc]
    ProfileNotFound = _BotocoreMissingError  # type: ignore[assignment,misc]
    ReadTimeoutError = _BotocoreMissingError  # type: ignore[assignment,misc]
    SSLError = _BotocoreMissingError  # type: ignore[assignment,misc]

from mintd._atomic import _try_fsync_file, _try_fsync_parent_dir
from mintd._dvc_invoke import dvc_cmd
from mintd.model import FastPullResult

if TYPE_CHECKING:
    from mintd._console import Reporter

logger = logging.getLogger(__name__)

_DVC_FLOOR = (3, 66)
_DVC_CEILING = (4, 0)  # exclusive
# ClientError codes worth retrying. Deliberately wide: a single
# throttle/reset among a big dir-out's per-file fetches used to demote the
# whole out to plain `dvc pull` — the command fast-sync exists to avoid.
_RETRYABLE_S3_ERRORS = {
    "503", "500", "RequestTimeout", "SlowDown",
    "Throttling", "ThrottlingException", "RequestThrottled",
    "InternalError",
}
# botocore network-layer exceptions (no HTTP response to inspect) that are
# transient by nature: endpoint unreachable, read timeout, connection
# reset/closed, TLS hiccup. NoCredentialsError is deliberately absent —
# retrying cannot mint credentials (see is_transient_s3_error).
_TRANSIENT_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectionClosedError,
    SSLError,
)
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_BASE_S = 0.5
_RETRY_BACKOFF_CAP_S = 8.0
_SPOT_CHECK_N = 5
_DEFAULT_DVC_CACHE_REL = Path(".dvc/cache")

def _check_dvc() -> tuple[bool, str | None]:
    """Probe the bundled dvc. Return (ok, reason_if_not_ok)."""
    # Same "subprocess argv:" prefix and shlex quoting as run_streaming, so
    # -vv output has one grep-able, copy-pasteable format for every spawn.
    logger.debug("subprocess argv: %s", shlex.join([*dvc_cmd(), "--version"]))
    try:
        result = subprocess.run(
            [*dvc_cmd(), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return False, "dvc not installed"
    except subprocess.TimeoutExpired:
        return False, "dvc version probe timed out"
    if result.returncode != 0:
        # `sys.executable -m dvc` returns exit 1 + "No module named 'dvc'" on
        # stderr when dvc isn't installed in mintd's env — re-emit the
        # honest reason rather than the opaque "probe failed" string.
        if "No module named 'dvc'" in result.stderr or "No module named dvc" in result.stderr:
            return False, "dvc not installed"
        return False, f"dvc version probe failed (exit {result.returncode})"
    version = result.stdout.strip()
    parts = version.split(".")
    try:
        major, minor = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return False, f"dvc version unparseable: {version!r}"
    if (major, minor) < _DVC_FLOOR:
        return False, f"dvc {major}.{minor} below floor {_DVC_FLOOR[0]}.{_DVC_FLOOR[1]}"
    if (major, minor) >= _DVC_CEILING:
        return False, f"dvc {major}.{minor} above ceiling {_DVC_CEILING[0]}.{_DVC_CEILING[1]}"
    return True, None


@dataclass(frozen=True)
class DvcFileEntry:
    md5: str
    relpath: str
    size: int = 0
    version_id: str | None = None
    # Slice 27: True when the entry's version_id lives ONLY under cloud[]
    # (DVC's version_aware mode). The S3 key is then the file's real path,
    # not files/md5/XX/YYYY. See _is_path_based_file_entry.
    is_path_based: bool = False


@dataclass(frozen=True)
class DvcOut:
    target: str
    path: str
    md5: str
    is_dir: bool
    version_id: str | None = None
    is_files_format: bool = False
    files: list[DvcFileEntry] | None = None
    size: int = 0  # aggregate bytes for the out (sum across files for dirs); used by Reporter.progress
    # Slice 27: True when the out's version_id lives ONLY under cloud[]
    # (DVC's version_aware mode). The S3 key is the .dvc file's parent
    # directory (relative to project_path) joined with out.path, not
    # files/md5/XX/YYYY. See _is_path_based_entry + s3_key_for_out.
    is_path_based: bool = False
    # The path of the .dvc file this out was parsed from. Required for
    # path-based key reconstruction; None on synthetic DvcOuts (tests).
    dvc_file: Path | None = None
    # Slice 29: True when the parent .dvc has any deps[].repo block,
    # i.e. the file was produced by `dvc import`. The data lives in the
    # source repo's bucket — not this consumer repo's — so fast-sync
    # cannot serve it and routes the target straight to `dvc pull`.
    is_import: bool = False

    @property
    def materializes_as_dir(self) -> bool:
        """``dvc checkout`` writes this out as a DIRECTORY: ``.dir``-md5
        outs AND files-format outs (which have no top-level md5, so
        ``is_dir`` stays False). Every dir-vs-file dispatch on workspace
        shape must use this predicate, never ``is_dir`` alone."""
        return self.is_dir or self.is_files_format


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    expected: str
    actual: str


def is_version_aware(out: "DvcOut") -> bool:
    """Pure format predicate: does the out pin S3 object versions (a
    ``version_id`` at any level — top-level, cloud-nested/path-based, or on
    any files-format entry)?

    Routing decisions should call :func:`dvc_pull_can_serve` instead; this
    predicate says nothing about which command can restore the out.
    """
    if out.version_id or out.is_path_based:
        return True
    if not out.files:
        return False
    return any(fe.version_id or fe.is_path_based for fe in out.files)


def dvc_pull_can_serve(out: "DvcOut") -> bool:
    """Routing policy: can plain ``dvc pull`` restore this out?

    - dvc-imports: yes, always — their data lives in the source repo's
      bucket, which ``dvc pull`` reaches and fast-sync cannot (slice 29).
      A pinned version_id on an import does not change that. When even plain
      ``dvc pull`` cannot materialize an import (a version-aware producer
      bucket whose committed lock recorded no version_id), the downstream
      consumer-side rescue lane (``_import_rescue_ops``) fetches the
      producer's objects directly — this routing is unchanged; the rescue
      fires only after the fallback pull has already failed.
    - version-aware outs: no — plain ``dvc pull`` is documented broken on
      these (rehash-on-pull re-downloads, StorageKeyError tuple crashes;
      see the fallback-scope comments in data_ops.py). When fast-sync
      cannot serve them they must fail loudly instead.
    - everything else (md5-keyed): yes.

    This is THE fallback-vs-blocked split; every routing site goes through
    it (directly or via :func:`_route_out`) so the policy cannot diverge.
    """
    return out.is_import or not is_version_aware(out)


def _route_out(
    out: "DvcOut",
    why: str,
    *,
    fallback: list[str],
    blocked_targets: list[str],
    blocked_reasons: dict[str, str],
) -> None:
    """Route one out fast-sync could not serve: fallback when plain
    ``dvc pull`` can restore it, otherwise a loud per-target error with
    ``why`` as its reason."""
    if dvc_pull_can_serve(out):
        fallback.append(out.target)
    else:
        blocked_targets.append(out.target)
        blocked_reasons.setdefault(out.target, why)


def _degrade_all_targets(
    project_path: Path,
    targets: list[str],
    remote_name: str,
    pipeline_outs: list["DvcOut"] | None,
    reason: str,
) -> FastPullResult:
    """All-or-nothing degradation: a guard stopped the whole try_fast_pull
    call (dvc version mismatch, remote config missing, non-S3 remote, boto3
    missing, credentials/versioning probes, versioning disabled).

    Degrading must NOT dump every target into one plain ``dvc pull`` — that
    command is documented broken on version-aware outs. Classify targets
    FIRST (pure .dvc parsing: no S3, no subprocess, works even when boto3
    is absent), then split per :func:`dvc_pull_can_serve`:

    - imports, md5-keyed outs, unparseable and hash-missing targets keep the
      fallback route (plain ``dvc pull`` genuinely serves them);
    - version-aware outs land in ``blocked_targets`` with the guard's reason —
      the caller reports each loudly and exits non-zero.
    """
    all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)
    fallback = fallback + hash_missing
    by_target: dict[str, list[DvcOut]] = {}
    for out in all_outs:
        by_target.setdefault(out.target, []).append(out)
    blocked_targets: list[str] = []
    blocked_reasons: dict[str, str] = {}
    for target, outs in by_target.items():
        if all(dvc_pull_can_serve(o) for o in outs):
            fallback.append(target)
        else:
            blocked_targets.append(target)
            blocked_reasons.setdefault(target, reason)
    for out in pipeline_outs or []:
        _route_out(
            out, reason,
            fallback=fallback,
            blocked_targets=blocked_targets,
            blocked_reasons=blocked_reasons,
        )
    blocked_targets = list(dict.fromkeys(blocked_targets))
    fallback = list(dict.fromkeys(fallback))
    return FastPullResult(
        success=False,
        fallback_targets=fallback,
        reason=reason,
        blocked_targets=blocked_targets,
        blocked_reasons=blocked_reasons,
    )


_T = TypeVar("_T")


def is_transient_s3_error(exc: BaseException) -> bool:
    """Pure classification (no I/O, no sleeps) of S3/network errors worth retrying.

    Shared policy: the share/cache lanes may import this together with
    :func:`retry_transient` instead of forking their own list.

    Transient:
      - ``ClientError`` whose code is in ``_RETRYABLE_S3_ERRORS`` (5xx,
        RequestTimeout, SlowDown, throttling family) or whose HTTP status
        is 500/503;
      - botocore network-layer errors: endpoint-connection, read-timeout,
        connection-closed (reset), SSL.

    NOT transient: ``NoCredentialsError`` — retrying cannot mint
    credentials; callers surface it as a named degradation reason instead.
    """
    if isinstance(exc, NoCredentialsError):
        return False
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        if str(error.get("Code", "")) in _RETRYABLE_S3_ERRORS:
            return True
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status in (500, 503)
    return isinstance(exc, _TRANSIENT_NETWORK_ERRORS)


def retry_transient(fn: Callable[[], _T], *, attempts: int = _RETRY_ATTEMPTS) -> _T:
    """Run ``fn()``; retry transient S3/network errors with capped exponential backoff.

    3 attempts total, sleeping ``min(8s, 0.5s * 2**attempt)`` between tries.
    Deliberately no config knob — this is shared retry *policy*, not a
    tuning surface; share/cache can import it as-is.
    Non-transient errors (see :func:`is_transient_s3_error`) and the final
    attempt's error propagate unchanged.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts - 1 or not is_transient_s3_error(exc):
                raise
            time.sleep(min(_RETRY_BACKOFF_CAP_S, _RETRY_BACKOFF_BASE_S * (2 ** attempt)))
    raise AssertionError("unreachable: retry_transient loop returns or raises")


def _extract_version_id(out_dict: dict[str, Any]) -> str | None:
    if "version_id" in out_dict:
        return str(out_dict["version_id"])
    if "cloud" in out_dict:
        for cloud_info in out_dict["cloud"].values():
            if "version_id" in cloud_info:
                return str(cloud_info["version_id"])
    return None


def _extract_version_id_from_file_entry(entry: dict[str, Any], remote_name: str) -> str | None:
    cloud = entry.get("cloud")
    if not isinstance(cloud, dict):
        return None
    remote_block = cloud.get(remote_name)
    if isinstance(remote_block, dict) and "version_id" in remote_block:
        return str(remote_block["version_id"])
    return None


def _is_path_based_entry(entry: dict[str, Any], remote_name: str | None = None) -> bool:
    """True iff the entry's version_id comes ONLY from cloud[] metadata —
    meaning the S3 object is keyed by its file path (DVC's version_aware
    mode). False when a top-level version_id is present.

    Mirrors v1 ``mintd/utils/fast_sync.py:_is_path_based_entry``.
    Mixed entries (both top-level AND cloud-nested ``version_id``) are
    treated md5-keyed: top-level wins (v1 contract).
    """
    if entry.get("version_id"):
        return False
    cloud = entry.get("cloud") or {}
    if remote_name and isinstance(cloud.get(remote_name), dict):
        return bool(cloud[remote_name].get("version_id"))
    return any(
        isinstance(v, dict) and v.get("version_id")
        for v in cloud.values()
    )


def _is_path_based_file_entry(entry: dict[str, Any], remote_name: str | None = None) -> bool:
    """Parallel to ``_is_path_based_entry`` for files-format file entries.

    Today the rule is identical; the alias exists so call sites don't lie
    about which kind of dict they're inspecting and so we can diverge later
    without touching callers.
    """
    return _is_path_based_entry(entry, remote_name)


def parse_dvc_outs(dvc_path: Path, remote_name: str) -> list[DvcOut]:
    """Parse a .dvc file into DvcOut entries.

    DVC marks directories by suffixing the md5 hash with ``.dir`` — that's
    the only signal; there is no separate ``is_dir`` YAML field. Files-format
    directories use an inline ``files:`` list with no top-level md5.
    Entries with no md5 AND no files list are hash-missing — returned as
    empty so the caller routes them to fallback.
    """
    try:
        with open(dvc_path) as f:
            data = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError):
        return []

    outs = []
    if not isinstance(data, dict) or "outs" not in data:
        return []
    # Slice 29: `dvc import` produces a .dvc with `deps[].repo` blocks
    # pointing at the source repo. The data lives in that source repo's
    # bucket, not this one — fast-sync cannot serve it. Detect once per
    # file; stamp every out so classify_targets can short-circuit.
    deps = data.get("deps") or []
    is_import = any(
        isinstance(d, dict) and isinstance(d.get("repo"), dict)
        for d in deps
    )
    for out in data["outs"]:
        has_md5 = bool(out.get("md5"))
        has_files = "files" in out
        if not has_md5 and not has_files:
            continue
        md5 = str(out.get("md5", ""))
        files_list: list[DvcFileEntry] | None = None
        if has_files:
            files_list = [
                DvcFileEntry(
                    md5=str(fe.get("md5", "")),
                    relpath=str(fe.get("relpath", "")),
                    size=int(fe.get("size", 0) or 0),
                    version_id=_extract_version_id_from_file_entry(fe, remote_name),
                    is_path_based=_is_path_based_file_entry(fe, remote_name),
                )
                for fe in out["files"]
            ]
        outs.append(DvcOut(
            target="",
            path=str(out.get("path", "")),
            md5=md5,
            is_dir=md5.endswith(".dir"),
            version_id=_extract_version_id(out),
            is_files_format=has_files and not has_md5,
            files=files_list,
            size=int(out.get("size", 0) or 0),
            is_path_based=_is_path_based_entry(out, remote_name),
            dvc_file=dvc_path,
            is_import=is_import,
        ))
    return outs


def parse_dvc_lock_outs(project_path: Path, remote_name: str) -> list[DvcOut]:
    """Parse ``project_path/dvc.lock`` into ``DvcOut`` entries for pipeline-
    stage outputs (no per-output ``.dvc`` pointer files). Slice 37 — gives
    fast-sync a way to enumerate pipeline products so they bypass DVC's
    rehash-on-pull cost on version_aware remotes.

    Reads ``dvc.yaml`` (optional) to build the ``stage → wdir`` map so
    lock-relative ``out.path`` resolves correctly to project-relative.
    When ``dvc.yaml`` is missing, every stage's ``wdir`` defaults to ``.``.

    Returns ``[]`` when ``dvc.lock`` is missing or malformed.
    """
    yaml_path = project_path / "dvc.yaml"
    lock_path = project_path / "dvc.lock"

    wdir_map: dict[str, str] = {}
    try:
        if yaml_path.exists():
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "stages" in data:
                    for stage, stage_data in data["stages"].items():
                        wdir = stage_data.get("wdir", ".")
                        if Path(wdir).is_absolute():
                            logger.warning(
                                "absolute wdir in dvc.yaml stage %s: %s; skipping stage",
                                stage, wdir,
                            )
                            wdir_map[stage] = "SKIP"
                        else:
                            wdir_map[stage] = wdir
    except (FileNotFoundError, yaml.YAMLError, OSError):
        pass

    try:
        with open(lock_path) as f:
            lock_data = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError, OSError):
        return []

    if not isinstance(lock_data, dict) or "stages" not in lock_data:
        return []

    outs: list[DvcOut] = []
    for stage, stage_data in lock_data["stages"].items():
        if wdir_map.get(stage) == "SKIP":
            continue
        if "outs" not in stage_data:
            continue
        wdir = wdir_map.get(stage, ".")
        for out in stage_data["outs"]:
            has_md5 = bool(out.get("md5"))
            has_files = "files" in out
            if not has_md5 and not has_files:
                continue
            try:
                raw_path = out["path"]
                abs_path = (project_path / wdir / raw_path).resolve()
                rel = abs_path.relative_to(project_path.resolve()).as_posix()
            except (ValueError, KeyError):
                logger.warning(
                    "path resolution failed for stage %s, out %s; skipping",
                    stage, out.get("path"),
                )
                continue

            cloud_block = out.get("cloud") or {}
            remote_cloud = cloud_block.get(remote_name) or {}
            version_id_raw = remote_cloud.get("version_id")
            version_id = str(version_id_raw) if version_id_raw else None
            is_path_based = bool(version_id) and not out.get("version_id")
            md5 = str(out.get("md5", ""))

            if has_files:
                is_files_format = True
                is_dir = True
                files_list: list[DvcFileEntry] | None = [
                    DvcFileEntry(
                        md5=str(fe.get("md5", "")),
                        relpath=str(fe.get("relpath", "")),
                        size=int(fe.get("size", 0) or 0),
                        version_id=_extract_version_id_from_file_entry(fe, remote_name),
                        is_path_based=_is_path_based_file_entry(fe, remote_name),
                    )
                    for fe in out["files"]
                ]
            else:
                is_files_format = False
                is_dir = md5.endswith(".dir")
                files_list = None

            outs.append(DvcOut(
                target=rel,
                path=rel,
                md5=md5,
                is_dir=is_dir,
                version_id=version_id,
                is_files_format=is_files_format,
                files=files_list,
                size=int(out.get("size", 0) or 0),
                is_path_based=is_path_based,
                dvc_file=lock_path,
                is_import=False,
            ))
    return outs


def _is_fast_syncable_pipeline_out(out: DvcOut) -> bool:
    """Whether fast-sync can serve this ``dvc.lock`` stage out.

    Two shapes qualify:

    1. Single-file outs whose top-level ``cloud.<remote>`` block carries a
       ``version_id`` — the straightforward case.
    2. ``files:``-format directory outs whose per-file entries each carry
       ``cloud.<remote>.version_id``. Real-world DVC lockfiles don't emit a
       top-level ``cloud`` block on dir-outs (only per-file ones inside
       ``files:``), so this branch is what makes pipeline dir-outs reachable
       in practice. ``fetch_files_dir_contents`` operates on per-file entries
       and doesn't need the top-level version_id.

    Outs that fit neither shape route to ``dvc pull`` instead.
    """
    if out.version_id:
        return True
    return bool(out.is_files_format and out.files and all(fe.version_id for fe in out.files))


def partition_pipeline_outs(
    project_path: Path, remote_name: str
) -> tuple[list[DvcOut], list[DvcOut]]:
    """Parse ``dvc.lock`` once and partition its stage outs.

    Returns ``(fast_syncable, all_outs)``: the first is the subset fast-sync
    can serve (see ``_is_fast_syncable_pipeline_out``); the second is every
    stage out in the lockfile. Callers diff the two to find the outs that must
    route to ``dvc pull``. One parse, so ``data_pull`` doesn't read the lock
    twice.
    """
    all_outs = parse_dvc_lock_outs(project_path, remote_name)
    fast_syncable = [o for o in all_outs if _is_fast_syncable_pipeline_out(o)]
    return fast_syncable, all_outs


def discover_all_outs(project_path: Path) -> list[str]:
    """Walk project_path recursively for ``*.dvc`` files; return paths
    relative to project_path, sorted lexicographically.

    Excludes the ``.dvc/`` internals directory (DVC metadata, not data
    pointers) and ``dvc.lock`` (pipeline lockfile). Mirrors v1's
    ``discover_dvc_targets`` (mintd/utils/dvc_guards.py) for the .dvc-file
    half; pipeline-stage names from ``dvc.yaml`` are NOT included because
    fast-sync's ``classify_targets`` cannot consume them — such targets
    fall through to ``dvc pull`` as the normal fallback path.

    Used by ``data_pull`` when ``targets is None`` to route through
    fast-sync (boto3 → cache) instead of straight to ``dvc pull``, which
    hits a cache-write bug in DVC 3.66.1 on version_aware buckets.
    """
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_path, followlinks=False):
        # Prune .dvc/ internals in-place so os.walk doesn't descend.
        if ".dvc" in dirnames:
            dirnames.remove(".dvc")
        for name in filenames:
            if not name.endswith(".dvc") or name == "dvc.lock":
                continue
            abs_path = Path(dirpath) / name
            rel = abs_path.relative_to(project_path)
            results.append(rel.as_posix())
    return sorted(results)


def cache_path_for(cache_dir: Path, md5: str) -> Path:
    if not md5 or len(md5) < 3:
        return cache_dir / "files" / "md5" / md5
    return cache_dir / "files" / "md5" / md5[:2] / md5[2:]


def is_cached(cache_dir: Path, md5: str) -> bool:
    if not md5:
        return False
    return cache_path_for(cache_dir, md5).exists()


def read_cached_dir_manifest(cache_dir: Path, dir_md5: str) -> "list[DvcFileEntry] | None":
    """Parse a locally-cached ``.dir`` manifest into file entries.

    ``dir_md5`` may arrive with or without the ``.dir`` suffix; the cache
    filename always carries it (DVC's own layout — see ensure_dir_manifest).
    Returns ``None`` when the manifest is absent or malformed in any way
    (bad JSON, non-list payload, non-dict entries): callers treat that as
    "manifest not usable from cache" and fetch or route accordingly.
    """
    full_md5 = dir_md5 if dir_md5.endswith(".dir") else f"{dir_md5}.dir"
    manifest_path = cache_path_for(cache_dir, full_md5)
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_bytes())
        if not isinstance(payload, list):
            return None
        return [
            DvcFileEntry(
                md5=str(e.get("md5", "")),
                relpath=str(e.get("relpath", "")),
                size=int(e.get("size", 0) or 0),
            )
            for e in payload
        ]
    except Exception:
        return None


def ensure_out_cached(cache_dir: Path, out: "DvcOut") -> bool:
    """True when every blob this out pins is verifiably in the local DVC cache.

    Used by the degraded pull paths (data_ops) to decide what `dvc checkout`
    can materialize WITHOUT any fallback `dvc pull`. The cache is
    content-addressed against the md5s pinned in the `.dvc` / `dvc.lock`
    entry, so a fully-cached out is materializable regardless of what
    happened on the remote.

    Workspace-side twin: :func:`outs_materialized` ("DID checkout
    materialize it?"). Its dir-vs-file dispatch is
    ``DvcOut.materializes_as_dir``; the arm ordering below (files-format
    first, then ``.dir`` md5, then single file) encodes the same fact.

    "ensure" because the files-format arm has a repair side effect:

    - dvc-imports are never "cached": their data lives in the source repo's
      bucket and they route to plain `dvc pull` intentionally (slice 29).
    - files-format dir-outs: all per-file blobs must be present. When they
      are, the synthetic ``.dir`` manifest is (re)written so `dvc checkout`
      can find the directory — idempotent, local-only, and the exact repair
      fast-sync itself performs after fetching (slice 27).
    - md5-keyed dir-outs: the ``.dir`` manifest must be in cache AND every
      entry it lists must be cached.
    - single-file outs: the pinned blob must be in cache.
    """
    if out.is_import:
        return False
    if out.is_files_format:
        if out.files is None:
            return False
        if not is_dir_fully_cached(cache_dir, out.files):
            return False
        ensure_dir_manifest(cache_dir, out.files)
        return True
    if not out.md5:
        return False
    if out.is_dir:
        entries = read_cached_dir_manifest(cache_dir, out.md5)
        return entries is not None and is_dir_fully_cached(cache_dir, entries)
    return is_cached(cache_dir, out.md5)


# DVC's content address for the empty directory: md5 of the empty-list
# JSON manifest (b"[]"). An md5-keyed dir out pinning this hash legally
# tracks a directory with zero files.
EMPTY_DIR_MD5 = "d751713988987e9331980363e24189ce.dir"


def workspace_path_for(project_path: Path, out: "DvcOut") -> Path:
    """Where ``out`` lives in the workspace.

    ``.dvc``-file outs record ``path`` relative to the ``.dvc`` file's own
    directory; ``dvc.lock`` stage outs (and synthetic test outs with no
    ``dvc_file``) already carry a project-relative path. S3-side twin of
    this anchoring convention: :func:`s3_key_for_out`.
    """
    if out.dvc_file is not None and out.dvc_file.name != "dvc.lock":
        return out.dvc_file.parent / out.path
    return project_path / out.path


def out_pins_no_files(out: "DvcOut") -> bool:
    """True when the out's pinned content is *known* to be zero files, so
    ``dvc checkout`` correctly materializes it as an EMPTY directory:
    a files-format out with an empty ``files:`` list, or an md5-keyed dir
    out pinning the canonical empty-dir manifest hash."""
    if out.files is not None:
        return not out.files
    return out.md5 == EMPTY_DIR_MD5


def outs_materialized(project_path: Path, outs: "list[DvcOut]") -> bool:
    """Stat-only materialization probe: every dir-out exists (and is
    non-empty unless the out pins zero files), every file-out exists.

    Workspace-side twin of :func:`ensure_out_cached` — that one asks "CAN
    checkout materialize this out from cache?", this one asks "DID checkout
    materialize it?". Dir-ness dispatches on ``DvcOut.materializes_as_dir``
    (what ``dvc checkout`` writes), never ``is_dir`` alone.
    """
    for out in outs:
        ws = workspace_path_for(project_path, out)
        if out.materializes_as_dir:
            if not ws.is_dir():
                return False
            if out_pins_no_files(out):
                continue  # legitimately empty dir: existence suffices
            try:
                next(iter(ws.iterdir()))
            except StopIteration:
                return False
        elif not ws.is_file():
            return False
    return True


def outs_for_target(project_path: Path, target: str, remote_name: str) -> "list[DvcOut]":
    """Normalize ``target``, resolve its ``.dvc`` file, parse its outs.

    Returns ``[]`` for missing or malformed ``.dvc`` files — including
    YAML-valid files with wrong-typed fields, where parse_dvc_outs raises.
    The degraded pull paths treat such a target as simply "not cached" /
    zero bytes so it keeps its fallback route instead of crashing the whole
    pull with a raw traceback.
    """
    nt = normalize_target(target)
    dvc_path = (
        project_path / nt if nt.endswith(".dvc") else project_path / f"{nt}.dvc"
    )
    try:
        return parse_dvc_outs(dvc_path, remote_name)
    except Exception:
        return []


def resolve_target_outs(
    project_path: Path,
    target: str,
    remote_name: str,
    pipeline_by_target: "dict[str, DvcOut]",
) -> "list[DvcOut]":
    """Resolve a checkout candidate to its outs: prefer the already-parsed
    ``dvc.lock`` pipeline out, else parse the target's ``.dvc`` file.

    INVARIANT: the cache probe (:func:`cached_targets`) and the workspace
    verify pass (data_ops) MUST share this resolution — the verify pass
    stats exactly what the cache probe promised ``dvc checkout`` could
    materialize. Returns ``[]`` for missing/malformed ``.dvc`` targets
    (treated as "not cached" / unverifiable).
    """
    pipeline_out = pipeline_by_target.get(target)
    if pipeline_out is not None:
        return [pipeline_out]
    return outs_for_target(project_path, target, remote_name)


def cached_targets(
    project_path: Path,
    candidates: list[str],
    remote_name: str,
    pipeline_outs: "list[DvcOut] | None" = None,
) -> list[str]:
    """Subset of ``candidates`` whose pinned blobs are ALL in the local cache.

    Both degraded branches of ``data_pull`` previously skipped `dvc checkout`
    entirely, dropping data fast-sync had already downloaded into the cache.
    This probe is filesystem-only (parse the `.dvc` / reuse the
    already-parsed ``dvc.lock`` out, stat cache blobs) — no S3, no
    subprocess. Order is preserved; unparseable / missing / hash-missing
    targets are simply not cached.
    """
    cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
    pipeline_by_target = {o.target: o for o in pipeline_outs or []}
    cached: list[str] = []
    for target in candidates:
        outs = resolve_target_outs(
            project_path, target, remote_name, pipeline_by_target,
        )
        if outs and all(ensure_out_cached(cache_dir, o) for o in outs):
            cached.append(target)
    return cached


def normalize_target(target: str) -> str:
    """Normalize a catalog-supplied target into the posix-relative form
    that the ``.dvc`` lookup (classify_targets / data_pull) expects.

    The no-flag clone path discovers targets via ``discover_all_outs``,
    which already emits ``rel.as_posix()`` strings. ``--primary`` is the
    only caller that turns a hand-written ``data_products.primary`` string
    into a target, so a value stored on Windows (backslash separators), or
    written with a leading ``./`` or a trailing ``/``, would otherwise miss
    the on-disk ``.dvc`` file. Normalize once, at the boundary.

    Backslashes are treated as path separators. On POSIX a backslash is a
    legal filename character, but mintd data products never use them (DVC
    stores posix outs), and the value being normalized is a hand-edited
    catalog metadata string whose intent is a path separator.
    """
    t = target.replace("\\", "/")
    if t.startswith("./"):
        t = t[2:]
    return t.rstrip("/")


def classify_targets(project_path: Path, targets: list[str], remote_name: str) -> tuple[list[DvcOut], list[str], list[str]]:
    """Resolve each user-supplied target to a list of single-file DvcOut entries.

    Each user target's outs are appended to ``all_outs`` regardless of shape;
    the orchestrator's per-out loop dispatches on ``is_dir`` / ``is_files_format``.
    Hash-missing entries (entries declaring a hash type but no value) are
    surfaced in ``hash_missing`` and the orchestrator merges them into
    ``fallback`` so they never silently mark as synced.
    """
    all_outs = []
    fallback = []
    hash_missing = []

    for target in targets:
        lookup = normalize_target(target)
        dvc_path = project_path / lookup if lookup.endswith(".dvc") else project_path / f"{lookup}.dvc"
        outs = parse_dvc_outs(dvc_path, remote_name)
        if not outs:
            if dvc_path.exists():
                hash_missing.append(target)
            else:
                fallback.append(target)
            continue

        # Slice 29: `dvc import` files (deps[].repo) have data in the
        # source repo's bucket; fast-sync cannot serve them. Route to
        # fallback before any S3 client / spot-check / manifest-fetch.
        # `is_import` is a per-file flag (deps applies to the whole
        # .dvc), so we add `target` to fallback exactly once and break.
        # A mixed import+non-import .dvc would over-route the file —
        # perf regression at worst, never a correctness loss.
        if outs[0].is_import:
            logger.info(
                "fast-sync: skipping %r — dvc-import (data lives in source repo)",
                target,
            )
            fallback.append(target)
            continue

        for out in outs:
            all_outs.append(dataclasses.replace(out, target=target))

    return all_outs, fallback, hash_missing


def _default_remote_name_from_config(cp: "configparser.ConfigParser") -> str | None:
    """Resolve the *default* DVC remote name from a parsed config.

    Order (the same logic ``data_ops._default_dvc_remote`` delegates here so
    it is written once):
      1. ``[core] remote = <name>`` if present (DVC's standard default);
      2. the single ``[remote "..."]`` section if there's exactly one
         (covers freshly-cloned products whose ``.dvc/config`` declares one
         remote and no ``[core]`` default);
      3. ``None`` — ambiguous (multiple remotes, no default) or no remote.

    DVC has shipped three section-header spellings: ``'remote "name"'``
    (single-quoted, the modern default), ``remote "name"`` (double-quoted),
    and ``remote name`` (unquoted); all three are matched.
    """
    import re
    if cp.has_section("core") and cp.has_option("core", "remote"):
        return cp.get("core", "remote")
    remote_names: list[str] = []
    for section in cp.sections():
        m = re.fullmatch(r"""'?remote\s+"?(?P<name>[^"']+)"?'?""", section)
        if m:
            remote_names.append(m.group("name"))
    if len(remote_names) == 1:
        return remote_names[0]
    return None


def parse_remote_config_text(text: str, remote_name: str | None) -> dict[str, str]:
    """Parse a DVC ``.dvc/config`` (given as text) into one remote's key/value map.

    DVC has shipped two formats for remote sections: the older quoted form
    ``['remote "origin"']`` (literal single quotes in the section header)
    and the unquoted forms ``[remote "origin"]`` / ``[remote origin]``.
    Probe all three so the parser works across DVC versions.

    When ``remote_name is None`` the default remote is resolved via
    :func:`_default_remote_name_from_config`; an ambiguous config (multiple
    remotes and no ``[core]`` default) raises ``KeyError`` so callers can map
    it to an actionable hint. A named-but-absent remote also raises
    ``KeyError``.
    """
    cp = configparser.ConfigParser()
    cp.read_string(text)

    if remote_name is None:
        resolved = _default_remote_name_from_config(cp)
        if resolved is None:
            raise KeyError(
                "no default DVC remote: config has no [core] remote and not "
                "exactly one remote section"
            )
        remote_name = resolved

    candidates = (
        f"'remote \"{remote_name}\"'",
        f'remote "{remote_name}"',
        f"remote {remote_name}",
    )
    for section in candidates:
        if cp.has_section(section):
            return dict(cp[section])

    raise KeyError(f"remote {remote_name} not found in DVC config")


def get_remote_config(project_path: Path, remote_name: str) -> dict[str, str]:
    """Read a DVC remote section from ``.dvc/config`` (path wrapper over
    :func:`parse_remote_config_text`)."""
    config_path = project_path / ".dvc" / "config"
    if not config_path.exists():
        raise FileNotFoundError(f"no .dvc/config at {config_path}")
    try:
        return parse_remote_config_text(config_path.read_text(), remote_name)
    except KeyError as exc:
        raise KeyError(f"{exc.args[0]} ({config_path})") from exc


def parse_s3_url(url: str) -> tuple[str, str]:
    if not url.startswith("s3://"):
        raise ValueError(f"not an s3 url: {url}")
    parts = url[5:].split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    return bucket, prefix


def s3_key_for(prefix: str, md5: str) -> str:
    parts = [prefix] if prefix else []
    parts.extend(["files", "md5", md5[:2], md5[2:]])
    return "/".join(parts)


def s3_key_for_out(prefix: str, out: DvcOut, project_path: Path) -> str:
    """S3 object key for ``out``.

    Slice 27: branches on ``out.is_path_based``:
      - Path-based (version_aware): ``<prefix>/<dvc_file.parent_rel>/<out.path>``.
        ``dvc_file.parent_rel`` is ``Path(".")`` when the .dvc file sits at
        the project root, in which case the rel-dir segment is omitted.
      - Md5-keyed: ``<prefix>/files/md5/XX/YYYY`` (delegates to ``s3_key_for``).

    Raises ``ValueError`` when ``is_path_based=True`` and ``dvc_file is None``
    (synthetic out misuse); the orchestrator's per-out try/except routes
    such outs to the dvc-pull fallback.
    """
    if out.is_path_based:
        if out.dvc_file is None:
            raise ValueError(f"path-based DvcOut {out.path!r} missing dvc_file")
        rel_dir = out.dvc_file.parent.relative_to(project_path)
        rel = out.path if rel_dir == Path(".") else f"{rel_dir.as_posix()}/{out.path}"
        return f"{prefix}/{rel}" if prefix else rel
    return s3_key_for(prefix, out.md5)


def check_bucket_versioning(s3: Any, bucket: str) -> bool:
    """Probe the bucket's versioning status.

    Transient errors are retried (:func:`retry_transient`); an error that
    still fails PROPAGATES instead of being read as "versioning disabled" —
    a transient 503 on this probe used to demote every target to the
    dvc-pull fallback with a misleading reason. Only a successful response
    decides the boolean.
    """
    resp = retry_transient(lambda: s3.get_bucket_versioning(Bucket=bucket))
    return resp.get("Status") == "Enabled"


# ClientError codes / statuses that VERIFY the pinned object version is gone.
_DRIFT_404_CODES = {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


def spot_check_versions(
    s3: Any, bucket: str, prefix: str, outs: list[DvcOut], project_path: Path, n: int = _SPOT_CHECK_N
) -> list[tuple[str, str]]:
    """Randomly sample up to ``n`` outs and HEAD them to detect version_id drift.

    Returns ``[(target, why), ...]`` for outs with VERIFIED drift only: an
    HTTP 404 / NoSuchVersion on the pinned version, or a VersionId mismatch
    in a successful response. The orchestrator demotes ONLY those targets
    (previously any transient ``ClientError`` here demoted all targets under
    the misleading reason "version_id spot-check drift").

    Each HEAD is retried via :func:`retry_transient`. A probe that still
    fails after retries is INCONCLUSIVE — logged and skipped, never drift
    (the md5 verify after download stays the safety net).
    ``NoCredentialsError`` propagates so the orchestrator can name it as a
    non-retried degradation reason. Outs without a ``version_id``
    (md5-keyed, content-addressed) are skipped as before.

    Slice 27: uses ``s3_key_for_out`` so path-based (version_aware) outs
    are HEAD'd at their real file path, not at ``files/md5/...``.
    """
    drift: list[tuple[str, str]] = []
    to_check = random.sample(outs, min(n, len(outs)))
    for out in to_check:
        if not out.version_id:
            continue
        try:
            key = s3_key_for_out(prefix, out, project_path)
        except ValueError as exc:
            # Per-out misuse (synthetic out without dvc_file) — demote just
            # this out; the rest of the batch is unaffected.
            drift.append((out.target, f"cannot build S3 key: {exc}"))
            continue
        def _head(key: str = key, vid: str | None = out.version_id) -> Any:
            # Defaults bind the loop variables at definition time.
            return s3.head_object(Bucket=bucket, Key=key, VersionId=vid)

        try:
            resp = retry_transient(_head)
        except NoCredentialsError:
            raise
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in _DRIFT_404_CODES or status == 404:
                drift.append((out.target, f"HEAD 404 for pinned version {out.version_id} of {key}"))
            else:
                logger.warning(
                    "fast-sync: spot-check inconclusive for %r (%s); not treating as drift",
                    out.target, exc,
                )
            continue
        except Exception as exc:
            logger.warning(
                "fast-sync: spot-check inconclusive for %r after retries (%s); not treating as drift",
                out.target, exc,
            )
            continue
        if resp.get("VersionId") != out.version_id:
            drift.append((
                out.target,
                f"version drift: pinned {out.version_id}, remote returned {resp.get('VersionId')}",
            ))
    return drift


def verify_download(cache_path: Path, expected_md5: str) -> VerifyResult:
    """Stream-verify a downloaded file's md5 against the expected hash.

    Streams in 1 MiB chunks so we don't OOM on multi-GB parquet files. On
    mismatch or read error, the partial download is unlinked.
    """
    try:
        h = hashlib.md5(usedforsecurity=False)
        with open(cache_path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        actual = h.hexdigest()
        ok = (actual == expected_md5)
        if not ok:
            cache_path.unlink(missing_ok=True)
        return VerifyResult(ok=ok, expected=expected_md5, actual=actual)
    except Exception:
        cache_path.unlink(missing_ok=True)
        return VerifyResult(ok=False, expected=expected_md5, actual="")


def is_dir_fully_cached(cache_dir: Path, entries: list[DvcFileEntry]) -> bool:
    return all(is_cached(cache_dir, e.md5) for e in entries)


def ensure_dir_manifest(cache_dir: Path, entries: list[DvcFileEntry]) -> str:
    """Write a synthetic .dir manifest into the local cache.

    Byte-exact match against real DVC's output: sorted by relpath,
    ``[{"md5": ..., "relpath": ...}]`` only (no size or other fields),
    ``json.dumps(..., sort_keys=True)`` with otherwise-default kwargs
    (``separators=(", ", ": ")``, ``ensure_ascii=True``, no trailing
    newline). ``sort_keys=True`` is a defensive measure — Python preserves
    dict insertion order so the bytes happen to match without it, but a
    future refactor that adds a field or reorders keys would silently
    diverge.

    **Cache filename convention:** the manifest lives at
    ``.dvc/cache/files/md5/XX/YYYY.dir`` (with the literal ``.dir`` suffix
    on the filename). This matches DVC's own layout
    (``dvc_data.hashfile.db.local.LocalHashFileDB.oid_to_path``); any
    deviation breaks `dvc checkout`'s cache lookup. Returns ``{md5}.dir``.
    """
    sorted_entries = sorted(entries, key=lambda e: e.relpath)
    payload = [{"md5": e.md5, "relpath": e.relpath} for e in sorted_entries]
    serialized = json.dumps(payload, sort_keys=True).encode()
    manifest_md5 = hashlib.md5(serialized, usedforsecurity=False).hexdigest()
    full_md5 = f"{manifest_md5}.dir"
    manifest_path = cache_path_for(cache_dir, full_md5)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_path.write_bytes(serialized)
    _try_fsync_file(tmp_path)
    tmp_path.replace(manifest_path)
    _try_fsync_parent_dir(manifest_path)
    return full_md5


def fetch_to_cache(
    s3: Any, bucket: str, key: str, cache_path: Path, expected_md5: str,
    version_id: str | None = None,
    *,
    progress: Callable[[int], None] | None = None,
) -> bool:
    """Download an object to the DVC cache atomically.

    Sequence: download to ``cache_path.tmp`` → md5 verify → fsync the tmp file →
    rename to ``cache_path`` → fsync the parent directory. Parent-dir fsync is
    required for durability across crashes (POSIX rename atomicity does not
    guarantee the directory entry itself is durable).

    ``version_id`` is passed via ``ExtraArgs={"VersionId": ...}`` — boto3's
    ``download_file`` does not accept ``VersionId`` as a top-level kwarg.

    ``progress``, if provided, is passed as boto3's ``Callback=`` — invoked per
    transferred chunk with ``bytes_delta: int`` (default 8 MB chunks; may be
    smaller on the final chunk or for individual multipart parts). MUST be
    thread-safe — boto3's transfer manager runs multipart parts on its own
    internal pool.

    Retry: transient S3/network errors are retried via :func:`retry_transient`
    (3 attempts, capped backoff); the tmp file is unlinked between attempts.
    Non-transient errors (incl. md5 mismatch) propagate immediately.
    """
    tmp_path = cache_path.with_suffix(".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    extra_args: dict[str, str] | None = (
        {"VersionId": version_id} if version_id else None
    )

    def _attempt() -> bool:
        try:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
            if extra_args is not None:
                kwargs["ExtraArgs"] = extra_args
            if progress is not None:
                kwargs["Callback"] = progress
            s3.download_file(Filename=str(tmp_path), **kwargs)

            vr = verify_download(tmp_path, expected_md5)
            if not vr.ok:
                raise ValueError(f"md5 mismatch: expected {vr.expected}, got {vr.actual}")

            # fsync the tmp file's data, rename, then fsync the parent dir.
            # Both fsyncs are best-effort (no-op on platforms that reject
            # them, e.g. Windows) — the rename is what makes the write
            # visible; fsync only hardens durability.
            _try_fsync_file(tmp_path)
            tmp_path.replace(cache_path)
            _try_fsync_parent_dir(cache_path)
            return True
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return retry_transient(_attempt)


def fetch_dir_manifest(
    s3: Any, bucket: str, prefix: str, dir_md5: str, cache_dir: Path
) -> list[DvcFileEntry] | None:
    """Fetch a .dir manifest from S3 to local cache; return parsed entries.

    DVC stores manifests at ``prefix/files/md5/XX/YYYY.dir`` on S3 and
    ``.dvc/cache/files/md5/XX/YYYY.dir`` locally — the ``.dir`` suffix
    survives in the filename. ``raw_md5`` (without the suffix) is used
    only for the md5 verify, since that's the actual md5 of the manifest
    bytes; the cache/S3 paths use ``full_md5``.
    """
    full_md5 = dir_md5 if dir_md5.endswith(".dir") else f"{dir_md5}.dir"
    raw_md5 = full_md5[:-4]
    key = s3_key_for(prefix, full_md5)
    cache_path = cache_path_for(cache_dir, full_md5)
    try:
        fetch_to_cache(s3, bucket, key, cache_path, raw_md5, version_id=None)
    except Exception as exc:
        logger.warning("fast-sync: dir manifest fetch failed for %s: %s", full_md5, exc)
        cache_path.unlink(missing_ok=True)
        return None
    try:
        payload = json.loads(cache_path.read_bytes())
    except Exception as exc:
        logger.warning("fast-sync: dir manifest JSON parse failed for %s: %s", full_md5, exc)
        cache_path.unlink(missing_ok=True)
        return None
    if not isinstance(payload, list):
        cache_path.unlink(missing_ok=True)
        return None
    return [
        DvcFileEntry(
            md5=str(e.get("md5", "")),
            relpath=str(e.get("relpath", "")),
            size=int(e.get("size", 0) or 0),
            version_id=None,
        )
        for e in payload
    ]



def fetch_dir_contents(
    s3: Any, bucket: str, prefix: str, entries: list[DvcFileEntry],
    cache_dir: Path, jobs: int,
    *,
    progress: Callable[[int], None] | None = None,
) -> list[str]:
    """Concurrent download of md5-keyed-dir constituents.

    Deduplicates entries by md5 before submitting to the thread pool:
    ``fetch_to_cache`` uses a static ``.tmp`` sibling per cache_path,
    so two threads downloading entries with the same md5 would race
    on the same temp file. Any two entries with the same md5 yield the
    same bytes from S3, so one download serves every duplicate relpath.
    First-occurrence wins for test determinism.

    ``progress``, if provided, fires (a) per-chunk via boto3's Callback during
    each entry's download (from boto3's transfer threads) and (b) once per
    cache-hit with ``entry.size``. MUST be thread-safe — fetches run in a
    ThreadPoolExecutor.
    """
    if not entries:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in entries:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    # Slice 28: progress total = sum(out.size) which counts duplicate-content
    # entries by all their relpaths. Fire advance for each duplicate that
    # dedup dropped so the bar reaches 100% on dirs with repeated content.
    if progress is not None:
        for e in entries:
            if e.md5 in unique and unique[e.md5] is not e:
                try:
                    progress(e.size)
                except Exception:
                    pass

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            if progress is not None:
                try:
                    progress(entry.size)
                except Exception:
                    pass
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                s3_key_for(prefix, entry.md5),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                None,
                progress=progress,
            )
            return (entry.relpath, None)
        except Exception as exc:
            return (entry.relpath, f"{entry.relpath}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = [ex.submit(_one, e) for e in unique_entries]
        for fut in as_completed(futures):
            _, err = fut.result()
            if err:
                failures.append(err)
    return failures


def fetch_files_dir_contents(
    s3: Any, bucket: str, prefix: str, out: DvcOut,
    cache_dir: Path, jobs: int, remote_name: str,
    *,
    project_path: Path,
    progress: Callable[[int], None] | None = None,
) -> list[str]:
    """Concurrent download of files-format dir constituents.

    Like ``fetch_dir_contents`` but with real-path S3 keys and per-file
    version_ids. md5 dedup applies for the same .tmp-race reason. The
    *manifest* (written by ``ensure_dir_manifest``) receives the full
    un-deduped list so ``dvc checkout`` can materialize every relpath.

    Slice 27: branches per ``entry.is_path_based``. The non-path-based
    branch preserves pre-slice-27 behavior: ``<prefix>/<out.path>/<relpath>``.
    The path-based branch reconstructs ``<prefix>/<dvc_file.parent_rel>/<out.path>/<relpath>``
    so files-format dirs in nested .dvc files (e.g. ``data/raw/x.dvc``)
    fetch from the correct S3 location. ``project_path`` is required to
    compute ``dvc_file.parent_rel`` for path-based entries.

    Slice 28: ``progress``, if provided, fires (a) per-chunk via boto3's
    Callback during each entry's download and (b) once per cache-hit with
    ``entry.size``. MUST be thread-safe (ThreadPoolExecutor + boto3 transfer
    threads).
    """
    del remote_name
    if not out.files:
        return []
    failures: list[str] = []
    unique: dict[str, DvcFileEntry] = {}
    for e in out.files:
        unique.setdefault(e.md5, e)
    unique_entries = list(unique.values())

    # Slice 28: see fetch_dir_contents — fire advance for dedup-dropped
    # entries so the bar reaches 100% when out.files has duplicate md5s.
    if progress is not None:
        for e in out.files:
            if e.md5 in unique and unique[e.md5] is not e:
                try:
                    progress(e.size)
                except Exception:
                    pass

    def _key(entry: DvcFileEntry) -> str:
        if entry.is_path_based:
            if out.dvc_file is None:
                raise ValueError(
                    f"path-based DvcFileEntry under {out.path!r} missing parent dvc_file"
                )
            rel_dir = out.dvc_file.parent.relative_to(project_path)
            base = out.path if rel_dir == Path(".") else f"{rel_dir.as_posix()}/{out.path}"
            parts = [p for p in (prefix, base, entry.relpath) if p]
        else:
            # Preserve pre-slice-27 behaviour for non-path-based files-format
            # entries: prefix/out.path/<entry.relpath>. Existing tests at
            # tests/test_fast_sync.py:564, :767 pin this shape.
            parts = [p for p in (prefix, out.path, entry.relpath) if p]
        return "/".join(parts)

    def _one(entry: DvcFileEntry) -> tuple[str, str | None]:
        if is_cached(cache_dir, entry.md5):
            if progress is not None:
                try:
                    progress(entry.size)
                except Exception:
                    pass
            return (entry.relpath, None)
        try:
            fetch_to_cache(
                s3, bucket,
                _key(entry),
                cache_path_for(cache_dir, entry.md5),
                entry.md5,
                entry.version_id,
                progress=progress,
            )
            return (entry.relpath, None)
        except Exception as exc:
            return (entry.relpath, f"{entry.relpath}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = [ex.submit(_one, e) for e in unique_entries]
        for fut in as_completed(futures):
            _, err = fut.result()
            if err:
                failures.append(err)
    return failures



class _UnsyncableOut(Exception):
    """Raised by :func:`_fetch_out` when the out cannot be served for a
    named, expected reason (vs. an unexpected error, which propagates and
    is logged by the caller before routing)."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def _fetch_out(
    s3: Any,
    bucket: str,
    prefix: str,
    out: DvcOut,
    cache_dir: Path,
    jobs: int,
    remote_name: str,
    project_path: Path,
    advance: Callable[[int], None],
) -> list[str]:
    """Fetch one out's blobs into the local cache; return per-file failures.

    Pure mechanics — no routing decisions. Three out shapes:

    - files-format dirs: fetch constituents; on full success (re)write the
      synthetic ``.dir`` manifest so `dvc checkout` can find the directory.
    - md5-keyed dirs: reuse a cached ``.dir`` manifest when present, else
      fetch it (raises ``_UnsyncableOut`` when the remote doesn't have it);
      then fetch any uncached constituents. When everything was already
      cached, fire the aggregate ``advance`` so the progress bar still
      moves on warm-cache re-runs.
    - single files: fetch unless already cached (aggregate ``advance``).

    Returns the (possibly empty) list of per-file failures that survived
    the transient retries; the caller routes those. Out-level failures
    raise: ``_UnsyncableOut`` for the named manifest-unavailable case,
    anything else propagates as-is.
    """
    if out.is_files_format:
        assert out.files is not None
        failures = fetch_files_dir_contents(
            s3, bucket, prefix, out, cache_dir, jobs, remote_name,
            project_path=project_path,
            progress=advance,
        )
        if failures:
            return failures
        ensure_dir_manifest(cache_dir, out.files)
        return []
    if out.is_dir:
        entries = read_cached_dir_manifest(cache_dir, out.md5)
        if entries is None:
            entries = fetch_dir_manifest(s3, bucket, prefix, out.md5, cache_dir)
            if entries is None:
                raise _UnsyncableOut("dir manifest unavailable on remote")
        if not is_dir_fully_cached(cache_dir, entries):
            return fetch_dir_contents(
                s3, bucket, prefix, entries, cache_dir, jobs,
                progress=advance,
            )
        advance(out.size)
        return []
    if is_cached(cache_dir, out.md5):
        advance(out.size)
    else:
        fetch_to_cache(
            s3, bucket,
            s3_key_for_out(prefix, out, project_path),
            cache_path_for(cache_dir, out.md5),
            out.md5,
            out.version_id,
            progress=advance,
        )
    return []


def _build_fast_pull_result(
    *,
    synced: int,
    fallback: list[str],
    incomplete_targets: list[str],
    blocked_targets: list[str],
    blocked_reasons: dict[str, str],
    drift_notes: list[str],
    files_dir_failures: list[str],
) -> FastPullResult:
    """Assemble try_fast_pull's result: dedupe the buckets (first occurrence
    wins) and compose the human-readable ``reason`` summary."""
    unique_incomplete = list(dict.fromkeys(incomplete_targets))
    unique_blocked = list(dict.fromkeys(blocked_targets))
    reason_bits: list[str] = []
    if fallback:
        reason_bits.append("partial fallback")
    if drift_notes:
        reason_bits.append("version_id spot-check drift: " + "; ".join(drift_notes))
    if unique_incomplete:
        reason_bits.append(
            "per-file download failures (not demoted to dvc pull): "
            + ", ".join(unique_incomplete)
        )
    if unique_blocked:
        reason_bits.append(
            "version-aware target(s) failed (never routed to dvc pull): "
            + ", ".join(unique_blocked)
        )
    return FastPullResult(
        success=not fallback and not unique_incomplete and not unique_blocked,
        synced_count=synced,
        fallback_targets=fallback,
        incomplete_targets=unique_incomplete,
        reason="; ".join(reason_bits),
        files_dir_failures=files_dir_failures,
        blocked_targets=unique_blocked,
        blocked_reasons=blocked_reasons,
    )


def _create_s3_client(remote_cfg: dict[str, str], aws_profile_name: str | None) -> Any:
    try:
        session = boto3.Session(profile_name=aws_profile_name)
    except ProfileNotFound:
        session = boto3.Session()

    endpoint_url = remote_cfg.get("endpointurl")
    if not endpoint_url:
        endpoint_url = remote_cfg.get("endpoint")
    return session.client("s3", endpoint_url=endpoint_url, region_name=remote_cfg.get("region"))


class FastSyncOps(Protocol):
    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
        reporter: Optional["Reporter"] = None,
    ) -> FastPullResult: ...


class SubprocessFastSyncOps:
    def __init__(
        self,
        *,
        jobs: int = 8,
        progress: Callable[[int], None] | None = None,
        aws_profile_name: str | None = None,
    ) -> None:
        # Progress callback: ``Callable[[int], None]`` invoked with bytes_delta.
        # Fires per-chunk during S3 transfers (via boto3's ``Callback=`` on
        # ``download_file``) AND once per cache-hit / dir-fully-cached path with
        # the out's / entry's declared size. See ``set_progress`` for the
        # thread-safety + retry contract.
        self._default_jobs = jobs
        self._progress = progress
        self._aws_profile_name = aws_profile_name

    def set_progress(self, progress: Callable[[int], None] | None) -> None:
        """Install an ``advance(n_bytes)`` callback for subsequent
        ``try_fast_pull`` invocations. Pass ``None`` to disable. Idempotent.

        The callback fires from boto3's per-chunk ``Callback`` during S3
        transfers (default 8 MB chunks; may be smaller for the final chunk
        or for individual multipart parts) AND once per cache-hit with the
        fully-cached out's / entry's declared size, AND once per
        dir-fully-cached short-circuit with the dir's aggregate size.

        Thread-safety: ``fetch_{dir,files_dir}_contents`` submit per-entry
        work to a ``ThreadPoolExecutor``, and boto3's transfer manager runs
        multipart parts on its own internal pool. The callback therefore
        runs on worker threads. ``rich.Progress.update`` is thread-safe and
        satisfies this contract; custom callables must be thread-safe too.

        On retry: ``fetch_to_cache`` makes up to 3 attempts on transient S3
        errors. Bytes from a failed attempt are NOT rolled back from the
        advance total; the bar may visually lap past 100% within a single
        out across retries. This is intentional — the bar reflects
        bytes-on-the-wire, not bytes-committed.
        """
        self._progress = progress

    def try_fast_pull(
        self,
        *,
        project_path: Path,
        targets: list[str],
        remote_name: str,
        jobs: int = 8,
        pipeline_outs: list[DvcOut] | None = None,
        reporter: Optional["Reporter"] = None,
    ) -> FastPullResult:
        def _degrade_all(reason: str) -> FastPullResult:
            """Curry this call's constants into :func:`_degrade_all_targets`
            (the all-or-nothing guard degradation, split per
            :func:`dvc_pull_can_serve`)."""
            return _degrade_all_targets(
                project_path, targets, remote_name, pipeline_outs, reason,
            )

        ok, reason = _check_dvc()
        if not ok:
            return _degrade_all(reason or "dvc version mismatch")

        try:
            remote_cfg = get_remote_config(project_path, remote_name)
        except (FileNotFoundError, KeyError) as exc:
            return _degrade_all(f"remote config not found: {exc}")

        try:
            bucket, prefix = parse_s3_url(remote_cfg.get("url", ""))
        except ValueError as exc:
            return _degrade_all(f"non-S3 remote: {exc}")

        if boto3 is None:
            return _degrade_all("boto3 not importable")

        s3 = _create_s3_client(remote_cfg, self._aws_profile_name)
        try:
            versioning_enabled = check_bucket_versioning(s3, bucket)
        except NoCredentialsError as exc:
            # Named, non-retried degradation: retrying cannot mint credentials.
            return _degrade_all(f"AWS credentials unavailable (not retried): {exc}")
        except Exception as exc:
            # Probe failed even after transient retries — say so, instead of
            # the old (misleading) "bucket versioning disabled".
            return _degrade_all(f"bucket versioning probe failed: {exc}")
        if not versioning_enabled:
            return _degrade_all("bucket versioning disabled")

        all_outs, fallback, hash_missing = classify_targets(project_path, targets, remote_name)

        if pipeline_outs:
            all_outs.extend(pipeline_outs)

        fallback.extend(hash_missing)

        # Spot-check drift demotes ONLY the affected outs (named in the
        # reason), never the whole batch. Probe errors are retried inside
        # spot_check_versions; still-failing probes are inconclusive and
        # demote nothing. A drifted out is by definition version-aware
        # (only outs with a version_id are probed), so it becomes a loud
        # error — never a plain `dvc pull`.
        try:
            drift = spot_check_versions(s3, bucket, prefix, all_outs, project_path)
        except NoCredentialsError as exc:
            return _degrade_all(f"AWS credentials unavailable (not retried): {exc}")
        blocked_targets: list[str] = []
        blocked_reasons: dict[str, str] = {}
        drift_notes: list[str] = []
        if drift:
            drifted_targets = list(dict.fromkeys(t for t, _ in drift))
            drift_notes = [f"{t} ({why})" for t, why in drift]
            for t, why in drift:
                logger.warning("fast-sync: spot-check drift for %r: %s", t, why)
                blocked_reasons.setdefault(t, f"version_id spot-check drift: {why}")
            blocked_targets.extend(drifted_targets)
            drifted_set = set(drifted_targets)
            all_outs = [o for o in all_outs if o.target not in drifted_set]

        cache_dir = project_path / _DEFAULT_DVC_CACHE_REL
        synced = 0
        files_dir_failures: list[str] = []
        incomplete_targets: list[str] = []

        def _record_per_file_failures(out: DvcOut, failures: list[str]) -> None:
            # A per-file failure that survives the transient retries fails
            # THAT FILE by name. Same split as _route_out, except the
            # unservable bucket differs: an out plain `dvc pull` can restore
            # (md5-keyed) keeps the fallback route, while a version-aware
            # out lands in incomplete_targets (its cache blobs are partial)
            # so the caller skips both its checkout and any fallback pull,
            # reports it loudly, and exits non-zero.
            files_dir_failures.extend(f"{out.target}: {f}" for f in failures)
            if dvc_pull_can_serve(out):
                fallback.append(out.target)
                logger.warning(
                    "fast-sync: %d file(s) of %r failed after retries; "
                    "md5-keyed out, demoting to the dvc pull fallback",
                    len(failures), out.target,
                )
                return
            incomplete_targets.append(out.target)
            logger.warning(
                "fast-sync: %d file(s) of %r failed after retries; NOT demoting to dvc pull",
                len(failures), out.target,
            )
            if reporter is not None:
                reporter.warn(
                    f"fast-sync: {len(failures)} file(s) of {out.target} failed to download; "
                    f"retry with: mintd data pull {out.target}"
                )

        def _advance(n: int) -> None:
            # Slice 28: piecewise progress accounting. Fires from boto3's
            # Callback during transfers (per-chunk), from cache-hit branches
            # below, and from the dir-fully-cached short-circuit. Bound to
            # ``self._progress`` at call time so set_progress(None) mid-loop
            # still disables it. Bare except keeps UI bugs from aborting pulls.
            if self._progress is not None:
                try:
                    self._progress(n)
                except Exception:
                    pass

        # The loop body makes routing decisions only; fetch mechanics live
        # in _fetch_out. Per-file failures route via _record_per_file_failures
        # (fallback vs incomplete); out-level failures route via _route_out
        # (fallback vs blocked).
        for i, out in enumerate(all_outs, start=1):
            if reporter is not None:
                target = out.target if len(out.target) <= 50 else out.target[:47] + "..."
                reporter.update_progress_desc(f"Pulling {target} ({i}/{len(all_outs)})...")
            try:
                failures = _fetch_out(
                    s3, bucket, prefix, out, cache_dir, jobs, remote_name,
                    project_path, _advance,
                )
                if failures:
                    _record_per_file_failures(out, failures)
                    continue
                synced += 1
            except _UnsyncableOut as exc:
                _route_out(
                    out, exc.why,
                    fallback=fallback,
                    blocked_targets=blocked_targets,
                    blocked_reasons=blocked_reasons,
                )
            except Exception as exc:
                logger.warning("fast-sync target %r failed: %s", out.target, exc)
                _route_out(
                    out, str(exc),
                    fallback=fallback,
                    blocked_targets=blocked_targets,
                    blocked_reasons=blocked_reasons,
                )

        return _build_fast_pull_result(
            synced=synced,
            fallback=fallback,
            incomplete_targets=incomplete_targets,
            blocked_targets=blocked_targets,
            blocked_reasons=blocked_reasons,
            drift_notes=drift_notes,
            files_dir_failures=files_dir_failures,
        )