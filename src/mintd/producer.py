"""`ProducerView` — typed, immutable view over a producer's metadata.json at a pinned commit.

The two-source-of-truth contract: the catalog is canonical for identity
(slice 2/3), but the producer-at-pin is canonical for pipeline correctness.
`ProducerView` is the runtime object that *is* "producer at pin" — fetched
on demand via `Fetcher`, cached content-addressed by commit SHA, validated
with a typed failure taxonomy (`ProducerError`) at the construction boundary.

Replaces legacy mintd's `fetch_producer_metadata` (which returned
`dict[str, Any]` and swallowed every failure into `{}`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.parse
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from ._producer_git_ops import (
    FetchError,
    Fetcher,
    GitArchiveFetcher,
)
from .model import DataProductOutput, Metadata

__all__ = [
    "FetchError",
    "Fetcher",
    "GitArchiveFetcher",
    "MissingPrimaryDataProduct",
    "ProducerError",
    "ProducerView",
]


logger = logging.getLogger(__name__)


class MissingPrimaryDataProduct(Exception):
    """The producer's metadata has no `data_products.primary` to resolve."""


class ProducerError(Exception):
    """Validation-level failure constructing a ProducerView.

    Wraps the three `FetchError` transport reasons plus two validation
    reasons (metadata_invalid, schema_too_old). Translation from
    `FetchError` happens at `ProducerView.at` — Fetchers raise transport
    errors; views raise typed view errors.
    """

    class Reason(StrEnum):
        UNREACHABLE = "unreachable"
        PIN_MISSING = "pin_missing"
        METADATA_MISSING = "metadata_missing"
        METADATA_INVALID = "metadata_invalid"
        SCHEMA_TOO_OLD = "schema_too_old"

    def __init__(
        self,
        reason: "ProducerError.Reason",
        repo: str,
        pin: str,
        detail: str = "",
    ) -> None:
        super().__init__(f"{reason}: repo={repo} pin={pin} detail={detail}")
        self.reason = reason
        self.repo = repo
        self.pin = pin
        self.detail = detail

    @classmethod
    def unreachable(cls, repo: str, pin: str, detail: str = "") -> "ProducerError":
        return cls(cls.Reason.UNREACHABLE, repo, pin, detail)

    @classmethod
    def pin_missing(cls, repo: str, pin: str, detail: str = "") -> "ProducerError":
        return cls(cls.Reason.PIN_MISSING, repo, pin, detail)

    @classmethod
    def metadata_missing(cls, repo: str, pin: str, detail: str = "") -> "ProducerError":
        return cls(cls.Reason.METADATA_MISSING, repo, pin, detail)

    @classmethod
    def metadata_invalid(cls, repo: str, pin: str, detail: str = "") -> "ProducerError":
        return cls(cls.Reason.METADATA_INVALID, repo, pin, detail)

    @classmethod
    def schema_too_old(cls, repo: str, pin: str, detail: str = "") -> "ProducerError":
        return cls(cls.Reason.SCHEMA_TOO_OLD, repo, pin, detail)

    @classmethod
    def from_fetch_error(cls, fe: FetchError) -> "ProducerError":
        mapping = {
            FetchError.Reason.UNREACHABLE: cls.Reason.UNREACHABLE,
            FetchError.Reason.PIN_MISSING: cls.Reason.PIN_MISSING,
            FetchError.Reason.METADATA_MISSING: cls.Reason.METADATA_MISSING,
        }
        return cls(mapping[fe.reason], fe.repo, fe.pin, fe.detail)


def _safe_repo_dirname(repo: str, *, max_len: int = 200) -> str:
    """Return a filesystem-safe directory name for a repo URL.

    Common case: `urllib.parse.quote(repo, safe="")` — readable and
    debuggable. Long URLs (quoted form > max_len bytes) get truncated to a
    `max_len`-byte prefix with a sha256 suffix of the *full* repo string,
    so distinct long URLs map to distinct dirnames and the result is always
    `NAME_MAX`-safe.
    """
    quoted = urllib.parse.quote(repo, safe="")
    if len(quoted.encode("utf-8")) <= max_len:
        return quoted
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]
    prefix = quoted.encode("utf-8")[: max_len - 17].decode("utf-8", errors="ignore")
    return f"{prefix}_{digest}"


class _ProducerCache:
    """Disk cache of producer metadata.json bytes, keyed by (repo, pin).

    Bytes-only: validation runs on every read so the cache survives model
    evolution. Atomic write via per-process+per-call unique tmp filename
    avoids cross-writer races. OSError is swallowed and logged — cache is
    best-effort and never breaks the typed taxonomy at `ProducerView.at`.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _path_for(self, repo: str, pin: str) -> Path:
        return self._cache_dir / _safe_repo_dirname(repo) / f"{pin}.json"

    def read(self, repo: str, pin: str) -> bytes | None:
        path = self._path_for(repo, pin)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("producer cache read failed for %s@%s: %s", repo, pin, e)
            return None

    def write(self, repo: str, pin: str, data: bytes) -> None:
        try:
            path = self._path_for(repo, pin)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("producer cache write failed for %s@%s: %s", repo, pin, e)


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "mintd" / "producers"


def _peek_schema_version(raw: bytes) -> str | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(parsed, dict):
        version = parsed.get("schema_version")
        if isinstance(version, str):
            return version
    return None


@dataclass(frozen=True)
class ProducerView:
    """Immutable view of a producer's metadata.json at a specific commit.

    Construct via `ProducerView.at(repo, pin)` (strict, raises
    `ProducerError`) or `ProducerView.try_at(...)` (non-raising, returns
    `ProducerView | ProducerError`).
    """

    repo: str
    pin: str
    metadata: Metadata

    @classmethod
    def at(
        cls,
        repo: str,
        pin: str,
        *,
        fetcher: Fetcher | None = None,
        cache_dir: Path | None = None,
    ) -> "ProducerView":
        active_fetcher: Fetcher = fetcher if fetcher is not None else GitArchiveFetcher()
        cache = _ProducerCache(cache_dir if cache_dir is not None else _default_cache_dir())

        raw = cache.read(repo, pin)
        if raw is None:
            try:
                raw = active_fetcher.fetch_metadata_at(repo, pin)
            except FetchError as e:
                raise ProducerError.from_fetch_error(e) from e
            cache.write(repo, pin, raw)

        peeked = _peek_schema_version(raw)
        if peeked is not None and peeked != "2.0":
            raise ProducerError.schema_too_old(repo, pin, detail=f"schema_version={peeked}")

        try:
            meta = Metadata.model_validate_json(raw)
        except ValidationError as e:
            raise ProducerError.metadata_invalid(repo, pin, detail=str(e)) from e

        return cls(repo=repo, pin=pin, metadata=meta)

    @classmethod
    def at_head(
        cls,
        repo: str,
        *,
        fetcher: Fetcher | None = None,
        cache_dir: Path | None = None,
    ) -> tuple["ProducerView", str]:
        """Resolve HEAD on the remote, fetch metadata at that SHA, validate.

        Returns `(view, resolved_head_sha)`. HEAD itself is not content-
        addressable, so this method always pays the round-trip cost — the
        *result* (validated metadata at the resolved SHA) IS cached via
        the existing `at(repo, sha)` path, keyed by the resolved SHA. The
        cache is never keyed by the literal string `"HEAD"`.
        """
        active_fetcher: Fetcher = fetcher if fetcher is not None else GitArchiveFetcher()
        try:
            raw, head_sha = active_fetcher.fetch_metadata_at_head(repo)
        except FetchError as e:
            raise ProducerError.from_fetch_error(e) from e

        cache_dir_arg = cache_dir if cache_dir is not None else _default_cache_dir()
        _ProducerCache(cache_dir_arg).write(repo, head_sha, raw)
        view = cls.at(repo, head_sha, fetcher=active_fetcher, cache_dir=cache_dir_arg)
        return view, head_sha

    @classmethod
    def try_at(
        cls,
        repo: str,
        pin: str,
        *,
        fetcher: Fetcher | None = None,
        cache_dir: Path | None = None,
    ) -> "ProducerView | ProducerError":
        try:
            return cls.at(repo, pin, fetcher=fetcher, cache_dir=cache_dir)
        except ProducerError as e:
            return e

    def outputs(self) -> list[DataProductOutput]:
        return list(self.metadata.data_products.outputs)

    def output_paths(self) -> list[str]:
        return [o.path for o in self.outputs()]

    def primary_or_raise(self) -> str:
        primary = self.metadata.data_products.primary
        if not primary:
            raise MissingPrimaryDataProduct(
                f"producer {self.repo}@{self.pin} has no data_products.primary"
            )
        return primary
