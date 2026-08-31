"""Tests for `import_product` orchestration."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mintd.catalog import CatalogEntry, CatalogNotFound, InMemoryCatalogClient
from mintd.data import (
    StaleBackupExists,
    ImportDestinationExists,
    MissingPrimaryDataProduct,
    UnknownProductPath,
    import_product,
)
from mintd.model import Metadata
from mintd.producer import FetchError, ProducerError, ProducerView

from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.producer import ErroringFetcher, StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"


def _register(
    client: InMemoryCatalogClient,
    name: str = "provider_xw",
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["project"]["name"] = name
    # Mirror real init: full_name = "<type>_<name>". The fixture's
    # default project.type is "data", so full_name becomes
    # `data_<name>`. import_product uses this as the dest namespace.
    project_type = data["project"].get("type", "data")
    data["project"]["full_name"] = f"{project_type}_{name}"
    data["repository"]["github_url"] = f"https://github.com/example-org/{name}"
    if mutate is not None:
        mutate(data)
    client.register(Metadata.model_validate(data))


def _with_primary(primary: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = primary

    return mutate


def _with_outputs(*paths: str) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["data_products"]["outputs"] = [
            {
                "path": p,
                "description": "",
                "primary": i == 0,
                "last_published": "",
            }
            for i, p in enumerate(paths)
        ]

    return mutate


def test_import_product_uses_primary_when_no_path(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path
    )

    assert len(produced) == 1
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.path == "outputs/main.parquet"
    assert call.repo_url == "https://github.com/example-org/provider_xw"
    # Slice 38: dest is namespaced by the producer's full_name so
    # multiple imports into the same dest_root don't collide.
    assert call.dest == tmp_path / "data_provider_xw" / "outputs" / "main.parquet"


def test_import_product_path_override(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    import_product(
        client, fake, "provider_xw", path="outputs/other.csv", cwd=tmp_path, dest_root=tmp_path
    )

    assert fake.calls[0].path == "outputs/other.csv"
    assert fake.calls[0].dest == tmp_path / "data_provider_xw" / "outputs" / "other.csv"


def test_import_product_all_outputs_loops(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(
        client,
        mutate=_with_outputs("outputs/a.csv", "outputs/b.csv", "outputs/c.csv"),
    )
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", all_outputs=True, cwd=tmp_path, dest_root=tmp_path
    )

    assert len(produced) == 3
    assert [c.path for c in fake.calls] == [
        "outputs/a.csv",
        "outputs/b.csv",
        "outputs/c.csv",
    ]


def test_import_all_skips_an_output_nested_inside_another(tmp_path: Path) -> None:
    """A product declaring a directory AND a file inside it. DVC cannot track
    both — the file is already in the directory — so mirroring the producer's
    paths (D-A) put pointer two INSIDE pointer one's payload, and real dvc
    answered `The file '.../data/final/b.csv' already exists locally`: one
    pointer written, exit 1, half done, nothing rolled back. Following mintd's
    own `--force` remedy then failed differently (`bad DVC file name ... is
    git-ignored`) and re-rendered as the same message.

    The inner path was never a distinct product, so it is dropped, loudly.

    Mutation: delete the `_drop_nested_paths` call -> two import calls, which
    is the state real dvc rejects.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_outputs("data/final", "data/final/b.csv"))
    fake = _FakeDvcOps()
    reporter = _RecordingReporter()

    produced = import_product(
        client, fake, "provider_xw", all_outputs=True, cwd=tmp_path,
        dest_root=tmp_path, reporter=reporter,
    )

    assert [c.path for c in fake.calls] == ["data/final"]
    assert len(produced) == 1
    assert any("data/final/b.csv" in m and "data/final" in m for m in reporter.infos)


def test_explicit_nested_paths_collapse_the_same_way(tmp_path: Path) -> None:
    """Same guard, reached through `--path` rather than `--all` — the producer
    is not the only source of an overlapping pair."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_outputs("data/final", "data/final/b.csv"))
    fake = _FakeDvcOps()

    import_product(
        client, fake, "provider_xw", path=["data/final/b.csv", "data/final/"],
        cwd=tmp_path, dest_root=tmp_path,
    )

    assert [c.path for c in fake.calls] == ["data/final/"]


def test_a_failure_partway_through_all_names_what_landed(tmp_path: Path) -> None:
    """`import_product` rolls nothing back. It must at least say which outputs
    are already on disk, rather than exiting on a half-written import the user
    cannot see."""
    from mintd._dvc_ops import DvcOpError

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_outputs("outputs/a.csv", "outputs/b.csv"))
    fake = _FakeDvcOps()
    reporter = _RecordingReporter()
    real_import = fake.import_

    def fail_on_second(**kw: Any) -> Path:
        if kw["path"] == "outputs/b.csv":
            raise DvcOpError("dvc import failed (exit 1)")
        return real_import(**kw)

    fake.import_ = fail_on_second  # type: ignore[method-assign]

    with pytest.raises(DvcOpError):
        import_product(
            client, fake, "provider_xw", all_outputs=True, cwd=tmp_path,
            dest_root=tmp_path, reporter=reporter,
        )

    assert any("1 of 2 outputs were imported" in m for m in reporter.warns)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupted_import_still_names_what_landed(
    tmp_path: Path, interrupt: type[BaseException]
) -> None:
    """Ctrl-C is not an `Exception`, and this is the one message that says
    which half of a multi-output import is already on disk.

    `run_streaming` re-raises `KeyboardInterrupt` unchanged (`_subprocess.py`,
    the `proc.wait` handler), so interrupting the second of two `dvc import`
    calls walked straight past the warning: the first output stayed on disk,
    `main` printed only "interrupted by user", and nothing named the file the
    user now has. Sibling of
    `test_data.py::test_an_interrupted_bump_leaves_the_payload_in_place` --
    the same interrupt-blind `except Exception`, in the other verb of the same
    module, reachable from `mintd data import <name> --all`.

    Mutation: `except BaseException` -> `except Exception` in `import_product`
    -> both cells redden on the missing warning.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_outputs("outputs/a.csv", "outputs/b.csv"))
    fake = _FakeDvcOps()
    reporter = _RecordingReporter()
    real_import = fake.import_

    def interrupt_on_second(**kw: Any) -> Path:
        if kw["path"] == "outputs/b.csv":
            raise interrupt()
        return real_import(**kw)

    fake.import_ = interrupt_on_second  # type: ignore[method-assign]

    with pytest.raises(interrupt):
        import_product(
            client, fake, "provider_xw", all_outputs=True, cwd=tmp_path,
            dest_root=tmp_path, reporter=reporter,
        )

    assert any("1 of 2 outputs were imported" in m for m in reporter.warns), (
        "an interrupted import said nothing about the output already on disk"
    )


class _RecordingReporter:
    """Only the three surfaces `import_product` reaches; `status` must be a
    context manager because the import loop runs inside it."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def status(self, msg: str) -> Any:
        return contextlib.nullcontext()

    def update_status(self, msg: str) -> None:
        pass


def _producer_bytes(
    *,
    primary: str | None = "outputs/at_rev.parquet",
    outputs: list[dict[str, Any]] | None = None,
) -> bytes:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"]["primary"] = primary
    if outputs is not None:
        data["data_products"]["outputs"] = outputs
    return json.dumps(data).encode()


def test_import_product_rev_without_path_resolves_via_producer_view(
    tmp_path: Path,
) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/from_catalog.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = StaticFetcher(
        {(repo_url, "abc123"): _producer_bytes(primary="outputs/at_rev.parquet")}
    )

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    import_product(
        client,
        fake,
        "provider_xw",
        rev="abc123",
        cwd=tmp_path, dest_root=tmp_path,
        producer_view_factory=factory,
    )

    assert fake.calls[0].path == "outputs/at_rev.parquet"
    assert fake.calls[0].rev == "abc123"
    assert fake.calls[0].repo_url == repo_url


def test_import_product_propagates_producer_error(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = ErroringFetcher(FetchError.pin_missing(repo_url, "abc123"))

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(ProducerError) as ei:
        import_product(
            client,
            fake,
            "provider_xw",
            rev="abc123",
            cwd=tmp_path, dest_root=tmp_path,
            producer_view_factory=factory,
        )

    assert ei.value.reason == ProducerError.Reason.PIN_MISSING
    assert fake.calls == []


def test_import_product_rev_without_path_no_primary_raises(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    repo_url = "https://github.com/example-org/provider_xw"
    fetcher = StaticFetcher({(repo_url, "abc123"): _producer_bytes(primary=None)})

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(MissingPrimaryDataProduct):
        import_product(
            client,
            fake,
            "provider_xw",
            rev="abc123",
            cwd=tmp_path, dest_root=tmp_path,
            producer_view_factory=factory,
        )


def test_import_product_default_factory_is_producer_view_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    captured: list[tuple[str, str]] = []

    def stub(repo: str, pin: str) -> Any:
        captured.append((repo, pin))
        return SimpleNamespace(primary_or_raise=lambda: "outputs/from_stub.parquet")

    monkeypatch.setattr("mintd.data.ProducerView.at", stub)

    import_product(client, fake, "provider_xw", rev="abc123", cwd=tmp_path, dest_root=tmp_path)

    assert captured == [("https://github.com/example-org/provider_xw", "abc123")]
    assert fake.calls[0].path == "outputs/from_stub.parquet"


def test_import_product_rev_with_path_passes_through(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    def factory_must_not_run(r: str, p: str) -> ProducerView:
        pytest.fail("factory must not be called when --path is provided")

    import_product(
        client,
        fake,
        "provider_xw",
        path="outputs/x.csv",
        rev="abc123",
        cwd=tmp_path, dest_root=tmp_path,
        producer_view_factory=factory_must_not_run,
    )

    assert fake.calls[0].rev == "abc123"


def test_import_product_missing_primary_raises(tmp_path: Path) -> None:
    # Slice 32 fixture switched to publish-valid (with primary); clear
    # it explicitly here so this test exercises the missing-primary path.
    def _clear_primary(d):
        d["data_products"]["primary"] = None
        d["data_products"]["outputs"] = []
    client = InMemoryCatalogClient()
    _register(client, mutate=_clear_primary)
    fake = _FakeDvcOps()

    with pytest.raises(MissingPrimaryDataProduct):
        import_product(client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path)


def test_import_product_unknown_name_raises(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    fake = _FakeDvcOps()

    with pytest.raises(CatalogNotFound):
        import_product(client, fake, "nope", cwd=tmp_path, dest_root=tmp_path)


def test_import_product_returns_produced_dvc_files(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path
    )

    assert produced == [tmp_path / "data_provider_xw" / "outputs" / "main.parquet.dvc"]
    assert produced[0].exists()


def test_import_product_refuses_existing_dvc(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "data_provider_xw" / "outputs").mkdir(parents=True)
    (tmp_path / "data_provider_xw" / "outputs" / "main.parquet.dvc").write_text("preexisting")

    with pytest.raises(ImportDestinationExists):
        import_product(client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path)
    assert fake.calls == []


def test_import_product_force_overwrites(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "data_provider_xw" / "outputs").mkdir(parents=True)
    (tmp_path / "data_provider_xw" / "outputs" / "main.parquet.dvc").write_text("preexisting")

    produced = import_product(
        client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path, force=True
    )

    assert len(produced) == 1
    assert fake.calls[0].force is True


def test_import_product_trailing_slash_in_path(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client)
    fake = _FakeDvcOps()

    import_product(
        client,
        fake,
        "provider_xw",
        path="outputs/cms_based/",
        cwd=tmp_path, dest_root=tmp_path,
    )

    assert fake.calls[0].dest == tmp_path / "data_provider_xw" / "outputs" / "cms_based"


def test_import_product_creates_dest_parent_when_missing(tmp_path: Path) -> None:
    """Regression: dvc import requires the destination's parent directory
    to exist (it doesn't auto-create). A fresh consumer project running
    `mintd data import <name>` against the default `data/imports/` dest
    root previously failed with the cryptic 'stage working dir ... does
    not exist'. import_product now creates dest.parent up-front.

    Also asserts the slice-38 producer-namespacing: dest is nested under
    `<dest_root>/<full_name>/` so multiple imports don't collide on
    shared output names."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    nested_dest = tmp_path / "data" / "imports"
    assert not nested_dest.exists()

    import_product(client, fake, "provider_xw", cwd=tmp_path, dest_root=nested_dest)

    # Both dest_root and the per-producer namespace dir get auto-created.
    assert (nested_dest / "data_provider_xw").is_dir()
    assert fake.calls[0].dest == nested_dest / "data_provider_xw" / "outputs" / "main.parquet"


# ---------------------------------------------------------------------------
# issue09 fix 3 — force clears an existing directory destination
# ---------------------------------------------------------------------------


def test_import_product_force_clears_an_existing_directory_dest(
    tmp_path: Path,
) -> None:
    """`dvc import -o <existing-dir>` nests the source inside the directory
    and then refuses the overlap; 14 of 21 catalog products publish a
    directory, so a force re-import must clear the old payload first. The
    fake raises on an existing dir dest (mirroring real dvc), so this test
    reddens if the rmtree is dropped (M14c)."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_provider_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "stale.csv").write_text("old payload")
    (dest.parent / "cms_based.dvc").write_text("preexisting")

    produced = import_product(
        client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path, force=True
    )

    assert len(fake.calls) == 1
    assert not (dest / "stale.csv").exists()
    assert produced == [dest.parent / "cms_based.dvc"]


@pytest.mark.parametrize("shape", ["dir", "file", "symlink"])
def test_a_failed_forced_import_restores_the_payload(
    tmp_path: Path, shape: str
) -> None:
    """D16: `--force` means "overwrite the destination", not "destroy it and
    leave me nothing if the network drops halfway".

    The forced clear was a bare `shutil.rmtree(dest)` — no rename-aside, no
    restore — so an import that died mid-transfer took the payload with it.
    That is the defect `bump_import` had already fixed one function away, and
    the standing rule is that when two paths do the same job the weaker one
    sets the posture. Both now share `_payload_backup`.

    Parametrised over every shape because the old branch was narrow as well
    (`dest.is_dir() and not dest.is_symlink()`): a file or a symlinked
    destination was never cleared, so it was never backed up either.

    The fake writes a corpse at `dest` before dying, which is what makes these
    cells real: with a fake that raises before touching disk, "restored" and
    "never moved" are indistinguishable and all three pass against a helper
    that does nothing at all — including one with `engage` re-narrowed to the
    pre-D15 dir-only guard, which is exactly the change this test exists to
    pin. That mutation survived the entire suite before the fake was fixed.
    """
    from mintd._dvc_ops import DvcOpError

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))

    class _PartialThenFailDvcOps(_FakeDvcOps):
        """dvc that writes PART of the destination and then dies.

        Raising before touching disk (the obvious fake) makes "restored from
        the backup" and "never moved at all" observationally identical, so
        every assertion below would be satisfied by inaction and the test
        would pass against a helper that does nothing. Writing a corpse at
        `dest` first is the only state in which restore differs from no-op.
        """

        def import_(self, **kwargs: Any) -> Path:
            corpse: Path = kwargs["dest"]
            corpse.mkdir(parents=True, exist_ok=True)
            (corpse / "half.csv").write_text("truncated\n", encoding="utf-8")
            raise DvcOpError("dvc import failed (exit 1): connection reset by peer")

    dest = tmp_path / "data_provider_xw" / "outputs" / "cms_based"
    dest.parent.mkdir(parents=True)
    # The pointer beside it is what makes this destination mintd's to move.
    (dest.parent / "cms_based.dvc").write_text("preexisting")

    if shape == "dir":
        dest.mkdir()
        (dest / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")
    elif shape == "file":
        dest.write_text("keep me\n", encoding="utf-8")
    else:
        # Inside the import root on purpose: `dest.resolve()` in the
        # containment check FOLLOWS an existing symlink, so a link pointing
        # anywhere outside is refused as an escaping path before the backup is
        # ever reached. That is pre-existing and out of scope here; see the
        # `_payload_backup` follow-up in notes/BACKLOG.md.
        target = dest.parent / "dvc-cache-payload"
        target.mkdir()
        (target / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")
        dest.symlink_to(target)

    with pytest.raises(DvcOpError):
        import_product(
            client, _PartialThenFailDvcOps(), "provider_xw", cwd=tmp_path,
            dest_root=tmp_path, force=True,
        )

    if shape == "file":
        assert dest.read_text(encoding="utf-8") == "keep me\n"
    else:
        assert (dest / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n"
    if shape == "symlink":
        assert dest.is_symlink(), "restored as a copy, not as the link dvc wrote"
    # The payload is back under its own name, not stranded in the backup.
    assert not list(dest.parent.glob("*.mintd-bump-backup"))


def test_a_plain_import_leaves_a_stray_backup_alone(tmp_path: Path) -> None:
    """The refusal and the success-path clear are gated on each other.

    `bump` can only ever pass `engage=True` (it derives `dest` from the `.dvc`
    it is rewriting), so nothing exercised `_payload_backup` with `engage`
    False until `import` became the second caller. On that path the helper
    moves nothing and deletes nothing, so a `.mintd-bump-backup` beside the
    destination is none of its business.

    Ungating either half alone is a bug, in opposite directions: an ungated
    REFUSAL aborts plain imports that worked before, with a message asserting
    an import was interrupted where none ever ran; an ungated success-path
    CLEAR silently deletes a backup the helper never created — the exact
    destroy D14 was written to stop. This test is the tripwire for the second,
    which is the dangerous one because it exits 0.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_provider_xw" / "outputs" / "cms_based"
    dest.parent.mkdir(parents=True)
    # No `.dvc` and no `dest`: nothing here is mintd's to move. The stray is a
    # remnant of a killed bump against an older layout.
    stray = dest.with_name(dest.name + ".mintd-bump-backup")
    stray.mkdir()
    (stray / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    import_product(client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path)

    assert (stray / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n"


def test_a_forced_import_refuses_a_stale_backup_instead_of_destroying_it(
    tmp_path: Path,
) -> None:
    """D14 on the import arm, reachable for the first time since the two verbs
    share the backup. `--force` after a hard kill must refuse, not clear."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_provider_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "partial.csv").write_text("half\n", encoding="utf-8")
    (dest.parent / "cms_based.dvc").write_text("preexisting")
    backup = dest.with_name(dest.name + ".mintd-bump-backup")
    backup.mkdir()
    (backup / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(StaleBackupExists) as excinfo:
        import_product(
            client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path,
            force=True,
        )

    assert str(backup) in str(excinfo.value)
    assert (backup / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n"
    assert not fake.calls, "refused after already asking dvc to import"


def test_import_product_force_never_destroys_a_stray_directory(
    tmp_path: Path,
) -> None:
    """The rmtree is guarded on the `.dvc` existing too: a directory at the
    destination that mintd did NOT import (no pointer beside it) is user
    data and must survive — the import fails instead."""
    from mintd._dvc_ops import DvcImportDestinationExists

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_provider_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "precious.csv").write_text("not mintd's to delete")

    with pytest.raises(DvcImportDestinationExists):
        import_product(
            client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path,
            force=True,
        )

    assert (dest / "precious.csv").read_text() == "not mintd's to delete"


@pytest.mark.parametrize(
    "escaping_path",
    [
        "../../../../elsewhere/raw/",
        "/tmp/absolute/raw/",
        "outputs/../../../../elsewhere/raw/",
    ],
    ids=["dotdot", "absolute", "dotdot-mid-path"],
)
def test_import_refuses_an_output_path_that_escapes_the_destination(
    tmp_path: Path, escaping_path: str
) -> None:
    """D-A dropped the basename clamp, which was the only thing normalizing a
    producer-controlled path. `outputs[].path` is a bare `str` on the model
    and `_validate_requested_targets` never runs for import, so `..` escaped
    the project and pathlib discards the whole left operand for an absolute
    path — with the force-path `rmtree` following it out.

    Mutation: drop the containment check -> these reddens.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary(escaping_path))
    fake = _FakeDvcOps()

    with pytest.raises(UnknownProductPath):
        import_product(
            client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path
        )

    assert fake.calls == [], "refused before any dvc invocation"


def test_import_never_deletes_outside_the_destination_on_force(
    tmp_path: Path,
) -> None:
    """The harm the containment check exists to prevent: `--force` rmtree's
    the dest, so an escaping path deletes a directory outside the project
    entirely. Assert on the victim, not on the exception."""
    victim = tmp_path / "elsewhere" / "raw"
    victim.mkdir(parents=True)
    (victim / "irreplaceable.csv").write_text("years of work")

    project = tmp_path / "consumer"
    project.mkdir()
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("../../../../elsewhere/raw/"))

    with pytest.raises(UnknownProductPath):
        import_product(
            client, _FakeDvcOps(), "provider_xw",
            cwd=project, dest_root=project / "data" / "imports", force=True,
        )

    assert (victim / "irreplaceable.csv").read_text() == "years of work"
    assert victim.is_dir()


def _stage_legacy_import(ns: Path, *, producer_path: str, leaf: str) -> Path:
    """An import written by the pre-layout-change writer: the pointer sits
    directly under the namespace folder, while recording a nested producer
    path. Every real import on a consumer machine predates the change, so
    this is the shape the writer must recognise."""
    ns.mkdir(parents=True, exist_ok=True)
    dvc = ns / f"{leaf}.dvc"
    dvc.write_text(
        "outs:\n"
        "  - md5: e8f3a2b1c4d5e6f7a8b9c0d1e2f3a4b5\n"
        "    size: 1\n"
        f"    path: {leaf}\n"
        "deps:\n"
        f"  - path: {producer_path}\n"
        "    repo:\n"
        "      url: https://github.com/example-org/provider_xw\n"
        "      rev: main\n"
        '      rev_lock: "' + "a" * 40 + '"\n'
    )
    return dvc


def test_reimport_of_a_legacy_import_is_refused_not_duplicated(
    tmp_path: Path,
) -> None:
    """The `ImportDestinationExists` guard checked the NEW mirrored location,
    so a legacy pointer was invisible to it: a plain re-import wrote a second
    pointer for the same output (and, with real dvc, a second copy of the
    payload), after which every `--bump` for that product died on
    `AmbiguousImport`.

    Mutation: drop the `_imports_index` lookup -> the re-import succeeds and
    this reddens.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    ns = tmp_path / "data_provider_xw"
    legacy = _stage_legacy_import(ns, producer_path="outputs/main.parquet", leaf="main.parquet")

    with pytest.raises(ImportDestinationExists) as ei:
        import_product(client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path)

    assert "main.parquet.dvc" in str(ei.value)
    assert fake.calls == []
    assert sorted(p.name for p in ns.rglob("*.dvc")) == ["main.parquet.dvc"]
    assert legacy.exists()


def test_force_reimport_of_a_legacy_import_rewrites_it_in_place(
    tmp_path: Path,
) -> None:
    """The layout change moves nothing on disk, so a forced re-import of a
    legacy import must rewrite THAT pointer rather than leave it orphaned
    beside a new mirrored sibling — the same in-place rule `--bump` follows.

    Mutation: drop the `target_dvc = existing` reassignment -> two pointers.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    ns = tmp_path / "data_provider_xw"
    _stage_legacy_import(ns, producer_path="outputs/main.parquet", leaf="main.parquet")

    produced = import_product(
        client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path, force=True
    )

    assert [p.name for p in ns.rglob("*.dvc")] == ["main.parquet.dvc"]
    assert produced == [ns / "main.parquet.dvc"]
    assert fake.calls[0].dest == ns / "main.parquet"


def _with_full_name(full_name: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(d: dict[str, Any]) -> None:
        d["project"]["full_name"] = full_name

    return mutate


@pytest.mark.parametrize(
    "namespace",
    [".", "..", "/tmp/abs", "../scratch", "a/b", "..\\win", "data_a/"],
    ids=["dot", "dotdot", "absolute", "escaping", "nested", "backslash", "trailing-slash"],
)
def test_import_refuses_a_namespace_that_is_not_one_folder(
    tmp_path: Path, namespace: str
) -> None:
    """`full_name` names the `data/imports/` folder and is producer-supplied
    (a bare `str` on the model, no validators). The containment check cannot
    cover it: `.` makes `nested_root == dest_root`, which IS inside the
    import root. So the namespace must be one plain path component — the
    same rule `data clone` already applies to a product name.

    Matched on the message, not just the type: `..` and an absolute
    namespace are ALSO caught downstream by the containment check, so a rule
    that let them through would still raise `UnknownProductPath` — with the
    wrong diagnosis here, and (on `--bump`, which has no containment check)
    no guard at all.

    Mutations: accept `..` / accept `.` / accept absolute / accept
    multi-component -> the matching case reddens.
    """
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_full_name(namespace))
    fake = _FakeDvcOps()

    with pytest.raises(UnknownProductPath, match="single folder name"):
        import_product(
            client, fake, "provider_xw", cwd=tmp_path, dest_root=tmp_path / "imports"
        )

    assert fake.calls == [], "refused before any dvc invocation"


def test_import_never_clobbers_another_products_import(tmp_path: Path) -> None:
    """The harm the namespace rules exist to prevent, via the vector the
    ONE-COMPONENT rule does not see: provider_b's entry declares provider_a's
    `full_name` verbatim. Both namespaces are perfectly legal single
    components, so shape says yes; `_imports_index` then hands back
    provider_a's `.dvc`, the force path `shutil.rmtree`s provider_a's payload
    and the import rewrites provider_a's pointer to provider_b — exit 0,
    "✓ imported".

    Filesystem-independent on purpose. The same aliasing arrives on macOS and
    Windows as a mere recasing (`DATA_PROVIDER_A`) — GitHub renames are
    case-preserving but case-insensitive, and so are APFS and NTFS — and via
    a symlinked namespace folder. A duplicated `full_name` reproduces it on
    every filesystem, including Linux CI.

    Asserts on the victim, not on an exception type: a rule that raises the
    wrong class still passes here, a rule that lets the delete through does
    not.

    Mutation: drop the `_require_owner` call from `import_product` -> this
    reddens.
    """
    client = InMemoryCatalogClient()
    _register(client, "provider_a", mutate=_with_primary("data/final/"))

    def impostor(d: dict[str, Any]) -> None:
        d["data_products"]["primary"] = "data/final/"
        d["project"]["full_name"] = "data_provider_a"  # provider_a's namespace

    _register(client, "provider_b", mutate=impostor)
    dest_root = tmp_path / "data" / "imports"

    a_dvc = import_product(
        client, _FakeDvcOps(), "provider_a", cwd=tmp_path, dest_root=dest_root
    )[0]
    payload = a_dvc.with_suffix("")
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "irreplaceable.csv").write_text("years of work")

    with contextlib.suppress(Exception):
        import_product(
            client, _FakeDvcOps(), "provider_b",
            cwd=tmp_path, dest_root=dest_root, force=True,
        )

    assert (payload / "irreplaceable.csv").is_file(), "provider_a's payload was deleted"
    assert "example-org/provider_a" in a_dvc.read_text(), (
        "provider_a's .dvc now names another producer"
    )


def test_import_never_clobbers_a_sibling_namespace(tmp_path: Path) -> None:
    """Third vector of the same clobber: namespace clean, ownership clean,
    but the producer's OUTPUT path walks out of the namespace.
    `../data_provider_a/data/final/` resolves INSIDE `dest_root` — so a
    `dest_root`-anchored containment check passes — and lands on another
    product's `.dvc`, whose payload the force path rmtree's. It never reaches
    `_require_owner` either: the index only scans provider_b's OWN namespace,
    where provider_a's pointer does not appear. The namespace rule is what
    makes the tighter `nested_root` anchor safe to use.

    Asserts on the victim, not on an exception type.

    Mutation: anchor the containment check on `dest_root` -> this reddens.
    """
    client = InMemoryCatalogClient()
    _register(client, "provider_a", mutate=_with_primary("data/final/"))
    _register(
        client, "provider_b",
        mutate=_with_primary("../data_provider_a/data/final/"),
    )
    dest_root = tmp_path / "data" / "imports"

    a_dvc = import_product(
        client, _FakeDvcOps(), "provider_a", cwd=tmp_path, dest_root=dest_root
    )[0]
    payload = a_dvc.with_suffix("")
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "irreplaceable.csv").write_text("years of work")
    # provider_b's own namespace exists as soon as anything of theirs has
    # been imported; without it the `..` walk dies on a missing intermediate.
    (dest_root / "data_provider_b").mkdir(parents=True, exist_ok=True)

    with contextlib.suppress(Exception):
        import_product(
            client, _FakeDvcOps(), "provider_b",
            cwd=tmp_path, dest_root=dest_root, force=True,
        )

    assert (payload / "irreplaceable.csv").is_file(), "provider_a's payload was deleted"
    assert "example-org/provider_a" in a_dvc.read_text(), (
        "provider_a's .dvc now names another producer"
    )


def _raw_client(payload: dict[str, Any]) -> SimpleNamespace:
    """Serves an entry exactly as the registry YAML deserializes it —
    `InMemoryCatalogClient.register` takes a strict `Metadata`, which is
    precisely the validation a v1-era registry entry never went through.
    `CatalogEntry` is `extra="allow"`, so every value below reaches `data.py`.
    """
    entry = CatalogEntry.model_validate(payload)
    return SimpleNamespace(fetch=lambda _name: entry)


_V1_BASE: dict[str, Any] = {
    "schema_version": "2.0",
    "project": {"name": "provider_a", "full_name": "data_provider_a", "type": "data"},
    "repository": {"github_url": "https://github.com/example-org/provider_a"},
    "data_products": {"primary": "data/final/"},
}


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"project": "provider_a"}, None),
        ({"data_products": {"primary": ["data/final/", "data/interim/"]}}, UnknownProductPath),
        ({"repository": "https://github.com/example-org/provider_a"}, ValueError),
        ({"data_products": "data/final/"}, MissingPrimaryDataProduct),
    ],
    ids=["project-scalar", "primary-list", "repository-scalar", "data_products-scalar"],
)
def test_import_reports_a_v1_shaped_entry_instead_of_tracebacking(
    tmp_path: Path, override: dict[str, Any], expected: type[Exception] | None
) -> None:
    """A registry entry whose blocks are the wrong TYPE must fail as a
    documented error, never as `AttributeError: 'str' object has no attribute
    'get'`.

    Not hypothetical: `metadata_migrate.py` documents v1 files where
    `primary` is a list, and an entry registered before the v2 shape landed
    sits in the registry as-is. `_section` is what makes the block readers
    survive it; `project` as a scalar is a *degrade*, not an error — the
    namespace simply falls back to the product name, which is what `or name`
    always meant.

    Mutation: revert any `_section(...)` call to `entry.get(...) or {}` ->
    the matching case reddens with an AttributeError instead.
    """
    fake = _FakeDvcOps()
    client = _raw_client({**_V1_BASE, **override})

    if expected is None:
        produced = import_product(
            client, fake, "provider_a", cwd=tmp_path, dest_root=tmp_path
        )
        # namespace fell back to the product name; nothing crashed
        assert produced == [tmp_path / "provider_a" / "data" / "final.dvc"]
        return

    with pytest.raises(expected):
        import_product(client, fake, "provider_a", cwd=tmp_path, dest_root=tmp_path)
    assert fake.calls == []
