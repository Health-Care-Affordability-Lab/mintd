"""Tests for `clone_and_pull_product` (slice 24 — `mintd data clone`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mintd._dvc_ops import DvcOpError
from mintd._registry_git_ops import GitOpError
from mintd.catalog import InMemoryCatalogClient
from mintd.data import (
    ImportDestinationExists,
    MissingPrimaryDataProduct,
    clone_and_pull_product,
)
from mintd.model import Metadata
from mintd.producer import ProducerError

from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.registry_git_ops import CloneCall, _FakeRegistryGitOps

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


# ---------- helpers ------------------------------------------------------


def _register(
    client: InMemoryCatalogClient,
    name: str = "provider-xw",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    data["project"]["full_name"] = f"data_{name}"
    data["repository"]["github_url"] = f"https://github.com/example-org/{name}"
    data["data_products"]["primary"] = "outputs/main.parquet"
    if mutate is not None:
        mutate(data)
    client.register(Metadata.model_validate(data))


class _NoopCloneGitOps(_FakeRegistryGitOps):
    """Records clone calls and `mkdir`s the dest; does NOT shell out to git."""

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
    ) -> None:
        self.clone_calls.append(CloneCall(url, Path(dest), shallow, branch))
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / ".dvc").mkdir()


# ---------- tests --------------------------------------------------------


def test_clone_and_pull_product_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    dest = clone_and_pull_product(
        client, dvc, git, None,
        name="provider-xw",
    )

    assert dest.dest == (tmp_path / "data_provider-xw").resolve()
    assert len(git.clone_calls) == 1
    assert git.clone_calls[0].shallow is False
    assert git.clone_calls[0].branch is None
    assert git.clone_calls[0].url == "https://github.com/example-org/provider-xw"
    assert len(dvc.pull_calls) == 1
    # Default now pulls everything (targets=None); --primary narrows to primary path.
    assert dvc.pull_calls[0].targets is None


def test_clone_and_pull_product_with_explicit_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    dest_arg = tmp_path / "x"
    dest = clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", dest=dest_arg,
    )

    assert dest.dest == dest_arg.resolve()
    assert git.clone_calls[0].dest == dest_arg.resolve()


def test_clone_and_pull_product_with_rev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", rev="v1.2",
    )

    assert git.clone_calls[0].branch == "v1.2"


def test_clone_and_pull_product_default_pulls_all_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (no flags) pulls every tracked output — dvc pull with targets=None."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw",
    )

    assert dvc.pull_calls[0].targets is None


def test_clone_and_pull_product_with_primary_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``primary_only=True`` narrows the dvc pull to the primary path."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", primary_only=True,
    )

    assert dvc.pull_calls[0].targets == ["outputs/main.parquet"]


def test_clone_and_pull_product_normalizes_windows_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--primary: a catalog primary stored with backslashes, a leading
    './', or a trailing '/' still resolves to the posix .dvc target."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _denormalize(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = ".\\outputs\\main.parquet\\"

    _register(client, mutate=_denormalize)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", primary_only=True,
    )

    assert dvc.pull_calls[0].targets == ["outputs/main.parquet"]


def test_clone_and_pull_product_refuses_existing_nonempty_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    pre_existing = tmp_path / "data_provider-xw"
    pre_existing.mkdir()
    (pre_existing / "foo.txt").write_text("stale", encoding="utf-8")

    client = InMemoryCatalogClient()
    _register(client)

    with pytest.raises(ImportDestinationExists) as exc:
        clone_and_pull_product(
            client, _FakeDvcOps(), _NoopCloneGitOps(), None,
            name="provider-xw",
        )
    assert "non-empty" in str(exc.value)
    assert str(pre_existing.resolve()) in str(exc.value)


def test_clone_and_pull_product_raises_when_no_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``primary_only=True`` on an entry with no primary raises clearly."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _drop_primary(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = None

    _register(client, mutate=_drop_primary)

    with pytest.raises(MissingPrimaryDataProduct):
        clone_and_pull_product(
            client, _FakeDvcOps(), _NoopCloneGitOps(), None,
            name="provider-xw", primary_only=True,
        )


class _AssertNotFetchedClient:
    """CatalogClient stub that raises if `fetch` is called — proves name
    validation happens BEFORE the registry round-trip."""

    def fetch(self, name: str) -> Any:
        raise AssertionError(f"fetch should not be called; got {name!r}")

    def list(self, filter: Any = None) -> list[Any]:
        return []

    def register(self, m: Any) -> None:
        raise AssertionError("register should not be called")

    def update(self, m: Any) -> None:
        raise AssertionError("update should not be called")


@pytest.mark.parametrize("bad_name", ["../escape", "foo/bar", "..", ".", ""])
def test_clone_and_pull_product_rejects_bad_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_name: str
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        clone_and_pull_product(
            _AssertNotFetchedClient(),  # type: ignore[arg-type]
            _FakeDvcOps(),
            _NoopCloneGitOps(),
            None,
            name=bad_name,
        )


def test_clone_and_pull_product_strips_legacy_prefix_in_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, name="data_aha")
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    dest = clone_and_pull_product(client, dvc, git, None, name="data_aha")

    assert dest.dest == (tmp_path / "data_aha").resolve()


def test_clone_and_pull_product_code_type_uses_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 39: cloning a code-type entry lands in `foo/` (bare), matching
    `mintd init code foo` — not `code_foo/`. The clone-dest is the sixth
    prefix site, routed through `project_full_name`."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(
        client, name="foo", mutate=lambda d: d["project"].update({"type": "code"})
    )
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    dest = clone_and_pull_product(client, dvc, git, None, name="foo")

    assert dest.dest == (tmp_path / "foo").resolve()


class _RaisingCloneGitOps(_NoopCloneGitOps):
    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
    ) -> None:
        raise GitOpError(["git", "clone"], "fatal: repository not found")


def test_clone_and_pull_product_translates_git_failure_to_producer_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)

    with pytest.raises(ProducerError) as exc:
        clone_and_pull_product(
            client, _FakeDvcOps(), _RaisingCloneGitOps(), None,
            name="provider-xw",
        )
    msg = str(exc.value)
    assert "provider-xw" in msg or "https://github.com/example-org/provider-xw" in msg
    assert str((tmp_path / "data_provider-xw").resolve()) in msg
    assert "partial clone left in place" in msg


class _CwdRecordingDvcOps(_FakeDvcOps):
    def __init__(self) -> None:
        super().__init__()
        self.pull_cwds: list[Path] = []

    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.pull_cwds.append(Path.cwd())
        super().pull(targets=targets, remote=remote, jobs=jobs, extra_args=extra_args)


def test_clone_and_pull_product_runs_dvc_inside_clone_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _CwdRecordingDvcOps()

    clone_and_pull_product(
        client, dvc, _NoopCloneGitOps(), None, name="provider-xw",
    )

    assert dvc.pull_cwds == [(tmp_path / "data_provider-xw").resolve()]
    assert Path.cwd() == tmp_path  # restored after return


class _DvcPullErrorOps(_FakeDvcOps):
    def pull(
        self,
        *,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        raise DvcOpError("boom")


def test_clone_and_pull_product_restores_cwd_on_dvc_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)

    with pytest.raises(DvcOpError):
        clone_and_pull_product(
            client, _DvcPullErrorOps(), _NoopCloneGitOps(), None,
            name="provider-xw",
        )

    assert Path.cwd() == tmp_path  # restored even on failure


# ---------- slice 26: reporter threaded through to data_pull -----------


def test_clone_and_pull_product_forwards_reporter_to_data_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 26: clone_and_pull_product accepts an optional ``reporter``
    kwarg and forwards it to ``data_pull``. Production users get the
    progress bar; tests can pass None and skip it."""
    from mintd._console import Reporter

    received: dict[str, object] = {}

    def _spy_data_pull(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr("mintd.data.data_pull", _spy_data_pull)
    monkeypatch.chdir(tmp_path)

    client = InMemoryCatalogClient()
    _register(client)
    reporter = Reporter(json_mode=False, no_color=True)

    clone_and_pull_product(
        client, _FakeDvcOps(), _NoopCloneGitOps(), None,
        name="provider-xw",
        reporter=reporter,
    )

    assert received.get("reporter") is reporter
