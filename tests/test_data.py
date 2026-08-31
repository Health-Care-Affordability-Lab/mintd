"""Tests for `bump_import` (slice 7) and `ProducerView.at_head`.

`bump_import` consumes slice-6 `_consumer_findings` and re-resolves the
producer's `data_products.primary` at HEAD. These tests exercise the
severity dispatch, the batch `check_findings` injection seam, the default
`ProducerView.at_head` factory wiring, and the `(view, sha)` return shape
of `at_head`.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mintd.catalog import InMemoryCatalogClient
from mintd.check import CheckFinding
from mintd.data import (
    AmbiguousImport,
    BumpBlocked,
    ImportDestinationExists,
    ImportNotFound,
    PrimaryRemovedAtHead,
    UnknownProductPath,
    bump_import,
)
from mintd.model import DataProductOutput, DataProducts, Metadata
from mintd.producer import ProducerView

from tests._fakes.dvc_ops import _FakeDvcOps
from tests._fakes.producer import StaticFetcher

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "metadata_v2_minimal.json"
STANDALONE_IMPORT = FIXTURES / "dvc_files" / "standalone_import.dvc"

REPO_URL = "https://github.com/example-org/provider-xw"
PIN_SHA = "4f7c2a1abcd1234567890abcdef0123456789abc"  # matches fixture
HEAD_SHA = "b" * 40


def _stage_project(
    tmp_path: Path, *, dvc_filename: str = "cms_based.dvc", namespace: str = "cms_based"
) -> Path:
    """Lay out a project with metadata.json + one canonical .dvc import.

    The import sits under `data/imports/<namespace>/` — the folder D-A's
    resolution scans. The default matches the bump tests' product name
    (their `InMemoryCatalogClient` is empty, so `_resolve_import_source`
    falls back to the name itself as the namespace).

    Returns the path to the staged `.dvc` file.
    """
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    imports_dir = tmp_path / "data" / "imports" / namespace
    imports_dir.mkdir(parents=True, exist_ok=True)
    dvc_path = imports_dir / dvc_filename
    shutil.copy(STANDALONE_IMPORT, dvc_path)
    return dvc_path


def _view_with_primary(primary: str | None) -> ProducerView:
    meta = Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8"))
    meta = meta.model_copy(
        update={
            "data_products": DataProducts(
                primary=primary,
                outputs=[
                    DataProductOutput(
                        path=primary,
                        description="desc",
                        primary=True,
                        last_published="2023-01-01T00:00:00Z",
                    )
                ]
                if primary
                else [],
            )
        }
    )
    return ProducerView(repo=REPO_URL, pin=HEAD_SHA, metadata=meta)


def _drift_finding(source: Path) -> CheckFinding:
    return CheckFinding(
        severity="warning",
        section="consumer",
        message=(
            "upgrade available: producer now publishes "
            "'outputs/new.parquet' (you have 'cms_based')"
        ),
        source=source,
        kind="drift",
    )


def _up_to_date_finding(source: Path) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=source,
        kind="up_to_date",
    )


# ---------------------------------------------------------------------------
# Severity dispatch
# ---------------------------------------------------------------------------


def test_bump_up_to_date_returns_none(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_up_to_date_finding(dvc_path)],
    )

    assert result.changed is False
    assert result.dvc_path is None
    assert result.new_pin is None
    assert fake.calls == []


def test_a_failed_bump_leaves_the_payload_in_place(tmp_path: Path) -> None:
    """`bump_import` deleted the payload BEFORE the re-import. A producer that
    went unreachable mid-bump then left the directory gone and the `.dvc`
    still at the old pin — from `mintd data import X --bump`, with no `--force`
    anywhere. Recoverable via `dvc checkout`, but the researcher was not told
    so, and re-pulling a large product is not free.

    Mutation: replace the rename/restore with `shutil.rmtree(dest)` before the
    import -> `payload.is_dir()` reddens.
    """
    from mintd._dvc_ops import DvcOpError

    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.mkdir()
    (payload / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    fake = _FakeDvcOps()
    fake.import_raises = DvcOpError("dvc import failed (exit 1): Failed to clone repo")

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    with pytest.raises(DvcOpError):
        bump_import(
            InMemoryCatalogClient(),
            fake,
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert payload.is_dir(), "the payload was deleted by a bump that then failed"
    assert (payload / "irreplaceable.csv").read_text() == "keep me\n"
    assert not list(payload.parent.glob("*.mintd-bump-backup"))


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupted_bump_leaves_the_payload_in_place(
    tmp_path: Path, interrupt: type[BaseException]
) -> None:
    """The rollback caught `Exception`, and Ctrl-C is not one.

    `run_streaming` re-raises `KeyboardInterrupt` unchanged (`_subprocess.py`,
    the `proc.wait` handler), so Ctrl-C during the `dvc import` of a large
    product walked straight past the restore: the payload was left in
    `<dest>.mintd-bump-backup`, the `.dvc` still at the old pin, and `main`
    printed only "interrupted by user". Nothing named the moved directory.

    Mutation: `except BaseException` -> `except Exception` in `bump_import`
    -> both cells redden on `payload.is_dir()`.
    """
    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.mkdir()
    (payload / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    fake = _FakeDvcOps()
    fake.import_raises = interrupt()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    with pytest.raises(interrupt):
        bump_import(
            InMemoryCatalogClient(),
            fake,
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert payload.is_dir(), "an interrupted bump left the payload moved aside"
    assert (payload / "irreplaceable.csv").read_text() == "keep me\n"
    assert not list(payload.parent.glob("*.mintd-bump-backup"))


def _link_target(p: Path) -> Path:
    r"""The path a symlink points at, without Windows' extended-length prefix.

    `Path.readlink()` returns `\\?\C:\...` on Windows and the bare target on
    POSIX. That prefix is a Win32 API artifact, not part of the identity these
    tests assert — "the payload came back as the same link", not "the string
    round-trips byte for byte".
    """
    return Path(str(p.readlink()).removeprefix("\\\\?\\"))


class _PartialThenFailDvcOps(_FakeDvcOps):
    """dvc that writes part of the destination and then dies.

    A real `dvc import` that loses its connection mid-transfer leaves a
    partly-written `dest` behind. `_FakeDvcOps.import_raises` fires before
    touching the disk, so no existing test reaches that shape.

    `shape` is what the corpse looks like. A directory is the common case, but
    not the only one: the output can be a single file at HEAD (exactly the
    drift a bump acts on), and dvc's `cache.type = symlink` — the standard way
    a lab avoids a second copy of a large product — leaves a symlink.
    """

    def __init__(self, shape: str = "dir") -> None:
        super().__init__()
        self.shape = shape

    def import_(self, **kwargs: Any) -> Path:
        from mintd._dvc_ops import DvcOpError

        dest: Path = kwargs["dest"]
        # `--force` clears whatever is at `dest` before writing. Only reachable
        # once the backup covers non-directory payloads: until then `dest` was
        # either absent or already renamed aside, so the fake never met one.
        # Without it the fake dies on the payload it is meant to replace
        # (`mkdir` over an existing file, `symlink_to` over an existing link)
        # and raises `FileExistsError` instead of the dvc error under test.
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink(missing_ok=True)
        if self.shape == "dir":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "half.csv").write_text("truncated\n", encoding="utf-8")
        elif self.shape == "file":
            dest.write_text("truncated\n", encoding="utf-8")
        else:
            cached = dest.with_name("half-cache")
            cached.mkdir(parents=True, exist_ok=True)
            (cached / "half.csv").write_text("truncated\n", encoding="utf-8")
            dest.symlink_to(cached)
        raise DvcOpError("dvc import failed (exit 1): connection reset by peer")


@pytest.mark.parametrize("shape", ["dir", "file", "symlink"])
def test_a_bump_that_fails_midway_restores_over_the_partial_destination(
    tmp_path: Path, shape: str
) -> None:
    """The restore has to clear what the failed import left behind first.

    Without the clear, `backup.rename(dest)` meets the leftover and raises
    `OSError` — which replaces the dvc error the CLI knows how to render (the
    `--bump` arm catches `DvcOpError`, not `OSError`), so the researcher gets
    a traceback out of `main()` and the payload stays in `.mintd-bump-backup`
    under a name nothing mentions.

    The leftover is not always a directory, and `rmtree` alone is a silent
    no-op on the other two shapes, so `file` and `symlink` both died on
    `NotADirectoryError` inside the handler until the clear stopped assuming.

    Mutations: drop the clear entirely -> `dir` reddens; narrow it back to a
    bare `shutil.rmtree(dest, ignore_errors=True)` -> `file` and `symlink`
    redden; drop `not dest.is_symlink()` from it -> `symlink` reddens. The
    other failure tests kill none of these: their fake raises before writing
    anything, so `dest` is absent and the bare rename succeeds.
    """
    from mintd._dvc_ops import DvcOpError

    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.mkdir()
    (payload / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    with pytest.raises(DvcOpError):
        bump_import(
            InMemoryCatalogClient(),
            _PartialThenFailDvcOps(shape),
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert payload.is_dir() and not payload.is_symlink()
    assert (payload / "irreplaceable.csv").read_text() == "keep me\n"
    assert not (payload / "half.csv").exists()
    assert not list(payload.parent.glob("*.mintd-bump-backup"))


@pytest.mark.parametrize("shape", ["file", "symlink", "dangling_symlink"])
def test_a_failed_bump_restores_a_payload_that_is_not_a_directory(
    tmp_path: Path, shape: str
) -> None:
    """D15: a single-file payload and a symlinked one deserve the rename-aside
    a directory gets.

    The backup was guarded by `dest.is_dir() and not dest.is_symlink()`, so
    every other shape took NO BACKUP AT ALL and `dvc import --force` then
    overwrote the payload with nothing to roll back to. Both shapes are
    ordinary: an output that is a single file at HEAD (exactly the drift a
    bump acts on), and dvc's `cache.type = symlink` layout. The restore side
    already NAMES both — the fix acknowledged them while the backup side still
    skipped them.

    `not dest.is_symlink()` was never a decision to exclude symlinks:
    `Path.is_dir()` follows them, so that conjunct only ever separated "a real
    directory" from "a link to one". `dest.rename(backup)` works identically
    for a directory, a file and a symlink.

    A dangling link needs `is_symlink()` in its own right — `dest.exists()` is
    False for one, so an `exists()`-only widening still skips it. A pruned dvc
    cache leaves exactly that behind, and the link is the only record of where
    the payload was.

    Mutations: put `dest.is_dir() and not dest.is_symlink()` back -> all three
    redden; widen to `dest.exists()` alone -> `dangling_symlink` reddens. The
    directory cells above kill neither.
    """
    from mintd._dvc_ops import DvcOpError

    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    store = tmp_path / "dvc-cache-store"
    if shape == "file":
        payload.write_text("keep me\n", encoding="utf-8")
    else:
        if shape == "symlink":
            store.mkdir()
            (store / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")
        payload.symlink_to(store)

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    with pytest.raises(DvcOpError):
        bump_import(
            InMemoryCatalogClient(),
            _PartialThenFailDvcOps("dir"),
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    if shape == "file":
        assert payload.is_file() and not payload.is_symlink(), (
            "the single-file payload was destroyed by a bump that then failed"
        )
        assert payload.read_text(encoding="utf-8") == "keep me\n"
    else:
        assert payload.is_symlink(), (
            "the symlinked payload was destroyed by a bump that then failed"
        )
        assert _link_target(payload) == store
        assert payload.exists() is (shape == "symlink")
    if shape == "symlink":
        assert (store / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n"
    assert not list(payload.parent.glob("*.mintd-bump-backup"))


@pytest.mark.parametrize("case", ["partial-dest", "no-dest", "dangling-backup"])
def test_a_bump_refuses_a_stale_backup_instead_of_destroying_it(
    tmp_path: Path, case: str
) -> None:
    """D14: `.mintd-bump-backup` already there means the LAST bump never
    finished, and what is in it is the only complete payload left.

    `except BaseException` covers a soft failure, not a hard kill — power
    loss, SIGKILL, an OOM reaper. One of those mid-transfer leaves a complete
    payload in `.mintd-bump-backup` and a half-written `dest`. On the retry
    `_remove_payload(backup)` ran unconditionally one line before
    `dest.rename(backup)`, so the retry deleted the only complete copy and
    moved the partial corpse into its place; if that import also failed, the
    restore handed the researcher the corpse.

    `no-dest` is the same kill a moment earlier — after the rename, before
    dvc wrote anything. `moved` is then False, so the retry takes no backup,
    succeeds, and the success path's `_remove_payload(backup)` deletes the old
    payload silently. The refusal has to sit ABOVE `if moved:` to cover it.

    `dangling-backup` is dvc's `cache.type = symlink` layout after the cache
    was pruned: `backup.exists()` is False for a dangling link, and that link
    is the only record of where the payload was — the same reason `moved`
    tests `is_symlink()` in its own right.

    Nothing on this path may delete anything: refuse, name the backup, and let
    the user look.

    Mutations: drop the refusal -> all three redden; narrow it to
    `backup.exists()` -> `dangling-backup` reddens; move it inside `if moved:`
    -> `no-dest` reddens.
    """
    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    backup = payload.with_name(payload.name + ".mintd-bump-backup")
    store = tmp_path / "pruned-dvc-cache"

    if case == "dangling-backup":
        backup.symlink_to(store)
    else:
        backup.mkdir()
        (backup / "irreplaceable.csv").write_text("keep me\n", encoding="utf-8")
    if case != "no-dest":
        payload.mkdir()
        (payload / "half.csv").write_text("truncated\n", encoding="utf-8")

    fake = _FakeDvcOps()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    with pytest.raises(ImportDestinationExists) as excinfo:
        bump_import(
            InMemoryCatalogClient(),
            fake,
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert str(backup) in str(excinfo.value), "the refusal must name the backup"
    assert fake.calls == [], "the import ran over a payload nobody had looked at"
    if case == "dangling-backup":
        assert backup.is_symlink(), "the only record of where the payload was is gone"
        assert _link_target(backup) == store
    else:
        assert (backup / "irreplaceable.csv").read_text(encoding="utf-8") == "keep me\n", (
            "the retry destroyed the last complete payload"
        )
    if case != "no-dest":
        assert (payload / "half.csv").read_text(encoding="utf-8") == "truncated\n"


def test_a_successful_bump_leaves_no_backup_beside_a_file_payload(
    tmp_path: Path,
) -> None:
    """The backup is cleaned up with `shutil.rmtree(..., ignore_errors=True)`,
    a silent no-op on anything that is not a directory.

    Harmless while only directories were moved aside; once a single-file
    payload is too (D15), every SUCCESSFUL bump leaves
    `<name>.mintd-bump-backup` sitting in `data/imports/` — a stale copy of
    the old payload under a name nothing mentions, growing one per bump.

    Mutation: `_remove_payload(backup)` on the success path ->
    `shutil.rmtree(backup, ignore_errors=True)` -> reddens. This is the cost
    of the widening, so it cannot redden before it: with the old guard no
    backup is taken here at all.
    """
    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.write_text("stale\n", encoding="utf-8")

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    result = bump_import(
        InMemoryCatalogClient(),
        _FakeDvcOps(),
        project_path=tmp_path,
        name="cms_based",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert result.changed is True
    assert not list(payload.parent.glob("*.mintd-bump-backup"))


def test_bump_forwards_extra_dvc_args(tmp_path: Path) -> None:
    """`--jobs` rides `extra_args` as `-j <n>`, the same seam the plain import
    arm uses. `bump_import` had no parameter for it at all, so the CLI's
    assembled argv was built and dropped."""
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    bump_import(
        InMemoryCatalogClient(),
        fake,
        project_path=tmp_path,
        name="cms_based",
        extra_dvc_args=["--verbose", "-j", "8"],
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert fake.calls[0].extra_args == ["--verbose", "-j", "8"]


def test_a_successful_bump_clears_the_backup(tmp_path: Path) -> None:
    """The safety copy must not survive the bump it protected."""
    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.mkdir()
    (payload / "old.csv").write_text("old\n", encoding="utf-8")

    fake = _FakeDvcOps()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    bump_import(
        InMemoryCatalogClient(),
        fake,
        project_path=tmp_path,
        name="cms_based",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert not list(payload.parent.glob("*.mintd-bump-backup"))


def test_bump_with_drift_rewrites_dvc_file(tmp_path: Path) -> None:
    """D-C2: the bump re-imports the path this `.dvc` RECORDS, into the same
    `.dvc` — nothing orphaned, even when the producer's primary moved."""
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def factory(repo: str) -> tuple[ProducerView, str]:
        assert repo == REPO_URL
        return _view_with_primary("outputs/new.parquet"), HEAD_SHA

    produced = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert produced.changed is True
    assert produced.new_pin == HEAD_SHA
    # The SAME file is rewritten — not a sibling named after HEAD's primary.
    assert produced.dvc_path == dvc_path
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # The recorded producer path, not HEAD's primary.
    assert call.path == "outputs/cms_based/"
    assert call.rev == HEAD_SHA
    assert call.force is True
    assert call.repo_url == REPO_URL
    assert call.dest == dvc_path.with_suffix("")
    # No orphan: the only `.dvc` under the namespace is the rewritten one.
    assert list(dvc_path.parent.glob("*.dvc")) == [dvc_path]


def test_bump_name_not_imported_raises_import_not_found(tmp_path: Path) -> None:
    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    (tmp_path / "data" / "imports").mkdir(parents=True)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    with pytest.raises(ImportNotFound):
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[],
        )
    assert fake.calls == []


# ---------------------------------------------------------------------------
# D-A — the positional is the data product name; the output is `--path`
# ---------------------------------------------------------------------------

FULL_NAME = "data_test_project"  # `project.full_name` in the fixture
#: `repository.github_url` in the SAME fixture. The staged imports below are
#: imports *of that product*, so they have to record its URL: an import under
#: a product's namespace that names a different producer is now refused
#: (`_require_owner`) — that is a namespace collision, not a re-import.
FIXTURE_URL = "https://github.com/test-org/data_test_project"


def _register_fixture_product(client: InMemoryCatalogClient) -> None:
    client.register(Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8")))


def _stage_namespaced(
    tmp_path: Path, *, name: str, producer_path: str, local_path: str | None = None
) -> Path:
    """One import of the fixture product, in the D-A layout
    (`data/imports/<full_name>/…`), recording `producer_path`."""
    from tests._harness.consumer import Import, write_import

    shutil.copy(MINIMAL, tmp_path / "metadata.json")
    return write_import(
        tmp_path,
        Import(
            name=name,
            producer_url=FIXTURE_URL,
            pin=PIN_SHA,
            producer_path=producer_path,
            local_path=local_path or name,
        ),
        under=f"data/imports/{FULL_NAME}",
    )


def test_bump_accepts_the_data_product_name(tmp_path: Path) -> None:
    """issue09 test (iii): `mintd data import test_project --bump` works —
    the same identifier import takes, mapped to the namespace via the
    catalog entry's `full_name`."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    dvc_path = _stage_namespaced(
        tmp_path, name="final", producer_path="data/final/"
    )

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="test_project",
        check_findings=[_up_to_date_finding(dvc_path)],
    )

    assert result.changed is False
    assert fake.calls == []


def test_a_delisted_product_stays_bumpable(tmp_path: Path) -> None:
    """`_resolve_import_source`'s `CatalogNotFound` arm promises, in its own
    docstring, that "an import already on disk must stay bumpable when its
    entry is gone". It fell back to the CATALOG NAME (`test_project`) while
    imports live under `full_name` (`data_test_project`), so the arm was dead
    for every product `mintd init` scaffolds — `ImportNotFound` on a pointer
    sitting right there on disk.

    Mutation: drop the `_namespace_by_scan` fallback -> ImportNotFound.
    """
    fake = _FakeDvcOps()
    dvc_path = _stage_namespaced(
        tmp_path, name="final", producer_path="data/final/"
    )

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("data/final/"), HEAD_SHA

    result = bump_import(
        InMemoryCatalogClient(),  # empty: the entry is gone
        fake,
        project_path=tmp_path,
        name="test_project",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert result.changed is True
    assert result.new_pin == HEAD_SHA


def test_a_renamed_full_name_stays_bumpable(tmp_path: Path) -> None:
    """The folder froze `full_name` at import time. A producer that renamed it
    since leaves the derived namespace pointing at nothing — the entry exists,
    so this never reaches the `CatalogNotFound` arm at all."""
    fake = _FakeDvcOps()
    dvc_path = _stage_namespaced(
        tmp_path, name="final", producer_path="data/final/"
    )
    client = InMemoryCatalogClient()
    meta = Metadata.model_validate_json(MINIMAL.read_text(encoding="utf-8"))
    renamed = meta.project.model_copy(update={"full_name": "data_test_project_v2"})
    client.register(meta.model_copy(update={"project": renamed}))

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("data/final/"), HEAD_SHA

    result = bump_import(
        client, fake, project_path=tmp_path, name="test_project",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert result.changed is True


def test_two_namespaces_that_could_be_the_name_refuse_to_guess(tmp_path: Path) -> None:
    """Picking the wrong folder re-pins ANOTHER product, and `_require_owner`
    cannot catch it when there is no entry left to compare a URL against."""
    _stage_namespaced(tmp_path, name="final", producer_path="data/final/")
    (tmp_path / "data" / "imports" / "code_test_project").mkdir(parents=True)

    with pytest.raises(AmbiguousImport):
        bump_import(
            InMemoryCatalogClient(),
            _FakeDvcOps(),
            project_path=tmp_path,
            name="test_project",
        )


def test_bump_rejects_the_bare_output_leaf(tmp_path: Path) -> None:
    """The leaf (`final`) stops being a key at all — D-A has one identifier."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    dvc_path = _stage_namespaced(
        tmp_path, name="final", producer_path="data/final/"
    )

    with pytest.raises(ImportNotFound):
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="final",
            check_findings=[_up_to_date_finding(dvc_path)],
        )
    assert fake.calls == []


def test_bump_with_path_selects_that_row(tmp_path: Path) -> None:
    """issue09 test (ii): `--path` picks the row, and the bump rewrites THAT
    row's `.dvc` — the sibling is untouched (D-C2 at the fake)."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    final_dvc = _stage_namespaced(tmp_path, name="final", producer_path="data/final/")
    extract_dvc = _stage_namespaced(
        tmp_path, name="extract", producer_path="data/extract/"
    )
    final_before = final_dvc.read_text(encoding="utf-8")

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("data/final/"), HEAD_SHA

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="test_project",
        path="data/extract",
        producer_view_factory=factory,
        check_findings=[_drift_finding(extract_dvc)],
    )

    assert result.changed is True
    assert result.dvc_path == extract_dvc
    assert len(fake.calls) == 1
    assert fake.calls[0].path == "data/extract/"
    assert fake.calls[0].dest == extract_dvc.with_suffix("")
    assert final_dvc.read_text(encoding="utf-8") == final_before


def test_bump_by_catalog_name_raises_on_two_candidates(tmp_path: Path) -> None:
    """Several imported outputs and no `--path`: refuse, naming them."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    _stage_namespaced(tmp_path, name="final", producer_path="data/final/")
    _stage_namespaced(tmp_path, name="extract", producer_path="data/extract/")

    with pytest.raises(AmbiguousImport) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="test_project",
            check_findings=[],
        )
    assert "data/final" in str(ei.value)
    assert "data/extract" in str(ei.value)
    assert "--path" in str(ei.value)
    assert fake.calls == []


def test_two_dvc_files_recording_the_same_output_path_raise(tmp_path: Path) -> None:
    """A real duplicate (same producer path recorded twice in one namespace)
    is never resolved silently — that is the last-writer-wins defect D-A
    exists to kill."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    _stage_namespaced(tmp_path, name="final", producer_path="data/final/")
    # The pre-mirroring layout wrote the basename; the new layout mirrors the
    # producer path. Both record `data/final/` — the duplicate case.
    _stage_namespaced(
        tmp_path, name="data/final", producer_path="data/final/", local_path="final"
    )

    with pytest.raises(AmbiguousImport) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="test_project",
            path="data/final",
            check_findings=[],
        )
    assert "same producer path" in str(ei.value)
    assert fake.calls == []


def test_bump_pin_missing_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message=f"producer pin missing: {PIN_SHA[:7]} not found in {REPO_URL}",
        source=dvc_path,
        kind="pin_missing",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert ei.value.name == "cms_based"
    assert fake.calls == []


def test_bump_unreachable_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="producer unreachable: git archive timed out",
        source=dvc_path,
        kind="unreachable",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []


def test_bump_schema_too_old_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message=(
            f"producer at pin {PIN_SHA[:7]} uses schema_version 1.5 (expected 2.0)"
        ),
        source=dvc_path,
        kind="schema_too_old",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []


def test_bump_metadata_invalid_raises_bump_blocked(tmp_path: Path) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="error",
        section="consumer",
        message=f"producer metadata invalid at pin {PIN_SHA[:7]}: validation error",
        source=dvc_path,
        kind="metadata_invalid",
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding


def test_bump_head_primary_removed_raises_primary_removed_at_head(
    tmp_path: Path,
) -> None:
    """The primary fallback (and its raise) covers only shapes that record
    no producer path of their own — D-C2 sends recorded paths straight to
    the import target, HEAD's primary unconsulted."""
    dvc_path = _stage_project(tmp_path)
    dvc_path.write_text(
        dvc_path.read_text(encoding="utf-8").replace(
            "  - path: outputs/cms_based/", '  - path: ""'
        ),
        encoding="utf-8",
    )
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary(None), HEAD_SHA

    with pytest.raises(PrimaryRemovedAtHead) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            producer_view_factory=factory,
            check_findings=[_drift_finding(dvc_path)],
        )

    assert ei.value.name == "cms_based"
    assert ei.value.repo == REPO_URL
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Injection seams
# ---------------------------------------------------------------------------


def test_bump_consumes_provided_check_findings_without_recomputing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def must_not_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("check_project must not be called when check_findings is provided")

    monkeypatch.setattr("mintd.data.check_project", must_not_call)

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_up_to_date_finding(dvc_path)],
    )

    assert result.changed is False


def test_bump_default_uses_check_project_when_no_findings_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    calls: list[tuple[Path, dict[str, Any]]] = []

    def recorder(path: Path, **kwargs: Any) -> list[CheckFinding]:
        calls.append((path, kwargs))
        return [_up_to_date_finding(dvc_path)]

    monkeypatch.setattr("mintd.data.check_project", recorder)

    result = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
    )

    assert result.changed is False
    assert len(calls) == 1
    assert calls[0][0] == tmp_path
    assert calls[0][1] == {"upgrades": True}


def test_bump_default_uses_producer_view_at_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    captured: list[str] = []

    def stub(repo: str) -> tuple[Any, str]:
        captured.append(repo)
        return (
            SimpleNamespace(primary_or_raise=lambda: "outputs/new.parquet"),
            HEAD_SHA,
        )

    monkeypatch.setattr("mintd.data.ProducerView.at_head", stub)

    bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        check_findings=[_drift_finding(dvc_path)],
    )

    assert captured == [REPO_URL]
    assert fake.calls[0].rev == HEAD_SHA
    # D-C2: the import target is the RECORDED path, not HEAD's primary.
    assert fake.calls[0].path == "outputs/cms_based/"


# ---------------------------------------------------------------------------
# ProducerView.at_head primitive
# ---------------------------------------------------------------------------


def _producer_bytes(*, primary: str | None = "outputs/main.parquet") -> bytes:
    data = json.loads(MINIMAL.read_text(encoding="utf-8"))
    data["data_products"]["primary"] = primary
    return json.dumps(data).encode()


def test_at_head_returns_resolved_sha_not_symbolic_ref(tmp_path: Path) -> None:
    resolved = "deadbeef" * 5  # 40-char hex
    fetcher = StaticFetcher(
        {},
        head_store={REPO_URL: (_producer_bytes(primary="outputs/x.parquet"), resolved)},
    )

    view, sha = ProducerView.at_head(
        REPO_URL, fetcher=fetcher, cache_dir=tmp_path / "cache"
    )

    assert sha == resolved
    assert view.pin == resolved
    assert view.pin != "HEAD"
    # Cache file lives under the resolved SHA, never under "HEAD".
    cache_root = tmp_path / "cache"
    cache_files = list(cache_root.rglob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].name == f"{resolved}.json"
    assert "HEAD" not in str(cache_files[0])
    assert fetcher.head_calls == [REPO_URL]


# ---------------------------------------------------------------------------
# Slice 9 — defensive `kind is None` arm
# ---------------------------------------------------------------------------


def test_bump_missing_kind_raises_bump_blocked(tmp_path: Path) -> None:
    """A consumer-section finding without `kind` is a regression contract
    violation post-slice-9; `bump_import` must raise `BumpBlocked` rather
    than silently dispatching as no-op."""
    dvc_path = _stage_project(tmp_path)
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    finding = CheckFinding(
        severity="warning",
        section="consumer",
        message="upgrade available: producer now publishes 'X' (you have 'Y')",
        source=dvc_path,
        # kind deliberately omitted (default None)
    )

    with pytest.raises(BumpBlocked) as ei:
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="cms_based",
            check_findings=[finding],
        )

    assert ei.value.finding is finding
    assert fake.calls == []


def test_bump_clears_the_existing_directory_before_reimport(
    tmp_path: Path,
) -> None:
    """issue09 fix 3, bump side: a re-bump of a directory product hits dvc's
    container-nesting refusal unless the old payload is cleared. The fake
    raises on an existing dir dest, so dropping the rmtree reddens here
    (M14c)."""
    dvc_path = _stage_project(tmp_path)
    payload = dvc_path.with_suffix("")
    payload.mkdir()
    (payload / "v1.csv").write_text("old bytes")
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("outputs/cms_based/"), HEAD_SHA

    produced = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="cms_based",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert produced.changed is True
    assert len(fake.calls) == 1
    assert not payload.exists()


def test_bump_rewrites_a_renamed_dvc_file_in_place(tmp_path: Path) -> None:
    """M14b's falsifier: dest derives from the FILE being bumped, never
    recomputed from the target path. A `.dvc` whose stem no longer matches
    the recorded path's basename (hand-renamed) must still be rewritten in
    place — recomputing would silently write a sibling and orphan it."""
    fake = _FakeDvcOps()
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    dvc_path = _stage_namespaced(
        tmp_path, name="final-v1", producer_path="data/final/", local_path="final-v1"
    )

    def factory(repo: str) -> tuple[ProducerView, str]:
        return _view_with_primary("data/final/"), HEAD_SHA

    produced = bump_import(
        client,
        fake,
        project_path=tmp_path,
        name="test_project",
        producer_view_factory=factory,
        check_findings=[_drift_finding(dvc_path)],
    )

    assert produced.dvc_path == dvc_path
    assert fake.calls[0].dest == dvc_path.with_suffix("")
    assert list(dvc_path.parent.glob("*.dvc")) == [dvc_path]


@pytest.mark.parametrize(
    "namespace",
    [".", "..", "", "/tmp/abs", "a/b", "data_a/"],
    ids=["dot", "dotdot", "empty", "absolute", "nested", "trailing-slash"],
)
def test_bump_refuses_a_namespace_that_is_not_one_folder(
    tmp_path: Path, namespace: str
) -> None:
    """`--bump` resolves the same namespace as the writer and has NO
    containment check, so the one-component rule is the only thing standing
    between a bad namespace and an `_imports_index` rglob over the whole
    `data/` tree.

    Empty catalog on purpose: that is the `except CatalogNotFound` arm,
    where the namespace is the user-typed product NAME rather than the
    producer's `full_name`.

    Mutation: apply the rule in `import_product` only, leaving
    `_resolve_import_source`'s fallback on the raw `name` -> every case
    reddens.
    """
    staged = _stage_project(tmp_path)
    fake = _FakeDvcOps()

    with pytest.raises(UnknownProductPath, match="single folder name"):
        bump_import(
            InMemoryCatalogClient(),
            fake,
            project_path=tmp_path,
            name=namespace,
            check_findings=[_drift_finding(staged)],
        )

    assert fake.calls == [], "refused before any dvc invocation"


def test_bump_never_touches_another_products_import(tmp_path: Path) -> None:
    """The bump arm's ownership guard, driven through a catalog HIT.

    Both bump tests above take the `except CatalogNotFound` arm, where the
    namespace is the user-typed name — self-harm, not the threat model. The
    threat model is `project.full_name`, which is PRODUCER-supplied: an entry
    naming another product's namespace resolves the index to that product's
    `.dvc`, and bump then `shutil.rmtree`s its payload and silently re-pins
    it to ITS head, reporting "✓ bumped <the product you asked for>".

    Asserts on the victim, not on an exception type.

    Mutation: drop the `_require_owner` call from `_resolve_import_source`
    -> this reddens (and the whole existing suite stays green, which is why
    it is here).
    """
    client = InMemoryCatalogClient()
    _register_fixture_product(client)
    impostor = json.loads(MINIMAL.read_text(encoding="utf-8"))
    impostor["project"]["name"] = "impostor"
    impostor["project"]["full_name"] = FULL_NAME  # another product's namespace
    impostor["repository"]["github_url"] = "https://github.com/example-org/impostor"
    client.register(Metadata.model_validate(impostor))

    victim = _stage_namespaced(tmp_path, name="final", producer_path="data/final/")
    payload = victim.with_suffix("")
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "irreplaceable.csv").write_text("years of work")
    fake = _FakeDvcOps()

    def factory(repo: str) -> tuple[ProducerView, str]:
        # Stubbed so the bump reaches its `shutil.rmtree` + re-import. With
        # the real `ProducerView.at_head` the fixture URL is unreachable and
        # the harm never gets a chance to happen, which would leave the
        # guard untested.
        return _view_with_primary("data/final/"), HEAD_SHA

    with contextlib.suppress(Exception):
        bump_import(
            client,
            fake,
            project_path=tmp_path,
            name="impostor",
            producer_view_factory=factory,
            check_findings=[_drift_finding(victim)],
        )

    assert (payload / "irreplaceable.csv").is_file(), "the victim's payload was deleted"
    assert FIXTURE_URL in victim.read_text(encoding="utf-8")
    assert fake.calls == [], "re-imported another product's pointer"
