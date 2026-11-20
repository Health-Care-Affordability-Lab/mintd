"""The enclave lane: pinned data products, bundled for a locked-down server.

`enclave_manifest.yaml` is written by people and appended to by mintd:

- `approved_products[]` names each product, and the pin, that may cross.
  People edit it; mintd never rewrites it.
- `transferred[]` is the outside audit log. `enclave_package` appends a
  `packaged` row for each version it bundles, one JSON object per line,
  which YAML reads as a mapping. `transferred:` stays the last section, so a
  written row or comment is never rewritten.

`enclave_package` brings each approved version in as an ordinary import
(`data.import_product` at the approved pin) and writes one zip whose paths
mirror the enclave folder:

    data/<product>/<version>/<producer path>/...
    transfers/<transfer id>/MD5SUMS        one `<md5>  <path>` line per file
    transfers/<transfer id>/manifest.json  enclave, products, versions, date
    transfers/<transfer id>/README.md      the unzip and check commands

The version folder is `<the pin's committer date, UTC>_<first 7 of the pin>`,
so it is the same in every enclave. Every file's md5 comes from its import
pointer and is checked as the file goes into the zip. Inside, `unzip -n` puts
the files in place and never overwrites one, and the operating system's own
md5 tool checks them against MD5SUMS; nothing inside needs Python or mintd.
`transfers/<transfer id>/` is the inside record: each bundle adds one folder.

What this gives up, by design: the check runs after the files are in place,
nothing refuses a bundle built for another enclave (the README and
manifest.json name it), and nothing records a landing date inside.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import re
import stat
import time
from typing import TYPE_CHECKING, Any
import zipfile

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ._atomic import _atomic_write_text
from ._dvc_ops import DvcOps
from ._dvc_state import dvc_cache_dir
from ._fast_sync_ops import DvcFileEntry, cache_path_for, dir_manifest_md5, parse_dvc_outs, read_cached_dir_manifest
from ._producer_git_ops import FetchError, GitArchiveFetcher
from .catalog import CatalogClient
from .check import _POINTER_ABSENT, MatchKind, _out_identity, _pointer_match
from .data import _import_namespace, _imports_index, _require_repo_url, import_product
from .errors import MintdError
from .imports import DataDependency
from .producer import ProducerError, ProducerView

if TYPE_CHECKING:
    from ._console import Reporter

__all__ = [
    "ApprovedProduct",
    "ChecksumMismatch",
    "EnclaveManifest",
    "ManifestNotAppendable",
    "NothingNewToPackage",
    "NothingToPackage",
    "enclave_package",
]


class NothingToPackage(MintdError):
    """No approved product matched the selection."""

    hint = "approve a version with a row in approved_products in enclave_manifest.yaml"


class NothingNewToPackage(NothingToPackage):
    """Every selected version has already crossed the gap.

    A routine outcome, not a failure: the CLI catches it first and exits 0."""


class ChecksumMismatch(MintdError):
    """An imported file is missing, or does not hash to the md5 its import
    pointer records."""


class ManifestNotAppendable(MintdError):
    """`transferred:` is missing, or is not the manifest's last section."""

    hint = "move transferred: to the end of enclave_manifest.yaml"


class ApprovedProduct(BaseModel):
    """One approval: a catalog product, at a producer commit (`pin`), for one
    output (`source_path`), every output (`all`), or the primary (neither)."""

    model_config = ConfigDict(extra="forbid")
    repo: str
    # A full commit SHA: a tag or branch can move after it was approved.
    pin: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_path: str | None = None
    all: bool = False
    # Written by older mintd and ignored: the catalog says where the entry is.
    registry_entry: str | None = None

    @field_validator("source_path")
    @classmethod
    def _a_producer_path(cls, v: str | None) -> str | None:
        """A path as the producer's outputs name it: `./x`, `/x`, `a//b`, `..`
        or an empty path names no output, so it is refused before any import."""
        if v is not None and (
            v.startswith("/") or "\\" in v or any(s in ("", ".", "..") for s in v.rstrip("/").split("/"))
        ):
            raise ValueError("a relative producer path such as data/final, with no empty, '.' or '..' part")
        return v


class EnclaveManifest(BaseModel):
    """Typed view of `enclave_manifest.yaml`. Unknown keys are refused, so a
    misspelt `source_path` cannot quietly approve the primary instead."""

    model_config = ConfigDict(extra="forbid")
    enclave_name: str
    approved_products: list[ApprovedProduct] = []
    # Rows from older mintd have no `event`, `path`, `version` or `dest`.
    transferred: list[dict[str, Any]] = []
    # Written by older mintd and ignored; `downloaded[]` is now the import
    # pointers under data/imports/.
    schema_version: str | None = None
    registry_url: str | None = None
    downloaded: list[Any] | None = None

    @classmethod
    def load(cls, path: Path) -> "EnclaveManifest":
        try:
            with path.open(encoding="utf-8") as fh:
                return cls.model_validate(yaml.safe_load(fh) or {})
        except FileNotFoundError:
            raise MintdError(
                f"{path} not found",
                hint="run this from the enclave repo root, or pass --manifest <path>",
            ) from None
        except (yaml.YAMLError, ValidationError) as exc:
            raise MintdError(
                f"{path} is not a valid enclave manifest: {exc}",
                hint="pins are full 40-character commit SHAs, and unknown keys are refused",
            ) from exc
        except UnicodeDecodeError as exc:
            raise MintdError(f"{path} is not UTF-8: {exc}", hint="save it as UTF-8") from exc
        except OSError as exc:
            raise MintdError(f"{path} cannot be read: {exc}") from exc


@dataclass(frozen=True)
class _Version:
    """One output of one product at one pin, as imported on this machine."""

    repo: str
    path: str
    pin: str  # the commit the import pointer records (`rev_lock`)
    md5: str  # the out's md5 in that pointer
    src: Path
    files: tuple[tuple[str, str], ...]  # (relpath under src, "" for a file out; md5)
    unit: str = ""  # the import it is read from, when that is a folder above `path`
    anchored: bool = True  # checked against the producer's own pointer (`_check_against_producer`)


def enclave_package(
    client: CatalogClient,
    dvc_ops: DvcOps,
    *,
    manifest_path: Path,
    name: str | None = None,
    output_archive: Path | None = None,
    output_dir: Path | None = None,
    resend: bool = False,
    commit_time: Callable[[str, str], int] | None = None,
    reporter: "Reporter | None" = None,
    warnings: list[str] | None = None,
) -> tuple[Path, list[tuple[str, str, str]]]:
    """Bring in every approved version, bundle the new ones, record them.

    Exactly one of `output_archive` / `output_dir` is given; with `output_dir`
    the archive is `<output_dir>/<transfer_id>.zip`. An existing archive is
    refused, never replaced.

    Bundles are incremental: a version whose `(repo, path, pin)` is already in
    `transferred[]` is neither imported nor bundled (rows from older mintd,
    which record no path, match any path at their pin), and
    `NothingNewToPackage` is raised when nothing is left. `resend=True`
    bundles them anyway, for a bundle that was built but never arrived.

    Each (product, pin) is written into the zip right after it is imported,
    so two approved pins of one output, which share one import folder, both
    cross. The manifest is appended to only after the zip is complete, and on
    a failure the zip is removed unless the log already records it. A hard
    kill skips that cleanup and can leave a zip with no row, which the rerun
    refuses as already existing.

    Returns `(archive, skipped)`, `skipped` as `(repo, path, pin)`. A path
    the producer's own pointer does not name on its own crosses with a
    warning, printed and written into the bundle's manifest.json; when
    `warnings` is given, the same lines are appended to it.
    """
    if output_archive is None and output_dir is None:
        raise ValueError("Either output_archive or output_dir must be provided")
    manifest = EnclaveManifest.load(manifest_path)
    # Refuse a log rows cannot be appended to before anything is imported or
    # written, with a probe row in place of the real ones.
    _with_rows(manifest_path.read_bytes().decode("utf-8"), [{"event": "probe"}], manifest_path)
    root = manifest_path.parent
    rows = [ap for ap in manifest.approved_products if name is None or ap.repo == name]
    if not rows:
        raise NothingToPackage(
            f"no approved_products{' for ' + name if name else ''} in {manifest_path}"
        )

    # Resolve every version before anything is written.
    shipped = {(r.get("repo"), r.get("path"), r.get("contract_pin")) for r in manifest.transferred}
    entries: dict[str, dict[str, Any]] = {}
    pending: dict[tuple[str, str], list[str]] = {}  # (repo, pin) -> paths
    skipped: list[tuple[str, str, str]] = []
    for ap in rows:
        entry = client.fetch(ap.repo).model_dump()
        entries[ap.repo] = entry
        repo_url = _require_repo_url(entry, name=ap.repo)
        for path in _resolve_outputs(ap, repo_url, ProducerView.at):
            key = (ap.repo, path.rstrip("/"), ap.pin)
            if key[1] in pending.get((ap.repo, ap.pin), []) or key in skipped:
                continue
            if not resend and (key in shipped or (ap.repo, None, ap.pin) in shipped):
                skipped.append(key)
                continue
            pending.setdefault((ap.repo, ap.pin), []).append(key[1])
    # Every path this run approves, crossed or not: a path inside one of them
    # is read from that folder's import (see `_bring_in`).
    approved: dict[str, set[str]] = {}
    for repo, path, _pin in [*skipped, *((r, p, q) for (r, q), ps in pending.items() for p in ps)]:
        approved.setdefault(repo, set()).add(path)
    if not pending:
        raise NothingNewToPackage(
            f"all {len(skipped)} approved version(s){' for ' + name if name else ''} "
            "have already crossed the gap"
        )
    fetcher = GitArchiveFetcher()
    producer_pointers: dict[tuple[str, ...], Any] = {}
    when = commit_time or fetcher.commit_time_at
    versions: dict[tuple[str, str], str] = {}
    for repo, pin in pending:
        try:
            seconds = when(_require_repo_url(entries[repo], name=repo), pin)
        except FetchError as exc:
            raise ProducerError.from_fetch_error(exc) from exc
        versions[(repo, pin)] = f"{datetime.fromtimestamp(seconds, timezone.utc).date().isoformat()}_{pin[:7]}"

    # `dvc import` needs a DVC repo; an enclave keeps no DVC remote of its own.
    if not (root / ".dvc").exists():
        dvc_ops.init(cwd=root)
    today = date.today().isoformat()
    transfer_id = _next_transfer_id(manifest, today)
    if output_archive is None:
        assert output_dir is not None  # for mypy; checked above
        output_archive = output_dir / f"{transfer_id}.zip"
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(output_archive, "x", zipfile.ZIP_STORED, allowZip64=True, strict_timestamps=False)
    except FileExistsError:
        raise MintdError(f"{output_archive} already exists", hint="remove it, or pass --output <path>") from None

    contents: list[dict[str, Any]] = []
    sums: dict[str, str] = {}
    unanchored: list[str] = []
    try:
        with zf:
            groups = sorted(pending, key=lambda k: f"data/{k[0]}/{versions[k]}/")
            for repo, pin in groups:
                version = versions[(repo, pin)]
                members: dict[str, tuple[Path, str, _Version]] = {}
                for path in pending[(repo, pin)]:
                    v = _bring_in(
                        client, dvc_ops, root, entries[repo], repo, pin, path, approved[repo], reporter,
                        partial(_pointer_match, fetcher, _require_repo_url(entries[repo], name=repo), pin,
                                memo=producer_pointers),
                    )
                    if not v.anchored:
                        unanchored.append(
                            f"{repo} {path} at {pin[:7]}: the bytes match the local import, but are not checked "
                            "against the producer's own record, whose pointer does not name this path on its own"
                        )
                        if reporter is not None:
                            reporter.warn(unanchored[-1])
                    dest = _member(f"data/{repo}/{version}/{path}")
                    for rel, md5 in v.files:
                        _hold(members, _member(f"{dest}/{rel}" if rel else dest), (v.src / rel if rel else v.src, md5, v),
                              _import_again(dvc_cache_dir(root), repo, v.unit or v.path, pin))
                    contents.append({
                        "repo": repo, "path": path, "version": version, "contract_pin": v.pin,
                        "artifact_pin": v.md5, "dest": dest,
                    })
                for member in sorted(members):
                    src, md5, of = members[member]
                    try:
                        matches = _md5(src) == md5
                    except FileNotFoundError:
                        matches = False
                    if not matches:
                        raise ChecksumMismatch(
                            f"{src} is missing or does not match its pin (md5 {md5})",
                            hint=_import_again(dvc_cache_dir(root), repo, of.unit or of.path, of.pin, md5),
                        )
                    # `write` stats through a link, so the entry is a regular
                    # file holding the bytes just checked.
                    zf.write(src, member)
                    sums[member] = md5
            transfer = {
                "enclave_name": manifest.enclave_name,
                "transfer_id": transfer_id,
                "transfer_date": today,
                "contents": contents,
                **({"warnings": unanchored} if unanchored else {}),
            }
            if warnings is not None:
                warnings.extend(unanchored)
            record = f"transfers/{transfer_id}"
            for member, text in (
                (f"{record}/MD5SUMS", "".join(f"{md5}  {m}\n" for m, md5 in sorted(sums.items()))),
                (f"{record}/README.md", _bundle_readme(transfer, output_archive.name)),
                (f"{record}/manifest.json", json.dumps(transfer, indent=2) + "\n"),
            ):
                info = zipfile.ZipInfo(member, date_time=time.localtime()[:6])
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                zf.writestr(info, text.encode("utf-8"))
        _append_rows(manifest_path, [_row("packaged", transfer, c) for c in contents])
    except BaseException:
        # A zip exists exactly when the log records it. Whatever failed, and
        # wherever, the log on disk says which side of the write it fell on:
        # unrecorded, the zip goes, so nothing crosses unrecorded; recorded
        # (an interrupt after the rename), it stays with its rows.
        if not _records(manifest_path, transfer_id):
            output_archive.unlink(missing_ok=True)
        raise
    return output_archive, skipped


def _bring_in(
    client: CatalogClient,
    dvc_ops: DvcOps,
    root: Path,
    entry: dict[str, Any],
    repo: str,
    pin: str,
    path: str,
    approved: set[str],
    reporter: "Reporter | None",
    producer_match: Callable[[str], tuple[str | None, MatchKind | None, str]],
) -> _Version:
    """Make `path` of `repo` readable from an import at `pin`, and read it.

    One import folder holds one pin, so the import unit is the outermost
    folder above `path` (or `path` itself) that this run approves or that is
    imported on disk, and `path` is read from it. The approved pin wins: a
    unit imported at any other commit (another approval's, or a `data import
    --bump` the manifest has not caught up with) is re-imported at `pin`.
    The unit, and `path` when it is not the unit, must match the producer's
    own pointer at `pin` wherever that pointer names them on its own
    (`_check_against_producer`): a parent folder imported on disk, which the
    producer names nowhere, never downgrades a path it does name."""
    imports = root / "data" / "imports"
    namespace = imports / _import_namespace(entry, repo)
    index = _imports_index(namespace, name=repo)
    unit = min((u for u in approved | set(index) | {path} if path == u or path.startswith(u + "/")), key=len)
    pointer = index.get(unit)
    if pointer is None or DataDependency.from_dvc_file(pointer).contract_pin != pin:
        if pointer is not None and reporter is not None:
            reporter.warn(f"{pointer} is not at the approved pin {pin[:7]}; re-importing it there")
        import_product(
            client, dvc_ops, repo, cwd=root, dest_root=imports, path=unit,
            rev=pin, force=pointer is not None, reporter=reporter,
        )
        pointer = _imports_index(namespace, name=repo)[unit]
    v = _read_version(root, repo, unit, pointer)
    cache = dvc_cache_dir(root)
    anchored = _check_against_producer(v, producer_match, pointer, cache)
    if unit == path:
        return replace(v, anchored=anchored)
    inner = _inside(v, path)
    return replace(inner, anchored=_check_against_producer(inner, producer_match, pointer, cache) or anchored)


def _check_against_producer(
    v: _Version, producer_match: Callable[[str], tuple[str | None, MatchKind | None, str]], pointer: Path, cache: Path
) -> bool:
    """Whether `v` is anchored to the producer's own pointer, refusing it
    where that pointer names `v.path` and records other content. The import
    pointer is local, and a re-import from a changed cache rewrites it to
    match the change, so the chain starts at the producer: its pointer, then
    the `.dir` hash, then each file's md5.

    check's own matcher (`_pointer_match`) says how the producer names
    `v.path`: on its own (`exact`, or `inside-files`, an entry of a per-file
    pointer), or not (absent: a folder holding several pointers; `beneath`:
    a folder of per-file lock outs; `inside-tree`: a path inside a folder
    tracked as one tree, whose identity is the whole tree's). Where it does
    not, the bytes are checked against the local import only, and False says
    so. A read that fails is never taken for a difference."""
    want, kind, within = producer_match(v.path)
    if want is None:
        raise MintdError(
            f"could not read {v.repo}'s own pointer for {v.path} at {v.pin[:7]}",
            hint="check git auth (gh auth status / ssh -T git@github.com), then retry",
        )
    if want == _POINTER_ABSENT or kind in ("beneath", "inside-tree"):
        return False
    if not _same_as_producer(v, want, within):
        raise ChecksumMismatch(
            f"{pointer}: the import of {v.path} ({v.md5}) is not what {v.repo}'s own pointer records at {v.pin[:7]} ({want})",
            hint=_import_again(cache, v.repo, v.unit or v.path, v.pin, want, v.md5),
        )
    return True


def _same_as_producer(v: _Version, want: str, within: str) -> bool:
    """Whether `v`'s files are what `want`, the producer's own identity for
    `v.path`, names: an md5 (a file, an entry of a per-file pointer, or a
    folder's tree), or a `.files` digest of a per-file pointer, or of a
    folder `within` it, computed by check's own `_out_identity` from `v`'s
    entries placed where that pointer lists them."""
    if want == v.md5:
        return True
    entries = [{"relpath": f"{within}/{r}" if within else r, "md5": m} for r, m in v.files if r]
    return want.endswith(".files") and _out_identity({"files": entries}, within) == want


def _import_again(cache: Path, repo: str, path: str, pin: str, *md5s: str) -> str:
    """Import at the pin, after deleting each cache object named in `md5s`
    whose bytes do not hash to its name: DVC trusts a read-only cache object,
    so `data pull` leaves a changed one in place and `--force` alone can
    adopt it. One that hashes to its name is a good copy, and stays."""
    stale = sorted({
        str(c) for m in md5s if (c := cache_path_for(cache, m)).is_file() and _md5(c) != m.removesuffix(".dir")
    })
    first = f"delete {', '.join(stale)}, then " if stale else ""
    return f"{first}import it again at the pin: 'mintd data import {repo} --force --path {path} --rev {pin}'"


def _hold(members: dict[str, tuple[Path, str, _Version]], member: str, entry: tuple[Path, str, _Version], again: str) -> None:
    """Hold `entry` as the source of zip member `member`, refusing a second
    source with another md5: keeping either would bundle bytes the other
    says are wrong, and a dict keeps the last silently."""
    held = members.get(member)
    if held is not None and held[1] != entry[1]:
        raise ChecksumMismatch(f"{member} would be bundled twice, with md5 {held[1]} and {entry[1]}", hint=again)
    members[member] = entry


def _inside(folder: _Version, path: str) -> _Version:
    """`path`, inside `folder`'s import at the same pin, read from it: a
    second `dvc import` into a folder already imported is refused. A file
    keeps its own md5; a sub-folder gets the tree md5 DVC would give it."""
    rel = path[len(folder.path) + 1:]
    files = tuple(
        (r[len(rel) + 1:] if r != rel else "", m) for r, m in folder.files if r == rel or r.startswith(rel + "/")
    )
    if not files:
        raise MintdError(f"{path} is not in {folder.path} at {folder.pin[:7]}", hint="check the approved source_path")
    md5 = files[0][1] if files[0][0] == "" else dir_manifest_md5(
        [DvcFileEntry(md5=m, relpath=r, size=0) for r, m in files]
    )
    return _Version(folder.repo, path, folder.pin, md5, folder.src / rel, files, unit=folder.path)


def _resolve_outputs(
    ap: ApprovedProduct, repo_url: str, factory: Callable[[str, str], ProducerView]
) -> list[str]:
    if ap.source_path is not None:
        return [ap.source_path]
    view = factory(repo_url, ap.pin)
    return view.output_paths() if ap.all else [view.primary_or_raise()]


def _read_version(root: Path, repo: str, path: str, pointer: Path) -> _Version:
    """The files an import pointer pins, with their md5s.

    From the pointer's `files:` list when it has no top-level md5, else from
    the folder's `.dir` manifest, which `dvc import` leaves in the project's
    cache."""
    (out,) = parse_dvc_outs(pointer, remote_name="")
    # A `files:` list counts only with no top-level md5, as check's
    # `_out_identity` reads it: beside a `.dir` md5 it is unchecked text, and
    # the hash-checked `.dir` is what the md5 names.
    if out.is_files_format and out.files is not None:
        files = [(f.relpath, f.md5) for f in out.files]
    elif out.is_dir:
        entries = read_cached_dir_manifest(dvc_cache_dir(root), out.md5)
        if entries is None:
            raise ChecksumMismatch(
                f"{pointer}: the folder's file list ({out.md5}) is not in the DVC cache, or is not the list dvc "
                "wrote (it does not hash to it, or lists a path twice)",
                hint=_import_again(
                    dvc_cache_dir(root), repo, path, DataDependency.from_dvc_file(pointer).contract_pin, out.md5
                ),
            )
        files = [(e.relpath, e.md5) for e in entries]
    else:
        files = [("", out.md5)]
    pin = DataDependency.from_dvc_file(pointer).contract_pin
    return _Version(repo, path, pin, out.md5, pointer.parent / out.path, tuple(files))


def _member(name: str) -> str:
    """`name` as a zip member and MD5SUMS path, or refused.

    Producer paths reach both, so this is the package-time guard: no leading
    `/`, no empty, `.` or `..` segment (unzip would write outside the enclave
    folder), no backslash, and no CR or LF (MD5SUMS is one path per line)."""
    if (
        name.startswith("/")
        or any(seg in ("", ".", "..") for seg in name.split("/"))
        or any(ch in name for ch in "\\\r\n")
    ):
        raise MintdError(
            f"refusing to bundle {name!r}: a bundle path must be relative, with no empty, '.' "
            "or '..' segment, backslash or line break",
            hint="the producer's output path or file name needs renaming",
        )
    return name


def _md5(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()


def _row(event: str, transfer: dict[str, Any], content: dict[str, Any]) -> dict[str, Any]:
    """One audit row: the transfer and one version it carries."""
    return {
        "event": event,
        "transfer_id": transfer["transfer_id"],
        "transfer_date": transfer["transfer_date"],
        **content,
    }


_LOG_KEY = re.compile(r"^transferred:[ \t]*(\[\s*\])?[ \t]*(#[^\r\n]*)?(?=\r?$)", re.MULTILINE)
_SURROGATE_PAIR = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})")


def _row_line(row: dict[str, Any]) -> str:
    """One row as one line of JSON that YAML also reads back exactly.

    `json.dumps` escapes every other non-ASCII character, and YAML reads those
    escapes back. A character past U+FFFF it escapes as a surrogate pair,
    which YAML reads back as two lone surrogates, so that one is written as
    itself, which JSON and YAML both accept."""
    return _SURROGATE_PAIR.sub(
        lambda m: chr(0x10000 + ((int(m[1], 16) - 0xD800) << 10) + int(m[2], 16) - 0xDC00),
        json.dumps(row),
    )


def _with_rows(text: str, rows: list[dict[str, Any]], manifest_path: Path) -> str:
    """The manifest text with `rows` appended to `transferred:`, or refused.

    Rows go on at column 0 at the end of the file, leaving every byte before
    them as it was (bar an empty `transferred: []` losing its `[]`; a comment
    after it stays). The result is parsed back and refused unless
    `transferred` is a list ending in exactly these rows: a later top-level
    key, or a log written as an indented or flow-style list, would otherwise
    take the rows or stop the file parsing at all."""
    unreadable = ManifestNotAppendable(
        f"{manifest_path}: rows appended to transferred: would not read back as its last rows",
        hint="keep transferred: last, as a list with each row on its own line starting with '- ' at column 0",
    )
    key = _LOG_KEY.search(text)
    if key is None:
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict) and "transferred" in parsed:
            raise unreadable  # a log in another shape: never suggest a second key
        raise ManifestNotAppendable(
            f"{manifest_path}: no transferred: section to append rows to",
            hint="add `transferred: []` as the last line of enclave_manifest.yaml",
        )
    if key.group(1):
        line = "transferred:" + (f" {key.group(2)}" if key.group(2) else "")
        text = text[: key.start()] + line + text[key.end():]
    nl = "\r\n" if "\r\n" in text else "\n"  # the file's own line ending
    if not text.endswith("\n"):
        text += nl
    new = text + "".join(f"- {_row_line(r)}{nl}" for r in rows)
    try:
        parsed = yaml.safe_load(new)
    except yaml.YAMLError:
        parsed = None
    log = parsed.get("transferred") if isinstance(parsed, dict) else None
    if not isinstance(log, list) or log[len(log) - len(rows):] != rows:
        raise unreadable
    return new


def _records(manifest_path: Path, transfer_id: str) -> bool:
    """Whether the log on disk has a row for `transfer_id`. A manifest that
    cannot be read counts as no."""
    try:
        log = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["transferred"]
        return any(row.get("transfer_id") == transfer_id for row in log)
    except (OSError, ValueError, yaml.YAMLError, LookupError, TypeError, AttributeError):
        return False


def _append_rows(manifest_path: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows to `transferred:` (see `_with_rows`), atomically, at the
    file a symlink points to, keeping its mode and every byte before them."""
    target = manifest_path.resolve()
    new = _with_rows(target.read_bytes().decode("utf-8"), rows, manifest_path)
    _atomic_write_text(target, new, newline="", mode=stat.S_IMODE(target.stat().st_mode))


def _next_transfer_id(manifest: EnclaveManifest, today_iso: str) -> str:
    """`transfer-YYYY-MM-DD-NNNNNN`, the lowest sequence number unused today."""
    prefix = f"transfer-{today_iso}-"
    used = set()
    for r in manifest.transferred:
        tid = str(r.get("transfer_id", ""))
        if tid.startswith(prefix) and tid[len(prefix):].isdigit():
            used.add(int(tid[len(prefix):]))
    seq = 0
    while seq in used:
        seq += 1
    return f"{prefix}{seq:06d}"


# Each check fails, not passes, when MD5SUMS cannot be read: the macOS loop
# counts the lines it read, and PowerShell stops on a Get-Content error.
_MACOS_CHECK = (
    'bad=0; n=0; while IFS= read -r line || [ -n "$line" ]; do n=$((n+1)); f=${line#*  }; '
    'if [ "$(md5 -q "$f")" = "${line%%  *}" ]; then echo "$f: OK"; '
    'else echo "$f: FAILED"; bad=1; fi; done < MD5SUMS_PATH; [ "$bad" = 0 ] && [ "$n" -gt 0 ]'
)
_WINDOWS_CHECK = (
    "$failed = 0; $n = 0; Get-Content -Encoding UTF8 -ErrorAction Stop MD5SUMS_PATH | ForEach-Object { "
    "$n++; $sum, $file = $_ -split '  ', 2; "
    'if ((Get-FileHash -Algorithm MD5 -LiteralPath $file).Hash -eq $sum) { "${file}: OK" } '
    'else { "${file}: FAILED"; $failed++ } }; '
    'if (-not $n) { throw "no file was checked" }; if ($failed) { throw "$failed file(s) FAILED the MD5 check" }'
)


def _bundle_readme(transfer: dict[str, Any], zip_name: str) -> str:
    """`transfers/<id>/README.md`, built from the transfer manifest so it
    cannot describe another bundle. Each system's section holds one fenced
    block of two lines, unzip then check, run from the enclave folder; the
    tests run those lines as written."""
    sums = f"transfers/{transfer['transfer_id']}/MD5SUMS"
    rows = "\n".join(
        f"| {c['repo']} | {c['path']} | {c['version']} | {c['dest']} |" for c in transfer["contents"]
    )
    n = len(transfer["contents"])
    return (
        f"# Transfer {transfer['transfer_id']}\n\n"
        f"Packaged {transfer['transfer_date']} for enclave `{transfer['enclave_name']}`. "
        f"It holds {n} data product version{'' if n == 1 else 's'}:\n\n"
        "| Product | Path | Version | Lands in |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        "## Land it\n\n"
        f"Copy `{zip_name}` into the enclave folder (the one with `enclave_manifest.yaml`), open a "
        "terminal there, and run the two commands for your system. The first puts the files in place "
        "and never overwrites a file already there. The second checks every file against its MD5 and "
        "prints one line per file; each should end in `OK`.\n\n"
        f"## Linux\n\n```sh\nunzip -n {zip_name}\nmd5sum -c {sums}\n```\n\n"
        f"## macOS\n\n```sh\nunzip -n {zip_name}\n{_MACOS_CHECK.replace('MD5SUMS_PATH', sums)}\n```\n\n"
        "## Windows (PowerShell)\n\n```powershell\n"
        f"Expand-Archive -Path {zip_name} -DestinationPath .\n"
        f"{_WINDOWS_CHECK.replace('MD5SUMS_PATH', sums)}\n```\n\n"
        "## If a line says FAILED\n\n"
        "That file is damaged. Delete its version folder, unzip again and check again. If it still "
        "fails, ask for the bundle to be sent again (`mintd enclave package --resend`). Never edit a "
        "version folder by hand: a new version lands in a new folder beside it.\n\n"
        "Like `md5sum -c`, the check covers every file this bundle lists, not extra files already "
        "in the folder. On Windows, expanding a bundle over files already in place prints an error "
        "for each one and leaves it as it is.\n"
    )
