"""Story 2: A failed import leaves my folder as it was (Holds).

- Given: a project with files and git history.
- When: an import fails partway: the producer is unreachable, or a file is
  missing from storage.
- Then: an error says what failed and what to do; the exit code is non-zero;
  the hint names the cause: the producer URL it could not reach, or the
  producer's data missing from storage.
- Pass:
  - files, HEAD, the git index and refs are byte-identical to before, with no
    half-made folder;
  - after fixing the cause, a rerun succeeds.
- Catches: folders left behind by a failed import (the bug fixed in #65).

Partial-transfer case (Target until a failed import stops leaving behind the
files it did land):
- When: an import fails after some files landed (one of the primary's two
  files is missing from storage), I fix storage and run the import again.
- Then: the rerun never exits 0 with a nested layout: it lands the primary
  flat, or it refuses and names the stray files.
- Pass: ideally the failed import leaves no file of its own, and the rerun
  lands `data/final/{a,b}.csv` flat, matching the producer's bytes; a rerun
  that exits non-zero names every stray file.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest
import yaml

from tests._harness import HarnessError
from tests._harness.expect import assert_landed_match_pointers, assert_unchanged, md5, snapshot
from tests._harness.git import _git
from tests._harness.producer import register
from tests._harness.s3 import BUCKET


def _delete_pinned(product, s3_server, names: set[str]):
    """Delete the pinned versions of the primary's files `names`; return the
    fix that uploads them again and re-pins the pointer."""
    if not s3_server.endpoint.startswith("http://127.0.0.1:"):  # not an assert: an xfail mark below would swallow it
        raise HarnessError(f"refusing to delete on {s3_server.endpoint}: deletes only on the emulator")
    pointer = product.producer.work / "data" / "final.dvc"
    doc = yaml.safe_load(pointer.read_text(encoding="utf-8"))
    gone = [f for f in doc["outs"][0]["files"] if f["relpath"] in names]
    if {f["relpath"] for f in gone} != names:  # pytest.fail: an xfail mark swallows a plain assert
        pytest.fail(f"no pinned file for {names - {f['relpath'] for f in gone}}")
    for f in gone:  # the emulator's client: never a real bucket
        s3_server.client.delete_object(
            Bucket=BUCKET, Key=f"{product.prefix}/data/final/{f['relpath']}",
            VersionId=f["cloud"]["storage"]["version_id"],
        )

    def fix() -> None:
        for f in gone:
            put = s3_server.client.put_object(
                Bucket=BUCKET, Key=f"{product.prefix}/data/final/{f['relpath']}",
                Body=product.outs["data/final"][f["relpath"]],
            )
            f["cloud"]["storage"]["version_id"] = put["VersionId"]  # same bytes, so the etag stays
        pointer.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        product.producer._commit_and_push("upload the primary again")

    return fix


def _unreachable(product, s3_server, registry: Path, tmp_path: Path):
    # A port bound and closed again refuses at once. One bound but not
    # listening would time out instead (about 20 s on macOS).
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    meta = product.producer.metadata()
    meta["repository"]["github_url"] = f"http://127.0.0.1:{port}/{product.name}.git"
    register(registry, meta, tmp_path / "registry-broken")

    def fix() -> None:
        register(registry, product.producer.metadata(), tmp_path / "registry-fixed")

    return [f"127.0.0.1:{port}"], fix


def _missing(product, s3_server, registry: Path, tmp_path: Path):
    # Every file of the primary goes, so nothing lands; one file missing is
    # the partial-transfer case below.
    fix = _delete_pinned(product, s3_server, set(product.outs["data/final"]))
    return [f"data/final/{name}" for name in product.outs["data/final"]], fix


# What each cause's hint must say (the URL check is in the test).
_HINT = {
    _unreachable: "could not reach the producer's repository",
    _missing: "the producer's data, or data it imports from another product, is missing from storage",
}


def _files_under(root: Path) -> set[str]:
    """Project-relative path of every file under `root` (`root` is `<project>/data`)."""
    return {p.relative_to(root.parent).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("cause", [_unreachable, _missing], ids=["producer-unreachable", "files-missing"])
def test_a_failed_import_leaves_the_project_as_it_was(
    product, fresh_project, mintd, s3_server, remote_registry_empty, tmp_path, cause,
) -> None:
    project = fresh_project()
    (project / "analysis.py").write_text("print('analysis')\n", encoding="utf-8")
    _git(["add", "analysis.py"], cwd=project)
    _git(["commit", "-m", "analysis"], cwd=project)
    (project / "notes.md").write_text("draft\n", encoding="utf-8")
    _git(["add", "notes.md"], cwd=project)  # staged, so the index differs from HEAD
    named, fix = cause(product, s3_server, remote_registry_empty, tmp_path)
    before = snapshot(project)

    run = mintd("data", "import", product.name, cwd=project)

    said = "".join(run.stderr.split())  # the console wraps at 80 columns, mid-word too: no space or break is kept
    assert run.returncode != 0, said
    assert "error:" in said and any(n in said for n in named), said
    assert "hint:" in said, said
    hint = said.split("hint:", 1)[1]
    assert "".join(_HINT[cause].split()) in hint, said
    if cause is _unreachable:
        assert named[0] in hint, said  # the URL it tried
    assert_unchanged(before, snapshot(project))

    fix()
    rerun = mintd("data", "import", product.name, cwd=project)
    assert rerun.returncode == 0, rerun.stderr
    assert assert_landed_match_pointers(project) == {
        f"data/imports/data_{product.name}/{k}": md5(b) for k, b in product.published("data/final").items()
    }


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "Target: a failed import keeps the files that did land, and the rerun nests the primary at data/final/final"))
def test_a_rerun_after_a_partial_transfer_lands_the_primary_flat(product, fresh_project, mintd, s3_server) -> None:
    project = fresh_project()
    fix = _delete_pinned(product, s3_server, {"b.csv"})  # a.csv still lands

    run = mintd("data", "import", product.name, cwd=project)
    if run.returncode == 0:  # not the Target's to excuse: an import missing a file must fail
        pytest.fail(f"the import exited 0 with data/final/b.csv missing from storage\n{run.stderr}")
    left = {p for p in _files_under(project / "data") if not p.endswith((".dvc", ".gitignore"))}

    fix()
    rerun = mintd("data", "import", product.name, cwd=project)
    if rerun.returncode != 0:  # refused: mintd's own error names every file the failed import left
        assert re.search(r"^error: ", rerun.stderr, re.M), rerun.stderr
        assert "Traceback" not in rerun.stdout + rerun.stderr, rerun.stderr
        said = "".join(rerun.stderr.split())
        assert left and all(p in said for p in left), rerun.stderr
        return
    landed = assert_landed_match_pointers(project)
    assert landed == {
        f"data/imports/data_{product.name}/{k}": md5(b) for k, b in product.published("data/final").items()
    }, "the primary must land flat"
    data = {p for p in _files_under(project / "data") if not p.endswith((".dvc", ".gitignore"))}
    assert data == set(landed), "a file beside the import that no pointer explains"
