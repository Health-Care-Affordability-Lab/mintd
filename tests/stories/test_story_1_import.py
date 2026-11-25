"""Story 1: Bring a dataset into my project.

What import brings: by default, only the product's primary; with `--path`,
only the named dataset or folder; with `--all`, everything the producer
tracks at the pinned rev. In every case each byte downloads once and nothing
outside the request is fetched.

- Given: a fresh project, and a published product that has a primary output,
  a declared non-primary output (`data/extra`), and a tracked folder no output
  declares (`data/raw`).
- Pass, in every case:
  - every landed file hashes to its pointer's checksum;
  - each byte downloads once, and nothing outside the request is fetched;
  - ✓ appears only if files landed;
  - exit 0.
- Catches: ✓ printed when nothing landed (the bug fixed in #60).

Default case (Holds):
- When: I run `mintd data import <name>` (later `mintd use <name>`).
- Then: only the primary lands in my project, ready to commit, and
  `mintd check` is clean.
- Pass: nothing besides the primary downloads.

`--path` case (Holds):
- When: I run `mintd data import <name> --path data/extra`, and separately
  `--path data/raw`.
- Then: only the named dataset or folder lands: a declared non-primary output,
  and an undeclared tracked folder.
- Pass: every file under the named path lands and matches; nothing outside it
  downloads, the primary included.

`--all` case (Target until `--all` changes):
- When: I run `mintd data import <name> --all`.
- Then: everything the producer tracks at that rev lands: every `.dvc` pointer
  and every `dvc.lock` out, declared or not.
- Pass: every tracked file lands, `data/raw` included.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests._harness.expect import assert_landed_match_pointers, md5, result, s3_state
from tests._harness.git import _git


def _changed(project: Path) -> set[str]:
    """The paths `git status` reports, untracked files listed one by one."""
    status = _git(["status", "--porcelain", "--untracked-files=all"], cwd=project)
    return {line[3:] for line in status.splitlines()}


def _tree(project: Path) -> set[str]:
    """Every file and folder outside .git and .dvc, empty folders included,
    which `git status` cannot see."""
    return {
        p.relative_to(project).as_posix() for p in project.rglob("*")
        if not {".git", ".dvc"} & set(p.relative_to(project).parts)
    }


@pytest.mark.parametrize(("flags", "outs"), [
    pytest.param((), ("data/final",), id="primary"),
    pytest.param(("--path", "data/extra"), ("data/extra",), id="path-extra"),
    pytest.param(("--path", "data/raw"), ("data/raw",), id="path-raw"),
    # Everything DVC tracks, the undeclared data/raw included.
    pytest.param(("--all",), ("data/final", "data/extra", "data/raw"), id="all"),
])
def test_import_lands_what_was_asked_and_downloads_each_object_once(
    product, fresh_project, mintd, s3_server, flags, outs,
) -> None:
    project = fresh_project()
    root = f"data/imports/data_{product.name}"

    run = mintd("data", "import", product.name, *flags, cwd=project)

    assert run.returncode == 0, run.stderr
    published = product.published(*outs)
    landed = assert_landed_match_pointers(project)
    assert landed == {f"{root}/{key}": md5(body) for key, body in published.items()}, "landed bytes are the producer's"
    assert result(run.stdout)["file_count"] == len(published)
    s3 = s3_state(s3_server, product.prefix)
    # Bytes, not GET counts: a large object downloads in ranged GETs.
    assert set(s3.gets) == set(published), "nothing besides what was asked downloads"
    assert s3.bytes_served == {key: len(body) for key, body in published.items()}, "no byte downloads twice"
    assert mintd("check", cwd=project).returncode == 0
    assert _changed(project) == {f"{root}/{out}.dvc" for out in outs} | {f"{root}/data/.gitignore"}


def _drop_files_list(product) -> None:
    """Rewrite the primary's pointer as `dvc commit -f` leaves a folder out:
    the tree's md5, with no `files:` list and so no S3 version IDs. On a
    version-aware remote nothing can fetch it."""
    pointer = product.producer.work / "data" / "final.dvc"
    doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    doc["outs"][0].pop("files")
    pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    product.producer._commit_and_push("primary pointer without its files list")


@pytest.mark.parametrize("deliverable", [True, False], ids=["delivers", "nothing-deliverable"])
def test_tick_only_when_files_landed(product, fresh_project, mintd, deliverable) -> None:
    if not deliverable:
        _drop_files_list(product)
    project = fresh_project()
    before = _tree(project)

    run = mintd("data", "import", product.name, cwd=project, json=False)

    if deliverable:
        assert run.returncode == 0, run.stderr
        assert "✓ imported" in run.stderr
        assert_landed_match_pointers(project)
    else:
        assert "✓" not in run.stdout + run.stderr
        assert run.returncode != 0
        assert _changed(project) == set()
        assert _tree(project) == before, "a failed import leaves no folder behind"
