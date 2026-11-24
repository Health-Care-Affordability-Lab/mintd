"""Tests for `import_product` orchestration."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from mintd.catalog import CatalogEntry, CatalogNotFound, InMemoryCatalogClient
from mintd.data import (
    StaleBackupExists,
    _tracked_output_targets,
    _validate_requested_targets,
    ImportDestinationExists,
    MissingPrimaryDataProduct,
    NoTrackedOutputs,
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
    name: str = "demo_xw",
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
        client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path
    )

    assert len(produced) == 1
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call.path == "outputs/main.parquet"
    assert call.repo_url == "https://github.com/example-org/demo_xw"
    # Slice 38: dest is namespaced by the producer's full_name so
    # multiple imports into the same dest_root don't collide.
    assert call.dest == tmp_path / "data_demo_xw" / "outputs" / "main.parquet"


def test_import_product_path_override(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()

    import_product(
        client, fake, "demo_xw", path="outputs/other.csv", cwd=tmp_path, dest_root=tmp_path
    )

    assert fake.calls[0].path == "outputs/other.csv"
    assert fake.calls[0].dest == tmp_path / "data_demo_xw" / "outputs" / "other.csv"


def test_import_product_all_outputs_loops(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(
        client,
        mutate=_with_outputs("outputs/a.csv", "outputs/b.csv", "outputs/c.csv"),
    )
    fake = _FakeDvcOps()

    produced = import_product(
        client, fake, "demo_xw", all_outputs=True, cwd=tmp_path, dest_root=tmp_path
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
        client, fake, "demo_xw", all_outputs=True, cwd=tmp_path,
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
        client, fake, "demo_xw", path=["data/final/b.csv", "data/final/"],
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
            client, fake, "demo_xw", all_outputs=True, cwd=tmp_path,
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
            client, fake, "demo_xw", all_outputs=True, cwd=tmp_path,
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
    repo_url = "https://github.com/example-org/demo_xw"
    fetcher = StaticFetcher(
        {(repo_url, "abc123"): _producer_bytes(primary="outputs/at_rev.parquet")}
    )

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    import_product(
        client,
        fake,
        "demo_xw",
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
    repo_url = "https://github.com/example-org/demo_xw"
    fetcher = ErroringFetcher(FetchError.pin_missing(repo_url, "abc123"))

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(ProducerError) as ei:
        import_product(
            client,
            fake,
            "demo_xw",
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
    repo_url = "https://github.com/example-org/demo_xw"
    fetcher = StaticFetcher({(repo_url, "abc123"): _producer_bytes(primary=None)})

    def factory(r: str, p: str) -> ProducerView:
        return ProducerView.at(r, p, fetcher=fetcher, cache_dir=tmp_path / "cache")

    with pytest.raises(MissingPrimaryDataProduct):
        import_product(
            client,
            fake,
            "demo_xw",
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

    import_product(client, fake, "demo_xw", rev="abc123", cwd=tmp_path, dest_root=tmp_path)

    assert captured == [("https://github.com/example-org/demo_xw", "abc123")]
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
        "demo_xw",
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
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)


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
        client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path
    )

    assert produced == [tmp_path / "data_demo_xw" / "outputs" / "main.parquet.dvc"]
    assert produced[0].exists()


def test_import_product_refuses_existing_dvc(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "data_demo_xw" / "outputs").mkdir(parents=True)
    (tmp_path / "data_demo_xw" / "outputs" / "main.parquet.dvc").write_text("preexisting")

    with pytest.raises(ImportDestinationExists):
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert fake.calls == []


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
@pytest.mark.parametrize("via", ["root", "dotdot"])
def test_an_import_over_a_folder_no_pointer_records_is_refused_naming_it(
    tmp_path: Path, via: str, force: bool,
) -> None:
    """A folder already at the destination with no pointer beside it (a stray
    copy, or the files a failed import left) made dvc import into it, a level
    down, and the landed check counted what was there: exit 0, data nested.
    It is refused before dvc runs, naming the folder and its files, with or
    without `force` (which never clears a folder no pointer records), and with
    a `..` in the dest root, which reads as absent while `foo` is missing.
    Mutation: drop the refusal -> red, dvc runs."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    (dest / "sub").mkdir(parents=True)
    (dest / "sub" / "keep.txt").write_text("mine\n")

    with pytest.raises(ImportDestinationExists, match=r"^data_demo_xw/outputs/cms_based is already a folder and no "
                                                      r"pointer records it \(it holds data_demo_xw/outputs/cms_based/"
                                                      r"sub/keep\.txt\), so dvc would import into it") as refused:
        import_product(client, fake, "demo_xw", cwd=tmp_path, force=force,
                       dest_root=tmp_path / "foo" / ".." if via == "dotdot" else tmp_path)

    assert refused.value.hint and "--dest-root" in refused.value.hint
    assert fake.calls == []
    assert [p.relative_to(dest).as_posix() for p in sorted(dest.rglob("*"))] == ["sub", "sub/keep.txt"]


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
def test_an_import_over_a_file_no_pointer_records_is_refused_and_the_file_kept(tmp_path: Path, force: bool) -> None:
    """`--force` over a plain file at the destination, with no pointer, used to
    replace it and exit 0: the user's file gone. Refused, plain or forced.
    Mutation: refuse only a folder (`is_dir()`) -> the force case loses the file, red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "main.parquet"
    dest.parent.mkdir(parents=True)
    dest.write_text("mine\n")
    with pytest.raises(ImportDestinationExists, match=r"main\.parquet is already a file and no pointer records it"):
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path, force=force)
    assert dest.read_text() == "mine\n" and fake.calls == []


def test_an_empty_folder_is_refused_with_the_command_that_removes_it(tmp_path: Path) -> None:
    """dvc imports into an empty folder too, a level down. Mutation: let an empty folder through -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    (tmp_path / "data_demo_xw" / "outputs" / "cms_based").mkdir(parents=True)
    with pytest.raises(ImportDestinationExists, match=r"cms_based is already a folder, an empty one") as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert refused.value.hint == "it is empty and safe to remove: rmdir -- data_demo_xw/outputs/cms_based"


def test_a_folder_holding_earlier_imports_names_them_not_a_stray(tmp_path: Path) -> None:
    """A file imported with --path earlier leaves its pointer inside the folder a
    whole-folder import lands in. Name it, and don't call the folder unrecorded
    or say to remove it. Mutation: ignore the pointers it holds -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    import_product(client, fake, "demo_xw", path="outputs/cms_based/a.csv", cwd=tmp_path, dest_root=tmp_path)
    with pytest.raises(ImportDestinationExists, match=r"cms_based already holds imports "
                                                      r"\(data_demo_xw/outputs/cms_based/a\.csv\.dvc\)") as refused:
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "no pointer records" not in str(refused.value)
    assert refused.value.hint == "import somewhere else with --dest-root, or remove those imports first"


def test_every_destination_is_checked_before_the_first_dvc_call(tmp_path: Path) -> None:
    """A stray folder at the second output of --all used to stop the import
    after the first had landed. Mutation: check each output as it is imported -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_outputs("outputs/a/", "outputs/b/"))
    fake = _FakeDvcOps()
    stray = tmp_path / "data_demo_xw" / "outputs" / "b"
    stray.mkdir(parents=True)
    (stray / "mine.txt").write_text("x\n")
    with pytest.raises(ImportDestinationExists, match=r"outputs/b is already a folder"):
        import_product(client, fake, "demo_xw", all_outputs=True, cwd=tmp_path, dest_root=tmp_path)
    assert fake.calls == [] and not (tmp_path / "data_demo_xw" / "outputs" / "a").exists()


def test_a_pointer_dvc_did_not_write_where_it_belongs_is_never_a_success(tmp_path: Path) -> None:
    """A folder that appears at the destination while dvc runs takes the import,
    a level down. The pointer is checked where it belongs after dvc, so that is
    reported, never a success. Mutation: drop the check -> it returns, red."""

    class _FolderAppears(_FakeDvcOps):
        def import_(self, *, dest: Path, **kw: Any) -> Path:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "late.txt").write_text("x\n")
            return super().import_(dest=dest, **kw)

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    with pytest.raises(ImportDestinationExists, match=r"dvc wrote no pointer at data_demo_xw/outputs/cms_based\.dvc") as nested:
        import_product(client, _FolderAppears(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "now holds" not in str(nested.value), "late.txt is not this import's to remove"
    assert "data_demo_xw/outputs/cms_based/cms_based.dvc" in nested.value.hint


def test_a_failed_import_names_what_it_left_and_the_rerun_says_whose_it_is(tmp_path: Path) -> None:
    """A failed fresh import keeps the files that landed (it never deletes a
    file), and its own error used to say to re-run, which is then refused. Its
    error names the files and the command that clears them; the refusal says
    they may be what a failed import left.
    Mutations: drop the note -> the first assertion red; drop that case from
    the refusal's hint -> the last red."""
    from mintd._dvc_ops import DvcImportStorageMissing

    class _PartWay(_FakeDvcOps):
        def import_(self, *, dest: Path, **kw: Any) -> Path:
            dest.mkdir()
            (dest / "a.csv").write_text("landed\n")
            raise DvcImportStorageMissing("dvc import failed (exit 1): No storage files available: data/b.csv")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    with pytest.raises(DvcImportStorageMissing) as failed:
        import_product(client, _PartWay(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert str(failed.value).endswith(
        "; the folder data_demo_xw/outputs/cms_based now holds data_demo_xw/outputs/cms_based/a.csv"
    )
    assert failed.value.hint.endswith(  # in order: tell the producer, clear the folder, re-run
        "that is the producer's to fix: send them this error, then clear the folder (if this is a folder this import "
        "left, not yours: rm -r -- data_demo_xw/outputs/cms_based; otherwise move that folder), then re-run the import"
    )
    assert DvcImportStorageMissing.hint.endswith("send them this error, then re-run the import")  # per instance

    with pytest.raises(ImportDestinationExists) as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert refused.value.hint.startswith(
        "if this is a folder an earlier mintd data import left, not yours: "
        "rm -r -- data_demo_xw/outputs/cms_based; otherwise move that folder"
    )


def test_a_dangling_link_at_the_destination_is_refused(tmp_path: Path) -> None:
    """Mutation: drop the `is_symlink()` half of the check -> dvc writes through the link, red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.parent.mkdir(parents=True)
    gone = dest.parent / "gone"  # inside the namespace: outside, the containment check refuses it first
    dest.symlink_to(gone)
    with pytest.raises(ImportDestinationExists, match=r"cms_based is already a link and no pointer records it"):
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert fake.calls == [] and not gone.exists()


def test_a_destination_inside_an_earlier_import_names_that_import(tmp_path: Path) -> None:
    """`outputs/` imported whole, then `--path outputs/cms_based`: the folder is
    that import's, not unrecorded, and not "safe to remove".
    Mutation: look only for pointers inside the destination -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/"))
    fake = _FakeDvcOps()
    import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    (tmp_path / "data_demo_xw" / "outputs" / "cms_based").mkdir()
    with pytest.raises(ImportDestinationExists, match=r"cms_based is inside an earlier import "
                                                      r"\(data_demo_xw/outputs\.dvc\)") as refused:
        import_product(client, fake, "demo_xw", path="outputs/cms_based/", cwd=tmp_path, dest_root=tmp_path)
    assert "safe to remove" not in str(refused.value) + refused.value.hint


@pytest.mark.parametrize("held", ["an empty folder", "a link to a folder"])
def test_a_folder_holding_no_files_is_not_called_safe_to_remove(tmp_path: Path, held: str) -> None:
    """A folder that isn't empty is not known safe to remove, whatever it
    holds: `os.walk` lists a link to a folder with the folders, so a folder
    holding only that was called "holding only empty folders ... safe to
    remove". Mutation: bring back that case -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    if held == "an empty folder":
        (dest / "sub").mkdir()
    else:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep.csv").write_text("not an import's\n")
        (dest / "link").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ImportDestinationExists, match=r"cms_based is already a folder and no pointer records it "
                                                      r"\(it holds folders but no files\)") as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "safe to remove" not in refused.value.hint
    assert refused.value.hint.startswith(
        "if this is a folder an earlier mintd data import left, not yours: rm -r -- data_demo_xw/outputs/cms_based;"
    )


def _failing_after(write: Callable[[Path], None], exc: BaseException) -> type[_FakeDvcOps]:
    class _Fails(_FakeDvcOps):
        def import_(self, *, dest: Path, **kw: Any) -> Path:
            write(dest)
            raise exc
    return _Fails


@pytest.mark.parametrize("error", ["locked", "unreachable", "path-not-found", "not-in-repo"])
def test_an_import_dvc_stopped_before_writing_names_nothing_to_remove(tmp_path: Path, error: str) -> None:
    """Someone else's file lands at the destination while dvc fails before
    writing anything (another import holds its lock, the producer is
    unreachable, the path or the repo is missing): the file is not this
    import's, so its error says nothing about removing it, and the cleanup
    leaves the folder alone, empty folders in it too.
    Mutations: drop `wrote_nothing` from the note's check -> "rm -r" on
    my_thesis.csv, red; from the cleanup's -> users_empty/ is removed, red."""
    from mintd._dvc_ops import DvcImportPathNotFound, DvcImportRepoUnreachable, DvcNotInRepoError, DvcOpError, DvcRepoLocked

    exc = {
        "locked": DvcRepoLocked("dvc import failed (exit 1): ERROR: Unable to acquire lock"),
        "unreachable": DvcImportRepoUnreachable("dvc import failed (exit 1): Failed to clone repo", "https://example.org/r"),
        "path-not-found": DvcImportPathNotFound("path 'outputs/cms_based/' not found at rev 'HEAD'"),
        "not-in-repo": DvcNotInRepoError("not inside a DVC repository while importing"),
    }[error]

    def theirs(dest: Path) -> None:
        (dest / "users_empty").mkdir(parents=True)
        (dest / "my_thesis.csv").write_text("someone else's\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    with pytest.raises(DvcOpError) as failed:
        import_product(client, _failing_after(theirs, exc)(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    said = f"{failed.value} {failed.value.hint}"
    assert "now holds" not in said and "rm " not in said and "my_thesis" not in said
    theirs_at = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    assert (theirs_at / "my_thesis.csv").is_file() and (theirs_at / "users_empty").is_dir()


def test_a_partial_failure_words_the_rm_as_an_if(tmp_path: Path) -> None:
    """dvc may have written before a failure like this, and another run may
    have: the files can't be told apart, so the rm is conditional, with the
    other way out. Mutation: drop the "if ..., not yours" from `_if_left` -> red."""
    from mintd._dvc_ops import DvcOpError

    def landed(dest: Path) -> None:
        dest.mkdir()
        (dest / "a.csv").write_text("landed\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fails = _failing_after(landed, DvcOpError("dvc import failed (exit 1): connection reset"))
    with pytest.raises(DvcOpError) as failed:
        import_product(client, fails(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert str(failed.value).endswith(
        "; the folder data_demo_xw/outputs/cms_based now holds data_demo_xw/outputs/cms_based/a.csv; before "
        "re-running, if this is a folder this import left, not yours: "
        "rm -r -- data_demo_xw/outputs/cms_based; otherwise move that folder"
    )


def test_another_runs_finished_import_through_a_dotdot_dest_root_is_not_named_as_left(tmp_path: Path) -> None:
    """The pointer check after a failure reads the pointer the way the refusal
    does. Through `foo/..` with `foo` (made for the import) gone again by then,
    the other run's pointer reads as absent unresolved.
    Mutation: `target_dvc.exists()` at the note -> "rm -r" on it, red."""
    from mintd._dvc_ops import DvcImportDestinationExists

    def theirs(dest: Path) -> None:
        dest.mkdir()
        (dest / "a.csv").write_text("the other run's\n")
        (dest.parent / "cms_based.dvc").write_text("the other run's pointer\n")
        if (tmp_path / "foo").is_dir():  # Windows folds `foo/..` without making `foo`
            (tmp_path / "foo").rmdir()

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    raced = DvcImportDestinationExists("dvc import failed (exit 1): output 'cms_based' already exists")
    with pytest.raises(DvcImportDestinationExists) as failed:
        import_product(client, _failing_after(theirs, raced)(), "demo_xw", cwd=tmp_path,
                       dest_root=tmp_path / "foo" / "..")
    assert not (tmp_path / "foo").exists()
    assert "now holds" not in str(failed.value) and "rm -r" not in str(failed.value)


def test_a_pointer_through_a_dotdot_dest_root_is_refused_without_force(tmp_path: Path) -> None:
    """Through `foo/..` with `foo` missing, a pointer at the destination reads
    as absent unresolved, and the import went ahead without --force.
    Mutation: `target_dvc.exists()` in the first refusal -> dvc called, red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest.parent / "cms_based.dvc").write_text("someone's pointer")
    with pytest.raises(ImportDestinationExists, match=r"cms_based\.dvc already exists; pass --force or remove it"):
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path / "foo" / "..")
    assert fake.calls == []


def test_a_link_at_the_destination_is_named_as_a_link_not_listed(tmp_path: Path) -> None:
    """`rm -r <link>` removes the link only, so listing the folder it points to
    under it reads as "delete these". The cleanup leaves the link and what is
    behind it alone, empty folders too.
    Mutations: list it as a folder -> red; walk the link's folder in the
    cleanup -> its empty folder is removed (on Windows, the link), red."""
    from mintd._dvc_ops import DvcOpError

    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "empty").mkdir(parents=True)
    (elsewhere / "keep.csv").write_text("not this import's\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fails = _failing_after(lambda dest: dest.symlink_to(elsewhere, target_is_directory=True),
                           DvcOpError("dvc import failed (exit 1): reset"))
    with pytest.raises(DvcOpError) as failed:
        import_product(client, fails(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert (tmp_path / "data_demo_xw" / "outputs" / "cms_based").is_symlink() and (elsewhere / "empty").is_dir()
    assert "keep.csv" not in str(failed.value)
    assert str(failed.value).endswith(
        "; the link data_demo_xw/outputs/cms_based is now there; before re-running, if this is a link this import "
        "left, not yours: rm -- data_demo_xw/outputs/cms_based; otherwise move that link"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows folds `..` before following a link, as Python does")
def test_a_dest_root_through_a_links_dotdot_names_the_folder_dvc_wrote_to(tmp_path: Path) -> None:
    """`link/..` is the folder above where `link` points, which is where dvc
    writes. Folded as text it is the project, where the user's own folder of
    the same name is. The failed import and its re-run used to list
    my_thesis.csv and give `rm -r` for the user's folder.
    Mutations: name or list the folder folded as text (`abspath`), in the note
    or in the refusal -> red."""
    from mintd._dvc_ops import DvcOpError

    project, far = tmp_path / "project", tmp_path / "far"
    (far / "sub").mkdir(parents=True)
    (project / "link").parent.mkdir()
    (project / "link").symlink_to(far / "sub", target_is_directory=True)
    mine = project / "data_demo_xw" / "outputs" / "cms_based"
    mine.mkdir(parents=True)
    (mine / "my_thesis.csv").write_text("the user's\n")
    wrote = Path(os.path.realpath(far)) / "data_demo_xw" / "outputs" / "cms_based"
    there = wrote.as_posix()

    def landed(dest: Path) -> None:
        dest.mkdir()
        (dest / "a.csv").write_text("landed\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fails = _failing_after(landed, DvcOpError("dvc import failed (exit 1): connection reset"))
    with pytest.raises(DvcOpError) as failed:
        import_product(client, fails(), "demo_xw", cwd=project, dest_root=project / "link" / "..")
    assert (wrote / "a.csv").is_file()
    assert "my_thesis" not in str(failed.value)
    assert str(failed.value).endswith(
        f"; the folder {there} now holds {there}/a.csv; before re-running, if this is a folder this import left, "
        f"not yours: {shlex.join(['rm', '-r', '--', there])}; otherwise move that folder"
    )

    with pytest.raises(ImportDestinationExists) as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=project, dest_root=project / "link" / "..")
    said = f"{refused.value} {refused.value.hint}"
    assert "my_thesis" not in said and f"(it holds {there}/a.csv)" in said
    assert shlex.join(["rm", "-r", "--", there]) in said
    assert (mine / "my_thesis.csv").is_file()


def test_a_link_to_a_folder_at_the_destination_is_refused_as_a_link(tmp_path: Path) -> None:
    """`rm -r <link>` removes the link only, so the refusal names a link as a
    link and lists nothing behind it. Mutation: check only `is_dir()` -> the
    folder's files are listed under "rm -r", red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    elsewhere = dest.parent / "elsewhere"  # inside the namespace: outside, the containment check refuses it first
    elsewhere.mkdir(parents=True)
    (elsewhere / "keep.csv").write_text("not an import's\n")
    dest.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ImportDestinationExists, match=r"cms_based is already a link and no pointer records it, "
                                                      r"so the import would write where it points") as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "keep.csv" not in f"{refused.value} {refused.value.hint}"


@pytest.mark.parametrize("name", [
    pytest.param("a\x1b[2J\nb", id="escape-and-newline",
                 marks=pytest.mark.skipif(sys.platform == "win32", reason="Windows names can't hold control characters")),
    pytest.param("a\u202eb", id="right-to-left-override"),
])
@pytest.mark.parametrize("held", ["a file", "nothing"])
def test_a_name_a_terminal_would_act_on_is_escaped_and_given_no_command(tmp_path: Path, name: str, held: str) -> None:
    """An escape in a path reached the terminal raw, where it can repaint the
    screen, and a newline split the hint's command in two. The name is shown
    escaped, and no command is offered for it: the escaped form names another
    path. Mutations: print the name as it is -> red; offer the command anyway -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary(f"outputs/{name}/"))
    stray = tmp_path / "data_demo_xw" / "outputs" / name
    stray.mkdir(parents=True)
    if held == "a file":
        (stray / "x.csv").write_text("x\n")
    with pytest.raises(ImportDestinationExists) as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    shown = name.encode("unicode_escape").decode("ascii")
    for text in (str(refused.value), refused.value.hint):
        assert not any(c in text for c in "\x1b\n\u202e"), text
    assert f"data_demo_xw/outputs/{shown}" in str(refused.value)
    assert "remove it by hand (its name has a character mintd won't print)" in refused.value.hint
    assert "rmdir" not in refused.value.hint and "rm -" not in refused.value.hint


def test_a_dest_root_that_reads_as_an_option_is_not_one_to_rm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--dest-root=-rf` gave "rm -r -rf/...", which rm reads as options.
    Mutation: drop the `--` from `_if_left` -> red."""
    monkeypatch.chdir(tmp_path)
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    stray = tmp_path / "-rf" / "data_demo_xw" / "outputs" / "cms_based"
    stray.mkdir(parents=True)
    (stray / "a.csv").write_text("x\n")
    with pytest.raises(ImportDestinationExists) as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=Path("-rf"))
    assert "not yours: rm -r -- -rf/data_demo_xw/outputs/cms_based;" in refused.value.hint


def test_another_runs_finished_import_is_not_named_as_left(tmp_path: Path) -> None:
    """A second run imported the same output while this one ran: its payload
    and pointer are there when this one fails, and are not this import's to
    remove. Mutation: drop the pointer condition -> "rm -r" on it, red."""
    from mintd._dvc_ops import DvcImportDestinationExists

    def theirs(dest: Path) -> None:
        dest.mkdir()
        (dest / "a.csv").write_text("the other run's\n")
        (dest.parent / "cms_based.dvc").write_text("the other run's pointer\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    raced = DvcImportDestinationExists("dvc import failed (exit 1): output 'cms_based' already exists")
    with pytest.raises(DvcImportDestinationExists) as failed:
        import_product(client, _failing_after(theirs, raced)(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "now holds" not in str(failed.value) and "rm -r" not in str(failed.value)


def test_an_interrupted_import_names_what_it_left_as_a_warning(tmp_path: Path) -> None:
    """Ctrl-C carries no hint or message to add to: the note is a warning.
    Mutation: drop the warning -> red."""
    def landed(dest: Path) -> None:
        dest.mkdir()
        (dest / "a.csv").write_text("landed\n")

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    reporter = _RecordingReporter()
    with pytest.raises(KeyboardInterrupt):
        import_product(client, _failing_after(landed, KeyboardInterrupt())(), "demo_xw", cwd=tmp_path,
                       dest_root=tmp_path, reporter=reporter)
    assert reporter.warns == ["the folder data_demo_xw/outputs/cms_based now holds data_demo_xw/outputs/cms_based/a.csv; "
                              "before re-running, if this is a folder this import left, not yours: "
                              "rm -r -- data_demo_xw/outputs/cms_based; otherwise move that folder"]


def test_a_failed_single_file_import_names_the_file_it_left(tmp_path: Path) -> None:
    """Mutation: treat it as a folder -> "left ... in" the same path twice, red."""
    from mintd._dvc_ops import DvcOpError

    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fails = _failing_after(lambda dest: dest.write_text("half\n"), DvcOpError("dvc import failed (exit 1): reset"))
    with pytest.raises(DvcOpError) as failed:
        import_product(client, fails(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert str(failed.value).endswith("; the file data_demo_xw/outputs/main.parquet is now there; before re-running, "
                                      "if this is a file this import left, not yours: "
                                      "rm -- data_demo_xw/outputs/main.parquet; otherwise move that file")


def test_a_forced_reimport_through_a_dotdot_dest_root_is_not_taken_for_a_stray_folder(tmp_path: Path) -> None:
    """With a pointer beside the folder, a forced re-import goes ahead. Through
    `foo/..` with `foo` missing, the pointer reads as absent unless resolved.
    Mutation: check `target_dvc.exists()` unresolved -> refused, red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "old.csv").write_text("the last import\n")
    (dest.parent / "cms_based.dvc").write_text("preexisting")

    import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path / "foo" / "..", force=True)

    assert len(fake.calls) == 1


def test_a_refusal_over_a_big_folder_names_eight_files_and_stops_walking(tmp_path: Path) -> None:
    """It names the first eight files, a folder's own before its subfolders',
    and says there are more, from a walk that stops at the ninth.
    Mutation: list the whole tree sorted (`rglob`) -> `a/z.csv` sorts first, red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    (dest / "a").mkdir(parents=True)
    (dest / "a" / "z.csv").write_text("x\n")
    for i in range(10):
        (dest / f"f{i}.csv").write_text("x\n")
    with pytest.raises(ImportDestinationExists, match=r"it holds data_demo_xw/outputs/cms_based/f0\.csv, .*"
                                                      r"cms_based/f7\.csv and more\)") as refused:
        import_product(client, _FakeDvcOps(), "demo_xw", cwd=tmp_path, dest_root=tmp_path)
    assert "z.csv" not in str(refused.value)


def test_import_product_force_overwrites(tmp_path: Path) -> None:
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/main.parquet"))
    fake = _FakeDvcOps()
    (tmp_path / "data_demo_xw" / "outputs").mkdir(parents=True)
    (tmp_path / "data_demo_xw" / "outputs" / "main.parquet.dvc").write_text("preexisting")

    produced = import_product(
        client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path, force=True
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
        "demo_xw",
        path="outputs/cms_based/",
        cwd=tmp_path, dest_root=tmp_path,
    )

    assert fake.calls[0].dest == tmp_path / "data_demo_xw" / "outputs" / "cms_based"


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

    import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=nested_dest)

    # Both dest_root and the per-producer namespace dir get auto-created.
    assert (nested_dest / "data_demo_xw").is_dir()
    assert fake.calls[0].dest == nested_dest / "data_demo_xw" / "outputs" / "main.parquet"


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
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "stale.csv").write_text("old payload")
    (dest.parent / "cms_based.dvc").write_text("preexisting")

    produced = import_product(
        client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path, force=True
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

    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
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
        # ever reached. That is pre-existing and out of scope here.
        target = dest.parent / "dvc-cache-payload"
        target.mkdir()
        (target / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")
        dest.symlink_to(target)

    with pytest.raises(DvcOpError):
        import_product(
            client, _PartialThenFailDvcOps(), "demo_xw", cwd=tmp_path,
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
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.parent.mkdir(parents=True)
    # No `.dvc` and no `dest`: nothing here is mintd's to move. The stray is a
    # remnant of a killed bump against an older layout.
    stray = dest.with_name(dest.name + ".mintd-bump-backup")
    stray.mkdir()
    (stray / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)

    assert (stray / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n"


def test_a_forced_import_refuses_a_stale_backup_instead_of_destroying_it(
    tmp_path: Path,
) -> None:
    """D14 on the import arm, reachable for the first time since the two verbs
    share the backup. `--force` after a hard kill must refuse, not clear."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "partial.csv").write_text("half\n", encoding="utf-8")
    (dest.parent / "cms_based.dvc").write_text("preexisting")
    backup = dest.with_name(dest.name + ".mintd-bump-backup")
    backup.mkdir()
    (backup / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(StaleBackupExists) as excinfo:
        import_product(
            client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path,
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
    data and must survive a forced re-import.

    Real dvc treats an untracked directory dest as a *container*: it nests the
    source basename inside and exits 0 (licensed on both arms by
    `test_import_into_an_untracked_directory_dest_nests_inside` in
    tests/test_dvc_ops_contract.py). This test used to expect exactly that:
    the import went through, one level deep, and the pointer the seam
    returned was never written. That nested layout is a false success, so a
    forced import over such a folder is refused before dvc runs, and the
    stray's contents survive. Mutation: drop the refusal -> red."""
    client = InMemoryCatalogClient()
    _register(client, mutate=_with_primary("outputs/cms_based/"))
    fake = _FakeDvcOps()
    dest = tmp_path / "data_demo_xw" / "outputs" / "cms_based"
    dest.mkdir(parents=True)
    (dest / "precious.csv").write_text("not mintd's to delete")

    with pytest.raises(ImportDestinationExists, match="no pointer records it"):
        import_product(
            client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path,
            force=True,
        )

    assert (dest / "precious.csv").read_text() == "not mintd's to delete"
    assert fake.calls == []
    assert not (dest / "cms_based.dvc").exists()


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
            client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path
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
            client, _FakeDvcOps(), "demo_xw",
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
        "      url: https://github.com/example-org/demo_xw\n"
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
    ns = tmp_path / "data_demo_xw"
    legacy = _stage_legacy_import(ns, producer_path="outputs/main.parquet", leaf="main.parquet")

    with pytest.raises(ImportDestinationExists) as ei:
        import_product(client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path)

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
    ns = tmp_path / "data_demo_xw"
    _stage_legacy_import(ns, producer_path="outputs/main.parquet", leaf="main.parquet")

    produced = import_product(
        client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path, force=True
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
            client, fake, "demo_xw", cwd=tmp_path, dest_root=tmp_path / "imports"
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


@pytest.mark.parametrize("outputs", [True, "data/final", 7], ids=["bool", "str", "int"])
def test_scalar_outputs_never_reaches_a_for_loop(outputs: Any) -> None:
    """The sibling reader of `data_products.outputs`.

    `--all`'s guard was added to `_resolve_paths` alone, so the same malformed
    entry still exited `data clone --path` as a raw
    `TypeError: 'bool' object is not iterable` from `_tracked_output_targets`.
    Both readers go through `_rows` now; this pins the one the first fix
    missed, so a third reader cannot quietly reintroduce `or []`.
    """
    assert _tracked_output_targets({"data_products": {"outputs": outputs}}) == []


@pytest.mark.parametrize(
    "outputs,fault",
    [
        pytest.param([], "lists no data_products.outputs", id="declared-empty"),
        pytest.param(
            [{"description": "no path key"}, "junk"],
            "lists 2 data_products.outputs, none with a usable path",
            id="rows-unusable",
        ),
        pytest.param(True, "lists no data_products.outputs", id="scalar-not-a-list"),
    ],
)
def test_import_all_with_no_importable_outputs_refuses(
    tmp_path: Path, outputs: Any, fault: str
) -> None:
    """`--all` against an entry listing no usable output imported 0 files and
    exited 0 — a CI gate on that exit code passes on an import that moved
    nothing. A producer registered before its first publish carries
    `outputs: []`; a v1-era entry carries rows the resolver's isinstance
    filter drops.

    `outputs: true` is a v1 hand-edit away from valid and is TRUTHY, so it
    survived the resolver's `or []` and tracebacked out of `data import --all`
    as `TypeError: 'bool' object is not iterable` instead of refusing.

    `primary` IS set in every case, so this is not the missing-primary path:
    `--all` selects the outputs list, and an empty selection is a refusal,
    never a silent fallback to the primary.

    The message names WHICH fault it is: "no outputs" is the producer's to
    fix by publishing, whereas rows that exist but carry no usable `path` are
    a malformed entry. And the way out is the primary, not `--path`: `--all`
    wins over a `--path` the user already passed, so advising `--path` reads
    as advice they just followed.

    Mutations: drop `_resolve_paths`'s `if not selected` guard -> every param
    returns [] and `import_product` returns [] having called no dvc; drop its
    `isinstance(outputs, list)` guard -> `scalar-not-a-list` raises TypeError;
    collapse the two message arms into one -> `rows-unusable` reddens.
    """
    fake = _FakeDvcOps()
    client = _raw_client(
        {**_V1_BASE, "data_products": {"primary": "data/final/", "outputs": outputs}}
    )

    with pytest.raises(NoTrackedOutputs) as exc:
        import_product(
            client, fake, "provider_a", all_outputs=True,
            cwd=tmp_path, dest_root=tmp_path,
        )
    assert fault in str(exc.value), str(exc.value)
    assert "drop --all to import the primary ('data/final/')" in str(exc.value)
    assert "--path" not in str(exc.value), "advises a flag --all already overrode"
    assert fake.calls == []


def test_a_non_string_path_row_is_dropped_by_both_readers() -> None:
    """`_tracked_output_targets` filtered on `"path" in o` where
    `_resolve_paths` filters on `isinstance(..., str)`, so `outputs:
    [{path: 7}]` put an int into the tracked list and `data clone --path x`
    died inside `normalize_target` with `AttributeError: 'int' object has no
    attribute 'replace'` -- the raw traceback the sibling reader's own
    docstring says cannot happen. Both readers apply the same test now.

    Mutation: weaken either filter back to `"path" in o` -> this reddens.
    """
    entry = {"data_products": {"primary": None, "outputs": [{"path": 7}]}}

    assert _tracked_output_targets(entry) == []

    # And the user gets the listing error rather than an AttributeError.
    with pytest.raises(UnknownProductPath, match="no tracked output"):
        _validate_requested_targets(entry, requested=["data/final/"], name="provider_a")
