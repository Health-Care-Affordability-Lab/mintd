"""``mintd cache`` — durable repo file cache (S3) policy.

A second policy skin over share S1's verb-agnostic transport core
(``_share_ops`` Stratum T): different prefix, different lifetime, same bytes.
Three verbs — ``cache push`` / ``cache pull`` / ``cache ls`` — mirror arbitrary
repo files to ``<computed_prefix>/cache/<repo-relative-path>`` in the project's
own S3 prefix (read from ``.dvc/config``, never recomputed). Any file in the
working tree may be cached EXCEPT one that is DVC-tracked (that belongs to
``mintd data push``) or lives under ``.git/`` / ``.dvc/``. A pushed file's key
is its full repo-relative path under the single lifecycle-tagged ``cache/``
segment, so ``cache push data/scratch/x`` → ``<prefix>/cache/data/scratch/x``
and ``cache pull`` restores it back to ``data/scratch/x`` — pull reconstructs
files at their repo-relative paths, contained to the project root.

Boundary discipline (grep-auditable): this module contains ZERO
``upload_file`` / ``download_file`` / ``put_object`` calls — every byte moves
through ``upload_object`` / ``download_object`` from ``_share_ops``. Listing
(and the push size-precheck) reuse ``list_product_objects``; ``ls`` reuses the
``_data_ls_payload`` / ``_pretty_data_ls`` renderers. No new transport, no new
listing, no new render.

To disambiguate from DVC's ``.dvc/cache`` and mintd's ``config.cache_dir``,
every user-facing string calls this store the "repo file cache (S3)".
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional
from uuid import uuid4

from ._fast_sync_ops import (
    ClientError,
    _create_s3_client,
    check_bucket_versioning,
    discover_all_outs,
    get_remote_config,
    outs_for_target,
    parse_s3_url,
    partition_pipeline_outs,
    retry_transient,
    s3_key_for_out,
    workspace_path_for,
)
from ._s3_listing_ops import (
    S3ListingError,
    S3ListingResult,
    _normalise_sub_path,
    list_product_objects,
)
from ._share_ops import (
    RemoteObjectNotFound,
    TransferError,
    _has_control_char,
    download_object,
    file_sha256,
    head_remote_object,
    upload_object,
)

if TYPE_CHECKING:
    from ._config import Config
    from ._console import Reporter

# The ONLY occurrence of the literal cache-segment name.
CACHE_DIR_NAME = "cache"
# The per-object tag riding S1's extra_args; an admin lifecycle rule filters on
# it for NoncurrentVersionExpiration (see ``lifecycle_covers_cache_tag``).
CACHE_LANE_TAG_KEY = "mintd-lane"
CACHE_LANE_TAG_VALUE = "cache"
CACHE_LANE_TAGGING = f"{CACHE_LANE_TAG_KEY}={CACHE_LANE_TAG_VALUE}"
# The user-metadata key (``x-amz-meta-mintd-sha256``) carrying the full-file
# SHA256; botocore lowercases/de-prefixes it on HEAD to ``mintd-sha256``.
CACHE_SHA256_META_KEY = "mintd-sha256"

# Matches ``_create_s3_client`` and ``list_product_objects``' factory param.
Factory = Callable[[dict[str, str], Optional[str]], Any]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CacheError(Exception):
    """A cache operation failed. Carries a CLI-ready ``hint`` (may be ``None``).

    Mapped in cli.py exactly like ``ShareError`` → ``reporter.error``."""

    def __init__(self, msg: str, *, hint: str | None = None) -> None:
        super().__init__(msg)
        self.hint = hint


class CacheKeyError(CacheError):
    """A key/path failed the forbidden-set or containment gate (§C/§E)."""


class CacheCollisionError(CacheError):
    """A DVC-tracked out's key would overlap the repo file cache (S3) (§F)."""


# ---------------------------------------------------------------------------
# §C — the two-coordinate-space key mapping
# ---------------------------------------------------------------------------


def push_key(prefix: str, rel_posix: str) -> str:
    """FULL-KEY space. ``rel_posix`` is the file's full repo-relative path (any
    path, already refused if DVC-tracked / protected / forbidden). The cache
    mirrors it under the project's lifecycle-tagged ``cache/`` S3 segment, so the
    key is ``<prefix>/cache/<rel_posix>``. A file already under a top-level
    ``cache/`` dir maps to ``<prefix>/cache/cache/<…>`` — the outer ``cache/`` is
    the lane namespace, the inner is the repo path (intentional, uniform rule)."""
    base = cache_list_prefix(prefix)
    return f"{base}/{rel_posix}"


def _is_forbidden_path(path_posix: str) -> bool:
    """Forbidden-set test: a leading ``/``, any ``\\``, or any path *segment*
    that is empty (``""``), a single dot (``.``), or ``..``. Segment-scoped
    (not substring) so real traversal (``../evil``, ``x/../../y``, ``..``) is
    refused while a legitimate filename containing consecutive dots
    (``v1..2.csv``, ``report..final.parquet``) is allowed — a bare ``..``
    substring match over-refuses and, on pull, one such stored key would break
    the entire operation.

    The ``""`` / ``.`` segments matter for the PULL gate specifically: a planted
    key ``<prefix>/cache/.`` (or ``…/sub/.`` or ``…/cache`` with no remainder)
    lists back as the remainder ``.`` / ``sub/.`` / ``""``. Those normalise (via
    pathlib) back onto the ``cache/`` directory location itself, so BOTH the
    string gate (a ``..``-only check) and the filesystem containment gate
    (``dest.resolve()`` collapses ``.`` and lands inside ``cache_root``) would
    otherwise pass — and ``download_object`` would write attacker bytes AS A FILE
    onto the ``cache/`` path. A legitimate remainder never contains an empty or
    ``.`` segment, so refusing them only rejects corrupted/hostile keys.

    Control characters (C0 incl. NUL/CR/LF/TAB, and DEL/ESC) are refused too —
    the same screen ``_share_ops._has_control_char`` applies to share refs. S3
    object keys freely allow embedded ``\\n`` / ``\\x1b`` bytes, so anyone with
    the shared lab-wide ``[mintd]`` profile (§E threat model) could otherwise
    plant a key like ``cache/legit.bin\\n✓ pulled 999 file(s)…`` that slips the
    ``..``/``/``/``\\`` gate and, on a sha match, downloads and writes a file
    whose NAME is a forged status line (or, on a mismatch, smuggles that forged
    line into the pull's stderr). None belong in a cache key."""
    if path_posix.startswith("/") or "\\" in path_posix:
        return True
    if _has_control_char(path_posix):
        return True
    return any(seg in ("", ".", "..") for seg in path_posix.split("/"))


def safe_cache_remainder(remainder: str) -> str:
    """RELATIVE-REMAINDER space. ``remainder`` is what ``list_product_objects``
    returns when listing under ``<prefix>/cache``. Returns it unchanged iff it
    is forbidden-set clean; else raises ``CacheKeyError``. The independent
    second gate — a filesystem containment check — lives in ``cache_pull``."""
    if _is_forbidden_path(remainder):
        raise CacheKeyError(
            f"unsafe key in the repo file cache (S3): {remainder!r}",
            hint="a corrupted namespace is an admin conversation — the object was "
            "not written by mintd cache push",
        )
    return remainder


def _display_safe(text: str) -> str:
    """Neutralize control characters for interpolation into a reporter message.

    A rejected (hostile) cache key may carry embedded newline/ESC bytes; echoing
    them verbatim lets an attacker forge status lines or inject ANSI into the
    CLI's stderr (log injection). ``unicode_escape`` turns them into inert
    ``\\n`` / ``\\x1b`` text. Only ever applied to already-refused keys, so the
    aggressive escaping of legitimate unicode is immaterial."""
    return text.encode("unicode_escape").decode("ascii")


def cache_list_prefix(prefix: str) -> str:
    """The S3 prefix under which objects live for listing/enumeration."""
    return f"{prefix}/{CACHE_DIR_NAME}" if prefix else CACHE_DIR_NAME


# Repo subtrees mintd never mirrors: git's own store and DVC's internal state.
# A repo-relative path with ANY segment exactly ``.git`` / ``.dvc`` is refused on
# push and refused-per-object on pull — a hostile key could otherwise clobber
# ``.git/config`` or ``.dvc/config`` when pull reconstructs at the repo path, and
# a broad ``push .`` (or a vendored/submodule tree) would otherwise sweep git's
# object store. A DVC POINTER file (``data/foo.dvc``) has segments ``data`` /
# ``foo.dvc`` — never a bare ``.dvc`` segment — so only the internal directories
# match; likewise ``.gitignore`` is a filename, not a ``.git`` segment.
_PROTECTED_SEGMENTS = frozenset({".git", ".dvc"})


def _is_protected_repo_path(rel_posix: str) -> bool:
    # Case-fold each segment: on a case-insensitive filesystem (macOS APFS,
    # Windows NTFS — the reshape's default targets) a planted key '.GIT/config'
    # names the SAME on-disk file as '.git/config', so a case-sensitive compare
    # would let a pull write attacker bytes into git's internals (RCE on the next
    # git op). ``resolve()`` does NOT canonicalize case on macOS, so the compare —
    # not the OS — must be case-insensitive.
    return any(seg.casefold() in _PROTECTED_SEGMENTS for seg in rel_posix.split("/"))


# ---------------------------------------------------------------------------
# §D/§E — pure skip matrices (truth-table tested)
# ---------------------------------------------------------------------------

PushDecision = Literal["upload", "skip"]
PullDecision = Literal["download", "skip"]


def decide_push(
    *,
    local_size: int,
    remote_size: int | None,
    local_sha256: str | None,
    remote_sha256: str | None,
) -> PushDecision:
    """Skip iff the remote object exists, its size matches, AND its stored
    ``mintd-sha256`` metadata equals the local full-file SHA256. Absent remote
    object, size drift, absent metadata (object landed via ``aws s3 cp``), or a
    SHA mismatch all upload. Only a *verified* match skips."""
    if remote_size is None:
        return "upload"
    if remote_size != local_size:
        return "upload"
    if remote_sha256 is None or local_sha256 is None:
        return "upload"
    return "skip" if local_sha256 == remote_sha256 else "upload"


def decide_pull(
    *,
    local_exists: bool,
    local_size: int | None,
    remote_size: int,
    local_sha256: str | None,
    remote_sha256: str | None,
) -> PullDecision:
    """Skip iff a local file exists with matching size AND its full-file SHA256
    equals the remote ``mintd-sha256`` metadata. Missing file, size drift,
    absent metadata, or a mismatch all download."""
    if not local_exists:
        return "download"
    if local_size != remote_size:
        return "download"
    if remote_sha256 is None or local_sha256 is None:
        return "download"
    return "skip" if local_sha256 == remote_sha256 else "download"


# ---------------------------------------------------------------------------
# §F — collision guard (the key function IS the oracle)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Collision:
    key: str
    out_path: str
    source: str


def _all_dvc_outs(project_path: Path, remote_name: str) -> list:
    """Every DVC out for the project: ``.dvc`` pointers (via
    ``discover_all_outs`` + ``outs_for_target``) plus ``dvc.lock`` stage outs.
    The single enumeration both the collision guard and the tracked-path set
    read from, so neither can drift from the other's view of DVC reality."""
    outs = []
    for target in discover_all_outs(project_path):
        outs.extend(outs_for_target(project_path, target, remote_name))
    _, lock_outs = partition_pipeline_outs(project_path, remote_name)
    outs.extend(lock_outs)
    return outs


def dvc_tracked_paths(project_path: Path, remote_name: str) -> set[str]:
    """Repo-relative posix path of every DVC-tracked workspace out (files AND
    directory-out roots). ``cache push`` refuses any path in — or under a
    directory in — this set (a versioned product belongs to ``mintd data
    push``); ``cache pull`` refuses to restore over one (a hostile key must not
    clobber a tracked out). Uses ``workspace_path_for`` — the exact anchoring
    ``.dvc``-file/``dvc.lock`` outs record — so it matches where DVC materializes
    each out. Outs that resolve outside the project (shouldn't happen) are
    skipped."""
    root = project_path.resolve()
    tracked: set[str] = set()
    for out in _all_dvc_outs(project_path, remote_name):
        try:
            rel = workspace_path_for(project_path, out).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        tracked.add(rel)
    return tracked


def _is_tracked(rel_posix: str, tracked: set[str]) -> bool:
    """True iff ``rel_posix`` equals a tracked out or lives under a tracked
    directory out (``rel`` startswith ``<tracked>/``). Case-folded for the same
    reason as ``_is_protected_repo_path``: on a case-insensitive filesystem
    ``data/FINAL.parquet`` is the same file as a tracked ``data/final.parquet``,
    so a case-sensitive compare would let the cache clobber (pull) or shadow
    (push) a versioned out."""
    r = rel_posix.casefold()
    for t in tracked:
        tc = t.casefold()
        if r == tc or r.startswith(f"{tc}/"):
            return True
    return False


def _dvc_outs_under_cache(project_path: Path, remote_name: str) -> list[_Collision]:
    """Every DVC out whose path-based key starts with ``cache/``. Asks
    ``s3_key_for_out`` — the exact function that builds fast-sync's keys — so the
    guard can never drift from DVC-key reality. Md5-keyed outs return
    ``files/md5/…`` and never trip it; outs that raise ``ValueError``
    (synthetic/no key) are skipped."""
    collisions: list[_Collision] = []
    for out in _all_dvc_outs(project_path, remote_name):
        try:
            key = s3_key_for_out("", out, project_path)
        except ValueError:
            continue
        if key == CACHE_DIR_NAME or key.startswith(f"{CACHE_DIR_NAME}/"):
            source = out.path
            if out.dvc_file is not None:
                try:
                    source = out.dvc_file.relative_to(project_path).as_posix()
                except ValueError:
                    source = out.dvc_file.name
            collisions.append(_Collision(key=key, out_path=out.path, source=source))
    return collisions


def guard_no_dvc_outs_under_cache(project_path: Path, remote_name: str) -> None:
    """Refuse (naming the out + its source) when any DVC out's key would
    interleave with the repo file cache (S3). Runs on push AND pull before any
    write."""
    collisions = _dvc_outs_under_cache(project_path, remote_name)
    if not collisions:
        return
    c = collisions[0]
    raise CacheCollisionError(
        f"DVC-tracked output {c.out_path!r} ({c.source}) overlaps the "
        f"repo file cache (S3) namespace",
        hint=f"its version-aware S3 keys would interleave with objects under "
        f"{cache_list_prefix('')}/ — rename the tracked out or untrack it "
        f"(dvc remove {c.source})",
    )


# ---------------------------------------------------------------------------
# §B — resolution chain (try_fast_pull's sequence, failing loud)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoRemote:
    bucket: str
    prefix: str
    endpoint: str
    remote_cfg: dict[str, str]
    remote_name: str


def _endpoint_of(remote_cfg: Mapping[str, str]) -> str:
    return remote_cfg.get("endpointurl") or remote_cfg.get("endpoint") or ""


def resolve_repo_remote(project_path: Path, remote: str | None) -> RepoRemote:
    """Resolve ``(bucket, prefix, endpoint, remote_cfg)`` from the project's own
    ``.dvc/config`` — the materialized value is the truth, never recomputed via
    ``compute_storage_prefix``. Every failure is a typed, hinted ``CacheError``
    (no silent degradation — cache has no fallback lane)."""
    from .data_ops import _default_dvc_remote

    remote_name = remote or _default_dvc_remote(project_path) or "origin"
    try:
        remote_cfg = get_remote_config(project_path, remote_name)
    except (FileNotFoundError, KeyError) as exc:
        raise CacheError(
            f"could not resolve DVC remote {remote_name!r} from .dvc/config: {exc}",
            hint="check .dvc/config has an [remote] section with an s3:// url, "
            "or pass --remote",
        ) from exc
    url = remote_cfg.get("url", "")
    try:
        bucket, prefix = parse_s3_url(url)
    except ValueError as exc:
        raise CacheError(
            f"DVC remote {remote_name!r} is not an S3 remote: {url!r}",
            hint="the repo file cache (S3) requires an s3:// DVC remote",
        ) from exc
    return RepoRemote(
        bucket=bucket,
        prefix=prefix,
        endpoint=_endpoint_of(remote_cfg),
        remote_cfg=remote_cfg,
        remote_name=remote_name,
    )


def _listing_factory(factory: Factory, remote_cfg: dict[str, str]) -> Factory:
    """Region-faithful client closure for ``list_product_objects``: its default
    factory synthesizes ``{"endpoint": endpoint}`` and drops ``region``. We
    inject the project's full ``remote_cfg`` so one client convention (endpoint
    AND region) serves all three verbs, zero edits to the listing module."""
    return lambda _cfg, prof: factory(remote_cfg, prof)


def list_cache_objects(
    remote: RepoRemote,
    *,
    sub_path: str | None,
    aws_profile_name: str | None,
    factory: Factory,
) -> S3ListingResult:
    """One paginated ``ListObjectsV2`` under ``<prefix>/cache[/<sub>]`` in
    relative-remainder space. Serves ls, pull enumeration, and the push
    size-precheck. May raise ``S3ListingError`` (bucket missing / access
    denied) or ``ValueError`` (bad ``sub_path``) — the ls handler maps those to
    hinted errors; ``push``/``pull`` map them via ``_list_or_cache_error``."""
    return list_product_objects(
        bucket=remote.bucket,
        prefix=cache_list_prefix(remote.prefix),
        endpoint=remote.endpoint,
        sub_path=sub_path,
        recursive=True,
        include_versions=False,
        aws_profile_name=aws_profile_name,
        s3_client_factory=_listing_factory(factory, remote.remote_cfg),
    )


def _list_or_cache_error(
    remote: RepoRemote,
    *,
    sub_path: str | None,
    aws_profile_name: str | None,
    factory: Factory,
) -> S3ListingResult:
    """``list_cache_objects`` with ``S3ListingError`` mapped to a hinted
    ``CacheError`` so a missing/forbidden bucket on the push/pull LIST is a
    documented failure, never a traceback out of ``main()``."""
    try:
        return list_cache_objects(
            remote, sub_path=sub_path, aws_profile_name=aws_profile_name, factory=factory
        )
    except S3ListingError as exc:
        raise CacheError(
            f"could not list the repo file cache (S3): {exc}",
            hint="check the project's remote url in .dvc/config and AWS credentials "
            "(mintd config validate)",
        ) from exc


# ---------------------------------------------------------------------------
# Ledger + summaries
# ---------------------------------------------------------------------------

OutcomeStatus = Literal[
    "uploaded", "unchanged", "downloaded", "skipped_symlink",
    "skipped_existing", "failed",
]


@dataclass(frozen=True)
class TransferOutcome:
    rel: str  # repo-relative posix path (the file's path in the working tree)
    status: OutcomeStatus
    bytes: int = 0
    reason: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class CachePushSummary:
    outcomes: list[TransferOutcome]
    key_prefix: str  # the s3 key prefix objects live under (e.g. lab/proj/cache)
    bucket: str
    elapsed_s: float
    dry_run: bool

    def _count(self, status: OutcomeStatus) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def uploaded(self) -> int:
        return self._count("uploaded")

    @property
    def unchanged(self) -> int:
        return self._count("unchanged")

    @property
    def skipped_symlink(self) -> int:
        return self._count("skipped_symlink")

    @property
    def failed(self) -> list[TransferOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def uploaded_bytes(self) -> int:
        return sum(o.bytes for o in self.outcomes if o.status == "uploaded")


@dataclass(frozen=True)
class CachePullSummary:
    outcomes: list[TransferOutcome]
    sub: str  # normalized --prefix (e.g. "isochrones/ct/" or "")
    elapsed_s: float

    def _count(self, status: OutcomeStatus) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def pulled(self) -> int:
        return self._count("downloaded")

    @property
    def unchanged(self) -> int:
        return self._count("unchanged")

    @property
    def skipped_existing(self) -> list[TransferOutcome]:
        """Objects NOT pulled because an untracked local file of the same repo
        path already exists with different content (and ``--force`` was off)."""
        return [o for o in self.outcomes if o.status == "skipped_existing"]

    @property
    def failed(self) -> list[TransferOutcome]:
        return [o for o in self.outcomes if o.status == "failed"]

    @property
    def pulled_bytes(self) -> int:
        return sum(o.bytes for o in self.outcomes if o.status == "downloaded")


# ---------------------------------------------------------------------------
# §D — push enumeration + planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PushItem:
    rel: str  # repo-relative posix path (the file's path in the working tree)
    abs_path: Path
    size: int
    # Set when enumeration's ``stat()`` raised (a TOCTOU: a producer removed /
    # locked the file between ``os.walk`` listing it and ``_add_file`` stat'ing
    # it). The item is still enumerated so it lands in the ledger as a per-file
    # ``failed`` outcome (``_push_one`` short-circuits on it) rather than letting
    # a bare ``OSError`` abort the whole push as a raw traceback out of main().
    read_error: OSError | None = None


def _resolve_arg(project_path: Path, arg: str) -> Path:
    """The un-resolved candidate path (symlink-preserving). The caller MUST
    screen ``is_symlink()`` on this path *before* calling ``.resolve()`` —
    resolving first follows a top-level symlink arg to its target, so
    ``is_symlink()`` reads False and the skip-and-warn posture is bypassed
    (mirrors the in-walk handling on the un-resolved child path)."""
    p = Path(arg)
    return p if p.is_absolute() else project_path / p


# One refusal reason per path the enumerator declines. Ordered by the priority
# ``cache_push`` reports them in (the most likely user intent first).
RefusalReason = Literal[
    "dvc_tracked", "protected", "outside_project", "forbidden", "project_root"
]


@dataclass(frozen=True)
class _Refused:
    path: str  # repo-relative posix (or the raw arg when it escaped the project)
    reason: RefusalReason


@dataclass(frozen=True)
class _PushScan:
    """Everything one enumeration pass found — all violations collected, never
    first-error, so ``cache_push`` can report every problem at once."""
    items: list[_PushItem]
    symlinks: list[str]
    refused: list[_Refused]
    empty_args: list[str]

    def refused_by(self, reason: RefusalReason) -> list[str]:
        return [r.path for r in self.refused if r.reason == reason]


def enumerate_push_items(
    project_path: Path, paths: list[str], tracked: set[str]
) -> _PushScan:
    """Enumerate files under the given paths, collecting every refusal.

    Any file in the working tree is a candidate EXCEPT one that is DVC-tracked
    (``tracked`` — from :func:`dvc_tracked_paths`), lives under ``.git/`` /
    ``.dvc/``, escapes the project root, or is forbidden-set-unclean (a literal
    backslash / control char — never silently dropped, always refused loudly).
    Symlinks are skipped (not followed, the ``discover_all_outs`` posture) and
    reported. An arg matching zero files lands in ``empty_args``.
    """
    project_root = project_path.resolve()
    items: list[_PushItem] = []
    symlinks: list[str] = []
    refused: list[_Refused] = []
    empty_args: list[str] = []
    seen: set[str] = set()

    def _rel_of(p: Path) -> str:
        """Project-relative posix path of a symlink discovered mid-walk, so each
        skipped symlink is named individually (not by the outer ``arg``). ``p``
        lives under ``abs_path`` (itself under ``project_root``), so the
        ``relative_to`` is pure-path and never follows the link."""
        try:
            return p.relative_to(project_root).as_posix()
        except ValueError:
            return p.as_posix()

    def _repo_rel(abs_path: Path) -> str | None:
        """Repo-relative posix path iff ``abs_path`` is inside the project root
        and not the root itself; else None (escaped the project)."""
        try:
            rel = abs_path.relative_to(project_root).as_posix()
        except ValueError:
            return None
        return rel if rel != "." else None

    def _classify(rel: str) -> RefusalReason | None:
        """The refusal reason for a repo-relative path, or None if cacheable.
        Order matters only for the single-file arg branch's message; the scan
        records exactly one reason per path."""
        if _is_forbidden_path(rel):
            return "forbidden"
        if _is_protected_repo_path(rel):
            return "protected"
        if _is_tracked(rel, tracked):
            return "dvc_tracked"
        return None

    def _add_file(abs_path: Path) -> None:
        # A file discovered mid-walk that fails a check is REFUSED LOUDLY (added
        # to ``refused``), never silently dropped — a bare ``return`` would omit
        # it from every ledger, letting a sibling valid file report success while
        # this one stayed only on local disk (silent data loss, the red-team P0).
        try:
            rel = abs_path.relative_to(project_root).as_posix()
        except ValueError:
            refused.append(_Refused(_rel_of(abs_path), "outside_project"))
            return
        reason = _classify(rel)
        if reason is not None:
            refused.append(_Refused(rel, reason))
            return
        if rel in seen:
            return
        seen.add(rel)
        try:
            size = abs_path.stat().st_size
        except OSError as exc:
            # TOCTOU: gone/unreadable between os.walk and this stat. Enumerate it
            # anyway (size 0, carrying the error) so it becomes a per-file failed
            # ledger entry instead of a raw traceback (see _PushItem.read_error).
            items.append(_PushItem(rel=rel, abs_path=abs_path, size=0, read_error=exc))
            return
        items.append(_PushItem(rel=rel, abs_path=abs_path, size=size))

    for arg in paths:
        raw = _resolve_arg(project_path, arg)
        if raw.is_symlink():  # screen BEFORE resolve() so a symlink arg is skipped
            symlinks.append(arg)
            continue
        abs_path = raw.resolve()
        if abs_path.is_dir():
            rel = _repo_rel(abs_path)
            if rel is None:
                # The whole subtree escaped the project (or IS the project root —
                # we refuse a bare project-root arg rather than sweep the tree).
                reason: RefusalReason = (
                    "project_root" if abs_path == project_root else "outside_project"
                )
                refused.append(_Refused(arg, reason))
                continue
            # Refuse a DVC-tracked / protected directory as ONE violation rather
            # than walking it and refusing each file individually.
            dir_reason = _classify(rel)
            if dir_reason is not None:
                refused.append(_Refused(rel, dir_reason))
                continue
            before_refused = len(refused)
            files_here = 0  # regular (non-symlink) files this arg's walk found
            for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
                # Screen symlinked SUBDIRECTORIES: os.walk(followlinks=False)
                # yields their names here but never descends into them, so any
                # files beneath a symlinked dir would be silently omitted from
                # the push. Drop them from the walk (keeping os.walk's no-follow
                # contract honest) and record each as a skipped symlink named by
                # its own path — same posture as a symlinked file. Prune protected
                # subtrees (.git/.dvc) from the walk entirely — enumerating git's
                # object store is pointless and would refuse thousands of files.
                kept: list[str] = []
                for d in sorted(dirnames):
                    child = Path(dirpath) / d
                    if child.is_symlink():
                        symlinks.append(_rel_of(child))
                    elif _is_protected_repo_path(_rel_of(child)):
                        continue
                    else:
                        kept.append(d)
                dirnames[:] = kept
                for name in sorted(filenames):
                    fp = Path(dirpath) / name
                    if fp.is_symlink():
                        symlinks.append(_rel_of(fp))
                        continue
                    files_here += 1
                    _add_file(fp)
            if files_here == 0 and len(refused) == before_refused:
                # Zero regular files AND zero refusals from this dir — a genuinely
                # empty (or all-symlink) arg. Counting files_here (not items
                # growth) means a redundant/overlapping arg whose files were all
                # already enumerated by a SIBLING arg (and so deduped by ``seen``)
                # is NOT falsely flagged empty — which would abort the whole push.
                # If it produced refusals, those already speak; don't also flag it.
                empty_args.append(arg)
        elif abs_path.is_file():
            rel = _repo_rel(abs_path)
            if rel is None:
                refused.append(_Refused(arg, "outside_project"))
            else:
                file_reason = _classify(rel)
                if file_reason is not None:
                    refused.append(_Refused(rel, file_reason))
                else:
                    _add_file(abs_path)
        else:
            empty_args.append(arg)

    return _PushScan(
        items=items, symlinks=symlinks, refused=refused, empty_args=empty_args
    )


# ---------------------------------------------------------------------------
# §D — push orchestrator
# ---------------------------------------------------------------------------


def _push_local_error(rel: str, exc: OSError) -> TransferOutcome:
    """A local-filesystem error (a file removed / made unreadable between
    enumeration and hashing — a real TOCTOU when a producer is still writing
    into ``cache/``) resolves to a per-file ``failed`` ledger entry, never a
    raw traceback out of the executor (the 'no traceback on documented failure
    paths' norm; the rest of the push still completes)."""
    return TransferOutcome(
        rel=rel,
        status="failed",
        reason=f"local read error: {exc}",
        hint=f"the file could not be read (moved/deleted/permission mid-push) — "
        f"retry just this file: mintd cache push {rel}",
    )


def _push_one(
    item: _PushItem,
    *,
    s3: Any,
    bucket: str,
    prefix: str,
    remote_size: int | None,
    advance: Callable[[int], None],
    dry_run: bool,
) -> TransferOutcome:
    """Self-contained per-file task: size-precheck (from the shared LIST),
    HEAD+SHA only for size-matching candidates, then upload or skip. No shared
    mutable state; the ``remote_size`` argument is read from the read-only LIST
    map by the caller."""
    if item.read_error is not None:
        # Enumeration could not stat this file (removed/locked mid-push). Resolve
        # it here — before any S3 call — to the same per-file failed ledger entry
        # a hashing/upload TOCTOU produces, so the rest of the push completes and
        # the command exits 1 with a hinted per-file error (never a traceback).
        return _push_local_error(item.rel, item.read_error)
    full_key = push_key(prefix, item.rel)
    local_sha: str | None = None
    remote_sha: str | None = None
    if remote_size is not None and remote_size == item.size:
        try:
            local_sha = file_sha256(item.abs_path)
        except OSError as exc:
            return _push_local_error(item.rel, exc)
        try:
            info = head_remote_object(s3, bucket, full_key)
            remote_sha = info.metadata.get(CACHE_SHA256_META_KEY)
        except RemoteObjectNotFound:
            remote_size = None  # object vanished between LIST and HEAD -> upload
        except TransferError:
            remote_sha = None  # HEAD failed after retries -> upload anyway (loud)
    decision = decide_push(
        local_size=item.size,
        remote_size=remote_size,
        local_sha256=local_sha,
        remote_sha256=remote_sha,
    )
    if decision == "skip":
        advance(item.size)
        return TransferOutcome(rel=item.rel, status="unchanged", bytes=item.size)
    if dry_run:
        return TransferOutcome(rel=item.rel, status="uploaded", bytes=item.size)
    if local_sha is None:
        try:
            local_sha = file_sha256(item.abs_path)
        except OSError as exc:
            return _push_local_error(item.rel, exc)
    extra_args: dict[str, Any] = {
        "Tagging": CACHE_LANE_TAGGING,
        "Metadata": {CACHE_SHA256_META_KEY: local_sha},
    }
    try:
        n = upload_object(
            s3, bucket, full_key, item.abs_path, progress=advance, extra_args=extra_args
        )
    except TransferError as exc:
        retry = f"retry just this file: mintd cache push {item.rel}"
        hint = "\n".join(h for h in (exc.hint, retry) if h)
        return TransferOutcome(
            rel=item.rel, status="failed", reason=str(exc), hint=hint
        )
    except OSError as exc:
        # The producer-TOCTOU window is not fully closed by the file_sha256
        # guards above: boto3's upload_file re-opens/re-stats the local path
        # itself, so a producer that deletes/rotates the file AFTER we hashed it
        # but BEFORE the transfer opens it raises FileNotFoundError (an OSError).
        # upload_object does not map OSError, so without this it would escape the
        # executor as a raw traceback out of cli.main(). Symmetric with the pull
        # leg, which already guards download_object with except OSError.
        return _push_local_error(item.rel, exc)
    return TransferOutcome(rel=item.rel, status="uploaded", bytes=n)


# Per-reason error copy, reported in this priority order (first non-empty wins,
# but every path in that category is named — all-or-nothing, never partial).
_REFUSAL_COPY: dict[RefusalReason, tuple[str, str]] = {
    "dvc_tracked": (
        "these path(s) are tracked by DVC — the repo file cache (S3) is for "
        "untracked scratch files only",
        "publish versioned outputs with `mintd data push` (or share a one-off "
        "with `mintd share put`)",
    ),
    "protected": (
        "these path(s) are under .git/ or .dvc/ — mintd never caches git or DVC "
        "internals",
        "these are managed by git/DVC; cache only your own working files",
    ),
    "outside_project": (
        "these path(s) are outside the project root — only working-tree files "
        "can be cached",
        "pass paths inside the project, or use `mintd share put` for an "
        "arbitrary file",
    ),
    "forbidden": (
        "these path(s) contain characters not allowed in a cache key (a "
        "backslash or a control character)",
        "rename the file(s) — a cache key must be a clean forward-slash path",
    ),
    "project_root": (
        "refusing to cache the entire project root",
        "name the specific files or directories you want cached",
    ),
}


def _raise_on_refusals(scan: _PushScan) -> None:
    """Raise a hinted ``CacheError`` for the highest-priority refusal category
    present (naming every path in it). Priority follows ``_REFUSAL_COPY``'s
    insertion order so the most likely intent — a DVC-tracked path — is surfaced
    first."""
    for reason, (msg, hint) in _REFUSAL_COPY.items():
        paths = scan.refused_by(reason)
        if paths:
            raise CacheError(f"{msg}: {', '.join(sorted(set(paths)))}", hint=hint)


# Live-bar labels: the static ``desc=`` phrase (kept as a literal for the naming
# discipline) plus a ``done/total files`` counter that ticks as each concurrent
# transfer completes. A per-file NAME would flicker across the <=jobs files in
# flight at once, so a completion counter is the honest signal under concurrency.
def _push_label(done: int, total: int) -> str:
    return f"Pushing to the repo file cache (S3) · {done}/{total} file(s)"


def _pull_label(done: int, total: int) -> str:
    return f"Pulling from the repo file cache (S3) · {done}/{total} file(s)"


def cache_push(
    *,
    project_path: Path,
    paths: list[str],
    config: Config,
    reporter: Reporter,
    remote: str | None = None,
    jobs: int = 8,
    dry_run: bool = False,
    s3_client_factory: Factory | None = None,
) -> CachePushSummary:
    """Upload every named repo file to ``<prefix>/cache/<repo-relative-path>``.
    Any working-tree file may be cached except a DVC-tracked out, a ``.git/`` /
    ``.dvc/`` internal, or a forbidden-set-unclean path — all refused loudly,
    all-or-nothing. One paginated LIST precheck, HEADs only for size-matching
    candidates, concurrent uploads through S1's transport."""
    factory = s3_client_factory or _create_s3_client
    jobs = jobs or 8
    repo = resolve_repo_remote(project_path, remote)
    guard_no_dvc_outs_under_cache(project_path, repo.remote_name)
    tracked = dvc_tracked_paths(project_path, repo.remote_name)

    scan = enumerate_push_items(project_path, paths, tracked)
    _raise_on_refusals(scan)
    for s in scan.symlinks:
        reporter.warn(f"skipped symlink (not followed): {s}")
    if scan.empty_args:
        # A path that matched no files — a typo/nonexistent arg or an empty
        # directory — must fail the whole push, never be silently dropped just
        # because a SIBLING arg yielded files (which would let CI mask
        # un-pushed data). Mirrors the refusals above.
        raise CacheError(
            "no files under: " + ", ".join(scan.empty_args),
            hint="check the path(s) — each must name an existing file or a "
            "non-empty directory in the working tree",
        )
    items = scan.items
    if not items:
        raise CacheError(
            "no files to push under: " + ", ".join(paths),
            hint="pass files or directories in the working tree (not DVC-tracked)",
        )

    s3 = factory(repo.remote_cfg, config.aws_profile_name)
    listing = _list_or_cache_error(
        repo, sub_path=None, aws_profile_name=config.aws_profile_name, factory=factory
    )
    remote_by_remainder = {
        o.key: o.size
        for o in listing.objects
        if not o.is_dir and not o.key.endswith("/")
    }

    def _remote_size(item: _PushItem) -> int | None:
        # An object's list-remainder under <prefix>/cache IS the file's full
        # repo-relative path, so the precheck map keys directly on item.rel.
        return remote_by_remainder.get(item.rel)

    symlink_outcomes = [
        TransferOutcome(rel=s, status="skipped_symlink") for s in scan.symlinks
    ]
    start = time.monotonic()
    outcomes: list[TransferOutcome] = []
    if dry_run:
        for item in items:
            outcomes.append(
                _push_one(
                    item, s3=s3, bucket=repo.bucket, prefix=repo.prefix,
                    remote_size=_remote_size(item), advance=lambda _n: None,
                    dry_run=True,
                )
            )
    else:
        total_bytes = sum(i.size for i in items)
        n_files = len(items)
        with reporter.progress(
            total=total_bytes, desc="Pushing to the repo file cache (S3)"
        ) as advance:
            advance.set_description(_push_label(0, n_files))
            with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
                futures = [
                    ex.submit(
                        _push_one, item, s3=s3, bucket=repo.bucket,
                        prefix=repo.prefix, remote_size=_remote_size(item),
                        advance=advance, dry_run=False,
                    )
                    for item in items
                ]
                done = 0
                for fut in as_completed(futures):
                    outcomes.append(fut.result())
                    done += 1
                    advance.set_description(_push_label(done, n_files))
    outcomes.extend(symlink_outcomes)

    summary = CachePushSummary(
        outcomes=outcomes,
        key_prefix=cache_list_prefix(repo.prefix),
        bucket=repo.bucket,
        elapsed_s=time.monotonic() - start,
        dry_run=dry_run,
    )
    if not dry_run and summary.uploaded:
        _maybe_warn_lifecycle(s3, repo.bucket, reporter)
    return summary


# ---------------------------------------------------------------------------
# §E — pull orchestrator (the listing is untrusted input)
# ---------------------------------------------------------------------------


def _make_sha_verifier(expected_hex: str) -> Callable[[Path], None]:
    """Pre-replace verify hook for ``download_object``: full-file SHA256 must
    equal the object's ``mintd-sha256`` metadata. Covers multipart objects
    whose native ``ChecksumSHA256`` is a composite of parts. Raising a
    ``TransferError`` unlinks the tmp and is non-transient (no wasted retry)."""

    def _verify(tmp: Path) -> None:
        actual = file_sha256(tmp)
        if actual != expected_hex:
            raise TransferError(
                f"sha256 mismatch: expected {expected_hex}, got {actual}",
                hint="the repo file cache (S3) object may be corrupt — retry, "
                "or re-push it from the producer",
            )

    return _verify


def _pull_local_error(rel: str, exc: OSError) -> TransferOutcome:
    """A local-filesystem error while reading a would-be-skipped file or writing
    the download (e.g. a cache/ path component that already exists as a plain
    file) resolves to a per-file ``failed`` ledger entry — the rest of the pull
    still completes and the command exits 1 with a clean error, never a raw
    traceback out of the executor."""
    return TransferOutcome(
        rel=rel,
        status="failed",
        reason=f"local filesystem error: {exc}",
        hint="a working-tree path could not be read or written (a path component "
        "may be a file, not a directory, or is not writable) — retry: mintd cache pull",
    )


def _pull_one(
    obj_key: str,
    full_key: str,
    dest: Path,
    rel: str,
    obj_size: int,
    *,
    s3: Any,
    bucket: str,
    advance: Callable[[int], None],
    force: bool,
) -> TransferOutcome:
    try:
        info = head_remote_object(s3, bucket, full_key)
    except TransferError as exc:
        return TransferOutcome(rel=rel, status="failed", reason=str(exc), hint=exc.hint)
    remote_sha = info.metadata.get(CACHE_SHA256_META_KEY)
    try:
        local_exists = dest.exists()
        local_size = dest.stat().st_size if local_exists else None
        local_sha: str | None = None
        if local_exists and local_size == info.size and remote_sha:
            local_sha = file_sha256(dest)
    except OSError as exc:
        return _pull_local_error(rel, exc)
    decision = decide_pull(
        local_exists=local_exists,
        local_size=local_size,
        remote_size=info.size,
        local_sha256=local_sha,
        remote_sha256=remote_sha,
    )
    if decision == "skip":
        advance(info.size)
        return TransferOutcome(rel=rel, status="unchanged", bytes=info.size)
    if local_exists and not force:
        # The object would OVERWRITE an existing working-tree file with
        # different (or unverifiable) content. Under the repo-relative-path
        # model a pull can land anywhere in the tree, so we refuse to clobber a
        # user's own file silently — skip-and-warn, retryable with --force.
        advance(info.size)
        return TransferOutcome(
            rel=rel, status="skipped_existing", bytes=info.size,
            reason="a local file with different content already exists",
            hint=f"keep it, or overwrite from the repo file cache (S3): "
            f"mintd cache pull --force (or delete {rel})",
        )
    verify = _make_sha_verifier(remote_sha) if remote_sha else None
    try:
        n = download_object(
            s3, bucket, full_key, dest, progress=advance,
            verify_tmp=verify, expected_size=info.size,
            # dest is a user-controlled path in the working tree — a predictable
            # <name>.tmp would clobber a user's own scratch file of that name and
            # race a sibling task pulling a key literally named "<name>.tmp"
            # (whose FINAL dest would equal this tmp). A per-download uuid token
            # makes the tmp path collision-proof.
            tmp_suffix=f".{uuid4().hex}.mintd-tmp",
        )
    except TransferError as exc:
        retry = "retry: mintd cache pull"
        hint = "\n".join(h for h in (exc.hint, retry) if h)
        return TransferOutcome(rel=rel, status="failed", reason=str(exc), hint=hint)
    except OSError as exc:
        # A local-filesystem error (e.g. an intermediate cache/ path component
        # exists as a plain FILE, so download_object's tmp.parent.mkdir raises
        # FileExistsError/NotADirectoryError before any transport error mapping)
        # resolves to a per-file failed entry, never a raw traceback.
        return _pull_local_error(rel, exc)
    return TransferOutcome(rel=rel, status="downloaded", bytes=n)


def cache_pull(
    *,
    project_path: Path,
    config: Config,
    reporter: Reporter,
    prefix: str | None = None,
    remote: str | None = None,
    jobs: int = 8,
    force: bool = False,
    s3_client_factory: Factory | None = None,
) -> CachePullSummary:
    """Download objects under ``<prefix>/cache[/<k>]`` to their repo-relative
    paths in the working tree, atomically and verified. The server-returned
    listing is untrusted: every remainder passes ``safe_cache_remainder``, a
    filesystem containment check against the PROJECT ROOT, and a ``.git/`` /
    ``.dvc/`` and DVC-tracked screen before any write. An object whose path
    already exists locally with different content is skipped unless ``force``."""
    factory = s3_client_factory or _create_s3_client
    jobs = jobs or 8
    repo = resolve_repo_remote(project_path, remote)
    guard_no_dvc_outs_under_cache(project_path, repo.remote_name)
    tracked = dvc_tracked_paths(project_path, repo.remote_name)

    sub = _normalise_sub_path(prefix)  # "isochrones/ct/" or ""
    listing = _list_or_cache_error(
        repo, sub_path=prefix, aws_profile_name=config.aws_profile_name, factory=factory
    )
    objects = [
        o for o in listing.objects if not o.is_dir and not o.key.endswith("/")
    ]
    if not objects:
        reporter.warn(
            f"no objects in the repo file cache (S3) under "
            f"{cache_list_prefix(repo.prefix)}/{sub} — nothing pulled "
            f"(list with: mintd cache ls)"
        )
        return CachePullSummary(outcomes=[], sub=sub, elapsed_s=0.0)

    if not prefix:
        from .cli import _human_bytes

        total = sum(o.size for o in objects)
        reporter.info(
            f"pulling {len(objects)} file(s) ({_human_bytes(total)}) "
            f"from the repo file cache (S3)"
        )

    project_root = project_path.resolve()
    safe: list[tuple[str, str, Path, str, int]] = []
    unsafe_outcomes: list[TransferOutcome] = []
    for o in objects:
        remainder = sub + o.key  # the object's FULL repo-relative path
        full_key = f"{cache_list_prefix(repo.prefix)}/{remainder}"
        dest = project_path / remainder
        # Both rel and reason are echoed to stderr by _render_cache_pull; a
        # rejected remainder may carry embedded control chars (newline/ESC), so
        # neutralize before interpolating so a hostile key cannot forge status
        # lines or inject ANSI.
        refusal: str | None = None
        # ``resolved_rel`` is the repo-relative path AFTER symlink resolution —
        # the write target the OS will actually touch. Screen BOTH it and the raw
        # remainder: the raw catches the common case cheaply, ``resolved_rel``
        # closes a symlinked intermediate dir (``linkdir -> .git`` + a planted
        # ``linkdir/config`` key). The screens are case-insensitive, so this holds
        # even where ``resolve()`` does not canonicalize case (macOS).
        resolved_rel: str | None = None
        try:
            safe_cache_remainder(remainder)
            resolved_rel = dest.resolve().relative_to(project_root).as_posix()
        except (CacheKeyError, ValueError):
            refusal = f"unsafe key refused (escapes the project root): {_display_safe(full_key)}"
        screen_paths = [remainder] + ([resolved_rel] if resolved_rel is not None else [])
        if refusal is None and any(_is_protected_repo_path(p) for p in screen_paths):
            refusal = (
                f"refused: object maps under .git/ or .dvc/ "
                f"({_display_safe(remainder)}) — never restored by the cache"
            )
        if refusal is None and any(_is_tracked(p, tracked) for p in screen_paths):
            refusal = (
                f"refused: {_display_safe(remainder)} is DVC-tracked — restore it "
                f"with `mintd data pull`, not the cache"
            )
        if refusal is not None:
            unsafe_outcomes.append(
                TransferOutcome(
                    rel=_display_safe(remainder), status="failed", reason=refusal,
                    hint="a corrupted or hostile namespace is an admin conversation "
                    "— nothing was written",
                )
            )
            continue
        safe.append((o.key, full_key, dest, remainder, o.size))

    s3 = factory(repo.remote_cfg, config.aws_profile_name)
    start = time.monotonic()
    outcomes: list[TransferOutcome] = list(unsafe_outcomes)
    total_bytes = sum(size for (_k, _fk, _d, _r, size) in safe)
    n_files = len(safe)
    with reporter.progress(
        total=total_bytes, desc="Pulling from the repo file cache (S3)"
    ) as advance:
        # The byte-bar can't show file-count progress on its own; append a
        # completed-files counter that ticks as each concurrent download
        # finishes (a per-file name would flicker meaninglessly across the
        # <=jobs files in flight at once).
        advance.set_description(_pull_label(0, n_files))
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            futures = [
                ex.submit(
                    _pull_one, obj_key, full_key, dest, remainder, size,
                    s3=s3, bucket=repo.bucket, advance=advance, force=force,
                )
                for (obj_key, full_key, dest, remainder, size) in safe
            ]
            done = 0
            for fut in as_completed(futures):
                outcomes.append(fut.result())
                done += 1
                advance.set_description(_pull_label(done, n_files))
    return CachePullSummary(
        outcomes=outcomes, sub=sub, elapsed_s=time.monotonic() - start
    )


# ---------------------------------------------------------------------------
# §G — durability / lifecycle (mintd tags + reads; NEVER writes)
# ---------------------------------------------------------------------------


def lifecycle_covers_cache_tag(rules: list[dict]) -> bool:
    """Pure predicate: does any Enabled rule with ``NoncurrentVersionExpiration``
    filter on tag ``mintd-lane=cache`` (via ``Filter.Tag`` or
    ``Filter.And.Tags[]``)? Reusable by share S3 for its prefix-rule read."""
    for rule in rules:
        if rule.get("Status") != "Enabled":
            continue
        if "NoncurrentVersionExpiration" not in rule:
            continue
        filt = rule.get("Filter") or {}
        tags: list[dict] = []
        if "Tag" in filt:
            tags.append(filt["Tag"])
        tags.extend((filt.get("And") or {}).get("Tags") or [])
        for tag in tags:
            if (
                tag.get("Key") == CACHE_LANE_TAG_KEY
                and tag.get("Value") == CACHE_LANE_TAG_VALUE
            ):
                return True
    return False


def _maybe_warn_lifecycle(s3: Any, bucket: str, reporter: Reporter) -> None:
    """Best-effort, read-only durability check after a push. Never gates the
    operation; mintd NEVER calls ``PutBucketLifecycleConfiguration``."""
    try:
        versioned = check_bucket_versioning(s3, bucket)
    except Exception:
        versioned = True  # conservative — a failed probe still warns
    if not versioned:
        return  # unversioned bucket => no noncurrent bill => no warn
    try:
        resp = retry_transient(
            lambda: s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        )
        rules = resp.get("Rules", [])
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code == "NoSuchLifecycleConfiguration":
            rules = []
        elif code in ("AccessDenied", "403"):
            reporter.warn(
                "could not read the bucket lifecycle configuration for the "
                "repo file cache (S3) (AccessDenied) — ask an admin to confirm a "
                "NoncurrentVersionExpiration rule on tag mintd-lane=cache"
            )
            return
        else:
            return  # unknown error must never fail the push
    except Exception:
        # A network-layer error (EndpointConnectionError/ReadTimeoutError/…)
        # that survives retry_transient's budget is NOT a ClientError; on this
        # read-only, post-push, best-effort probe it must never fail an
        # already-successful push (§G: never gates the operation). Mirrors the
        # `except Exception` on the versioning probe above.
        return
    if not lifecycle_covers_cache_tag(rules):
        reporter.warn(
            "noncurrent versions in the repo file cache (S3) never expire — "
            "overwrites bill forever (admin: add a NoncurrentVersionExpiration "
            "rule on tag mintd-lane=cache)"
        )
