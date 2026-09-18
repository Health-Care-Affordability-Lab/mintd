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
from tests._fakes.fast_sync_ops import _FakeFastSyncOps
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

    #: Tracked outputs the cloned repo carries, as `<path>.dvc` pointers.
    tracks: tuple[str, ...] = ()

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
        for target in self.tracks:
            f = Path(dest) / f"{target}.dvc"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(
                f"outs:\n  - md5: {'b' * 32}\n    size: 0\n"
                f"    path: {Path(target).name}\n"
            )


def _fast() -> _FakeFastSyncOps:
    """A FastSyncOps double that serves nothing and hands every target to the
    fallback ``dvc pull`` — which is the seam these tests observe. Before
    issue18 they passed ``None`` here and got that same ``dvc pull`` from
    ``data_pull``'s degraded branch."""
    fake = _FakeFastSyncOps()
    fake.fallback_all = True
    return fake


# ---------- tests --------------------------------------------------------


def test_clone_and_pull_product_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("outputs/main.parquet",)

    dest = clone_and_pull_product(
        client, dvc, git, _fast(),
        name="provider-xw",
    )

    assert dest.dest == (tmp_path / "data_provider-xw").resolve()
    assert len(git.clone_calls) == 1
    assert git.clone_calls[0].shallow is False
    assert git.clone_calls[0].branch is None
    assert git.clone_calls[0].url == "https://github.com/example-org/provider-xw"
    assert len(dvc.pull_calls) == 1
    # Default pulls every DISCOVERED output; --primary narrows to the primary.
    assert dvc.pull_calls[0].targets == ["outputs/main.parquet.dvc"]


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
        client, dvc, git, _fast(), name="provider-xw", dest=dest_arg,
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
        client, dvc, git, _fast(), name="provider-xw", rev="v1.2",
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
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
    )

    assert dvc.pull_calls[0].targets == ["outputs/main.parquet.dvc"]


def test_clone_and_pull_product_with_primary_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``primary_only=True`` narrows the dvc pull to the primary path."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", primary_only=True,
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
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", primary_only=True,
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
            client, _FakeDvcOps(), _NoopCloneGitOps(), _fast(),
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
            client, _FakeDvcOps(), _NoopCloneGitOps(), _fast(),
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
            _fast(),
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

    dest = clone_and_pull_product(client, dvc, git, _fast(), name="data_aha")

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

    dest = clone_and_pull_product(client, dvc, git, _fast(), name="foo")

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
            client, _FakeDvcOps(), _RaisingCloneGitOps(), _fast(),
            name="provider-xw",
        )
    msg = str(exc.value)
    assert "provider-xw" in msg or "https://github.com/example-org/provider-xw" in msg
    assert str((tmp_path / "data_provider-xw").resolve()) in msg
    assert "partial clone left in place" in msg


def test_clone_and_pull_product_passes_the_clone_dest_as_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone lane names the repo it pulls, instead of standing in it.

    This test used to be `..._runs_dvc_inside_clone_dest` and sampled
    `Path.cwd()` from inside a `_CwdRecordingDvcOps` subclass, because that
    was the only way to observe which repo `data_pull` would act on: the
    answer lived in process state, put there by a `os.chdir` in `data.py`
    that existed solely because `DvcOps` had no `cwd`. Unit A gave the
    protocol a `cwd`, so the answer is now an argument and the fake records
    it — the subclass is gone with the chdir it was watching.
    """
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
    )

    assert [c.cwd for c in dvc.pull_calls] == [tmp_path / "data_provider-xw"]
    assert Path.cwd() == tmp_path  # never chdir'd in the first place


class _DvcPullErrorOps(_FakeDvcOps):
    def pull(
        self,
        *,
        cwd: Path,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        raise DvcOpError("boom")


def test_clone_and_pull_product_never_changes_the_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)

    git = _NoopCloneGitOps()
    git.tracks = ("outputs/main.parquet",)

    with pytest.raises(DvcOpError):
        clone_and_pull_product(
            client, _DvcPullErrorOps(), git, _fast(),
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
    git.tracks = ("data/final/a.csv", "data/intermediate/markets/defs_30min.parquet")

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
        paths=["data/intermediate/markets/defs_30min.parquet"],
    )

    assert dvc.pull_calls[0].targets == [
        "data/intermediate/markets/defs_30min.parquet"
    ]


def test_clone_and_pull_product_with_path_directory_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``paths`` works for a directory output too (fixture declares
    `data/final/`); the trailing slash is normalized away, and a declared
    directory tracked as per-file outs beneath it expands to those files —
    dvc rejects the bare directory."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final/a.csv", "data/final/b.csv")

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", paths=["data/final/"],
    )

    assert dvc.pull_calls[0].targets == ["data/final/a.csv", "data/final/b.csv"]


def test_clone_and_pull_product_with_repeated_paths_pulls_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_add_file_output)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = (
        "data/final/a.csv", "data/final/b.csv",
        "data/intermediate/markets/defs_30min.parquet",
    )

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
        paths=["data/final/", "data/intermediate/markets/defs_30min.parquet"],
    )

    assert dvc.pull_calls[0].targets == [
        "data/final/a.csv",
        "data/final/b.csv",
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
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
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
    git.tracks = ("data/final/a.csv", "data/final/b.csv")

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
        paths=[".\\data\\final\\"],
    )

    assert dvc.pull_calls[0].targets == ["data/final/a.csv", "data/final/b.csv"]


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
            dvc, git, _fast(),
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
    The corrected retry must not hit ImportDestinationExists.

    This is the cheap catalog pre-filter the post-clone resolver sits
    behind (S5's `test_clone_path_typo_still_fails_before_the_clone`
    criterion): a typo must never cost a multi-GB clone."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_add_file_output)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final/a.csv", "data/final/b.csv")

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw",
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
        client, dvc, git, _fast(), name="provider-xw", paths=["data/final/"],
    )
    assert len(git.clone_calls) == 1
    assert dvc.pull_calls[0].targets == ["data/final/a.csv", "data/final/b.csv"]


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
            client, _FakeDvcOps(), git, _fast(),
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
    git.tracks = ("data/intermediate/old_defs.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", rev="v1.0",
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
    git.tracks = ("data/rev-only.parquet",)

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw", rev="v1.0",
            paths=["data/typo.parquet"],
        )

    # Message lists the rev's outputs (from the clone), not HEAD's catalog.
    assert "data/rev-only.parquet" in str(exc.value)
    assert dvc.pull_calls == []
    assert not (tmp_path / "data_provider-xw").exists()

    # Corrected retry works against the same (now absent) dest.
    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", rev="v1.0",
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
    git.tracks = ("data/final/a.csv", "data/final/b.csv")

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", rev="v1.0",
        paths=["data/final/"],
    )
    assert dvc.pull_calls[0].targets == ["data/final/a.csv", "data/final/b.csv"]

    with pytest.raises(UnknownProductPath):
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw", rev="v1.0",
            paths=["data/nope.csv"], dest=tmp_path / "other-dest",
        )
    assert not (tmp_path / "other-dest").exists()


# ---------- S5: selectors resolve against the cloned repo ----------


def _primary_is_final(d: dict[str, Any]) -> None:
    d["data_products"]["primary"] = "data/final/"  # the fixture's directory output


def test_clone_primary_directory_prefix_expands_to_the_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--primary on a directory primary tracked as per-file outs: dvc receives
    the outs, not the directory. `strict_targets` reproduces dvc's rejection
    of 'data/final' over per-file pointers, so an unexpanded selector raises
    DvcPullError here. Mutation: delete the expansion -> red. Real-dvc twin:
    test_pre_units_journey.py::test_cli_data_clone_directory_primary_exits_zero_with_bytes_on_disk.
    """
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_primary_is_final)
    dvc = _FakeDvcOps()
    dvc.strict_targets = True
    git = _NoopCloneGitOps()
    git.tracks = ("data/final/a.csv", "data/final/b.csv")

    clone_and_pull_product(client, dvc, git, _fast(), name="provider-xw", primary_only=True)

    assert dvc.pull_calls[0].targets == ["data/final/a.csv", "data/final/b.csv"]


@pytest.mark.parametrize(
    "select",
    [{"primary_only": True}, {"paths": ["data/final/"]}],
    ids=["primary", "path-default-rev"],
)
def test_clone_unresolvable_selector_raises_and_removes_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, select: dict[str, Any]
) -> None:
    """The repo tracks nothing at, above or beneath the selected path: raise
    UnknownProductPath naming the repo's real outs and remove the clone so the
    corrected retry is not blocked -- for --primary and default-rev --path,
    not just --rev --path. Mutations: drop the rmtree -> dest-absent assert
    and the retry (ImportDestinationExists) red; drop the raise -> red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_primary_is_final)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/other.csv",)

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(client, dvc, git, _fast(), name="provider-xw", **select)

    assert "data/final" in str(exc.value)
    assert "data/other.csv" in str(exc.value)
    assert "stale" in str(exc.value)
    # The listed outs are DVC's, not the catalog's, so `--path` would refuse
    # them pre-clone: the hint must not send the user there.
    assert exc.value.hint is not None
    assert "drop --path/--primary" in exc.value.hint
    assert "pass --path" not in exc.value.hint
    assert len(git.clone_calls) == 1  # past the catalog pre-filter: the clone happened
    assert dvc.pull_calls == []
    assert not (tmp_path / "data_provider-xw").exists()

    git.tracks = ("data/final/a.csv",)  # producer fixed: the retry is not blocked
    clone_and_pull_product(client, dvc, git, _fast(), name="provider-xw", **select)
    assert dvc.pull_calls[0].targets == ["data/final/a.csv"]


def test_clone_path_accepted_by_the_catalog_but_absent_in_the_repo_fails_after_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalog declares data/final/hospital-all-owners/, repo tracks only
    data/final/a.csv (nothing at/above/beneath the declared path): passes the
    cheap pre-filter, rejected post-clone with a stale-catalog hint, clone
    removed. NOT the live shape where data/final/ is one directory out and
    the stale subdirectory sits under it: that resolves through direction 2
    and is caught AFTER the pull instead — see
    test_clone_subpath_of_a_directory_out_that_never_lands_is_a_failed_target.
    Mutation: drop the post-clone check -> the non-strict fake accepts, red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _stale_subdir(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"].append(
            {"path": "data/final/hospital-all-owners/", "description": "",
             "primary": False, "last_published": ""}
        )

    _register(client, mutate=_stale_subdir)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final/a.csv",)

    with pytest.raises(UnknownProductPath, match="stale") as exc:
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw",
            paths=["data/final/hospital-all-owners/"],
        )

    assert "data/final/a.csv" in str(exc.value)
    assert len(git.clone_calls) == 1
    assert dvc.pull_calls == []
    assert not (tmp_path / "data_provider-xw").exists()


def test_clone_path_under_a_directory_out_keeps_its_own_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direction 2: a declared FILE under one tracked directory out. dvc pulls
    a sub-path of a directory out granularly, and that is what the selector
    got before the resolver existed — substituting the enclosing out would
    turn a one-file `--path` into the whole directory. Mutation: always use
    `out.target` -> `["data/final"]`, red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _file_under_dir(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"].append(
            {"path": "data/final/big.parquet", "description": "",
             "primary": False, "last_published": ""}
        )

    _register(client, mutate=_file_under_dir)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final",)  # ONE directory out

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", paths=["data/final/big.parquet"],
    )

    assert dvc.pull_calls[0].targets == ["data/final/big.parquet"]


def test_clone_subpath_of_a_directory_out_that_never_lands_is_a_failed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clone-resolver plan's U1, decided: a declared sub-path under ONE
    directory out is forwarded on the catalog's word, dvc fetches only the
    `.dir` manifest for a sub-path it lacks and exits 0 with nothing on
    disk. That must not be a ✓ line: it counts as a failed target with its
    own error, and the clone stays (a pull outcome, not a rejection). The
    fake's `pull` never writes the workspace, which IS this outcome.
    Mutation: drop the post-pull existence check -> pull_error_count 0, red.
    """
    from tests._fakes.reporter import RecordingReporter

    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _stale_subdir(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"].append(
            {"path": "data/final/stale/", "description": "",
             "primary": False, "last_published": ""}
        )

    _register(client, mutate=_stale_subdir)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final",)  # ONE directory out; its manifest is not in git
    reporter = RecordingReporter()

    result = clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
        paths=["data/final/stale/"], reporter=reporter,
    )

    assert dvc.pull_calls[0].targets == ["data/final/stale"]  # forwarded, as dvc allows
    assert result.pull_error_count == 1
    assert (tmp_path / "data_provider-xw").exists()  # a pull outcome keeps the clone
    errors = [e for e in reporter.events if e[0] == "error"]
    assert any("data/final/stale" in e[1] and "stale" in (e[2] or "") for e in errors), errors
    # The clone is kept, so the hint must not send the user to a clone that
    # `ImportDestinationExists` refuses; it names the retry inside the clone.
    assert all("clone everything" not in (e[2] or "") for e in errors)
    assert any("mintd data pull" in (e[2] or "") for e in errors)


def test_clone_repeated_spellings_of_one_stale_subpath_count_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--path x --path x/` is one selection: dvc gets one target and a miss
    is one failed target, not one per spelling. Mutation: drop the
    `declared_rel not in unverified` guard -> 2, red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _stale_subdir(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"].append(
            {"path": "data/final/stale/", "description": "",
             "primary": False, "last_published": ""}
        )

    _register(client, mutate=_stale_subdir)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/final",)

    result = clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw",
        paths=["data/final/stale/", "data/final/stale"],
    )

    assert dvc.pull_calls[0].targets == ["data/final/stale"]
    assert result.pull_error_count == 1


def test_clone_rev_primary_subpath_that_never_lands_blames_the_rev_not_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--rev --primary` where the HEAD primary is a sub-path of the rev's one
    directory out and does not land: the post-pull error must say the rev
    may predate the primary, like the pre-pull rejection does — not blame
    the producer's metadata. Mutation: drop the rev branch of the post-pull
    hint -> red."""
    from tests._fakes.reporter import RecordingReporter

    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()

    def _primary_is_sub(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = "data/final/sub/"

    _register(client, mutate=_primary_is_sub)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps({"primary": "data/final/", "outputs": [{"path": "data/final/"}]})
    git.tracks = ("data/final",)
    reporter = RecordingReporter()

    result = clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", rev="v1.0", primary_only=True,
        reporter=reporter,
    )

    assert result.pull_error_count == 1
    hints = [e[2] or "" for e in reporter.events if e[0] == "error"]
    assert any("predate the primary" in h for h in hints), hints
    assert all("registry update" not in h for h in hints)


def test_clone_rev_primary_unresolvable_names_the_rev_not_a_stale_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--rev` + `--primary`: the primary comes from the HEAD catalog and the
    clone is at an older rev that may predate it. The message must say that,
    not diagnose a stale catalog and send the producer to republish.
    Mutation: drop the `rev` branch of the message -> red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_primary_is_final)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps({"primary": "data/old.parquet", "outputs": []})
    git.tracks = ("data/old.parquet",)

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw", rev="v1.0", primary_only=True,
        )

    assert "predate the primary" in str(exc.value)
    assert "stale" not in str(exc.value)
    assert "data/old.parquet" in str(exc.value)
    assert not (tmp_path / "data_provider-xw").exists()


def test_clone_rev_path_declared_at_that_rev_but_untracked_is_a_stale_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--rev --path` has already passed the rev's OWN metadata.json, so a
    resolver miss is a stale declaration at that rev — not a rev that
    predates the output. Mutation: key the wording on `rev` alone -> red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _MetadataWritingGitOps({"primary": None, "outputs": [{"path": "data/final/"}]})
    git.tracks = ("data/other.csv",)

    with pytest.raises(UnknownProductPath) as exc:
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw", rev="v1.0", paths=["data/final/"],
        )

    assert "stale" in str(exc.value)
    assert "predate" not in str(exc.value)
    assert not (tmp_path / "data_provider-xw").exists()


def test_clone_rejection_keeps_a_pre_existing_empty_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--dest .` in an empty directory is the user's cwd. A post-clone
    rejection must clear what the clone put there, never remove the
    directory itself. Mutation: rmtree unconditionally -> red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_primary_is_final)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("data/other.csv",)
    mine = tmp_path / "mine"
    mine.mkdir()

    with pytest.raises(UnknownProductPath):
        clone_and_pull_product(
            client, dvc, git, _fast(), name="provider-xw", primary_only=True, dest=mine,
        )

    assert mine.is_dir()
    assert list(mine.iterdir()) == []


def test_clone_and_pull_product_no_flags_unchanged_with_paths_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """paths=None + primary_only=False keeps the pull-everything default."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()
    git.tracks = ("outputs/main.parquet",)

    clone_and_pull_product(
        client, dvc, git, _fast(), name="provider-xw", paths=None,
    )

    assert dvc.pull_calls[0].targets == ["outputs/main.parquet.dvc"]


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
        from mintd.data_ops import PullSummary
        return PullSummary(targets_pulled=0, total_bytes=0, elapsed_s=0.0)

    monkeypatch.setattr("mintd.data.data_pull", _spy_data_pull)
    monkeypatch.chdir(tmp_path)

    client = InMemoryCatalogClient()
    _register(client)
    reporter = Reporter(json_mode=False, no_color=True)

    clone_and_pull_product(
        client, _FakeDvcOps(), _NoopCloneGitOps(), _fast(),
        name="provider-xw",
        reporter=reporter,
    )

    assert received.get("reporter") is reporter


# ---------- pull-all audit fix 4: pull outcome propagates to the CLI ----------


def test_clone_and_pull_product_propagates_pull_error_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clone_and_pull_product must not discard data_pull's PullSummary: its
    error_count (fail-loudly targets + per-file failures) surfaces on
    CloneResult.pull_error_count so `mintd data clone` can exit non-zero."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    from mintd.data_ops import PullSummary

    monkeypatch.setattr(
        "mintd.data.data_pull",
        lambda *a, **k: PullSummary(
            targets_pulled=1, total_bytes=0, elapsed_s=0.1, error_count=3,
        ),
    )
    result = clone_and_pull_product(client, dvc, git, _fast(), name="provider-xw")
    assert result.pull_error_count == 3


def test_clone_and_pull_product_clean_pull_error_count_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: a clean pull yields pull_error_count == 0."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client)
    dvc = _FakeDvcOps()
    git = _NoopCloneGitOps()

    result = clone_and_pull_product(client, dvc, git, _fast(), name="provider-xw")
    assert result.pull_error_count == 0
