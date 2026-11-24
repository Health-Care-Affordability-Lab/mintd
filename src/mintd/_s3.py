"""S3: the only module in mintd that imports boto3 or botocore.

Everything else reaches S3 through it: the client built from a DVC remote's
config, transient retry, the version-aware HEAD probe, one download, and the
exception classes other modules catch. boto3 is a hard dependency, but a broken
install still imports this module: ``available()`` is False and the exception
names are inert placeholders.
"""

from __future__ import annotations

import importlib
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from ._aws_credentials import shared_credentials_path
from .errors import MintdError

try:
    import boto3
    from botocore.config import Config as BotoConfig
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
    BotoConfig = None  # type: ignore[assignment,misc]

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

# ChecksumMode="ENABLED" makes botocore validate the response-body SHA256;
# a genuine mismatch raises FlexibleChecksumError (a BotoCoreError, NOT a
# ClientError) — so it needs its own mapping or it tracebacks. Guarded like
# the sentinels above so boto3-absent installs stay importable.
try:
    from botocore.exceptions import FlexibleChecksumError
except ImportError:  # boto3 absent — inert placeholder; the except clause never fires

    class FlexibleChecksumError(Exception):  # type: ignore[no-redef]
        """Placeholder when botocore is absent (mirrors the block above)."""

# botocore 1.43.0 wraps a GetObject error body in StreamingChecksumBody when the
# 5xx carries an x-amz-checksum-* header (httpchecksum.handle_checksum_body does
# not check the status), and parsers._is_generic_error_response then calls
# .strip() on it for status >= 500. A real server error therefore arrives as
# AttributeError. Its own guard, not the block above: if a
# botocore release moves the class, only this workaround goes inert.
try:
    from botocore.httpchecksum import StreamingChecksumBody
except ImportError:  # boto3 absent — inert placeholder; isinstance never matches

    class StreamingChecksumBody:  # type: ignore[no-redef]
        """Placeholder when botocore is absent (mirrors the block above)."""

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
        """Placeholder when boto3 is absent (mirrors the block above)."""

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
        """Placeholder when boto3 is absent (mirrors the block above)."""

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


_T = TypeVar("_T")


def is_transient_s3_error(exc: BaseException) -> bool:
    """Pure classification (no I/O, no sleeps) of S3/network errors worth retrying.

    Shared policy: the share/cache lanes may import this together with
    :func:`retry_transient` instead of forking their own list.

    Transient:
      - ``ClientError`` whose code is in ``_RETRYABLE_S3_ERRORS`` (5xx,
        RequestTimeout, SlowDown, throttling family) or whose HTTP status
        is 429/500/503. A HEAD has no error body, so its code is the bare
        status and only the status names a throttle;
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
        return status in (429, 500, 503)
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


class S3AccessDenied(Exception):
    """S3 refused a HEAD (403 / AccessDenied), or there are no AWS
    credentials at all: no verdict about any object can be trusted."""


@dataclass(frozen=True)
class HeadVerdict:
    """What one HEAD said about an object version.

    ``found``: S3 returned a real version (``version_id``, ``etag`` without
    quotes, ``size``). ``unpinned``: the object exists but its version is
    ``null`` (versioning suspended), so nothing can pin it. ``missing``: no
    such key or version, or a delete marker. ``inconclusive``: transient
    errors outlasted the retries, or an error nothing maps.
    """

    kind: str  # "found" | "unpinned" | "missing" | "inconclusive"
    version_id: str | None = None
    etag: str | None = None
    size: int | None = None
    reason: str | None = None


def _classify_head(head: Callable[[], Any]) -> HeadVerdict:
    try:
        resp = retry_transient(head)
    except NoCredentialsError as exc:
        raise S3AccessDenied("no AWS credentials were found") from exc
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", ""))
        meta = exc.response.get("ResponseMetadata", {})
        status = meta.get("HTTPStatusCode")
        headers = meta.get("HTTPHeaders", {}) or {}
        if code in ("403", "AccessDenied") or status == 403:
            raise S3AccessDenied(str(exc)) from exc
        if code in _DRIFT_404_CODES or status == 404:
            return HeadVerdict("missing", reason=f"{code or status}")
        if status == 405 and str(headers.get("x-amz-delete-marker", "")).lower() == "true":
            return HeadVerdict("missing", reason="delete marker")
        return HeadVerdict("inconclusive", reason=f"{code or status}: {exc}")
    vid = resp.get("VersionId")
    if not vid or vid == "null":
        return HeadVerdict("unpinned", reason="no version (versioning suspended?)")
    return HeadVerdict(
        "found", version_id=str(vid),
        etag=str(resp.get("ETag", "")).strip('"'), size=resp.get("ContentLength"),
    )


def probe_versions(
    s3: Any, bucket: str, requests: "list[tuple[str, str | None]]", *, jobs: int = 16,
) -> "dict[tuple[str, str | None], HeadVerdict]":
    """HEAD each ``(key, version_id)`` in a thread pool; ``version_id=None``
    HEADs the key's current object. Each HEAD retries transient errors.

    A 403 or missing credentials raises ``S3AccessDenied`` for the whole
    call, because a verdict about one object would then say nothing about
    the rest. Any
    other exception a worker raises becomes ``inconclusive`` for that
    request alone: taken bare, one worker's exception would escape the
    ``as_completed`` loop and lose every other outcome.
    """
    out: dict[tuple[str, str | None], HeadVerdict] = {}
    if not requests:
        return out

    def _one(key: str, vid: str | None) -> HeadVerdict:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if vid:
            kwargs["VersionId"] = vid
        return _classify_head(lambda: s3.head_object(**kwargs))

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        futures = {ex.submit(_one, key, vid): (key, vid) for key, vid in dict.fromkeys(requests)}
        for fut in as_completed(futures):
            try:
                out[futures[fut]] = fut.result()
            except S3AccessDenied:
                raise
            except Exception as exc:
                out[futures[fut]] = HeadVerdict(
                    "inconclusive", reason=f"unexpected error: {type(exc).__name__}: {exc}"
                )
    return out


def resolve_profile(remote_cfg: dict[str, str], aws_profile_name: str | None) -> tuple[str | None, str]:
    """The AWS profile a client for this remote uses, and where its name came from.

    The remote's ``profile`` in ``.dvc/config`` (or ``.dvc/config.local``), else
    ``AWS_DEFAULT_PROFILE`` or ``AWS_PROFILE`` (botocore's order), else mintd's
    ``[mintd]`` profile (``Config.aws_profile_name``); ``(None, "")`` means boto3's
    default credential chain, on purpose, so CI and sandboxes keep working. dvc
    resolves it the same way: dvc-s3 reads the remote's ``profile``, and mintd sets
    ``AWS_PROFILE`` for dvc only when none is exported.
    """
    if "profile" in remote_cfg:  # even if empty: dvc names profile '' then, as botocore does for AWS_PROFILE
        return remote_cfg["profile"], "named in .dvc/config"
    for var in ("AWS_DEFAULT_PROFILE", "AWS_PROFILE"):  # botocore's order: the first one set wins, even if empty
        if var in os.environ:
            return os.environ[var], f"named by {var}"
    if aws_profile_name:
        return aws_profile_name, "mintd's [mintd] profile"
    return None, ""


_UNNAME = {
    "named in .dvc/config": "remove `profile` from the remote in .dvc/config (or .dvc/config.local)",
    "named by AWS_DEFAULT_PROFILE": "unset AWS_DEFAULT_PROFILE",
    "named by AWS_PROFILE": "unset AWS_PROFILE",
}


def profile_session(remote_cfg: dict[str, str], aws_profile_name: str | None) -> Any:
    """A boto3 session for the profile :func:`resolve_profile` picks. A named profile
    that doesn't exist is a ``MintdError``, never a fallback to other credentials.
    No credentials are loaded yet, so this checks the name and nothing else."""
    name, source = resolve_profile(remote_cfg, aws_profile_name)
    try:
        return boto3.Session(profile_name=name)
    except ProfileNotFound as exc:
        unname = _UNNAME.get(source, "run mintd config setup")
        raise MintdError(
            f"AWS profile '{name}' ({source}) isn't in {shared_credentials_path()}",
            # An empty variable still names a profile for boto3 and dvc; there is no [] section to add.
            hint=f"add a [{name}] section there, or {unname}" if name else f"{unname}, or set it to a profile that exists",
        ) from exc


def load_credentials(remote_cfg: dict[str, str], aws_profile_name: str | None) -> tuple[Any, str]:
    """:func:`profile_session`'s session with its credentials loaded, and where they
    came from as botocore names it (``env``, ``shared-credentials-file``, ``sso``,
    ``custom-process``, ...). A ``MintdError`` when the named profile is missing, when
    there are no credentials, when loading them fails, or when the access key or
    secret is blank. Loading runs the whole chain: the instance metadata service
    (one try, a one-second timeout off EC2), and any SSO, assume-role or
    credential_process fetch. No message carries a key, secret or token."""
    from botocore.exceptions import CredentialRetrievalError  # only reached with boto3 installed

    session = profile_session(remote_cfg, aws_profile_name)
    hint = (f"add a [mintd] section to {shared_credentials_path()} (mintd config setup offers to), "
            "or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY")
    try:
        creds = session.get_credentials()
        if creds is None:
            raise MintdError("no AWS credentials were found", hint=hint)
        method = creds.method
        # SSO, assume-role and credential_process credentials are deferred: reading them is what proves them.
        frozen = creds.get_frozen_credentials()
    except MintdError:
        raise
    except CredentialRetrievalError as exc:
        # Never str(exc): for a credential_process it carries the process's stderr, which may hold anything.
        provider = exc.kwargs.get("provider", "credential")
        raise MintdError(
            f"AWS credentials could not be loaded: the {provider} provider failed",
            hint="run the profile's credential_process yourself to see its output" if provider == "custom-process" else hint,
        ) from None
    except OSError as exc:
        # botocore runs a credential_process without catching this: the program is missing or can't be
        # run (on Windows, a script with a shebang). It never ran, so there is no output; the text names
        # at most the program, never its arguments.
        raise MintdError(
            f"AWS credentials could not be loaded: the profile's credential_process couldn't start "
            f"({type(exc).__name__}: {exc})",
            hint="run the profile's credential_process yourself to see why",
        ) from exc
    except Exception as exc:  # partial keys, an expired SSO token, a malformed file: their messages name no secret
        raise MintdError(f"AWS credentials could not be loaded ({type(exc).__name__}): {exc}", hint=hint) from exc
    if not (frozen.access_key or "").strip() or not (frozen.secret_key or "").strip():
        raise MintdError(f"the AWS credentials from {method} have an empty access key or secret", hint=hint)
    return session, method


def create_client(
    remote_cfg: dict[str, str], aws_profile_name: str | None, *, pool: int | None = None, session: Any = None,
) -> Any:
    """An S3 client for a DVC remote section (``endpointurl``/``endpoint``, ``region``,
    ``profile``), on ``session`` when given (one :func:`load_credentials` returned),
    else on :func:`profile_session`'s."""
    if session is None:
        session = profile_session(remote_cfg, aws_profile_name)

    endpoint_url = remote_cfg.get("endpointurl")
    if not endpoint_url:
        endpoint_url = remote_cfg.get("endpoint")
    # A client shared by N threads needs N pooled connections. botocore's pool
    # never blocks: a thread past the limit opens a connection that urllib3
    # drops once the pool already holds its maximum idle. Never below boto3's 10.
    # ponytail: counts the caller's threads only; boto3's multipart transfer threads (up to ten per file) share this
    # pool too. Size it at ten per thread if large-file pulls show "Connection pool is full".
    config = BotoConfig(max_pool_connections=max(pool, 10)) if pool else None
    return session.client(
        "s3", endpoint_url=endpoint_url, region_name=remote_cfg.get("region"), config=config,
    )


def available() -> bool:
    """False on a broken install without boto3, or once ``sys.modules["boto3"]`` is None."""
    try:
        importlib.import_module("boto3")
    except ImportError:
        return False
    return boto3 is not None


def download(
    s3: Any, bucket: str, key: str, path: Path, *, version_id: str | None = None,
    checksum: bool = False, progress: Callable[[int], None] | None = None,
) -> None:
    """Download one object (at ``version_id`` when given) to ``path``, retrying
    transient errors (:func:`retry_transient`). Every lane downloads through here.
    A GetObject 5xx that botocore raises as ``AttributeError`` (see
    ``StreamingChecksumBody`` above) is re-raised as the 5xx ``ClientError`` it
    was, so it is retried and reported like any 500. ``checksum`` has botocore
    verify the object's stored checksum on the response. boto3's ``download_file``
    takes ``VersionId`` only through ``ExtraArgs``, and calls ``progress`` from its
    own transfer threads."""
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key, "Filename": str(path)}
    extra = {"VersionId": version_id} if version_id else {}
    if checksum:
        extra["ChecksumMode"] = "ENABLED"
    if extra:
        kwargs["ExtraArgs"] = extra
    if progress is not None:  # absent, not None: boto3 calls Callback(bytes)
        kwargs["Callback"] = progress

    def attempt() -> None:
        try:
            s3.download_file(**kwargs)
        except AttributeError as exc:
            if not isinstance(exc.obj, StreamingChecksumBody):
                raise  # a real bug: propagate verbatim
            raise ClientError(
                {
                    "Error": {
                        "Code": "InternalError",
                        "Message": "S3 returned a server error (5xx) whose body botocore could not parse",
                    },
                    # The stubs want every metadata key; only the status is read.
                    "ResponseMetadata": {"HTTPStatusCode": 500},  # type: ignore[typeddict-item]
                },
                "GetObject",
            ) from exc

    retry_transient(attempt)
