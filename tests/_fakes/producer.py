"""Fake `Fetcher` implementations for tests."""

from __future__ import annotations

from mintd.producer import FetchError


class StaticFetcher:
    """Returns canned bytes from an in-process dict keyed by (repo, pin).

    Records every call in `self.calls` so tests can assert lookup counts.
    """

    def __init__(self, store: dict[tuple[str, str], bytes]) -> None:
        self._store = store
        self.calls: list[tuple[str, str]] = []

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes:
        self.calls.append((repo, pin))
        if (repo, pin) not in self._store:
            raise FetchError.pin_missing(repo, pin)
        return self._store[(repo, pin)]


class ErroringFetcher:
    """Always raises the configured `FetchError` variant. Records every call."""

    def __init__(self, error: FetchError) -> None:
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def fetch_metadata_at(self, repo: str, pin: str) -> bytes:
        self.calls.append((repo, pin))
        raise self._error
