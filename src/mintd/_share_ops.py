"""``mintd share`` — ephemeral handoff transport + policy.

This module is deliberately split into two strata:

**Stratum T (transport — the future ``mintd cache`` core).** The three verb-
agnostic primitives ``head_remote_object`` / ``upload_object`` /
``download_object`` (plus ``file_sha256`` and the ``TransferError`` family)
take ``(s3, bucket, key, path, progress, extra_args)`` and MAY NOT reference
``Config``, ``Reporter``, ``SHARE_PREFIX``, or the ref grammar. They *compose*
the existing shared seams (``retry_transient`` / ``is_transient_s3_error``,
``_try_fsync_file`` / ``_try_fsync_parent_dir``) rather than paraphrase them.
``mintd cache`` imports them unchanged; when cache lands, Stratum T moves
verbatim to ``_transfer_ops.py`` (rename-only, shipped with its second
consumer — no speculative empty module now, per the defer-cross-cutting-
refactors norm). Integrity is a transport invariant: upload always sends
``ChecksumAlgorithm="SHA256"``, download always sends ``ChecksumMode="ENABLED"``,
and ``extra_args`` (cache's ``Tagging`` / ``Metadata`` seam) raises if it tries
to override the reserved checksum key.

**Stratum P (share policy).** The ``SHARE_PREFIX`` constant, the pure
``resolve_share_user`` / ``parse_share_ref`` / ``build_put_key`` helpers, and
the ``share_put`` / ``share_get`` orchestrators own ``Config`` / ``Reporter``
and raise ``ShareError(msg, hint)``.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

# Reuse the guarded-import sentinels from _fast_sync_ops rather than
# paraphrasing its try/except: these are the *same* class objects that
# is_transient_s3_error / retry_transient classify against, so behaviour
# cannot drift. When boto3 is absent both are inert placeholders whose
# ``except`` clauses never fire (see _fast_sync_ops:37-48).
from mintd._atomic import _try_fsync_file, _try_fsync_parent_dir
from mintd._fast_sync_ops import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
    SSLError,
    _create_s3_client,
    retry_transient,
)
from mintd._s3_listing_ops import _normalise_sub_path
from mintd._storage_state import SLUG_REGEX

# The botocore network-layer exceptions retry_transient itself classifies
# transient (is_transient_s3_error): they survive a full retry budget and,
# without an explicit clause, would escape Stratum T as a raw traceback.
_TRANSFER_NETWORK_ERRORS = (
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectionClosedError,
    SSLError,
)

# ChecksumMode="ENABLED" makes botocore validate the response-body SHA256;
# a genuine mismatch raises FlexibleChecksumError (a BotoCoreError, NOT a
# ClientError) — so it needs its own mapping or it tracebacks. Guarded like
# the sentinels above so boto3-absent installs stay importable.
try:
    from botocore.exceptions import FlexibleChecksumError
except ImportError:  # boto3 absent — inert placeholder; the except clause never fires

    class FlexibleChecksumError(Exception):  # type: ignore[no-redef]
        """Placeholder when botocore is absent (mirrors _fast_sync_ops:37-48)."""

# boto3's high-level ``upload_file`` catches every botocore ``ClientError`` from
# the transfer and re-raises it wrapped in ``S3UploadFailedError`` (which is NOT
# a ``ClientError``; boto3/s3/transfer.py:456-459). We import it via the same
# guarded pattern as the botocore sentinels above so ``upload_object`` can unwrap
# it back to the underlying ``ClientError`` — otherwise real upload failures
# (bad bucket, AccessDenied, SlowDown) would escape unmapped and un-retried.
try:
    from boto3.exceptions import S3UploadFailedError
except ImportError:  # boto3 absent — inert placeholder; the except clause never fires

    class S3UploadFailedError(Exception):  # type: ignore[no-redef]
        """Placeholder when boto3 is absent (mirrors _fast_sync_ops:37-48)."""


# s3transfer's download runs its OWN retry loop over the response-body stream
# and, once exhausted, boto3 re-raises boto3.exceptions.RetriesExceededError
# (a Boto3Error carrying .last_exception — NOT a ClientError/BotoCoreError, so
# neither is_transient_s3_error nor the network tuple sees it). Without this it
# would escape as a raw traceback on a real large-file download whose stream
# read-times-out mid-transfer.
try:
    from boto3.exceptions import RetriesExceededError
except ImportError:  # boto3 absent — inert placeholder; the except clause never fires

    class RetriesExceededError(Exception):  # type: ignore[no-redef]
        """Placeholder when boto3 is absent (mirrors _fast_sync_ops:37-48)."""

if TYPE_CHECKING:
    from mintd._config import Config
    from mintd._console import Reporter


# ---------------------------------------------------------------------------
# Stratum T — transport (verb-agnostic, cache-ready)
# ---------------------------------------------------------------------------


class TransferError(Exception):
    """A transfer failed. Carries a CLI-ready ``hint`` (may be ``None``)."""

    def __init__(self, msg: str, *, hint: str | None = None) -> None:
        super().__init__(msg)
        self.hint = hint


class RemoteObjectNotFound(TransferError):
    """404 / ``NoSuchKey`` — typed so policy layers (``share_get``, later
    ``mintd cache``) can re-word it with their own grammar (R5). The message
    names the *key* only; it carries no hint."""


@dataclass(frozen=True)
class RemoteObjectInfo:
    size: int
    checksum_sha256: str | None  # None => object was written without SHA256
    # User metadata (``x-amz-meta-*``), keys lowercased and de-prefixed by
    # botocore (so ``x-amz-meta-mintd-sha256`` arrives as ``mintd-sha256``).
    # ``mintd cache`` reads ``metadata.get("mintd-sha256")`` for its skip
    # compare and pre-replace verify; share ignores it. Additive + default-
    # empty so no existing caller/construction breaks.
    metadata: Mapping[str, str] = field(default_factory=dict)


_RESERVED_UPLOAD_ARGS = frozenset({"ChecksumAlgorithm"})


def _credentials_error(exc: BaseException) -> TransferError:
    # NoCredentialsError is not a ClientError, so _map_client_error never
    # sees it and retry_transient classifies it non-transient (re-raised on
    # the first attempt). Without this wrap it would escape main() as a raw
    # traceback (main catches only KeyboardInterrupt / WallTimeout /
    # ConfigError) — the precedent is is_transient_s3_error:293-296.
    return TransferError(
        f"AWS credentials unavailable (not retried): {exc}",
        hint="check AWS credentials: mintd config validate",
    )


def _map_transport_error(exc: BaseException, key: str) -> TransferError:
    """Map a KNOWN transport-layer error to a typed, hinted ``TransferError``
    so no DOCUMENTED failure path reaches ``main()`` as a raw traceback (the
    house 'no traceback on documented paths' norm). One helper, used by all
    three transport functions, so the mapping cannot drift between them.

    Called only with the error families in ``_MAPPED_TRANSPORT_ERRORS`` — a
    ``verify_tmp`` policy failure (unless it is itself a ``TransferError``) and
    any genuinely-unexpected exception are deliberately NOT caught at the call
    sites, so they propagate verbatim (R2: the policy layer owns its error; a
    real bug should surface loudly, not be masked as 'transfer failed')."""
    if isinstance(exc, RetriesExceededError):
        # s3transfer exhausted its own stream-retry loop. Unwrap to the real
        # cause so a network/credentials/client exhaustion gets its precise
        # hint; fall back to a generic transfer error otherwise.
        cause = getattr(exc, "last_exception", None)
        if cause is not None and cause is not exc:
            return _map_transport_error(cause, key)
        return TransferError(
            f"transfer failed for {key}: {exc}",
            hint="check network connectivity / AWS credentials: mintd config validate",
        )
    if isinstance(exc, TransferError):  # incl. RemoteObjectNotFound + TransferError-raising verify_tmp
        return exc
    if isinstance(exc, NoCredentialsError):
        return _credentials_error(exc)
    if isinstance(exc, ClientError):
        return _map_client_error(exc, key)
    if isinstance(exc, FlexibleChecksumError):
        return TransferError(
            f"checksum mismatch for {key}: {exc}",
            hint="retry the transfer; if it persists the object may be corrupted at the source",
        )
    # Only _TRANSFER_NETWORK_ERRORS remain (per the caught tuple).
    return TransferError(
        f"transfer failed for {key}: {exc}",
        hint="check network connectivity / AWS credentials: mintd config validate",
    )


# The transport-error families the three functions map to a hinted
# TransferError. NOT caught (propagate verbatim): a verify_tmp policy error
# that is not a TransferError, and any unexpected exception (a real bug).
_MAPPED_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    TransferError,
    NoCredentialsError,
    ClientError,
    FlexibleChecksumError,
    RetriesExceededError,
    *_TRANSFER_NETWORK_ERRORS,
)


def _map_client_error(exc: Any, key: str) -> TransferError:
    """Map a botocore ``ClientError`` to a hinted ``TransferError``.

    404 / ``NoSuchKey`` becomes the typed ``RemoteObjectNotFound`` (no hint;
    the policy layer supplies ref-based wording per R5)."""
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    # NoSuchBucket must be checked BEFORE the 404 branch: a real NoSuchBucket
    # response also carries HTTPStatusCode 404, so a status-first order would
    # mis-map a misconfigured bucket to RemoteObjectNotFound ("bad ref") and
    # bury the storage_bucket_prefix hint the user actually needs.
    if code == "NoSuchBucket":
        return TransferError(
            f"bucket not found for {key}: {exc}",
            hint="check storage_bucket_prefix (mintd config setup)",
        )
    if code in ("404", "NoSuchKey") or status == 404:
        return RemoteObjectNotFound(f"no object at {key}")
    if code in ("AccessDenied", "403") or status == 403:
        return TransferError(
            f"access denied for {key}: {exc}",
            hint="check AWS credentials: mintd config validate",
        )
    return TransferError(f"S3 error for {key}: {exc}")


def head_remote_object(s3: Any, bucket: str, key: str) -> RemoteObjectInfo:
    """HEAD an object, returning its size and stored SHA256 (if any).

    ``ChecksumSHA256`` present ⇔ the object was checksummed on write
    (multipart composites like ``"...-N"`` count as present). Missing key
    surfaces as the typed ``RemoteObjectNotFound``."""
    try:
        resp = retry_transient(
            lambda: s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        )
    except _MAPPED_TRANSPORT_ERRORS as exc:
        raise _map_transport_error(exc, key) from exc
    return RemoteObjectInfo(
        size=int(resp["ContentLength"]),
        checksum_sha256=resp.get("ChecksumSHA256"),
        metadata=resp.get("Metadata", {}),
    )


def upload_object(
    s3: Any,
    bucket: str,
    key: str,
    local_path: Path,
    *,
    progress: Callable[[int], None],
    extra_args: Mapping[str, Any] | None = None,
) -> int:
    """Upload ``local_path`` to ``bucket/key`` with a baked-in SHA256 checksum.

    ``extra_args`` (cache's ``Tagging`` / ``Metadata`` seam) is shallow-merged
    *over* the checksum base, but may not override ``ChecksumAlgorithm`` — the
    guard that makes "integrity is baked in" true rather than aspirational.
    Returns the uploaded byte count."""
    if extra_args:
        # Case-fold + strip so miscased/padded override attempts
        # ("checksumalgorithm", "ChecksumAlgorithm ") raise the typed guard
        # here rather than slipping through to an unmapped boto3 ValueError.
        reserved_cf = {k.casefold() for k in _RESERVED_UPLOAD_ARGS}
        overlap = sorted(k for k in extra_args if k.strip().casefold() in reserved_cf)
        if overlap:
            raise ValueError(
                f"extra_args may not override reserved upload arg(s): {overlap}"
            )
    merged: dict[str, Any] = {"ChecksumAlgorithm": "SHA256", **(extra_args or {})}

    def _attempt() -> None:
        try:
            s3.upload_file(
                str(local_path), bucket, key, ExtraArgs=merged, Callback=progress
            )
        except S3UploadFailedError as exc:
            # boto3 wraps every transfer-time ClientError in S3UploadFailedError
            # (not a ClientError; boto3/s3/transfer.py:456-459). Unwrap the
            # underlying ClientError — set as __context__ by boto3's bare
            # ``raise`` inside its except block — and re-raise it so both the
            # shared retry policy (SlowDown etc.) and the hint mapping below see
            # a ClientError, exactly as head/download do.
            cause = exc.__context__
            if isinstance(cause, ClientError):
                raise cause from exc
            raise TransferError(f"upload failed for {key}: {exc}") from exc

    try:
        retry_transient(_attempt)
    except _MAPPED_TRANSPORT_ERRORS as exc:
        raise _map_transport_error(exc, key) from exc
    # Prefer the pre-transfer size (share_put already stat()'d and the caller
    # passes it) is not available here, so re-stat defensively: a file that
    # vanished mid-upload would already have raised inside _attempt and been
    # mapped above; this stat only runs on success.
    try:
        return local_path.stat().st_size
    except OSError as exc:
        raise TransferError(
            f"local file vanished after uploading {key}: {exc}",
            hint="the upload succeeded but the local file is no longer readable",
        ) from exc


def download_object(
    s3: Any,
    bucket: str,
    key: str,
    dest: Path,
    *,
    progress: Callable[[int], None],
    verify_tmp: Callable[[Path], None] | None = None,
    expected_size: int | None = None,
    tmp_suffix: str | None = None,
) -> int:
    """Download ``bucket/key`` to ``dest`` atomically.

    Borrows ``fetch_to_cache``'s tmp→verify→fsync→replace discipline
    (_fast_sync_ops:1115-1147) with three grounded deviations:

    1. tmp name is ``dest.name + tmp_suffix`` (default ``".tmp"``, the
       ``write_manifest`` precedent at _fast_sync_ops:1081), NOT
       ``with_suffix(".tmp")`` — the latter would collapse user-named
       ``report.parquet`` / ``report.csv`` into one ``report.tmp``. The default
       suffix is safe for share (single file, dest namespace is share-owned and
       an existing dest is refused). Consumers whose dest namespace is
       *user-controlled and concurrent* — e.g. ``mintd cache pull`` mapping
       server keys straight into the working tree — MUST pass a collision-proof
       ``tmp_suffix`` (a per-call ``uuid4`` token) so the predictable ``.tmp``
       path can never (a) clobber a user's own ``<name>.tmp`` scratch file nor
       (b) equal a sibling task's *final* dest (pulling both ``foo`` and
       ``foo.tmp`` would otherwise race: ``foo``'s tmp IS ``foo.tmp``);
    2. no md5-pin verify — share has no pin; integrity rides
       ``ChecksumMode="ENABLED"`` (botocore verifies the stored SHA256 on the
       response, raising on mismatch). ``verify_tmp`` is the policy-free
       pre-``replace`` hook cache uses for its metadata-SHA256 check (R2): if
       it raises, the tmp is unlinked and the error propagates;
    3. cleanup is ``except BaseException`` (vs fetch_to_cache's ``Exception``)
       so Ctrl-C — caught later at cli.py:186-188 → exit 130 — also leaves no
       orphan tmp.

    Returns the downloaded byte count."""
    tmp = dest.with_name(dest.name + (tmp_suffix if tmp_suffix is not None else ".tmp"))
    tmp.parent.mkdir(parents=True, exist_ok=True)

    def _attempt() -> None:
        # Remove any pre-existing tmp (incl. a planted symlink) before the
        # download: s3transfer opens the filename with a plain open('wb'),
        # which would otherwise FOLLOW a symlink at this predictable path and
        # overwrite its target. Unlinking first forces a fresh regular file.
        tmp.unlink(missing_ok=True)
        try:
            s3.download_file(
                Bucket=bucket,
                Key=key,
                Filename=str(tmp),
                ExtraArgs={"ChecksumMode": "ENABLED"},
                Callback=progress,
            )
            # Enforce the HEAD ContentLength: for a checksummed object botocore
            # already validated the SHA256, but for an un-checksummed one this
            # size check is the ONLY integrity guarantee — it makes share_get's
            # "verified by size only" warning true rather than aspirational.
            if expected_size is not None:
                actual = tmp.stat().st_size
                if actual != expected_size:
                    raise TransferError(
                        f"size mismatch for {key}: expected {expected_size}, got {actual}",
                        hint="retry the transfer; the download was truncated or the object changed",
                    )
            if verify_tmp is not None:
                verify_tmp(tmp)
            # Best-effort fsyncs (Windows-safe post-2bccf43): the replace is
            # what makes the write visible; fsync only hardens durability.
            _try_fsync_file(tmp)
            tmp.replace(dest)
            _try_fsync_parent_dir(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    try:
        retry_transient(_attempt)
    except _MAPPED_TRANSPORT_ERRORS as exc:
        raise _map_transport_error(exc, key) from exc
    return dest.stat().st_size


def file_sha256(path: Path) -> str:
    """Chunked ``hashlib.sha256`` hex digest of ``path`` (R1 — cache's
    skip-compare + ``x-amz-meta-mintd-sha256`` source; S1 uses it in tests)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Stratum P — share policy
# ---------------------------------------------------------------------------

SHARE_PREFIX = "share/"


class ShareError(Exception):
    """A share operation failed. Carries a CLI-ready ``hint`` (may be ``None``).

    Mapped in cli.py exactly like ``S3ListingError`` → ``reporter.error``."""

    def __init__(self, msg: str, *, hint: str | None = None) -> None:
        super().__init__(msg)
        self.hint = hint


@dataclass(frozen=True)
class PutResult:
    name: str
    key: str
    ref: str  # the get-ref (key minus the "share/" prefix) a teammate pastes
    bytes: int
    elapsed_s: float


@dataclass(frozen=True)
class GetResult:
    name: str
    ref: str
    dest: Path
    bytes: int
    elapsed_s: float


def _slugify_author(author: str) -> str:
    """Lowercase + collapse whitespace runs to ``-`` (``"Maurice Dalton"`` →
    ``"maurice-dalton"``). Empty/whitespace-only author → ``""``."""
    return "-".join(author.lower().split())


def resolve_share_user(config: Config) -> tuple[str, Literal["share_user", "author"]]:
    """Resolve the share identity — pure and print-free (the CLI surfaces the
    💡 nudge). Precedence: ``config.share_user`` → slugified ``config.author``;
    never AWS identity (the shared ``[mintd]`` profile is identical for every
    lab member)."""
    if config.share_user:
        # SLUG_REGEX matches "." and "..", which would build keys like
        # share/../f — reject them so no share user can ever normalise outside
        # share/<user>/ (mirrors _validate_filename).
        if not SLUG_REGEX.fullmatch(config.share_user) or config.share_user in (".", ".."):
            raise ShareError(
                f"invalid share_user {config.share_user!r}",
                hint="share_user must match [a-zA-Z0-9._-]+ (mintd config setup)",
            )
        return config.share_user, "share_user"
    if config.author:
        slug = _slugify_author(config.author)
        if slug and slug not in (".", "..") and SLUG_REGEX.fullmatch(slug):
            return slug, "author"
    raise ShareError(
        "cannot determine share user",
        hint="set share_user in ~/.config/mintd/config.yaml (mintd config setup)",
    )


def _has_control_char(value: str) -> bool:
    """True if ``value`` contains any C0 control char (incl. NUL/CR/LF/TAB)
    or DEL — none belong in an S3 key segment and several (NUL, newline)
    enable key-smuggling / log-injection."""
    return any(ch < " " or ch == "\x7f" for ch in value)


def neutralize_control_chars(value: str) -> str:
    """Escape C0 control chars and DEL for safe display, preserving Unicode.

    Unlike ``unicode_escape`` (which mangles *every* non-ASCII byte, fine only
    for already-refused hostile keys), this touches solely the bytes that can
    forge terminal rows or inject ANSI — newline, CR, ESC, NUL, other C0, DEL.
    So a legitimate accented/CJK object key renders normally in ``mintd data
    ls`` / ``cache ls``, while a planted key like ``a\\n  ✓ pulled 9999 files``
    cannot smuggle a fake status line into the listing. The common (clean) case
    short-circuits with no allocation."""
    if not _has_control_char(value):
        return value
    return "".join(
        ch if not (ch < " " or ch == "\x7f")
        else ch.encode("unicode_escape").decode("ascii")
        for ch in value
    )


def _normalise_or_share_error(sub_path: str | None) -> str:
    """``_normalise_sub_path`` (the single source of escape truth) with its
    ``ValueError`` re-raised as a hinted ``ShareError``, plus a control-char
    screen ``_normalise_sub_path`` does not do."""
    if sub_path is not None and _has_control_char(sub_path):
        raise ShareError(
            "path contains a control character",
            hint="sub-paths must be printable — no NUL, newline, or tab",
        )
    try:
        return _normalise_sub_path(sub_path)
    except ValueError as exc:
        raise ShareError(
            f"path escapes share/<user>/: {exc}",
            hint="use a relative sub-path without '..', '\\', or a leading '/'",
        ) from exc


def _validate_filename(name: str) -> str:
    """A ref/``--as`` filename must be non-empty, not ``.``/``..``, no ``\\``,
    and free of control characters (NUL/CR/LF/TAB would land in the key)."""
    if not name:
        raise ShareError(
            "share ref must name a file",
            hint="folder refs (trailing '/') arrive with `share get -r` in S5",
        )
    if name in (".", "..") or "\\" in name or _has_control_char(name):
        raise ShareError(
            f"invalid filename: {name!r}",
            hint="the ref's last segment must be a plain file name",
        )
    return name


def parse_share_ref(ref: str) -> tuple[str, str, str]:
    """Parse ``<user>/<sub…>/<filename>`` → ``(user, sub_prefix, filename)``.

    The filename is split off first (it is not prefix-shaped); the directory
    portions run through the imported ``_normalise_sub_path`` so escape rules
    cannot drift. A ref that normalises outside ``share/<user>/`` is impossible
    by construction."""
    if ref.startswith("/"):
        raise ShareError(
            f"invalid ref (leading '/'): {ref!r}",
            hint="refs are <user>/<sub>/<file>, no leading slash",
        )
    segments = ref.split("/")
    if len(segments) < 2:
        raise ShareError(
            f"{ref!r} is not a full share ref (missing the sender)",
            hint="refs are <user>/<sub>/<file> — the first segment is the sender's share_user",
        )
    user = segments[0]
    # SLUG_REGEX matches "." and "..", so reject them explicitly — otherwise a
    # ref like "../secret.parquet" would build key share/../secret.parquet,
    # falsifying the "impossible to normalise outside share/<user>/" invariant.
    if not SLUG_REGEX.fullmatch(user) or user in (".", ".."):
        raise ShareError(
            f"invalid share user in ref: {user!r}",
            hint="the first ref segment is the sender's share_user",
        )
    filename = _validate_filename(segments[-1])
    middle = "/".join(segments[1:-1])
    sub_prefix = _normalise_or_share_error(middle or None)
    return user, sub_prefix, filename


def build_put_key(user: str, local_name: str, as_value: str | None) -> str:
    """Build the ``share/<user>/…`` upload key.

    ``--as`` absent → ``share/<user>/<basename>``; ``--as`` ending in ``/`` →
    into that folder; otherwise the last ``--as`` segment renames the target."""
    base = f"{SHARE_PREFIX}{user}/"
    if as_value is None:
        return f"{base}{local_name}"
    if as_value.endswith("/"):
        return f"{base}{_normalise_or_share_error(as_value)}{local_name}"
    directory, _, name = as_value.rpartition("/")
    target = _validate_filename(name)
    return f"{base}{_normalise_or_share_error(directory or None)}{target}"


def _require_storage(config: Config) -> tuple[str, str]:
    """Both ``storage_bucket_prefix`` and ``storage_endpoint`` are required
    before any S3 call (preflight-before-bytes)."""
    if not config.storage_bucket_prefix or not config.storage_endpoint:
        raise ShareError(
            "storage is not configured",
            hint="run mintd config setup to set storage_bucket_prefix / storage_endpoint",
        )
    return config.storage_bucket_prefix, config.storage_endpoint


def _resolve_get_dest(out: str | Path | None, filename: str) -> Path:
    """``--out`` default CWD; a trailing separator (directory intent — even for a
    not-yet-existing directory) or an existing directory keeps the filename
    inside it (mirroring ``data clone`` dest handling).

    The trailing-separator test runs on the *raw string* BEFORE ``Path(...)``,
    because ``pathlib`` strips trailing separators on every platform
    (``str(Path("inbox/")) == "inbox"``). The CLI passes ``--out`` through as a
    plain ``str`` for exactly this reason: converting to ``Path`` at the parser
    would silently discard the user's ``inbox/`` intent and write a file literally
    named ``inbox``."""
    if out is None:
        return Path(filename)
    # Check both separators on the string (Windows-safe: no os-specific API).
    wants_dir = isinstance(out, str) and (out.endswith("/") or out.endswith("\\"))
    dest = Path(out)
    if wants_dir or dest.is_dir():
        return dest / filename
    return dest


def share_put(
    *,
    local_path: Path,
    user: str,
    config: Config,
    reporter: Reporter,
    as_value: str | None = None,
    s3_client_factory: Callable[[dict[str, str], str | None], Any] | None = None,
) -> PutResult:
    """Upload ``local_path`` to ``share/<user>/…``. Preflight-before-bytes:
    every local/config failure is detected before the first S3 call.

    ``s3_client_factory`` defaults to ``_create_s3_client``, resolved at call
    time (not def time) so the CLI path stays injectable for tests."""
    factory = s3_client_factory or _create_s3_client
    if not local_path.exists():
        raise ShareError(
            f"no such file: {local_path}",
            hint="check the path to the file you want to share",
        )
    if not local_path.is_file():
        raise ShareError(
            f"not a file: {local_path}",
            hint="share put takes a single file (folder support arrives in S5)",
        )
    bucket, endpoint = _require_storage(config)
    key = build_put_key(user, local_path.name, as_value)
    s3 = factory({"endpoint": endpoint}, config.aws_profile_name)
    st_size = local_path.stat().st_size
    start = time.monotonic()
    with reporter.progress(total=st_size, desc=f"Uploading {local_path.name}") as advance:
        n = upload_object(s3, bucket, key, local_path, progress=advance)
    # The get-ref is the key without the SHARE_PREFIX — exactly what a teammate
    # passes to `mintd share get`. Derived here (in the policy layer that owns
    # SHARE_PREFIX) so the CLI never reconstructs the key grammar.
    ref = key[len(SHARE_PREFIX):]
    return PutResult(
        name=local_path.name, key=key, ref=ref, bytes=n,
        elapsed_s=time.monotonic() - start,
    )


def _parse_get_ref(ref: str, config: Config) -> tuple[str, str, str]:
    """``parse_share_ref`` with one CLI-side nicety: when the ref is a bare
    filename (no ``/`` — the sender segment dropped, the most common slip,
    e.g. fetching your own file back) AND the caller has a resolvable identity,
    the error suggests ``<your-user>/<ref>``. Best-effort — ``get`` stays
    zero-identity: identity is only READ to sharpen the hint, never required."""
    try:
        return parse_share_ref(ref)
    except ShareError as exc:
        if ref and "/" not in ref and not _has_control_char(ref) and ref not in (".", ".."):
            try:
                user, _ = resolve_share_user(config)
            except ShareError:
                raise exc from None  # no identity to suggest — keep the base error
            raise ShareError(
                f"{ref!r} is not a full share ref (missing the sender)",
                hint=f"refs are <user>/<sub>/<file> — did you mean: "
                     f"mintd share get {user}/{ref}",
            ) from exc
        raise


def share_get(
    *,
    ref: str,
    config: Config,
    reporter: Reporter,
    out: str | Path | None = None,
    s3_client_factory: Callable[[dict[str, str], str | None], Any] | None = None,
) -> GetResult:
    """Retrieve ``<user>/<sub>/<file>`` — zero identity setup (the author is in
    the ref). Refuses an existing dest file; a missing remote key is a clean
    error before any bytes are transferred.

    ``s3_client_factory`` defaults to ``_create_s3_client``, resolved at call
    time (not def time) so the CLI path stays injectable for tests."""
    factory = s3_client_factory or _create_s3_client
    user, sub_prefix, filename = _parse_get_ref(ref, config)
    bucket, endpoint = _require_storage(config)
    dest = _resolve_get_dest(out, filename)
    if dest.exists():
        raise ShareError(
            f"refusing to overwrite existing file: {dest}",
            hint="choose another --out or remove the existing file",
        )
    key = f"{SHARE_PREFIX}{user}/{sub_prefix}{filename}"
    s3 = factory({"endpoint": endpoint}, config.aws_profile_name)
    start = time.monotonic()
    try:
        info = head_remote_object(s3, bucket, key)
    except RemoteObjectNotFound as exc:
        raise ShareError(
            f"no share object at {ref}",
            hint="check the ref with the sender; share objects may also have expired",
        ) from exc
    if info.checksum_sha256 is None:
        reporter.warn("object has no stored SHA256 — transfer verified by size only")
    with reporter.progress(total=info.size, desc=f"Downloading {filename}") as advance:
        # Pass the HEAD size so download_object enforces it — this is what
        # makes the "verified by size only" warning above a real guarantee.
        n = download_object(
            s3, bucket, key, dest, progress=advance, expected_size=info.size
        )
    return GetResult(
        name=filename, ref=ref, dest=dest, bytes=n, elapsed_s=time.monotonic() - start
    )
