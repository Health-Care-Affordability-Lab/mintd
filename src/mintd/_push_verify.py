"""A push to a version-aware remote: repair the pins before it, check them after.

``prepare`` runs before any upload. It refuses what it can't check, plans the
repair of every pin in repair scope, writes it, and hands back the throwaway
``DVC_SITE_CACHE_DIR`` every ``dvc push`` of this command runs with, so DVC's
md5-keyed record of what is on the remote can't be stale. ``restore`` runs
after the uploads and puts back any pointer record DVC wasn't asked to change.
``judge`` then HEADs every pinned version in gate scope and proves its bytes,
and only its verdict may print a ✓.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from ._dvc_state import abbreviate, dvc_cache_dir
from ._fast_sync_ops import (
    _create_s3_client,
    discover_all_outs,
    parse_s3_url,
)
from ._pin_proof import (
    CacheHashes,
    Entry,
    Out,
    PointerWriteError,
    ProofMemo,
    RepairPlan,
    _dump,
    _records,
    cause_lines,
    load_rt,
    plan_repair,
    prove_pins,
    publishable_scope,
    read12,
    write_repair,
)

_CREDENTIAL_KEYS = ("access_key_id", "secret_access_key", "session_token")


class PushRefused(Exception):
    """Push stops before uploading or writing anything."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


def _parsers(root: Path) -> list[configparser.ConfigParser]:
    found = []
    for name in ("config", "config.local"):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(root / ".dvc" / name, encoding="utf-8")
        except configparser.Error:
            continue
        found.append(parser)
    return found


def _section(parser: configparser.ConfigParser, remote: str) -> dict[str, str]:
    from ._storage_state import _extract_remote_section_name

    for name in parser.sections():
        if _extract_remote_section_name(name) == remote:
            return dict(parser.items(name))
    return {}


def merged_remote(root: Path, remote: str) -> dict[str, str]:
    """The remote's settings as DVC pushes with them: ``.dvc/config`` with
    ``.dvc/config.local`` laid over it."""
    merged: dict[str, str] = {}
    for parser in _parsers(root):
        merged.update(_section(parser, remote))
    return merged


def _version_aware(cfg: dict[str, str]) -> bool:
    return str(cfg.get("version_aware", "")).strip().lower() in ("true", "1", "yes")


def merged_default_remote(root: Path) -> str | None:
    """``core.remote`` with config.local over config, as DVC merges them;
    else the one remote both files name between them."""
    from ._storage_state import _extract_remote_section_name

    parsers = _parsers(root)
    for parser in reversed(parsers):
        if parser.has_option("core", "remote"):
            return parser.get("core", "remote")
    names = {n for p in parsers for s in p.sections() if (n := _extract_remote_section_name(s))}
    return names.pop() if len(names) == 1 else None


def _own_remotes(root: Path, dvc_files: list[str], default: str) -> list[str]:
    """Outs that name a remote other than ``default`` (``remote:`` in a
    ``.dvc`` out or a ``dvc.yaml`` out)."""
    named: list[str] = []
    for rel in dvc_files:
        try:
            doc = read12((root / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        for out in (doc.get("outs") or []) if isinstance(doc, dict) else []:
            if isinstance(out, dict) and out.get("remote") not in (None, default):
                named.append(f"{rel}: {out.get('path')}")
    try:
        dvc_yaml = read12((root / "dvc.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        dvc_yaml = None
    stages = dvc_yaml.get("stages") if isinstance(dvc_yaml, dict) else None
    for stage, body in (stages or {}).items() if isinstance(stages, dict) else ():
        for entry in (body.get("outs") or []) if isinstance(body, dict) else []:
            for path, opts in entry.items() if isinstance(entry, dict) else ():
                if isinstance(opts, dict) and opts.get("remote") not in (None, default):
                    named.append(f"dvc.yaml:{stage}: {path}")
    return named


def _in_selection(entry: Entry | Out, selection: frozenset[str] | None) -> bool:
    if selection is None:
        return True
    if entry.pointer == "dvc.lock":
        # `dvc push dvc.yaml:fan` pushes every `fan@*` of a foreach/matrix group.
        return bool({f"dvc.yaml:{entry.stage}", f"dvc.yaml:{(entry.stage or '').split('@', 1)[0]}"} & selection)
    return entry.pointer in selection


def _identity(e: Entry) -> tuple:
    return (e.pointer, e.stage, e.record)


def _proof_id(e: Entry) -> tuple:
    return (e.key, e.version_id, e.md5, e.size)


def _sha256(path: Path) -> str | None:
    """sha256 of the file with CRLF read as LF, the form git stores, so the
    judged bytes compare with a committed blob on any platform."""
    try:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


@dataclass
class Gate:
    """``judge``'s verdict. ``ok`` is the only thing that may print a ✓."""

    ok: bool
    location: str
    counts: dict[str, int]
    lines: list[str]
    frozen: dict[str, str | None] = field(default_factory=dict)
    reattached: int = 0
    uploaded: int = 0
    memo: int = 0

    @property
    def success_line(self) -> str:
        parts = [f"{self.uploaded} uploaded and verified"] if self.uploaded else []
        if self.reattached:
            parts.append(f"{self.reattached} re-attached")
        if self.memo:
            parts.append(f"{self.memo} from memo")
        extra = f" ({', '.join(parts)})" if parts else ""
        return f"{self.counts['checked']} file(s) verified on {self.location}{extra}"

    def drifted(self, root: Path) -> list[str]:
        """Frozen pointer files whose bytes changed since they were judged."""
        return sorted(p for p, digest in self.frozen.items() if _sha256(root / p) != digest)


@dataclass
class Prepared:
    root: Path
    remote: str
    bucket: str
    prefix: str
    s3: Any
    cache: CacheHashes
    dvc_files: list[str]
    repair: tuple[Entry, ...]
    plan: RepairPlan
    written: list[str]
    snapshot: dict[str, Any]
    before: dict[tuple, tuple[str | None, str | None]]
    env: dict[str, str]
    _tmp: tempfile.TemporaryDirectory
    memo: ProofMemo | None = None
    selection: frozenset[str] | None = None

    @property
    def proven(self) -> set[tuple]:
        """``(key, version_id, md5, size)`` of every pin proven (or allowed)
        before the push. A version's bytes don't change, so the gate needn't
        HEAD one again; the tuple binds the proof to the file it proved, not
        to a position in a pointer that DVC may rewrite or reorder."""
        return {_proof_id(e) for e in self.plan.keep} | {_proof_id(new) for _, new, _ in self.plan.reattach}

    @property
    def location(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"

    @property
    def report(self) -> list[str]:
        """What the repair did, for the user: each file and why, then causes."""
        lines: list[str] = []
        # A file with no ID and nothing provable at its key is new data, not a repair.
        strips = [(e, r) for e, r in self.plan.strip if e.version_id or e.etag]
        if self.plan.reattach or strips:
            lines.append(
                f"re-pinned {len(self.plan.reattach) + len(strips)} file(s): "
                f"{len(self.plan.reattach)} re-attached from S3, {len(strips)} re-uploading"
            )
            lines += [f"  re-attached {e.path} ({reason})" for e, _, reason in self.plan.reattach]
            lines += [f"  re-uploading {e.path} ({reason})" for e, reason in strips]
            lines += cause_lines(self.plan, self.location)
        return lines

    def close(self) -> None:
        """Remove the throwaway DVC site cache once the uploads are done. The
        memo stays open for the check after them; it closes with the process."""
        self._tmp.cleanup()


def prepare(
    root: Path, *, remote_arg: str | None, selection: frozenset[str] | None,
    aws_profile: str | None, memo_path: Path | None = None,
) -> Prepared | None:
    """Refuse, plan and write the repair; None when the remote isn't
    version-aware (nothing there is pinned by version)."""
    default = merged_default_remote(root)
    name = remote_arg or default
    if name is None:
        return None
    cfg = merged_remote(root, name)
    versioned = [r for r in {name, default} if r and _version_aware(merged_remote(root, r))]
    if not versioned:
        return None
    if remote_arg is not None and remote_arg != default:
        raise PushRefused(
            f"--remote {remote_arg} isn't the default remote {default}. On a version-aware remote "
            "DVC records the versions it reads from the default remote, so the pins would point nowhere",
            hint="push to the default remote (drop --remote)",
        )
    if any(cfg.get(k) for k in _CREDENTIAL_KEYS):
        raise PushRefused(
            f"the DVC remote {name} carries its own access keys, so mintd can't check the push "
            "as the identity DVC uploads with",
            hint="use an AWS profile instead (dvc remote modify --unset)",
        )
    if not str(cfg.get("url", "")).startswith("s3://"):
        return None
    dvc_files = [f for f in discover_all_outs(root)  # DVC skips subrepos (their own .dvc/)
                 if not any((root / d / ".dvc").is_dir() for d in Path(f).parents if d != Path("."))]
    own = _own_remotes(root, dvc_files, name)
    if own:
        raise PushRefused(
            f"outputs with their own remote aren't supported on a version-aware remote: {abbreviate(own)}",
        )
    bucket, prefix = parse_s3_url(str(cfg["url"]))
    s3 = _create_s3_client(
        cfg, cfg.get("profile") or os.environ.get("AWS_PROFILE") or aws_profile, pool=16,
    )
    cache = CacheHashes(dvc_cache_dir(root))
    scope = publishable_scope(root, remote=name, prefix=prefix, cache_dir=cache.cache_dir,
                              dvc_files=dvc_files)
    repair = tuple(e for e in scope.entries if _in_selection(e, selection))
    stuck = [f"{o.path} (its folder tree isn't in the local cache)" for o in scope.unrecoverable
             if _in_selection(o, selection)]
    memo = None
    if memo_path:
        try:
            memo = ProofMemo(memo_path, cfg.get("endpointurl") or cfg.get("endpoint") or "aws")
        except (OSError, sqlite3.Error):
            memo = None  # an unwritable cache dir costs HEADs, never the push
    plan = plan_repair(s3, bucket, repair, cache, memo)
    stuck += [f"{e.path} ({why})" for e, why in plan.unrecoverable]
    if stuck:
        raise PushRefused(
            f"nothing was written or pushed: {len(stuck)} file(s) are not provably on S3 at their "
            f"current path and can't be re-uploaded: {abbreviate(stuck)}",
            hint="get the files (mintd data pull, or copy them in), then push",
        )
    try:
        written = write_repair(root, plan, name)
    except PointerWriteError as exc:
        raise PushRefused(str(exc)) from exc
    snapshot: dict[str, Any] = {}
    for rel in [*dvc_files, "dvc.lock"]:
        try:
            snapshot[rel] = load_rt((root / rel).read_text(encoding="utf-8"))
        except Exception:  # missing or unparseable: nothing to restore from
            continue
    tmp = tempfile.TemporaryDirectory(prefix="mintd-dvc-site-", ignore_cleanup_errors=True)
    return Prepared(
        root=root, remote=name, bucket=bucket, prefix=prefix, s3=s3, cache=cache,
        dvc_files=dvc_files, repair=repair, plan=plan, written=written, snapshot=snapshot,
        selection=selection,
        before={_identity(e): (e.version_id, e.etag) for e in scope.entries},
        env={"DVC_SITE_CACHE_DIR": tmp.name}, _tmp=tmp, memo=memo,
    )


def restore(prepared: Prepared) -> list[str]:
    """Put back every pointer record DVC changed outside repair scope.

    DVC re-dumps whole stages after a push, so a dep, a ``push: false`` or
    ``cache: false`` sibling, or an out of another stage can come back
    degraded. A push doesn't change the workspace, so the record from before
    it is still true. Returns the pointers rewritten.
    """
    from ._atomic import _atomic_write_text

    in_scope = {_identity(e)[:2] + (e.record[0],) for e in prepared.repair}
    rewritten: list[str] = []
    for rel, before in prepared.snapshot.items():
        path = prepared.root / rel
        try:
            after = load_rt(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if after == before or not isinstance(after, dict) or not isinstance(before, dict):
            continue
        stages: list[str | None] = [None] if rel != "dvc.lock" else list((before.get("stages") or {}))
        changed = False
        for stage in stages:
            old_body = before if stage is None else (before.get("stages") or {}).get(stage)
            new_body = after if stage is None else (after.get("stages") or {}).get(stage)
            if not isinstance(old_body, dict) or not isinstance(new_body, dict):
                continue
            if old_body.get("deps") != new_body.get("deps"):
                new_body["deps"] = old_body.get("deps")
                changed = True
            old_outs, new_outs = _records(before, stage), _records(after, stage)
            if len(old_outs) != len(new_outs):
                continue
            old_at = {o.get("path"): (i, o) for i, o in enumerate(old_outs) if isinstance(o, dict)}
            for j, new in enumerate(new_outs):
                i, old = old_at.get(new.get("path") if isinstance(new, dict) else None, (None, None))
                if old is not None and (rel, stage, i) not in in_scope and old != new:
                    new_outs[j] = old
                    changed = True
        if changed:
            _atomic_write_text(path, _dump(after))
            rewritten.append(rel)
    return rewritten


def _changed_records(root: Path, pointer: str, head_text: str | None) -> set[tuple[str | None, int]]:
    """``(stage, out index)`` of every out in ``pointer`` whose record differs
    from HEAD's record of the same path (all of them when HEAD has no file)."""
    try:
        now = read12((root / pointer).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        now = None
    old = read12(head_text)
    stages: list[str | None] = (
        list((now.get("stages") or {})) if pointer == "dvc.lock" and isinstance(now, dict) else [None]
    )
    found: set[tuple[str | None, int]] = set()
    for stage in stages:
        before = {o.get("path"): o for o in _records(old, stage) if isinstance(o, dict)}
        for i, o in enumerate(_records(now, stage)):
            if not isinstance(o, dict) or before.get(o.get("path")) != o:
                found.add((stage, i))
    return found


def _holds_files(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(p.is_file() for p in folder.rglob("*"))
    except OSError:
        return False


def _head_lists_files(head_text: str | None, out: Out) -> bool:
    """HEAD's record of ``out`` lists files (or names a folder tree)."""
    doc = read12(head_text)
    records = _records(doc, out.stage)
    rec = records[out.index] if out.index < len(records) else None
    return isinstance(rec, dict) and bool(rec.get("files") or str(rec.get("md5") or "").endswith(".dir"))


def judge(
    prepared: Prepared, changed: list[str] | None, *, head: dict[str, str | None],
    dvc_uploaded_nothing: bool,
) -> Gate:
    """HEAD every pinned version in gate scope and prove its bytes.

    Gate scope is the repair scope plus every out whose record differs from
    HEAD's (what the commit would change; ``head`` maps each changed pointer
    to its HEAD text). An unchanged record can't be made worse by the commit.
    An entry this command set or
    re-attached must prove its file; one it didn't touch may also pass a
    multipart allowance on size. Every file re-uploaded must now hold a new
    version that proves it.
    """
    root = prepared.root
    loc = prepared.location
    if changed is None:
        return Gate(False, loc, {"checked": 0}, [
            "git couldn't list the pointer files that differ from the last commit, so the push "
            "can't be checked"
        ])
    # Freeze before parsing: hash every pointer that can be in gate scope,
    # parse, then hash again. Every later step judges what was hashed.
    candidates = sorted({e.pointer for e in prepared.repair} | set(changed))
    frozen = {p: _sha256(root / p) for p in candidates}
    scope = publishable_scope(root, remote=prepared.remote, prefix=prepared.prefix,
                              cache_dir=prepared.cache.cache_dir, dvc_files=prepared.dvc_files)
    moved = [p for p in candidates if _sha256(root / p) != frozen[p]]
    if moved:
        return Gate(False, loc, {"checked": 0}, [f"{abbreviate(moved)} changed while the push was being checked"])
    repair_ids = {_identity(e) for e in prepared.repair}
    differs = {
        (p, stage, i) for p in changed for stage, i in _changed_records(root, p, head.get(p))
    }
    entries = [
        e for e in scope.entries
        if _identity(e) in repair_ids or (e.pointer, e.stage, e.record[0]) in differs
    ]
    # Structural: a folder out left listing no files, while the folder on disk
    # or HEAD's pointer holds some, has no entry to prove; and every file the
    # push was to re-upload must still be in its pointer.
    hollow = [
        o.path for o in scope.empty
        if (_in_selection(o, prepared.selection) or (o.pointer, o.stage, o.index) in differs)
        and (_holds_files(root / o.path) or _head_lists_files(head.get(o.pointer), o))
    ]
    after = {(e.pointer, e.out, e.relpath) for e in scope.entries}
    dropped = [e.path for e, _ in prepared.plan.strip if (e.pointer, e.out, e.relpath) not in after]
    proven = prepared.proven
    need = [e for e in entries if e.version_id and _proof_id(e) not in proven]
    verdicts = prove_pins(prepared.s3, prepared.bucket, need, prepared.cache, prepared.memo)
    # A re-uploaded file still holding the version seen before the push fails
    # the proof (re-attach takes every version that proves its file), so the
    # proof alone settles whether each upload arrived.
    uploads = {_identity(e) for e, _ in prepared.plan.strip}
    failed: dict[str, list[str]] = {}
    gate_only: list[str] = []
    not_arrived = 0
    counts = dict.fromkeys(
        ("ok", "missing", "unpinned", "unproven", "wrong_bytes", "no_id", "inconclusive"), 0,
    )
    for e in entries:
        ident = _identity(e)
        touched = prepared.before.get(ident) != (e.version_id, e.etag)
        if not e.version_id:
            verdict = "no_id"
        elif _proof_id(e) in proven:
            verdict = "ok"
        else:
            verdict = verdicts[(e.key, e.version_id)]
            if verdict == "size_only":
                verdict = "unproven" if touched else "ok"
        counts[verdict] += 1
        if verdict != "ok":
            failed.setdefault(verdict, []).append(e.path)
            not_arrived += ident in uploads
            if ident not in repair_ids:
                gate_only.append(e.path)
    counts["checked"] = len(entries)
    counts["memo"] = prepared.memo.hits if prepared.memo is not None else 0
    reasons = {
        "missing": "S3 has no such version at the file's path",
        "unpinned": "S3 holds no pinnable version (versioning suspended?)",
        "unproven": "the version's bytes can't be proven (multipart, no matching cache file)",
        "wrong_bytes": "the pinned version holds other bytes",
        "no_id": "no S3 version ID recorded",
        "inconclusive": "S3 didn't answer",
    }
    lines = [f"{reasons[v]}: {abbreviate(paths)}" for v, paths in failed.items()]
    if hollow:
        lines.append(f"lists no files, but the folder or the last commit holds some: {abbreviate(hollow)}")
    if dropped:
        lines.append(f"needed uploading but the push dropped them from their pointer: {abbreviate(dropped)}")
    counts["structural"] = len(hollow) + len(dropped)
    if not_arrived and dvc_uploaded_nothing:
        lines.append(f"DVC reported nothing to upload, but {not_arrived} file(s) needed uploading")
    if gate_only:
        lines.append(
            f"not in this push: {abbreviate(gate_only)}; push them too (mintd data push <path>)"
        )
    return Gate(
        ok=not failed and not hollow and not dropped, location=loc, counts=counts, lines=lines, frozen=frozen,
        reattached=len(prepared.plan.reattach), uploaded=len(prepared.plan.strip),
        memo=prepared.memo.hits if prepared.memo is not None else 0,
    )
