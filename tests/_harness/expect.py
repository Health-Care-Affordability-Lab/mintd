"""What a story checks, as helpers. Each Pass line of a story is one call.

The helpers read artifacts, never mintd's word for them: the bytes on disk,
the pointers dvc wrote, and what the S3 server recorded serving.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
from pathlib import Path

import yaml

from tests._harness.git import _git
from tests._harness.s3 import BUCKET, S3Server


def result(stdout: str) -> dict:
    """The one JSON document a `--json` call prints, and nothing else."""
    return json.loads(stdout)


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _files_of(project: Path, root: Path, out: dict) -> dict[str, str]:
    """`{path under project: md5}` for each file one pointer out names."""
    base = root.relative_to(project).as_posix()
    recorded = out["md5"]
    if not recorded.endswith(".dir"):
        return {base: recorded}
    entries = out.get("files")
    if entries is None:
        # The tree's manifest, from the project's cache. Its own md5 is the
        # pointer's, so a manifest that does not match the pointer fails here.
        # Read here rather than through mintd's reader: the check shares no
        # code with what it checks.
        h = recorded.removesuffix(".dir")
        manifest = (project / ".dvc" / "cache" / "files" / "md5" / h[:2] / f"{h[2:]}.dir").read_bytes()
        assert md5(manifest) == h, f"{root}: cached manifest does not hash to {recorded}"
        entries = json.loads(manifest)
    return {f"{base}/{e['relpath']}": e["md5"] for e in entries}


def assert_landed_match_pointers(project: Path) -> dict[str, str]:
    """Every file each `.dvc` pointer in `project` names is on disk and hashes
    to the pointer's md5, no other file sits in a pointer's folder, and at
    least one file landed. Returns `{path under project: md5}`."""
    expected: dict[str, str] = {}
    landed: dict[str, str] = {}
    for pointer in sorted(project.rglob("*.dvc")):
        if not pointer.is_file():
            continue  # the `.dvc` folder itself
        for out in yaml.safe_load(pointer.read_text(encoding="utf-8"))["outs"]:
            root = pointer.parent / out["path"]
            expected.update(_files_of(project, root, out))
            for f in [root] if root.is_file() else root.rglob("*"):
                if f.is_file():
                    landed[f.relative_to(project).as_posix()] = md5(f.read_bytes())
    assert landed == expected, f"landed files differ from their pointers: {landed} != {expected}"
    assert landed, f"no file landed in {project}"
    return landed



def snapshot(project: Path) -> dict[str, str]:
    """What a failed command must leave as it was: each file's md5 and each
    folder (an empty one too, which git cannot see) by path, plus HEAD, the
    index and the refs. Left out: .git, read through git instead, and dvc's
    scratch (.dvc/tmp, lock files on every run) and the objects it fetched
    (.dvc/cache)."""
    state: dict[str, str] = {}
    for p in project.rglob("*"):
        rel = p.relative_to(project)
        if rel.parts[0] == ".git" or rel.parts[:2] in {(".dvc", "tmp"), (".dvc", "cache")}:
            continue
        state[rel.as_posix()] = md5(p.read_bytes()) if p.is_file() else "folder"
    for args in (["rev-parse", "HEAD"], ["ls-files", "-s"], ["for-each-ref"]):
        state["git " + " ".join(args)] = _git(args, cwd=project)
    return state


def assert_unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    """No file, folder, HEAD, index entry or ref changed, appeared or went."""
    changed = {k: (before.get(k), after.get(k)) for k in sorted(before.keys() | after.keys())
               if before.get(k) != after.get(k)}
    assert not changed, f"changed, added or removed (before, after): {changed}"


@dataclasses.dataclass
class S3State:
    """Per key under a prefix: object PUTs, object GETs, and bytes served."""

    puts: collections.Counter[str]
    gets: collections.Counter[str]
    bytes_served: collections.Counter[str]


def s3_state(s3: S3Server, prefix: str) -> S3State:
    """What the server recorded under `s3://<bucket>/<prefix>/`, keyed by the
    path below the prefix. Requests outside the prefix are left out."""
    root = f"/{BUCKET}/{prefix.strip('/')}/"
    state = S3State(collections.Counter(), collections.Counter(), collections.Counter())
    for path in s3.puts:
        if path.startswith(root):
            state.puts[path.removeprefix(root)] += 1
    for path, size in s3.gets:
        if path.startswith(root):
            state.gets[path.removeprefix(root)] += 1
            state.bytes_served[path.removeprefix(root)] += size
    return state
