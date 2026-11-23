"""What a story checks, as helpers. Each Pass line of a story is one call.

The helpers read artifacts, never mintd's word for them: the bytes on disk,
the pointers dvc wrote, and what the S3 server recorded serving.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import os
import posixpath
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from tests._harness.git import _git
from tests._harness.s3 import BUCKET, S3Server


def result(stdout: str) -> dict:
    """The one JSON document a `--json` call prints, and nothing else."""
    return json.loads(stdout)


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def md5_file(path: Path) -> str:
    """A file's md5, read in chunks, so a multi-GB file never sits in memory."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()


class Mismatch(AssertionError):
    """Landed files differ from what dvc tracks. `counts` says how without
    naming a file, for a report that must not show paths or md5s."""

    def __init__(self, detail: str, counts: str) -> None:
        super().__init__(detail)
        self.counts = counts


class CannotCheck(Exception):
    """The check can't tell what a project should hold, so it stops rather
    than guess: a `dvc.yaml` dvc can't read, a step dvc lists that `dvc.yaml`
    doesn't define, or a `${...}` path it would have to expand itself."""


def _files_of(project: Path, root: Path, out: dict) -> dict[str, str]:
    """`{path under project: md5}` for each file one pointer out names."""
    base = root.relative_to(project).as_posix()
    entries = out.get("files")  # a folder out written with its list has no top-level md5
    if entries is None:
        recorded = out["md5"]
        if not recorded.endswith(".dir"):
            return {base: recorded}
        # The tree's manifest, from the project's cache. Its own md5 is the
        # pointer's, so a manifest that does not match the pointer fails here.
        # Read here rather than through mintd's reader: the check shares no
        # code with what it checks.
        h = recorded.removesuffix(".dir")
        manifest = (project / ".dvc" / "cache" / "files" / "md5" / h[:2] / f"{h[2:]}.dir").read_bytes()
        assert md5(manifest) == h, f"{root}: cached manifest does not hash to {recorded}"
        entries = json.loads(manifest)
    return {f"{base}/{e['relpath']}": e["md5"] for e in entries}


def _path_of(entry: str | dict) -> str:
    return entry if isinstance(entry, str) else next(iter(entry))


def _cached(entry: str | dict) -> bool:
    return isinstance(entry, str) or (next(iter(entry.values())) or {}).get("cache", True)


def _load(text: str | None) -> Any:
    """YAML as DVC reads its files: 1.2, with ruamel, DVC's own library, so a
    name like `on` or `no` stays a string. Not mintd's reader: the check shares
    no code with what it checks."""
    from ruamel.yaml import YAML

    return YAML(typ="safe", pure=True).load(text) if text else None


Reader = Callable[[str], "str | None"]
Live = Callable[[str], "set[str]"]


def _inside(rel: str, path: str) -> str:
    """`path`, unless it leaves the project: dvc never writes an out there, so
    a pointer or lock naming one is wrong, and says so plainly."""
    if posixpath.isabs(path) or path == ".." or path.startswith("../"):
        raise Mismatch(f"{rel}: an out resolves outside the project: {path}", "1 out outside the project")
    return path


def _outs(rels: Iterable[str], read: Reader, live: Live,
          skipped: list[str] | None = None) -> Iterator[tuple[str, dict]]:
    """`(path under the project, out)` for each out dvc pulls: every `.dvc`
    pointer's, then every cached out a `dvc.lock` records, against its stage's
    wdir. An out its `dvc.yaml` marks `cache: false` is committed to git,
    never pulled. `rels` are the project's pointers and locks; `read` gives a
    file's text, or None when there is none; `live` gives the steps dvc lists
    for a lock's folder.

    A lock step dvc doesn't list (a stage deleted from `dvc.yaml`, a foreach
    item dropped from its list) is one `dvc pull` skips, so it is skipped and
    added to `skipped`. With no `dvc.yaml` beside a lock, dvc reads no step
    from it. Raises `CannotCheck` rather than guess a step or a path."""
    rels = sorted(rels)
    for rel in rels:
        if rel.endswith(".dvc"):
            for out in _load(read(rel))["outs"]:
                yield _inside(rel, posixpath.normpath(posixpath.join(posixpath.dirname(rel), out["path"]))), out
    for rel in rels:
        if posixpath.basename(rel) != "dvc.lock":
            continue
        base = posixpath.dirname(rel)
        text = read(posixpath.join(base, "dvc.yaml"))
        listed = live(base) if text is not None else set()  # first: dvc names a dvc.yaml it can't read
        stages = (_load(text) or {}).get("stages", {}) if text is not None else {}
        if unknown := sorted(n for n in listed if n.split("@")[0] not in stages):
            raise CannotCheck(f"{rel}: dvc lists steps its dvc.yaml doesn't define: {unknown}")
        for name, stage in (_load(read(rel)) or {}).get("stages", {}).items():
            if name not in listed:
                if skipped is not None:
                    skipped.append(f"{rel}:{name}")
                continue
            body = stages[name.split("@")[0]]
            body = body.get("do", body)  # a foreach or matrix stage
            wdir = body.get("wdir", ".")
            uncached = [_path_of(e) for kind in ("outs", "metrics", "plots") for e in body.get(kind, []) if not _cached(e)]
            if templated := [p for p in (wdir, *uncached) if "${" in p]:
                raise CannotCheck(f"{rel}: step {name} needs {templated} expanded; the check doesn't expand ${{...}}")
            wdir = posixpath.join(base, wdir)
            uncached_paths = {posixpath.normpath(posixpath.join(wdir, p)) for p in uncached}
            for out in stage.get("outs", []):
                path = _inside(rel, posixpath.normpath(posixpath.join(wdir, out["path"])))
                if path not in uncached_paths:
                    yield path, out


def _stage_list(folder: Path) -> set[str]:
    """The steps dvc lists for `folder`'s `dvc.yaml`, foreach and matrix
    instances named by dvc itself, so the check never re-derives dvc's naming.
    Never `--all`: it skips a `dvc.yaml` it can't read and exits 0. dvc's
    global and system config and its site cache point at an empty folder, and
    analytics are off, so the host's dvc settings never reach the answer."""
    with tempfile.TemporaryDirectory() as empty:
        env = {**os.environ, "DVC_NO_ANALYTICS": "1", "DVC_GLOBAL_CONFIG_DIR": f"{empty}/global",
               "DVC_SYSTEM_CONFIG_DIR": f"{empty}/system", "DVC_SITE_CACHE_DIR": f"{empty}/site"}
        r = subprocess.run([sys.executable, "-m", "dvc", "stage", "list", "--name-only"],
                           cwd=folder, env=env, capture_output=True, text=True)
    if r.returncode:
        raise CannotCheck(f"dvc stage list exited {r.returncode} in {folder}: {(r.stderr or r.stdout).strip()}")
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def _on_disk(project: Path) -> tuple[list[str], Reader, Live]:
    """`project`'s pointers and locks as they are on disk, for `_outs`."""
    rels = [p.relative_to(project).as_posix() for pattern in ("*.dvc", "dvc.lock")
            for p in project.rglob(pattern) if p.is_file()]  # not the `.dvc` folder itself

    def read(rel: str) -> str | None:
        path = project / rel
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return rels, read, lambda base: _stage_list(project / base)


def assert_landed_match_pointers(project: Path) -> dict[str, str]:
    """Every file each out in `project` names (`.dvc` pointers and cached
    `dvc.lock` outs of the steps dvc still lists) is on disk and hashes to its
    recorded md5, no other file sits in an out's folder, and at least one file
    landed. Returns `{path under project: md5}`. On a difference it raises
    `Mismatch`, whose `counts` names no file; when it can't tell what should
    land, `CannotCheck`."""
    expected: dict[str, str] = {}
    landed: dict[str, str] = {}
    skipped: list[str] = []
    for rel, out in _outs(*_on_disk(project), skipped):
        root = project / rel
        expected.update(_files_of(project, root, out))
        for f in [root] if root.is_file() else root.rglob("*"):
            if f.is_file():
                landed[f.relative_to(project).as_posix()] = md5_file(f)
    if landed != expected or not landed:
        both = expected.keys() & landed.keys()
        counts = (
            f"{len(expected.keys() - landed.keys())} missing, {sum(expected[k] != landed[k] for k in both)} differ, "
            f"{len(landed.keys() - expected.keys())} extra, of {len(expected)} tracked"
        )
        detail = (
            f"landed files differ from their pointers: {landed} != {expected}" if landed != expected
            else f"no file landed in {project}"
        )
        if skipped:
            detail += f"; lock steps dvc no longer lists, not expected: {skipped}"
        raise Mismatch(detail, counts)
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


def assert_storage_holds_pins(s3: S3Server, repo: Path, prefix: str, *, rev: str = "main",
                              remote: str = "storage") -> int:
    """Story 3's Pass line: storage holds every version the pointers at `rev`
    pin, read from git (the producer's bare repo, which importers read), never
    from mintd. Outs are read as `assert_landed_match_pointers` reads them
    (`_outs`: a stage's wdir applied, `cache: false` and steps dvc no longer
    lists left out), from a checkout of `rev`, since dvc lists steps only in a
    git work tree. `rev` may be a branch or a commit. A pin
    is an out's md5 and its `cloud[remote]` version id; a folder out with a
    `files:` list is one pin per file. Each pinned version is fetched and its bytes hashed. Fails
    when no pin exists (never vacuous), or a pin has no version id, a version
    storage does not hold, or other bytes. Returns the count of pins. Its GETs
    land in the server's records, so read `s3_state` first."""
    from botocore.exceptions import ClientError

    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "checkout"
        _git(["clone", "-q", "--no-checkout", str(repo), str(checkout)])
        _git(["checkout", "-q", rev], cwd=checkout)  # a commit as well as a branch, so not `clone --branch`
        pins = [
            (root if entry is out else f"{root}/{entry['relpath']}", entry.get("md5", ""),
             (entry.get("cloud") or {}).get(remote) or {})
            for root, out in _outs(*_on_disk(checkout)) for entry in out.get("files") or [out]
        ]
    wrong: dict[str, str] = {}
    for path, digest, cloud in pins:
        if not cloud.get("version_id"):  # a .dir record with no file list has none either
            wrong[path] = "no version id"
            continue
        try:
            body = s3.client.get_object(Bucket=BUCKET, Key=f"{prefix}/{path}", VersionId=cloud["version_id"])["Body"].read()
        except ClientError as e:
            wrong[path] = e.response["Error"]["Code"]
            continue
        if md5(body) != digest:
            wrong[path] = "holds other bytes"
    assert pins, f"no pin at {rev} in {repo}"
    assert not wrong, f"storage does not hold what the pointers pin: {wrong}"
    return len(pins)
