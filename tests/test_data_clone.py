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
    UnknownProductPath,
    _resolve_paths,
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


# ---------- `--path` selector (issue: data-clone-path-selector) ---------


def _add_file_output(d: dict[str, Any]) -> None:
    """Track a non-primary single-file output alongside the `data/final/`
    directory output the fixture already declares."""
    d["data_products"]["outputs"].append(
        {
            "path": "data/intermediate/markets/defs_30min.parquet",
            "description": "drive-time market definitions",
            "primary": False,
            "last_published": "",
        }
    )


def test_clone_and_pull_product_with_path_pulls_only_that_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paths=[<file>]`` narrows the dvc pull to that single tracked file."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_add_file_output)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw",
        paths=["data/intermediate/markets/defs_30min.parquet"],
    )

    assert dvc.pull_calls[0].targets == [
        "data/intermediate/markets/defs_30min.parquet"
    ]


def test_clone_and_pull_product_with_path_directory_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paths`` works for a directory output too (fixture tracks
    `data/final/`); the trailing slash is normalized away."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", paths=["data/final/"],
    )

    assert dvc.pull_calls[0].targets == ["data/final"]


def test_clone_and_pull_product_with_repeated_paths_pulls_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_add_file_output)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw",
        paths=["data/final/", "data/intermediate/markets/defs_30min.parquet"],
    )

    assert dvc.pull_calls[0].targets == [
        "data/final",
        "data/intermediate/markets/defs_30min.parquet",
    ]


def test_clone_and_pull_product_path_accepts_primary_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary counts as a tracked output even when it isn't repeated
    in `data_products.outputs` (the fixture's primary is mutated to
    `outputs/main.parquet` while outputs only lists `data/final/`)."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw",
        paths=["outputs/main.parquet"],
    )

    assert dvc.pull_calls[0].targets == ["outputs/main.parquet"]


def test_clone_and_pull_product_normalizes_path_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'./x', 'x/', and backslash spellings of a tracked output all match —
    validation and the pull target go through normalize_target."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw",
        paths=[".\\data\\final\\"],
    )

    assert dvc.pull_calls[0].targets == ["data/final"]


def test_clone_and_pull_product_paths_plus_primary_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """paths + primary_only conflict fails fast — before the registry
    round-trip and before anything touches the filesystem."""
    monkeypatch.chdir(tmp_path)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    with pytest.raises(ValueError, match="mutually exclusive"):
        clone_and_pull_product(
            _AssertNotFetchedClient(),  # type: ignore[arg-type]
            dvc, git, None,
            name="provider-xw",
            paths=["data/final/"],
            primary_only=True,
        )

    assert git.clone_calls == []
    assert dvc.pull_calls == []


def test_clone_and_pull_product_unknown_path_lists_tracked_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown --path fails with the product's tracked outputs (and
    primary) in the message — not a raw DVC 'no such target' stderr —
    BEFORE the clone touches disk: no git clone, no dest dir, no dvc pull.
    The corrected retry must not hit ImportDestinationExists."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_add_file_output)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, None, name="provider-xw",
            paths=["data/nope.csv"],
        )

    msg = str(exc.value)
    assert "data/nope.csv" in msg
    assert "data/final" in msg
    assert "data/intermediate/markets/defs_30min.parquet" in msg
    assert "outputs/main.parquet (primary)" in msg
    assert git.clone_calls == []
    assert not (tmp_path / "data_provider-xw").exists()
    assert dvc.pull_calls == []

    # The corrected retry just works — no leftover clone in the way.
    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", paths=["data/final/"],
    )
    assert len(git.clone_calls) == 1
    assert dvc.pull_calls[0].targets == ["data/final"]


def test_clone_and_pull_product_missing_primary_fails_before_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """primary_only=True with no catalog primary fails before the clone —
    same pre-clone placement as the --path validation."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _drop_primary(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = None

    _register(client, mutate=_drop_primary)
    git = _NoopCloneGitOps()

    with pytest.raises(MissingPrimaryDataProduct):
        clone_and_pull_product(
            client, _FakeDvcOps(), git, None,
            name="provider-xw", primary_only=True,
        )

    assert git.clone_calls == []
    assert not (tmp_path / "data_provider-xw").exists()


# ---------- --rev pinned: validate against the cloned metadata.json ------


class _MetadataWritingGitOps(_NoopCloneGitOps):
    """Fake clone that also drops a metadata.json into the dest, standing in
    for the producer repo's metadata at the cloned rev."""

    def __init__(self, data_products: dict[str, Any] | None) -> None:
        super().__init__()
        self._data_products = data_products

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        shallow: bool = True,
        branch: str | None = None,
    ) -> None:
        super().clone(url, dest, shallow=shallow, branch=branch)
        if self._data_products is not None:
            (Path(dest) / "metadata.json").write_text(
                json.dumps({"data_products": self._data_products}),
                encoding="utf-8",
            )


def test_clone_and_pull_product_rev_validates_against_cloned_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --rev pinned, a --path that exists at the cloned rev is accepted
    even when the registry's (HEAD) catalog entry no longer lists it."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)  # catalog outputs: data/final/ (+ primary)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps(
        {
            "primary": "outputs/main.parquet",
            "outputs": [
                {"path": "data/final/"},
                {"path": "data/intermediate/old_defs.parquet"},
            ],
        }
    )

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", rev="v1.0",
        paths=["data/intermediate/old_defs.parquet"],
    )

    assert dvc.pull_calls[0].targets == ["data/intermediate/old_defs.parquet"]


def test_clone_and_pull_product_rev_unknown_path_removes_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --rev pinned the --path check runs post-clone against the cloned
    metadata.json; on failure the fresh clone is removed so the corrected
    retry doesn't hit ImportDestinationExists."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps(
        {"primary": None, "outputs": [{"path": "data/rev-only.parquet"}]}
    )

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, None, name="provider-xw", rev="v1.0",
            paths=["data/typo.parquet"],
        )

    # Message lists the rev's outputs (from the clone), not HEAD's catalog.
    assert "data/rev-only.parquet" in str(exc.value)
    assert dvc.pull_calls == []
    assert not (tmp_path / "data_provider-xw").exists()

    # Corrected retry works against the same (now absent) dest.
    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", rev="v1.0",
        paths=["data/rev-only.parquet"],
    )
    assert dvc.pull_calls[0].targets == ["data/rev-only.parquet"]


def test_clone_and_pull_product_rev_falls_back_to_catalog_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --rev pinned but no readable metadata.json in the clone, the
    --path check falls back to the catalog entry."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)  # catalog outputs: data/final/ (+ primary)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps(None)  # clone writes no metadata.json

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", rev="v1.0",
        paths=["data/final/"],
    )
    assert dvc.pull_calls[0].targets == ["data/final"]

    with pytest.raises(UnknownProductPath):
        clone_and_pull_product(
            client, dvc, git, None, name="provider-xw", rev="v1.0",
            paths=["data/nope.csv"], dest=tmp_path / "other-dest",
        )
    assert not (tmp_path / "other-dest").exists()


def test_clone_and_pull_product_no_flags_unchanged_with_paths_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """paths=None + primary_only=False keeps the pull-everything default."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    clone_and_pull_product(
        client, dvc, git, None, name="provider-xw", paths=None,
    )

    assert dvc.pull_calls[0].targets is None


# ---------- _resolve_paths precedence matrix (shared import/clone) -------


_ENTRY: dict[str, Any] = {
    "data_products": {
        "primary": "outputs/main.parquet",
        "outputs": [
            {"path": "data/final/"},
            {"path": "data/intermediate/markets/defs_30min.parquet"},
        ],
    }
}


@pytest.mark.parametrize(
    ("path", "all_outputs", "expected"),
    [
        # explicit single path (import --path) wins over primary fallback
        ("data/final/", False, ["data/final/"]),
        # explicit path list (clone --path, repeatable) is passed through
        (["a", "b"], False, ["a", "b"]),
        # all_outputs returns every outputs[].path
        (
            None,
            True,
            ["data/final/", "data/intermediate/markets/defs_30min.parquet"],
        ),
        # neither → primary fallback
        (None, False, ["outputs/main.parquet"]),
    ],
)
def test_resolve_paths_precedence_matrix(
    path: str | list[str] | None, all_outputs: bool, expected: list[str]
) -> None:
    assert (
        _resolve_paths(_ENTRY, path=path, all_outputs=all_outputs, name="x")
        == expected
    )


def test_resolve_paths_no_primary_raises_with_hint() -> None:
    entry: dict[str, Any] = {"data_products": {"primary": None, "outputs": []}}
    with pytest.raises(MissingPrimaryDataProduct, match="pass --path or --all"):
        _resolve_paths(entry, path=None, all_outputs=False, name="x")
    with pytest.raises(MissingPrimaryDataProduct, match="drop --primary"):
        _resolve_paths(
            entry, path=None, all_outputs=False, name="x",
            missing_primary_hint="drop --primary to pull all tracked outputs",
        )


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
