"""`_paths.inside`: whether a path is inside the project, by each named way of reading it."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from mintd._paths import Read, inside, rel


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:  # Windows without the privilege to make links
        pytest.skip(f"can't make a link here: {exc}")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "a" / "b").mkdir(parents=True)
    return root


# --- recorded: a POSIX string from a file -----------------------------------------------------


@pytest.mark.parametrize(("path", "want"), [
    ("data/x.csv", "data/x.csv"),
    ("a/../b", "b"),
    ("sub/..", None),  # the project root itself: not an out
    ("..", None),
    ("a/../../x", None),
    ("/abs/x", None),
    ("C:/x", None),  # a drive, on POSIX too
    ("\\\\server\\share\\x", None),
    ("a\\b", None),
])
def test_a_recorded_path_is_read_as_a_posix_string(root: Path, path: str, want: str | None) -> None:
    """Mutation: drop the drive rule -> `C:/x` reads as inside, red."""
    assert inside(root, path, read="recorded") == want


def test_a_recorded_path_is_tested_with_its_wdir_before_the_join(root: Path) -> None:
    """`wdir: C:/w` keeps nothing, where joining first gave `C:/w/a.csv`; and a drive in
    the path itself isn't hidden by the join.

    Mutation: test only the joined path -> `code/C:/x` reads as inside, red."""
    assert inside(root, "a.csv", read="recorded", wdir="C:/w") is None
    assert inside(root, "C:/x", read="recorded", wdir="code") is None  # joined first, `code/C:/x` hides the drive
    assert inside(root, "../data/final", read="recorded", wdir="code") == "data/final"
    assert inside(root, "..", read="recorded", wdir="code") is None  # the root: not an out


@pytest.mark.parametrize("read", ["recorded", "lexical", "follow", "folder"])
def test_the_root_itself_counts_only_with_root_ok(root: Path, read: Read) -> None:
    """Mutation: ignore `root_ok` -> one of the two, red."""
    here = "." if read == "recorded" else root
    assert inside(root, here, read=read) is None
    assert inside(root, here, read=read, root_ok=True) == "."
    there = "a/.." if read == "recorded" else root / "a" / ".."  # the root, spelled another way
    assert inside(root, there, read=read) is None
    assert inside(root, there, read=read, root_ok=True) == "."


@pytest.mark.parametrize("read", ["recorded", "lexical"])
def test_dotdot_refuse_refuses_a_dotdot_that_would_stay_inside(root: Path, read: Read) -> None:
    path = "a/../b" if read == "recorded" else root / "a" / ".." / "b"
    assert inside(root, path, read=read) == "b"
    assert inside(root, path, read=read, dotdot="refuse") is None


# --- native paths: lexical, follow, folder ----------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows `c:` is a drive")
def test_a_native_path_keeps_the_os_rules(root: Path) -> None:
    """On POSIX a folder named `c:` is a folder: only a recorded path treats it as a drive."""
    assert inside(root, root / "c:" / "x", read="lexical") == "c:/x"
    assert inside(root, "c:/x", read="recorded") is None


@pytest.mark.skipif(sys.platform != "win32", reason="drives are Windows'")
def test_another_drive_is_outside(root: Path) -> None:
    other = "D:\\x" if str(root)[:1].upper() != "D" else "E:\\x"
    assert inside(root, Path(other), read="lexical") is None


def test_link_dotdot_is_the_folder_above_where_the_link_points(root: Path) -> None:
    """`link/..`, with `link -> a/b`: `a` when links are followed (`follow`, `folder`),
    the root itself when read as text (`lexical`).

    Mutation: read `folder` as `lexical` -> `.`, red."""
    _link(root / "link", root / "a" / "b")
    assert inside(root, root / "link" / "..", read="follow") == "a"
    assert inside(root, root / "link" / "..", read="folder") == "a"
    assert inside(root, root / "link" / ".." / "x", read="folder") == "a/x"
    assert inside(root, root / "link" / "..", read="lexical", root_ok=True) == "."


def test_a_link_out_of_the_project(root: Path, tmp_path: Path) -> None:
    """`follow` says where it lands (outside); `folder` names the link itself; `lexical`
    reads the text."""
    (tmp_path / "elsewhere.csv").write_text("x")
    _link(root / "ln.csv", tmp_path / "elsewhere.csv")
    assert inside(root, root / "ln.csv", read="follow") is None
    assert inside(root, root / "ln.csv", read="folder") == "ln.csv"
    assert inside(root, root / "ln.csv", read="lexical") == "ln.csv"


def test_a_file_under_a_linked_folder_is_named_where_it_lands(root: Path) -> None:
    (root / "store").mkdir()
    _link(root / "data", root / "store")
    assert inside(root, root / "data" / "f.bin", read="folder") == "store/f.bin"
    assert inside(root, root / "data" / "f.bin", read="follow") == "store/f.bin"
    assert inside(root, root / "data", read="folder") == "data"  # the link itself
    assert inside(root, root / "data", read="follow") == "store"


@pytest.mark.parametrize("read", ["follow", "folder"])
def test_a_link_loop_reads_as_outside(root: Path, read: Read) -> None:
    """3.11's `Path.resolve()` raises `RuntimeError` on a loop, and 3.13's says nothing:
    both are outside here.

    Mutation: resolve without detecting the loop -> inside, red."""
    _link(root / "x", root / "y")
    _link(root / "y", root / "x")
    assert inside(root, root / "x" / "f.txt", read=read) is None


def test_a_path_that_does_not_exist_yet_resolves_as_far_as_it_can(root: Path) -> None:
    assert inside(root, root / "a" / "new" / "deeper" / "file.bin", read="follow") == "a/new/deeper/file.bin"


def test_case_is_not_folded(root: Path) -> None:
    """`inside` compares as the OS spells paths; `path_covers` is the one that folds case."""
    other = root.parent / root.name.upper()
    assert inside(root, other / "a", read="lexical") is None


def test_rel_returns_the_path_or_raises(root: Path, tmp_path: Path) -> None:
    """Mutations: drop the path from the message -> the match fails, red; make
    `root_ok` default to False -> `rel(root, root)` raises, red."""
    assert rel(root, root / "a" / "b") == "a/b"
    assert rel(root, root) == "."  # a walk's start is the root itself
    outside = tmp_path / "elsewhere"
    with pytest.raises(ValueError, match=re.escape(f"{outside} is not inside {root}")):
        rel(root, outside)


@pytest.mark.parametrize("read", ["recorded", "lexical", "follow", "folder"])
def test_a_path_holding_a_nul_is_outside_in_every_mode(root: Path, read: Read) -> None:
    """`realpath` raises on a NUL, so `follow` used to; every mode reads it as outside.

    Mutation: drop the NUL rule -> `follow` raises, red."""
    path = "a\x00b" if read == "recorded" else root / "a\x00b"
    assert inside(root, path, read=read) is None
    if read == "recorded":
        assert inside(root, "b", read=read, wdir="a\x00") is None


@pytest.mark.parametrize("read", ["lexical", "follow", "folder"])
def test_a_root_that_is_a_link_is_the_root_in_every_mode(root: Path, read: Read) -> None:
    """`folder` named a linked root by the link and its target by the folder, and
    said None for the root itself.

    Mutation: drop the root-itself check -> `folder` gives None, red."""
    linked = root.parent / "linked-proj"
    _link(linked, root)
    assert inside(linked, linked, read=read, root_ok=True) == "."
    assert inside(linked, linked, read=read) is None
    assert inside(linked, linked / "a", read=read) == "a"


def test_wdir_is_for_recorded_paths_only(root: Path) -> None:
    with pytest.raises(ValueError, match="wdir"):
        inside(root, "x", read="lexical", wdir="code")


def test_a_relative_root_reads_from_the_working_folder(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root-built path (`root / rel`) with a relative root isn't joined twice: both read
    from the working folder, as `os.path` reads them."""
    monkeypatch.chdir(root.parent)
    for read in ("lexical", "follow", "folder"):
        assert inside(Path("proj"), Path("proj") / "a", read=read) == "a"
