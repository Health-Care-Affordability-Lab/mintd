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
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
import time
from typing import TYPE_CHECKING, Any
import zipfile

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ._atomic import _atomic_write_text
from ._dvc_ops import DvcOps
from ._dvc_state import dvc_cache_dir
from ._fast_sync_ops import parse_dvc_outs, read_cached_dir_manifest
from ._producer_git_ops import FetchError, GitArchiveFetcher
from .catalog import CatalogClient
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


@dataclass(frozen=True)
class _Version:
    """One output of one product at one pin, as imported on this machine."""

    repo: str
    path: str
    pin: str  # the commit the import pointer records (`rev_lock`)
    md5: str  # the out's md5 in that pointer
    src: Path
    files: tuple[tuple[str, str], ...]  # (relpath under src, "" for a file out; md5)


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
    cross. A failure removes the partial zip, and the manifest is appended to
    only after the zip is complete.

    Returns `(archive, skipped)`, `skipped` as `(repo, path, pin)`.
    """
    if output_archive is None and output_dir is None:
        raise ValueError("Either output_archive or output_dir must be provided")
    manifest = EnclaveManifest.load(manifest_path)
    # Refuse a log rows cannot be appended to before anything is imported or
    # written, with a probe row in place of the real ones.
    _with_rows(manifest_path.read_text(encoding="utf-8"), [{"event": "probe"}], manifest_path)
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
    if not pending:
        raise NothingNewToPackage(
            f"all {len(skipped)} approved version(s){' for ' + name if name else ''} "
            "have already crossed the gap"
        )
    when = commit_time or GitArchiveFetcher().commit_time_at
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
    try:
        with zf:
            groups = sorted(pending, key=lambda k: f"data/{k[0]}/{versions[k]}/")
            for repo, pin in groups:
                version = versions[(repo, pin)]
                members: dict[str, tuple[Path, str]] = {}
                for path in pending[(repo, pin)]:
                    v = _bring_in(client, dvc_ops, root, entries[repo], repo, pin, path, reporter)
                    dest = _member(f"data/{repo}/{version}/{path}")
                    for rel, md5 in v.files:
                        members[_member(f"{dest}/{rel}" if rel else dest)] = (v.src / rel if rel else v.src, md5)
                    contents.append({
                        "repo": repo, "path": path, "version": version, "contract_pin": v.pin,
                        "artifact_pin": v.md5, "dest": dest,
                    })
                for member in sorted(members):
                    src, md5 = members[member]
                    try:
                        matches = _md5(src) == md5
                    except FileNotFoundError:
                        matches = False
                    if not matches:
                        raise ChecksumMismatch(
                            f"{src} is missing or does not match its pin (md5 {md5})",
                            hint=f"bring it back with 'mintd data pull', or 'mintd data import {repo} --force --path <path>'",
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
            }
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
    except BaseException:
        output_archive.unlink(missing_ok=True)
        raise

    _append_rows(manifest_path, [_row("packaged", transfer, c) for c in contents])
    return output_archive, skipped


def _bring_in(
    client: CatalogClient,
    dvc_ops: DvcOps,
    root: Path,
    entry: dict[str, Any],
    repo: str,
    pin: str,
    path: str,
    reporter: "Reporter | None",
) -> _Version:
    """Make `path` of `repo` an import at `pin`, and read it back.

    The approved pin wins: an import at any other commit (a `data import
    --bump` the manifest has not caught up with, say) is re-imported at it."""
    imports = root / "data" / "imports"
    namespace = imports / _import_namespace(entry, repo)
    pointer = _imports_index(namespace, name=repo).get(path)
    if pointer is None or DataDependency.from_dvc_file(pointer).contract_pin != pin:
        if pointer is not None and reporter is not None:
            reporter.warn(f"{pointer} is not at the approved pin {pin[:7]}; re-importing it there")
        import_product(
            client, dvc_ops, repo, cwd=root, dest_root=imports, path=path,
            rev=pin, force=pointer is not None, reporter=reporter,
        )
        pointer = _imports_index(namespace, name=repo)[path]
    return _read_version(root, repo, path, pointer)


def _resolve_outputs(
    ap: ApprovedProduct, repo_url: str, factory: Callable[[str, str], ProducerView]
) -> list[str]:
    if ap.source_path is not None:
        return [ap.source_path]
    view = factory(repo_url, ap.pin)
    return view.output_paths() if ap.all else [view.primary_or_raise()]


def _read_version(root: Path, repo: str, path: str, pointer: Path) -> _Version:
    """The files an import pointer pins, with their md5s.

    From the pointer's `files:` list when it has one, else from the folder's
    `.dir` manifest, which `dvc import` leaves in the project's cache."""
    (out,) = parse_dvc_outs(pointer, remote_name="")
    if out.files is not None:
        files = [(f.relpath, f.md5) for f in out.files]
    elif out.is_dir:
        entries = read_cached_dir_manifest(dvc_cache_dir(root), out.md5)
        if entries is None:
            raise ChecksumMismatch(
                f"{pointer}: the folder's file list ({out.md5}) is not in the DVC cache",
                hint=f"bring it back with 'mintd data pull', or 'mintd data import {repo} --force --path {path}'",
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


_LOG_KEY = re.compile(r"^transferred:[ \t]*(\[\s*\])?[ \t]*$", re.MULTILINE)


def _with_rows(text: str, rows: list[dict[str, Any]], manifest_path: Path) -> str:
    """The manifest text with `rows` appended to `transferred:`, or refused.

    Rows go on at column 0 at the end of the file, leaving every byte before
    them as it was (bar an empty `transferred: []` losing its `[]`). The
    result is parsed back and refused unless `transferred` is a list ending in
    exactly these rows: a later top-level key, or a log written as an indented
    or flow-style list, would otherwise take the rows or stop the file
    parsing at all."""
    key = _LOG_KEY.search(text)
    if key is None:
        raise ManifestNotAppendable(
            f"{manifest_path}: no transferred: section to append rows to",
            hint="add `transferred: []` as the last line of enclave_manifest.yaml",
        )
    if key.group(1):
        text = text[: key.start()] + "transferred:" + text[key.end():]
    if not text.endswith("\n"):
        text += "\n"
    new = text + "".join(f"- {json.dumps(r)}\n" for r in rows)
    try:
        parsed = yaml.safe_load(new)
    except yaml.YAMLError:
        parsed = None
    log = parsed.get("transferred") if isinstance(parsed, dict) else None
    if not isinstance(log, list) or log[len(log) - len(rows):] != rows:
        raise ManifestNotAppendable(
            f"{manifest_path}: rows appended to transferred: would not read back as its last rows",
            hint="keep transferred: last, as a list with each row on its own line starting with '- ' at column 0",
        )
    return new


def _append_rows(manifest_path: Path, rows: list[dict[str, Any]]) -> None:
    """Append rows to `transferred:` (see `_with_rows`), atomically."""
    _atomic_write_text(manifest_path, _with_rows(manifest_path.read_text(encoding="utf-8"), rows, manifest_path))


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
    'bad=0; n=0; while IFS= read -r line; do n=$((n+1)); f=${line#*  }; '
    'if [ "$(md5 -q "$f")" = "${line%%  *}" ]; then echo "$f: OK"; '
    'else echo "$f: FAILED"; bad=1; fi; done < MD5SUMS_PATH; [ "$bad" = 0 ] && [ "$n" -gt 0 ]'
)
_WINDOWS_CHECK = (
    "$failed = 0; Get-Content -Encoding UTF8 -ErrorAction Stop MD5SUMS_PATH | ForEach-Object { "
    "$sum, $file = $_ -split '  ', 2; "
    'if ((Get-FileHash -Algorithm MD5 -LiteralPath $file).Hash -eq $sum) { "${file}: OK" } '
    'else { "${file}: FAILED"; $failed++ } }; '
    'if ($failed) { throw "$failed file(s) FAILED the MD5 check" }'
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
