"""Fake `Fetcher` implementations for tests."""

from __future__ import annotations

from mintd.producer import FetchError


class StaticFetcher:
    """Returns canned bytes from an in-process dict keyed by (repo, pin).

    Records every call in `self.calls` so tests can assert lookup counts.
    HEAD resolution uses `head_store: {repo: (bytes, sha)}` and records
    each lookup in `self.head_calls`.
    """

    def __init__(
        self,
        store: dict[tuple[str, str], bytes],
        head_store: dict[str, tuple[bytes, str]] | None = None,
    ) -> None:
        self._store = store
        self._head_store = head_store or {}
        self.calls: list[tuple[str, str]] = []
        self.head_calls: list[str] = []

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes:
        self.calls.append((repo, pin))
        if (repo, pin) not in self._store:
            raise FetchError.pin_missing(repo, pin)
        return self._store[(repo, pin)]

    def fetch_metadata_at_head(self, repo: str) -> tuple[bytes, str]:
        self.head_calls.append(repo)
        if repo not in self._head_store:
            raise FetchError.unreachable(repo, "HEAD", "no HEAD configured")
        return self._head_store[repo]


class ErroringFetcher:
    """Always raises the configured `FetchError` variant. Records every call."""

    def __init__(self, error: FetchError) -> None:
        self._error = error
        self.calls: list[tuple[str, str]] = []
        self.head_calls: list[str] = []

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes:
        self.calls.append((repo, pin))
        raise self._error

    def fetch_metadata_at_head(self, repo: str) -> tuple[bytes, str]:
        self.head_calls.append(repo)
        raise self._error
