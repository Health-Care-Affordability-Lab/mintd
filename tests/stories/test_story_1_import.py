"""Story 1: Bring a dataset into my project.

- Given: a fresh project, and a published product with a primary output.
- When: I run `mintd data import <name>`.
- Then: the primary lands in my project, ready to commit, and `mintd check`
  is clean.
- Pass:
  - every landed file hashes to its pointer's checksum;
  - nothing besides the primary downloads;
  - files are not duplicated;
  - ✓ appears only if files landed;
  - exit 0.
- With `--all`, every declared output lands, and each object downloads once.
- Catches: ✓ printed when nothing landed (the bug fixed in #60).
"""

from __future__ import annotations

import pytest
import yaml

from tests._harness.expect import assert_landed_match_pointers, result, s3_state
from tests._harness.git import _git


def _keys(product, *outs: str) -> dict[str, int]:
    """`{S3 key under the product's prefix: size}` for each file of `outs`."""
    return {f"{out}/{name}": len(body) for out in outs for name, body in product.outs[out].items()}


@pytest.mark.parametrize(("flags", "outs"), [
    ((), ("data/final",)),
    (("--all",), ("data/final", "data/extra")),
], ids=["primary", "all"])
def test_import_lands_what_was_asked_and_downloads_each_object_once(
    product, fresh_project, mintd, s3_server, flags, outs,
) -> None:
    project = fresh_project()

    run = mintd("data", "import", product.name, *flags, cwd=project)

    assert run.returncode == 0, run.stderr
    landed = assert_landed_match_pointers(project)
    wanted = _keys(product, *outs)
    assert sorted(landed) == sorted(f"data/imports/data_{product.name}/{key}" for key in wanted)
    assert result(run.stdout)["file_count"] == len(wanted)
    s3 = s3_state(s3_server, product.prefix)
    # Bytes, not GET counts: a large object downloads in ranged GETs.
    assert set(s3.gets) == set(wanted), "nothing besides what was asked downloads"
    assert s3.bytes_served == wanted, "no byte downloads twice"
    assert mintd("check", cwd=project).returncode == 0


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

    run = mintd("data", "import", product.name, cwd=project, json=False)

    if deliverable:
        assert run.returncode == 0, run.stderr
        assert "✓ imported" in run.stderr
        assert_landed_match_pointers(project)
    else:
        assert "✓" not in run.stderr
        assert run.returncode != 0
        assert _git(["status", "--porcelain", "--untracked-files=all"], cwd=project) == ""
