"""Unified project validation.

This is the single source of validation findings for any mintd project.
Other modules (publish, the `mintd registry register` preflight, the bump
verbs) query check_project() instead of re-implementing checks.

Findings are split into two sections, by which artifact they read
(`CheckFinding.section` also admits "environment", which nothing emits):

  - producer: derivable from metadata.json — shape, required fields,
    storage config sanity (against `.dvc/config`) — plus, when `.dvc/`
    exists, the project's own DVC pointers and dvc.lock: every declared
    output must be one `mintd data import` can actually land. This is what
    a project owner is responsible for getting right before publishing.
  - consumer: derivable from the project's imports (the `.dvc` pointers
    under data/imports/ and dvc.lock deps with a `repo:` block) and
    enclave_manifest.yaml (each row's producer URL read from the catalog),
    plus — with `upgrades` — each upstream producer at the pin and at HEAD:
    pin resolvability, version compatibility, drift.
    This is what a project owner is responsible for keeping current as
    upstreams move.

Findings carry one of three severities:

  - error: blocks publish / blocks `mintd registry register`. The project is
    not in a valid state.
  - warning: surfaced to the user but does not block. Something is unusual
    or likely-wrong (e.g., the producer is unreachable, or its HEAD differs
    from your pin).
  - info: purely informational. Used sparingly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError

# `_dvc_state` imports no mintd module but the leaf `errors`; `_cache_ops` imports `_dvc_state`,
# so this module must never import `_cache_ops` at module level.
from ._dvc_state import Delivery, DeliveryState, _all_dvc_outs, abbreviate, delivery, path_covers
from ._pin_proof import unlisted_on_disk
from ._push_verify import pushed_lockfiles
from ._dvc_state import (
    PointerCutShort,
    check_dvc_file,
    check_lock,
    discover_all_outs,
    load12,
    normalize_target,
    resolve_out,
    stage_wdir,
    wdir_map,
)
from ._registry_git_ops import GitOpError, RegistryGitOps, specific_hint
from ._s3 import create_client as _create_s3_client  # the S3 boundary: tests stand a client in here
from .catalog import CatalogClient, CatalogNotFound
from .errors import unreachable_repo_hint
from .imports import DataDependency, scan_imports
from .model import Metadata
from .producer import FetchError, Fetcher, GitArchiveFetcher, ProducerError, ProducerView

if TYPE_CHECKING:
    # Avoid module-level import of enclave.py — enclave.py imports from this
    # module (CheckFinding) and from data.py, which imports this module too.
    # Lazy import inside the manifest walker breaks the cycle for runtime.
    from .enclave import ApprovedProduct

ProducerViewFactory = Callable[[str, str], "ProducerView | ProducerError"]

# Project types for which a `data_products.primary` is mandatory. Other types
# (code/project/enclave) may declare a primary but are not required to — a
# code/project repo publishes no consumable data product, and an enclave
# resolves *upstream* producers' primaries rather than publishing its own.
PRIMARY_REQUIRED_TYPES: frozenset[str] = frozenset({"data"})

# The delivery gate's finding kinds, keyed by what `_dvc_state.delivery` found.
# "pinned" and "via_parent" map to nothing on purpose: a subpath under a pinned
# `.dir` out is undecidable offline (its manifest is an S3 object), a warning
# there would print on every subpath of every healthy directory out and block
# nothing, and the clone lane's postcondition reports a missing subpath the
# moment it is decidable. UNPINNED and PARTIAL share one kind — same repair;
# the message names the count and the files.
_DeliveryKind = Literal[
    "data_products_output_untracked",
    "data_products_output_not_pullable",
    "data_products_output_unpinned",
    "data_products_output_missing_from_manifest",
]
_UNDELIVERABLE_KINDS: dict[DeliveryState, _DeliveryKind] = {
    "untracked": "data_products_output_untracked",
    "not_pullable": "data_products_output_not_pullable",
    "unpinned": "data_products_output_unpinned",
    "partial": "data_products_output_unpinned",
    "missing_from_manifest": "data_products_output_missing_from_manifest",
}

# dvc deletes a stage's outputs before it runs the stage's command
# (`Stage.run` -> `remove_outs`). A file placed there by hand is in neither git
# (the scaffold ignores /data/**) nor the DVC cache, so every hint that leads a
# producer to run a stage carries this warning.
_STAGE_RUN_DELETES = (
    "dvc deletes a stage's outputs before it runs, so first move anything you placed there by hand somewhere safe"
)

# ---------------------------------------------------------------------------
# Finding type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckFinding:
    severity: Literal["error", "warning", "info"]
    section: Literal["producer", "consumer", "environment"]
    message: str
    field_path: str | None = None
    source: Path | None = None  # NEW: which file the finding originated from
    kind: Literal[
        "drift",
        "drift_unknown",
        "up_to_date",
        "unreachable",
        "schema_too_old",
        "pin_missing",
        "metadata_missing",
        "metadata_invalid",
        "invalid_manifest",
        "catalog_unresolved",
        "storage_fresh",
        "storage_initialized",
        "storage_partial_meta_only",
        "storage_partial_dvc_only",
        "storage_name_mismatch",
        "storage_url_mismatch",
        "storage_bucket_empty",
        "data_products_primary_missing",
        "data_products_primary_mismatch",
        "data_products_output_untracked",
        "data_products_output_not_pullable",
        "data_products_output_unpinned",
        "data_products_output_missing_from_manifest",
        "dvc_state_unreadable",
        "dvc_lock_dep_unhashed",
        "pipeline_input_untracked",
        "pipeline_input_unchecked",
        "remote_pin_missing",
        "remote_pin_wrong_bytes",
        "remote_pin_unpinned",
        "remote_unchecked",
        "remote_unreadable",
        "remote_not_checked",
        "staged_drops_version_ids",
        "staged_unchecked",
        "dvc_folder_unlisted",
        "repository_github_url_missing",
    ] | None = None
    hint: str | None = None  # NEW: actionable repair suggestion


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_project(
    path: Path,
    *,
    upgrades: bool = False,
    producer_view_factory: ProducerViewFactory | None = None,
    client: CatalogClient | None = None,
    fetcher: Fetcher | None = None,
    remote: bool = False,
    all_outs: bool = False,
    git_ops: RegistryGitOps | None = None,
    aws_profile: str | None = None,
) -> list[CheckFinding]:
    """Validate a mintd project at `path` (the project directory).

    Returns a list of findings. Empty list means clean.

    Producer section:
      - metadata.json missing → 1 error finding
      - metadata.json malformed JSON → 1 error finding
      - metadata.json fails Pydantic → 1 error finding per ValidationError entry
      - storage drift → 1 error finding (checked even when Pydantic fails)
      - valid → data_products.primary, repository.github_url and, when `.dvc/`
        exists, DVC delivery of every declared output

    Consumer section: the project's imports and enclave approved products;
    `upgrades=True` also resolves each producer at its pin and at HEAD and
    reports drift.

    `remote=True` also asks S3 whether every pinned file version the
    working tree and HEAD record is there (`_check_remote`): the declared
    outputs' publishable outs, or all of them with `all_outs`.
    """
    findings = _producer_findings(path, git_ops=git_ops)
    if remote:
        findings.extend(_check_remote(path, all_outs=all_outs, git_ops=git_ops, aws_profile=aws_profile))
    try:
        findings.extend(
            _consumer_findings(
                path,
                upgrades=upgrades,
                producer_view_factory=producer_view_factory,
                client=client,
                fetcher=fetcher,
            )
        )
    except Exception as exc:
        # The consumer walk parses dvc.lock and the pointers under
        # data/imports/. When it fails and the producer section has already
        # named an unreadable DVC file, report that the imports went unchecked
        # instead of a traceback; any other failure is a real bug and
        # propagates. Reading the fact (did the walk fail?) rather than
        # predicting it from file paths keeps every consumer finding the walk
        # CAN compute -- `--bump` reads them.
        if not any(f.kind == "dvc_state_unreadable" for f in findings):
            raise
        findings.append(
            CheckFinding(
                severity="error",
                section="consumer",
                message=f"imports were not checked: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}",
                kind="dvc_state_unreadable",
                hint="fix the unreadable DVC file reported above, then re-run 'mintd check'.",
            )
        )
    return findings


def consumer_walk_failure(findings: list[CheckFinding]) -> str | None:
    """Why the consumer findings are missing, when an unreadable DVC file
    stopped the consumer walk: the bump verbs report this instead of "no
    consumer finding" for an import that exists."""
    if not any(f.section == "consumer" and f.kind == "dvc_state_unreadable" for f in findings):
        return None
    return "; ".join(f.message for f in findings if f.kind == "dvc_state_unreadable")


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _producer_findings(project_path: Path, git_ops: RegistryGitOps | None = None) -> list[CheckFinding]:
    """Producer-section checks: everything derivable from metadata.json (storage
    drift also reads `.dvc/config`), plus — when `.dvc/` exists — whether its
    declared outputs are ones DVC can deliver, read from the project's own
    pointers and dvc.lock."""
    from ._storage_state import StorageState, inspect_storage, repair_hint

    metadata_path = project_path / "metadata.json"

    if not metadata_path.is_file():
        return [
            CheckFinding(
                severity="error",
                section="producer",
                message=f"metadata.json not found at {metadata_path}",
                kind="metadata_missing",
            )
        ]

    raw = metadata_path.read_text(encoding="utf-8")

    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return [
            CheckFinding(
                severity="error",
                section="producer",
                message=f"malformed JSON in metadata.json: {e.msg} (line {e.lineno}, col {e.colno})",
                kind="metadata_invalid",
            )
        ]

    findings: list[CheckFinding] = []
    meta: Metadata | None = None

    try:
        meta = Metadata.model_validate_json(raw)
    except ValidationError as e:
        findings.extend(
            CheckFinding(
                severity="error",
                section="producer",
                message=err["msg"],
                field_path=".".join(str(p) for p in err["loc"]) or None,
                kind="metadata_invalid",
            )
            for err in e.errors()
        )

    # Slice 30: storage drift detection. Runs even when Pydantic validation
    # failed above — drift is independent of metadata-schema validity.
    inspection = inspect_storage(project_path)
    if inspection.state not in (StorageState.FRESH, StorageState.INITIALIZED):
        kind_map: dict[StorageState, Any] = {
            StorageState.PARTIAL_META_ONLY: "storage_partial_meta_only",
            StorageState.PARTIAL_DVC_ONLY: "storage_partial_dvc_only",
            StorageState.NAME_MISMATCH: "storage_name_mismatch",
            StorageState.URL_MISMATCH: "storage_url_mismatch",
            StorageState.BUCKET_EMPTY: "storage_bucket_empty",
        }
        findings.append(
            CheckFinding(
                severity="error",
                section="producer",
                message=f"storage drift detected: {inspection.state.value}",
                field_path="storage",
                source=metadata_path,
                kind=kind_map[inspection.state],
                hint=repair_hint(inspection),
            )
        )

    if meta:
        findings.extend(_check_data_products_primary(meta, metadata_path))
        findings.extend(_check_repository_identity(meta, metadata_path))
        # No `.dvc/` means no DVC truth to compare the declaration against
        # (`mintd init` creates one for every type but enclave); a bare
        # metadata.json is not a product that promised anything DVC-delivered.
        if (project_path / ".dvc").is_dir():
            # `check`, `publish` and `register` all route through here. A
            # dvc.yaml, dvc.lock or pointer file dvc itself would reject (a
            # bad merge's conflict markers, a hand edit) is this project's
            # defect to report by name -- not a traceback out of the gate, and
            # not a false "untracked" for the out it should have declared.
            # Known cost: the screen and `_all_dvc_outs` each parse every
            # pointer with pure-Python YAML (about 1 s on the largest live
            # product, 3-4 s at 5,000 pointers); sharing one parse is the fix
            # if that starts to matter.
            pointers = discover_all_outs(project_path)
            unreadable = _unreadable_dvc_files(project_path, pointers)
            findings.extend(
                CheckFinding(
                    severity="error",
                    section="producer",
                    message=f"could not read {rel}: {why}; declared outputs cannot be verified until it is fixed",
                    source=project_path / rel,
                    kind="dvc_state_unreadable",
                    hint=(
                        "fix the file by hand (a merge may have left conflict markers; 'git diff' shows "
                        "them), then re-run 'mintd check'."
                    ),
                )
                for rel, why in unreadable
            )
            if not unreadable:
                findings.extend(_unhashed_lock_deps(project_path))
                findings.extend(_untracked_pipeline_inputs(
                    project_path, pointers, inspection.dvc_remote_name or "", git_ops))
                # A folder pointer's list read against the folder on disk:
                # the only reference that shows a list cut at an entry, or
                # recorded before files were added. Publish refuses it too.
                findings.extend(
                    CheckFinding(
                        severity="error",
                        section="producer",
                        message=u.message(),
                        source=project_path / u.pointer,
                        kind="dvc_folder_unlisted",
                        hint=u.hint() + ", then re-run 'mintd check'.",
                    )
                    for u in unlisted_on_disk(project_path, [*pointers, *pushed_lockfiles(project_path)],
                                              remote=inspection.dvc_remote_name or "")
                )
                try:
                    findings.extend(
                        _check_data_products_delivery(
                            meta,
                            project_path,
                            metadata_path,
                            remote_name=inspection.dvc_remote_name or "",
                            version_aware=inspection.dvc_version_aware,
                            pointers=pointers,
                        )
                    )
                except Exception as exc:
                    # The screen above applies the parsers' cut-short rule, not
                    # every value check: what the parsers still reject (a lock
                    # out's `size: twelve`, say) is dvc.lock's.
                    findings.append(
                        CheckFinding(
                            severity="error",
                            section="producer",
                            message=f"could not read dvc.lock: {exc}; declared outputs cannot be verified until it is fixed",
                            source=project_path / "dvc.lock",
                            kind="dvc_state_unreadable",
                            hint="fix the entry by hand, then re-run 'mintd check'.",
                        )
                    )

    return findings


def _check_repository_identity(meta: Metadata, metadata_path: Path) -> list[CheckFinding]:
    """An entry with no repository.github_url is unusable to every consumer.

    `_require_repo_url` (data.py) raises on it, so `mintd data clone`
    against such a catalog entry exits 1. Presence only -- the derived
    `{org}/{full_name}` shape is a scaffold default a human may legitimately
    override (e.g. a catalog entry whose repo name differs from its catalog name).
    """
    if meta.repository.github_url.strip():
        return []
    return [
        CheckFinding(
            severity="error",
            section="producer",
            message="repository.github_url is not set",
            field_path="repository.github_url",
            source=metadata_path,
            kind="repository_github_url_missing",
            hint="set repository.github_url to this project's GitHub URL (e.g. 'https://github.com/<org>/<full_name>'). Without it, 'mintd data clone' against this entry exits 1.",
        )
    ]


def _check_data_products_primary(meta: Metadata, metadata_path: Path) -> list[CheckFinding]:
    findings: list[CheckFinding] = []
    primary = meta.data_products.primary
    if primary:
        # A declared primary must be valid regardless of project type.
        output_paths = [o.path for o in meta.data_products.outputs]
        if primary not in output_paths:
            hint = f"available outputs: {', '.join(output_paths) or '(none)'}; either add an outputs[] entry whose path == {primary!r}, or change primary to one of the listed paths."
            findings.append(
                CheckFinding(
                    severity="error",
                    section="producer",
                    message=f"data_products.primary={primary!r} does not match any outputs[].path",
                    field_path="data_products.primary",
                    source=metadata_path,
                    kind="data_products_primary_mismatch",
                    hint=hint,
                )
            )
    elif meta.project.type in PRIMARY_REQUIRED_TYPES:
        # A missing primary only blocks data-publishing project types.
        findings.append(
            CheckFinding(
                severity="error",
                section="producer",
                message="data_products.primary is not set",
                field_path="data_products.primary",
                source=metadata_path,
                kind="data_products_primary_missing",
                hint="set data_products.primary to one of your outputs[] paths (e.g. 'data/final/'). Consumers can't import this product without it.",
            )
        )
    return findings


def _unhashed_lock_deps(project_path: Path) -> list[CheckFinding]:
    """A root ``dvc.lock`` stage dep recorded with ``hash`` but no
    fingerprint at all (no md5, file list, etag, checksum or version ID): a
    re-dump wrote it without the dep's tree (a directory dep whose tree
    wasn't in the local cache). An out-of-repo dep records its remote's
    checksum (``etag`` on S3) instead of an md5, which is fine. The outputs
    still deliver; only the dep's fingerprint is lost, so DVC can't tell
    when to rerun."""
    from ruamel.yaml import YAMLError

    lock = project_path / "dvc.lock"
    if not lock.is_file() or not (project_path / "dvc.yaml").is_file():
        return []
    try:
        doc = load12(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, YAMLError):  # unreadable: the screen above names it
        return []
    stages = doc.get("stages") if isinstance(doc, dict) else None
    findings = []
    for stage, entry in (stages or {}).items():
        for dep in (entry.get("deps") or []) if isinstance(entry, dict) else []:
            if (isinstance(dep, dict) and dep.get("hash") and "files" not in dep
                    and not any(dep.get(k) for k in ("md5", "etag", "checksum", "version_id"))):
                findings.append(CheckFinding(
                    severity="warning", section="producer", source=lock, kind="dvc_lock_dep_unhashed",
                    message=f"dvc.lock stage {stage} records its dep {dep.get('path')} with no hash: the lock was "
                            "re-dumped without the dep's tree",
                    hint=f"run dvc commit {stage}, or dvc repro {stage}",
                ))
    return findings


_RAW_INPUT_HINT = (
    "'mintd data add {path}' tracks it in DVC and the next 'mintd data push' uploads it; or have a stage write it, "
    "like the scaffold's commented-out fetch stage. Whether raw files belong in lab storage (licence, size) is the "
    "producer's call."
)


def _untracked_pipeline_inputs(
    project_path: Path, pointers: list[str], remote_name: str, git_ops: RegistryGitOps | None,
) -> list[CheckFinding]:
    """A warning for each ``dvc.yaml`` dep that holds files git ignores and
    DVC doesn't track: ``git status`` never lists them, ``dvc repro`` records
    them only as inputs, and a fresh clone can't rerun the stage. The
    scaffold's ``data/raw/`` is the common case.

    A dep the pipeline writes (any stage's outs, metrics or plots) or a DVC
    out covers is fine; so are files git doesn't ignore (``git status``
    lists them already). A dep is read where it resolves, as git sees it,
    and its files are matched to git's list in one spelling. What it can't
    check gets a warning too: an unreadable or oddly shaped nested
    ``dvc.yaml``, an unreadable nested ``dvc.lock``, a templated dep, deps
    that aren't a list of paths, git failing, files git doesn't list (a
    repository of their own, another letter case) and symlinked folders
    inside a dep, which dvc doesn't hash. Only a URL dep is passed over: DVC fetches it and records
    its checksum. Reads, never writes.
    """
    import os
    import posixpath
    import unicodedata
    from pathlib import PureWindowsPath

    from ._dvc_state import _dvcignore, _files_on_disk, _nested_pipeline_outs, _tracked
    from ._push_verify import pushed_pipelines
    from ._registry_git_ops import SubprocessRegistryGitOps

    findings: list[CheckFinding] = []

    def warn(kind: Literal["pipeline_input_untracked", "pipeline_input_unchecked"], rel: str | None,
             message: str, hint: str) -> None:
        findings.append(CheckFinding(severity="warning", section="producer", kind=kind, message=message,
                                     source=project_path / rel if rel else None, hint=hint))

    def resolve(base: str, wdir: str | None, path: str) -> str | None:
        """``path`` relative to the project root; None when it lies outside."""
        if wdir is None or any(posixpath.isabs(p) or PureWindowsPath(p).drive for p in (wdir, path)):
            return None
        rel = posixpath.normpath(posixpath.join(base, wdir, path))
        return None if rel == ".." or rel.startswith("../") else rel

    inputs: dict[str, tuple[str, str]] = {}  # dep -> (dvc.yaml, stage): the first stage that names it
    covered: list[str] = []
    for yaml_rel in pushed_pipelines(project_path):
        base = posixpath.dirname(yaml_rel)
        try:
            doc = load12((project_path / yaml_rel).read_text(encoding="utf-8"))
        except Exception as exc:  # unreadable, not UTF-8, not YAML: the root's is screened before this runs
            why = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            warn("pipeline_input_unchecked", yaml_rel, f"could not read {yaml_rel} ({why}), so its stages' "
                 "inputs aren't checked", "fix the file by hand, then re-run 'mintd check'.")
            continue
        stages = doc.get("stages") if isinstance(doc, dict) else doc
        if stages is None:
            continue  # declares no stages
        if not isinstance(stages, dict):
            warn("pipeline_input_unchecked", yaml_rel, f"{yaml_rel} isn't in the shape dvc writes (its stages are "
                 "not a mapping), so its inputs aren't checked", "fix the file by hand, then re-run 'mintd check'.")
            continue
        wdirs = wdir_map(doc)
        for name, stage in stages.items():
            body = stage.get("do", stage) if isinstance(stage, dict) else None
            if not isinstance(body, dict):
                warn("pipeline_input_unchecked", yaml_rel, f"{yaml_rel} stage '{name}' isn't in the shape dvc "
                     "writes, so its inputs aren't checked", "fix the file by hand, then re-run 'mintd check'.")
                continue
            wdir = wdirs.get(str(name), ".")
            for key in ("outs", "metrics", "plots"):
                entries = body.get(key)
                for entry in entries if isinstance(entries, list) else []:
                    for out in [entry] if isinstance(entry, str) else entry if isinstance(entry, dict) else []:
                        if isinstance(out, str) and (rel := resolve(base, wdir, out)) is not None:
                            covered.append(rel)
            deps = body.get("deps")
            if deps is None:
                continue
            where = f"{yaml_rel} stage '{name}'"
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                warn("pipeline_input_unchecked", yaml_rel, f"{where} lists deps check can't read "
                     f"({repr(deps)[:60]}), so they aren't checked",
                     "list each dep as a path, as dvc expects, then re-run 'mintd check'.")
                continue
            templated = [d for d in deps if "${" in d or "${" in str(body.get("wdir", ""))]
            if templated:
                warn("pipeline_input_unchecked", yaml_rel, f"{where} names inputs check can't resolve: "
                     f"{abbreviate(templated)}", "check that git or DVC tracks what they resolve to.")
            for dep in deps:
                if "://" in dep or dep in templated:
                    continue  # a URL: DVC fetches it and records its checksum in dvc.lock
                rel = resolve(base, wdir, dep)
                if rel is None:
                    warn("pipeline_input_untracked", yaml_rel, f"pipeline input {dep!r} ({where}) is outside the "
                         "project, so a fresh clone has it only if that path exists on its machine",
                         "copy it into the project (e.g. data/raw/) and point the dep there; then "
                         + _RAW_INPUT_HINT.format(path="data/raw"))
                else:
                    inputs.setdefault(rel, (yaml_rel, str(name)))
    if not inputs:
        return findings
    try:
        nested = _nested_pipeline_outs(project_path, remote_name)
    except PointerCutShort as exc:  # the screen reads only the root dvc.lock
        lock = Path(os.path.relpath(exc.pointer, project_path)).as_posix()
        warn("pipeline_input_unchecked", lock, f"could not read {lock} ({exc.detail}), so pipeline inputs aren't "
             "checked", "restore it from git, or rerun its stages (dvc repro), then re-run 'mintd check'.")
        return findings
    covered += _tracked(project_path, [*_all_dvc_outs(project_path, remote_name, pointers), *nested])

    def is_covered(rel: str) -> bool:
        return any(path_covers(c, rel) for c in covered)

    def norm(rel: str) -> str:
        """One spelling for a path from the walk and from git: no ``./``, and
        NFC, as git prints a name on macOS whatever form the disk keeps."""
        return unicodedata.normalize("NFC", posixpath.normpath(rel))

    # Each dep is read where it really is, which is what git sees: a
    # symlinked folder through its target. Its files, in that spelling.
    root_real = os.path.realpath(project_path)
    candidates: dict[str, tuple[str, list[str]]] = {}
    for dep, (yaml_rel, name) in inputs.items():
        real = os.path.realpath(project_path / dep)
        query = Path(os.path.relpath(real, root_real)).as_posix()
        if is_covered(dep) or is_covered(query) or not os.path.exists(real):
            continue  # covered, or missing: dvc repro names a missing dep
        if query == ".." or query.startswith("../"):
            warn("pipeline_input_untracked", yaml_rel, f"pipeline input {dep!r} ({yaml_rel} stage '{name}') is a "
                 "symlink to a path outside the project, so a fresh clone has it only if that path exists on its "
                 "machine", "copy it into the project (e.g. data/raw/) and point the dep there; then "
                 + _RAW_INPUT_HINT.format(path="data/raw"))
            continue
        if os.path.isdir(real):
            files = [norm(posixpath.join(query, f)) for f in _files_on_disk(project_path, query)]
        else:
            parts = Path(query).parts
            dirs = [project_path, *(project_path.joinpath(*parts[:k]) for k in range(1, len(parts)))]
            files = [] if _dvcignore(project_path, dirs).match_file(query) else [norm(query)]
        candidates[dep] = (query, [f for f in files if not is_covered(f)])
    if not candidates:
        return findings
    git = git_ops or SubprocessRegistryGitOps()
    queries = sorted({query for query, _ in candidates.values()})
    try:
        listed = {norm(f) for f in git.listed_files(project_path, queries)}
        ignored = {norm(f) for f in git.ignored_files(project_path, queries)}
    except GitOpError as exc:
        warn("pipeline_input_unchecked", None, f"could not ask git which pipeline inputs it ignores: "
             f"{_git_error_summary(exc)}", "make sure 'git status' works here, then re-run 'mintd check'.")
        return findings

    def repo_holding(rel: str, query: str) -> str | None:
        """The folder at or under ``query`` holding ``rel`` in a git
        repository of its own, which git doesn't look inside."""
        d = posixpath.dirname(rel)
        while d and path_covers(query, d):
            if (project_path / d / ".git").exists():
                return d
            d = posixpath.dirname(d)
        return None

    for dep, (query, files) in candidates.items():
        yaml_rel, name = inputs[dep]
        where = f"{yaml_rel} stage '{name}'"
        if untracked := [f for f in files if f in ignored]:
            warn("pipeline_input_untracked", yaml_rel, f"pipeline input {dep!r} ({where}) is tracked by neither git "
                 f"nor DVC: {abbreviate(untracked)}; a fresh clone can't rerun the stage",
                 _RAW_INPUT_HINT.format(path=dep))
        # What git's answer can't be matched against is named, never passed.
        if unseen := [f for f in files if f not in listed]:
            repos = sorted({r for f in unseen if (r := repo_holding(f, query))})
            why = (f"{abbreviate(repos)} holds a git repository of its own" if repos
                   else "git spells the folder or these names differently, e.g. in letter case")
            warn("pipeline_input_unchecked", yaml_rel, f"git doesn't list {len(unseen)} file(s) DVC reads for "
                 f"pipeline input {dep!r} ({where}): {abbreviate(unseen)}; {why}, so check can't tell whether they "
                 "are tracked", "check by hand that git or DVC tracks them.")
        links = sorted(f for f in listed if f != query and path_covers(query, f) and not is_covered(f)
                       and (project_path / f).is_symlink() and (project_path / f).is_dir())
        if links:
            # dvc hashes a symlinked dep's target, but not a symlinked
            # folder inside one: what the stage may read there goes unrecorded.
            warn("pipeline_input_unchecked", yaml_rel, f"pipeline input {dep!r} ({where}) holds symlinked folders, "
                 f"which DVC doesn't hash: {abbreviate(links)}; if the stage reads them, a fresh clone may not have "
                 "what they hold", "list each as a dep of its own (DVC hashes a symlinked dep's target), or copy the "
                 "folder in place of the link.")
    return findings


_RemoteKind = Literal["remote_pin_missing", "remote_pin_wrong_bytes", "remote_pin_unpinned", "remote_unchecked"]

_REMOTE_VERDICTS: dict[str, tuple[Literal["error", "warning"], _RemoteKind, str]] = {
    "missing": ("error", "remote_pin_missing", "its recorded S3 version isn't at its key"),
    "wrong_bytes": ("error", "remote_pin_wrong_bytes", "its recorded S3 version holds other bytes"),
    "unpinned": ("error", "remote_pin_unpinned", "versioning is suspended, so its recorded version is null"),
    "inconclusive": ("error", "remote_unchecked", "S3 gave no clear answer for its recorded version"),
    "unproven": ("warning", "remote_unchecked", "its multipart upload can't be proven without the file in the cache"),
    "size_only": ("warning", "remote_unchecked", "its multipart upload matches by size only"),
    # only where the offline unpinned finding doesn't report it (_check_remote), never twice
    "no_id": ("error", "remote_pin_unpinned", "it records no S3 version ID, so importers can't fetch it"),
}

def _declared_outputs(project_path: Path) -> list[str]:
    """The declared output paths, primary first, normalized; none when the
    metadata can't be read (the producer findings say why)."""
    try:
        meta = Metadata.from_json_file(project_path / "metadata.json")
    except Exception:
        return []
    paths = [meta.data_products.primary, *(o.path for o in meta.data_products.outputs)]
    return [n for p in paths if p and (n := normalize_target(p))]


def _check_remote(
    project_path: Path, *, all_outs: bool, git_ops: RegistryGitOps | None, aws_profile: str | None = None,
) -> list[CheckFinding]:
    """``check --remote``: is every pinned file version the working tree and
    HEAD record actually on S3? Read-only: one HEAD and a content proof per
    distinct pin (``prove_pins``, with no memo, so nothing remembered can
    hide a deletion), on the default remote, as the identity DVC pushes
    with (``pinned_remote``, push's own test). A version S3 gives no clear
    answer for is unproven, so an error. HEAD covers every ``.dvc`` file it
    tracks, deleted from the working tree or not, and no ``.gitignore``
    hides one: git never ignores a tracked file. An entry with no version ID
    is left to the offline check's unpinned finding where that covers it, a
    declared out in the working tree. At HEAD, which nothing offline reads,
    it is an error where the working tree's copy of the file has its ID
    (``dvc commit -f`` committed, the working tree since re-pinned), and so
    is a HEAD pointer that can't be read; under ``--all``, so is an
    undeclared out's. Only the root ``dvc.lock`` is read, as push
    verification does: a nested one is named. Git is read relative to the
    project, so a project below its repository's root reads the same; a git
    failure raises rather than skip HEAD."""
    import posixpath
    import tempfile

    from ._atomic import write_tree
    from ._dvc_state import default_remote_name, dvc_cache_dir
    from ._pin_proof import CacheHashes, prove_pins, publishable_scope
    from ._push_verify import (
        PushRefused,
        in_subrepo,
        pinned_remote,
        pushed_dvc_files,
        pushed_lockfiles,
    )
    from ._registry_git_ops import SubprocessRegistryGitOps
    from ._s3 import S3AccessDenied, resolve_profile
    from .errors import MintdError

    name = default_remote_name(project_path)
    pointers = pushed_dvc_files(project_path)
    try:
        remote = pinned_remote(project_path, name, project_path, pointers) if name else None
    except PushRefused as exc:  # push refuses it too
        return [CheckFinding(severity="error", section="producer", kind="remote_unreadable",
                             message=str(exc), hint=exc.hint)]
    if remote is None:
        return [CheckFinding(
            severity="info", section="producer", kind="remote_not_checked",
            message="the default remote isn't a version-aware S3 remote, so nothing is pinned by version to check",
        )]
    git = git_ops or SubprocessRegistryGitOps()
    declared_outs = _declared_outputs(project_path)
    cache = CacheHashes(dvc_cache_dir(project_path))
    findings: list[CheckFinding] = []
    trees: dict[str, list] = {}
    bare: dict[str, list] = {}  # folder outs recorded as a bare tree hash, with no files and no IDs
    nested = {lock for lock in pushed_lockfiles(project_path) if lock != "dvc.lock"}

    def declared(out: str) -> bool:
        return any(path_covers(d, out) or path_covers(out, d) for d in declared_outs)

    def in_scope(out: str) -> bool:
        return all_outs or declared(out)

    with tempfile.TemporaryDirectory(prefix="mintd-check-head-") as tmp:
        roots = [("in the working tree", project_path, pointers)]
        if git.has_commit(project_path):  # none yet: only the working tree has pointers
            tracked = set(git.head_files(project_path))
            ours = sorted(f for f in tracked if not in_subrepo(project_path, f))
            head_pointers = [f for f in ours if f.endswith(".dvc")]
            nested |= {f for f in ours if f.endswith("/dvc.lock") and f"{posixpath.dirname(f)}/dvc.yaml" in tracked}
            head = Path(tmp)
            texts = {rel: git.head_text(project_path, f"./{rel}") for rel in [*head_pointers, "dvc.lock", "dvc.yaml"]}
            write_tree(head, {rel: text for rel, text in texts.items() if text is not None})
            roots.append(("at HEAD", head, head_pointers))
        for where, root, listed in roots:
            if where == "at HEAD":  # the working tree's are the offline screen's
                findings.extend(_unreadable_at(root, listed, "at HEAD", project_path))
            scope = publishable_scope(root, remote=remote.name, prefix=remote.prefix, cache_dir=cache.cache_dir,
                                      dvc_files=listed)
            trees[where] = [e for e in scope.entries if in_scope(e.out)]
            bare[where] = [o for o in scope.unrecoverable if in_scope(o.path)]
    if nested:
        findings.append(CheckFinding(
            severity="warning", section="producer", kind="remote_unchecked",
            message=f"only the root dvc.lock is read, so the pins in {abbreviate(sorted(nested))} aren't checked",
        ))
    # One proof per distinct pin and recorded file: the trees can name the
    # same version with different bytes, and each is judged by its own md5.
    # A pin with no verdict is unproven, never "ok".
    verdicts: dict[tuple[str, str | None, str], str] = {}
    try:
        s3 = None
        for entries in trees.values():
            todo = {(e.key, e.version_id, e.md5): e for e in entries
                    if e.version_id and (e.key, e.version_id, e.md5) not in verdicts}
            if todo:
                s3 = s3 or _create_s3_client(remote.cfg, aws_profile, pool=16)
                got = prove_pins(s3, remote.bucket, list(todo.values()), cache, None)
                verdicts.update({pin: got.get((e.key, e.version_id), "inconclusive") for pin, e in todo.items()})
    except (S3AccessDenied, MintdError) as exc:  # access denied, no credentials, a profile that isn't there
        profile = resolve_profile(remote.cfg, aws_profile)[0]
        return [*findings, CheckFinding(
            severity="error", section="producer", kind="remote_unreadable",
            message=f"can't check the remote {remote.name} as "
                    f"{'the default AWS credentials' if profile is None else f'profile {profile!r}'}: {exc}",
            hint=getattr(exc, "hint", None) or "check the AWS profile DVC uses for this remote",
        )]
    failing: dict[tuple[str, str, str], set[str]] = {}  # (verdict, pointer, path) -> trees
    pinned_here = {e.path for e in trees["in the working tree"] if e.version_id}  # files, by S3 key
    for where, entries in trees.items():
        for e in entries:
            if e.version_id:
                verdict = verdicts.get((e.key, e.version_id, e.md5), "inconclusive")
            else:  # see the docstring: where nothing offline reports it
                lost = where == "at HEAD" and e.path in pinned_here
                verdict = "no_id" if lost or not declared(e.out) else "ok"
            if verdict in _REMOTE_VERDICTS:
                failing.setdefault((verdict, e.pointer, e.path), set()).add(where)
    for where, outs in bare.items():
        for o in outs:
            lost = where == "at HEAD" and any(path_covers(o.path, p) for p in pinned_here)
            if lost or not declared(o.path):
                failing.setdefault(("no_id", o.pointer, o.path), set()).add(where)
    groups: dict[tuple[str, str, str], list[str]] = {}
    for (verdict, pointer, path), wheres in failing.items():
        label = " and ".join(sorted(wheres, key=["in the working tree", "at HEAD"].index))
        groups.setdefault((verdict, pointer, label), []).append(path)
    for (verdict, pointer, label), paths in sorted(groups.items()):
        severity, kind, why = _REMOTE_VERDICTS[verdict]
        findings.append(CheckFinding(
            severity=severity, section="producer", kind=kind,
            source=project_path / pointer,
            message=f"{pointer} {label}: {len(paths)} file(s) where {why}: {abbreviate(sorted(paths))}",
            hint=("check the network and the AWS profile, then run it again" if verdict == "inconclusive"
                  else _remote_hint(project_path, pointer, label, severity)),
        ))
    return findings


def _unreadable_at(root: Path, pointers: list[str], where: str, project_path: Path) -> list[CheckFinding]:
    """The offline screen (``_unreadable_dvc_files``) over a scratch copy of
    HEAD's or the index's pointers: a file dvc would refuse there (conflict
    markers, a pointer cut short) is an error, not "no outs"."""
    return [CheckFinding(
        severity="error", section="producer", kind="dvc_state_unreadable", source=project_path / rel,
        message=f"could not read {rel} {where}: {why}",
        hint="fix the file by hand (a merge may have left conflict markers), then commit it",
    ) for rel, why in _unreadable_dvc_files(root, pointers)]


def _remote_hint(project_path: Path, pointer: str, label: str, severity: str) -> str:
    """At HEAD only, the working tree is right and importers read the pushed
    commit; a pointer gone from the working tree, or one ``.gitignore``
    excludes, is no longer push's."""
    from ._dvc_state import gitignored_pointers

    if label != "at HEAD" or severity != "error":
        return "mintd data push re-attaches or re-uploads them"
    if not (project_path / pointer).is_file():
        return f"{pointer} is gone from the working tree: commit its removal, or restore it and run mintd data push"
    if pointer in gitignored_pointers(project_path):  # push skips it: DVC never collects an ignored .dvc file
        return f"{pointer} is excluded by .gitignore, so push skips it: stop ignoring it and run mintd data push, or git rm it"
    return "mintd data push re-attaches or re-uploads them; importers read your pushed commit, so push the pointers again"


def _raw_out_md5(root: Path, pointer: str, stage: str | None, index: int) -> str:
    """The ``md5`` a pointer under ``root`` records for one out."""
    doc = load12((root / pointer).read_text(encoding="utf-8"))
    outs = doc["stages"][stage]["outs"] if stage is not None else doc["outs"]
    return str(outs[index].get("md5") or "")


def check_staged(project_path: Path, git_ops: RegistryGitOps | None = None) -> list[CheckFinding]:
    """``check --staged``: does the commit about to be made drop S3 version
    IDs from files whose bytes it doesn't change (``dvc commit -f <stage>``
    does)? Offline: the staged ``.dvc`` files and root ``dvc.lock`` against
    HEAD's copies of every pointer the commit changes, deleted ones
    included, and HEAD's root ``dvc.lock`` whenever a pointer changes, each
    tree read with its own ``dvc.yaml`` through ``publishable_scope``, the
    parsing push uses. A file is matched by its S3 identity, its path, so a
    pointer or stage renamed, or an out moved between a ``.dvc`` file and a
    stage, still compares; its ID is dropped only when no staged entry for
    the same bytes at that key still records one. A folder out
    ``dvc commit -f`` rewrote as a bare ``.dir`` md5 is compared by its tree
    when that tree isn't in the local cache. A changed md5, added IDs, a
    removed out or pointer, a new file and the repository's first commit
    pass, and so do this project's readable pointers on a remote push pins
    nothing on. HEAD's copies are screened too: one dvc couldn't read is
    named when the commit records a file with no ID. Staged pointers it can't read here
    are named: another DVC project's (whatever this project's remote), an
    error, and a nested ``dvc.lock``, changed or deleted, a warning. Git is read
    relative to the project, so a project below its repository's root reads
    the same; a git failure raises rather than pass.

    ``project_path`` need not be the project's root: the project is found as
    dvc finds it, at the nearest folder up from the path that holds
    ``.dvc/``, never past the repository's top level (found as git finds it,
    by its ``.git``). With no DVC project there (an enclave repo), only a
    commit that stages pointers needs one, and it is refused, naming them."""
    import tempfile

    from ._atomic import write_tree
    from ._dvc_invoke import dvc_root
    from ._dvc_state import DvcFileEntry, default_remote_name, dir_manifest_md5, dvc_cache_dir
    from ._pin_proof import publishable_scope
    from ._push_verify import PushRefused, in_subrepo, pinned_remote
    from ._registry_git_ops import SubprocessRegistryGitOps

    hint = "run mintd check --staged in the DVC project's folder, or pass it as the path"

    def unchecked(why: str, hint: str | None = hint) -> list[CheckFinding]:
        return [CheckFinding(
            severity="error", section="producer", kind="staged_unchecked",
            message=f"can't check the staged commit: {why}", hint=hint,
        )]

    def is_pointer(rel: str) -> bool:
        return rel.endswith(".dvc") or rel.rsplit("/", 1)[-1] == "dvc.lock"

    git = git_ops or SubprocessRegistryGitOps()
    path = Path(project_path).resolve()
    if not path.is_dir():  # before any git call, which would blame git for the wrong folder
        return unchecked(f"{project_path} isn't a folder")
    # By hand, not `git rev-parse`: it runs before any git call, so a wrong folder is named, not blamed on git.
    top = next((d for d in (path, *path.parents) if (d / ".git").exists()), None)  # a worktree's .git is a file
    if top is None:
        return unchecked(f"{project_path} isn't in a git repository")
    project = dvc_root(path, within=top)
    if not (project / ".dvc").is_dir():
        pointers = sorted(p for status, p in git.staged_changes(top) if status != "D" and is_pointer(p))
        if not pointers:
            return []  # no DVC project and no pointer staged: nothing to check
        return unchecked(f"{project_path} isn't in a DVC project, and the commit stages DVC pointers "
                         f"({abbreviate(pointers)})")
    project_path = project
    # Raises when git can't read the repository. A deleted pointer drops no
    # ID itself, but HEAD's copy is read: its IDs may have moved.
    changes = [(status, rel) for status, rel in git.staged_changes(project_path) if is_pointer(rel)]
    if not changes or not git.has_commit(project_path):
        return []  # no pointer staged, or the first commit: nothing to drop
    # Another DVC project's pointers are named whatever this project's remote
    # is: they are pinned on that project's own.
    other = sorted(rel for status, rel in changes if status != "D" and in_subrepo(project_path, rel))
    findings = unchecked(f"the commit stages pointers in another DVC project ({abbreviate(other)})") if other else []
    name = default_remote_name(project_path)
    if name is None:
        return findings  # no remote to pin to
    ours = [(status, rel) for status, rel in changes if not in_subrepo(project_path, rel)]
    changed = [rel for status, rel in ours if rel.endswith(".dvc") or rel == "dvc.lock"]
    staged = [rel for status, rel in ours if status != "D" and (rel.endswith(".dvc") or rel == "dvc.lock")]
    dvc_files = [rel for rel in staged if rel.endswith(".dvc")]
    with tempfile.TemporaryDirectory(prefix="mintd-check-staged-") as tmp:
        roots = {"staged": Path(tmp) / "staged", "head": Path(tmp) / "head"}
        read = {"staged": lambda rel: git.index_text(project_path, rel),
                "head": lambda rel: git.head_text(project_path, f"./{rel}")}
        # HEAD's root lock whenever a pointer changes: an out can move from an
        # unchanged lock into a changed .dvc file.
        head_rels = changed if "dvc.lock" in changed else [*changed, "dvc.lock"]
        for which, rels in (("staged", staged), ("head", head_rels)):
            texts = {rel: read[which](rel) for rel in rels}
            # Each tree's own dvc.yaml, for its lock's wdirs and stages. HEAD's
            # matters only beside a lock HEAD has: screened without one, a
            # broken dvc.yaml the commit fixes would refuse it for nothing.
            if which == "staged" or texts.get("dvc.lock") is not None:
                texts["dvc.yaml"] = read[which]("dvc.yaml")
            write_tree(roots[which], {rel: text for rel, text in texts.items() if text is not None})
        unreadable = _unreadable_at(roots["staged"], dvc_files, "in the staged commit", project_path)
        if unreadable:  # read as "no outs", a damaged pointer would look like one dropped on purpose
            return [*findings, *unreadable]
        try:
            if pinned_remote(project_path, name, roots["staged"], dvc_files) is None:
                return findings  # not version-aware: nothing is pinned by version, so nothing to drop
        except PushRefused as exc:  # push refuses it too
            return [*findings, *unchecked(str(exc), hint=exc.hint)]
        # A nested lock's pins, kept, changed or moved out of it by a deletion,
        # aren't compared. Re-running a nested pipeline is everyday work, so
        # the lock is named, not refused.
        nested = sorted(rel for status, rel in ours if rel.endswith("/dvc.lock"))
        if nested:
            findings.append(CheckFinding(
                severity="warning", section="producer", kind="staged_unchecked",
                message=f"only the root dvc.lock is read, so the pins in {abbreviate(nested)} aren't checked",
            ))
        scopes = {which: publishable_scope(root, remote=name, prefix="", cache_dir=dvc_cache_dir(project_path),
                                           dvc_files=[p for p in (changed if which == "head" else dvc_files)
                                                      if p.endswith(".dvc")])
                  for which, root in roots.items()}
        bare = {(o.pointer, o.path): _raw_out_md5(roots["staged"], o.pointer, o.stage, o.index)
                for o in scopes["staged"].unrecoverable}
        # HEAD's copy read as "no outs" would hide every ID it records. That
        # matters only when the staged commit records a file with no ID.
        could_drop = bool(bare) or any(not e.version_id for e in scopes["staged"].entries)
        head_unreadable = _unreadable_dvc_files(  # a new pointer has no HEAD copy
            roots["head"], [p for p in changed if p.endswith(".dvc") and (roots["head"] / p).is_file()])
        if could_drop and head_unreadable:
            return [*findings, *(f for rel, why in head_unreadable for f in unchecked(
                f"HEAD's {rel} can't be read ({why}), so the IDs it records can't be compared",
                hint="run mintd data push, which records the IDs, then commit"))]
    # A file's ID is dropped when HEAD records one for its bytes and no staged
    # entry for those bytes at that key still does (two pointers can record one key).
    pinned = {(e.path, e.md5) for e in scopes["head"].entries if e.version_id}  # HEAD's, by S3 identity
    kept = {(e.path, e.md5) for e in scopes["staged"].entries if e.version_id}
    dropped: dict[str, list[str]] = {}
    for e in scopes["staged"].entries:
        if not e.version_id and (e.path, e.md5) in pinned - kept:
            dropped.setdefault(e.pointer, []).append(e.path)
    # A staged folder out recorded as a bare .dir md5 whose tree isn't cached
    # (dvc commit -f's shape): the same tree as HEAD's file list means the
    # same files, and every ID HEAD recorded for them is gone.
    for (pointer, out), md5 in bare.items():
        # once per file: two of HEAD's pointers can record the same folder
        files = list({(e.relpath, e.md5): e for e in scopes["head"].entries if e.out == out}.values())
        tree = dir_manifest_md5([DvcFileEntry(md5=e.md5, relpath=e.relpath or "", size=e.size or 0) for e in files])
        lost = [e.path for e in files if e.version_id and (e.path, e.md5) not in kept]
        if files and md5 == tree and lost:
            dropped.setdefault(pointer, []).extend(lost)
    return [*findings, *(CheckFinding(
        severity="error", section="producer", kind="staged_drops_version_ids", source=project_path / pointer,
        message=f"{pointer} drops S3 version IDs from {len(paths)} unchanged file(s) ({abbreviate(sorted(paths))})",
        hint="run mintd data push, which puts them back, then commit",
    ) for pointer, paths in sorted(dropped.items()))]


def _check_data_products_delivery(
    meta: Metadata,
    project_path: Path,
    metadata_path: Path,
    *,
    remote_name: str,
    version_aware: bool,
    pointers: list[str] | None = None,
) -> list[CheckFinding]:
    """One error per declared output path that `mintd data import` could not
    land, judged by `_dvc_state.delivery` from the working tree's pointers
    alone (committed or not).

    Before this gate the producer's own `check` compared `primary` against
    `outputs[]` as text and never asked DVC: a product could declare a path
    DVC tracks nowhere, or one pushed before its remote became version-aware
    (no S3 version IDs), pass `check`, publish, and hand every consumer an
    empty directory at exit 0.

    The declared set is the primary first, then `outputs[].path`, deduplicated
    on the normalized spelling — `data/final/` and `data/final` are one path,
    and the primary is usually also an outputs[] row — so each undeliverable
    path is reported once, under the field that declares it first.
    """
    declared: dict[str, tuple[str, str]] = {}
    primary = meta.data_products.primary
    if primary and normalize_target(primary):
        declared[normalize_target(primary)] = (primary, "data_products.primary")
    for i, out in enumerate(meta.data_products.outputs):
        key = normalize_target(out.path)
        if key:
            declared.setdefault(key, (out.path, f"data_products.outputs[{i}].path"))
    if not declared:
        return []

    outs = _all_dvc_outs(project_path, remote_name, pointers=pointers)  # one workspace walk per check
    findings: list[CheckFinding] = []
    for path, field_path in declared.values():
        verdict = delivery(project_path, path, remote_name, version_aware=version_aware, outs=outs)
        if verdict.deliverable:
            continue
        what = (
            "is not a DVC-tracked output"
            if verdict.state == "untracked"
            else "cannot be delivered by 'mintd data import'"
        )
        findings.append(
            CheckFinding(
                severity="error",
                section="producer",
                message=f"{path!r} {what}: {verdict.detail}",
                field_path=field_path,
                source=metadata_path,
                kind=_UNDELIVERABLE_KINDS[verdict.state],
                hint=_delivery_hint(verdict, path, remote_name, project_path),
            )
        )
    return findings


def _delivery_hint(verdict: Delivery, path: str, remote_name: str, project_path: Path) -> str:
    """One hint per state: the facts a producer needs, and the repairs.
    Shape-specific commands are deliberately not prescribed -- `dvc add`
    refuses a path a stage declares -- so the producer, who knows which shape
    they have, chooses; a repair that runs a stage carries
    `_STAGE_RUN_DELETES`."""
    if verdict.state == "untracked":
        rel = normalize_target(path)
        twins = [t for t in verdict.tracked if path_covers(t, rel) or path_covers(rel, t)]
        if twins:
            return (
                f"DVC tracks {abbreviate(twins)}; the declaration differs from it only by case, "
                "which a case-sensitive consumer's filesystem treats as a different path. "
                "Declare the spelling DVC tracks."
            )
        facts = [f"DVC tracks nothing at {path!r}"]
        stage = _pipeline_stage_declaring(project_path, path)
        if stage:
            facts.append(f"dvc.yaml stage {stage!r} declares it but dvc.lock has no entry for it")
        if verdict.tracked:
            facts.append(f"tracked: {abbreviate(verdict.tracked)}")
        return (
            "; ".join(facts) + ". Declare a path DVC tracks, or track this one: for a dvc.yaml "
            f"output, run the stage that produces it ({_STAGE_RUN_DELETES}); for a loose file, "
            "'mintd data add'. Then 'mintd data push', which commits and pushes the pointer or dvc.lock when nothing else is uncommitted."
        )
    if verdict.state == "not_pullable":
        return (
            f"dvc never pushes {abbreviate(verdict.unpullable)}: 'cache: false' in dvc.yaml, or a "
            "'dvc import' whose bytes live in the source repo's bucket. Drop 'cache: false' (then "
            "'dvc commit' and 'mintd data push'), declare the source product instead of the import, "
            "or remove this outputs[] entry."
        )
    if verdict.state == "missing_from_manifest":
        stage = _pipeline_stage_declaring(project_path, path)
        owner = (
            f"the stage {stage!r} that owns it; {_STAGE_RUN_DELETES}"
            if stage
            else "the 'mintd data add' that tracks it"
        )
        return (
            f"the directory out above {path!r} is tracked but holds nothing at that name. Remove this "
            f"outputs[] entry, or make the file part of the directory out (through {owner}) and "
            "'mintd data push'."
        )
    return (
        f"remote {remote_name!r} is version_aware, so a consumer's pull needs an S3 version ID "
        f"recorded under that remote; none is for {abbreviate(verdict.unpinned)}. Run 'mintd data "
        "push' from this project; it commits and pushes the rewritten .dvc / dvc.lock files, or "
        "names them for you to commit when anything else is uncommitted."
    )


def _unreadable_dvc_files(project_path: Path, pointers: list[str]) -> list[tuple[str, str]]:
    """``(file, why)`` for every DVC file dvc itself could not read: dvc.yaml,
    dvc.lock and each ``*.dvc`` pointer that is not YAML (a merge's conflict
    markers are the common case) or not the shape dvc writes. The parsers
    downstream swallow these into "no outs", which is right for a pull lane
    choosing a fallback route and wrong for a gate that would then call the
    file's out untracked -- or, for the consumer walk, traceback.

    Each file is parsed as dvc parses it (``load12``: ruamel, YAML 1.2), so
    a duplicated key, which PyYAML takes and dvc refuses, is named too.
    Shape is checked only as far as the parsers assume it: dvc.yaml lists
    outs as strings or ``{path: options}`` mappings (under ``do:`` for a
    foreach stage); dvc.lock and pointers list outs as mappings.
    """
    bad: list[tuple[str, str]] = []

    def load(rel: str) -> tuple[bool, object]:
        try:
            return True, load12((project_path / rel).read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            bad.append((rel, "not UTF-8 text (dvc writes UTF-8)"))
            return False, None
        except Exception as exc:  # any reason the parse fails: yaml's own errors, an impossible date's ValueError
            why = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            bad.append((rel, f"not readable as YAML ({why})"))
            return False, None

    def outs_ok(block: object, *, strings_allowed: bool) -> bool:
        if not isinstance(block, dict):
            return False
        keys = ("outs", "metrics", "plots") if strings_allowed else ("outs",)
        for key in keys:
            outs = block.get(key)
            if outs is None:
                continue
            if not isinstance(outs, list):
                return False
            for out in outs:
                if not (isinstance(out, dict) or (strings_allowed and isinstance(out, str))):
                    return False
        return True

    def stages_ok(doc: object, *, strings_allowed: bool) -> bool:
        if doc is None:
            return True  # an empty file declares nothing
        if not isinstance(doc, dict):
            return False
        stages = doc.get("stages")
        if stages is None:
            return True
        if not isinstance(stages, dict):
            return False
        for stage in stages.values():
            body = stage.get("do", stage) if (strings_allowed and isinstance(stage, dict)) else stage
            if not outs_ok(body, strings_allowed=strings_allowed):
                return False
        return True

    # dvc.lock (where DVC reads it, beside a dvc.yaml) and every pointer get
    # the parsers' own cut-short rule, so the gate below never meets one.
    for rel, strings_allowed in (("dvc.yaml", True), ("dvc.lock", False)):
        if not (project_path / rel).is_file():
            continue
        ok, doc = load(rel)
        if not ok:
            continue
        if rel == "dvc.lock" and (project_path / "dvc.yaml").is_file():
            try:
                check_lock(rel, doc)
            except PointerCutShort as exc:
                bad.append((rel, exc.detail))
        elif not stages_ok(doc, strings_allowed=strings_allowed):
            bad.append((rel, "not in the shape dvc writes"))
    for rel in pointers:
        ok, doc = load(rel)
        if not ok:
            continue
        try:
            check_dvc_file(rel, doc)
        except PointerCutShort as exc:
            bad.append((rel, exc.detail))
    return bad


def _pipeline_stage_declaring(project_path: Path, declared: str) -> str | None:
    """The dvc.yaml stage that declares an out at, under or above ``declared``,
    if any. Such a path is one the pipeline promises but has never produced —
    no dvc.lock entry, so `_all_dvc_outs` cannot see it — which is every fresh
    scaffold the moment its primary is filled in. ``dvc add`` refuses it
    ("overlaps with an output of stage"), so the repair is running the stage.
    Templated outs (``${item}``) expand per foreach instance and are not
    matched here."""
    try:
        doc = yaml.safe_load((project_path / "dvc.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    stages = doc.get("stages") if isinstance(doc, dict) else None
    if not isinstance(stages, dict):
        return None
    rel = normalize_target(declared)
    wdirs = wdir_map(doc)
    for stage, stage_data in stages.items():
        body = stage_data.get("do", stage_data) if isinstance(stage_data, dict) else None
        wdir = wdirs.get(str(stage))
        if not isinstance(body, dict) or wdir is None:
            continue
        for key in ("outs", "metrics", "plots"):
            entries = body.get(key)
            for entry in entries if isinstance(entries, list) else []:
                for raw in entry.keys() if isinstance(entry, dict) else [entry]:
                    if not isinstance(raw, str) or "${" in raw:
                        continue
                    out = resolve_out(wdir, raw)
                    if path_covers(out, rel) or path_covers(rel, out):
                        return str(stage)
    return None


def _consumer_findings(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    client: CatalogClient | None = None,
    imports_under: str = "data/imports",
    fetcher: Fetcher | None = None,
) -> list[CheckFinding]:
    active_fetcher: Fetcher = fetcher if fetcher is not None else GitArchiveFetcher()
    # D-C reads the producer's DVC pointer at both pin and HEAD, per row.
    # `_resolve_once` in the enclave walker memoizes VIEWS only; pointer reads
    # and `dvc.yaml` reads share this one across both arms of a single walk.
    # Keyed `(repo, rev, path)` for a pointer, `(repo, rev)` for a wdir map --
    # different arity, so one dict holds both without collision.
    memo: dict[tuple[str, ...], Any] = {}
    findings = _consumer_findings_from_dvc(
        project_path,
        upgrades=upgrades,
        producer_view_factory=producer_view_factory,
        imports_under=imports_under,
        fetcher=active_fetcher,
        memo=memo,
    )
    findings.extend(
        _consumer_findings_from_enclave_manifest(
            project_path,
            upgrades=upgrades,
            producer_view_factory=producer_view_factory,
            client=client,
            fetcher=active_fetcher,
            memo=memo,
        )
    )
    return findings


def _consumer_findings_from_dvc(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    imports_under: str = "data/imports",
    fetcher: Fetcher | None = None,
    memo: dict[tuple[str, ...], Any] | None = None,
) -> list[CheckFinding]:
    if fetcher is None:
        fetcher = GitArchiveFetcher()
    if memo is None:
        memo = {}
    deps = scan_imports(project_path, under=imports_under)
    if not deps:
        return []

    findings: list[CheckFinding] = []
    factory = producer_view_factory if producer_view_factory is not None else ProducerView.try_at

    for dep in deps:
        # An EMPTY `rev_lock` is user data, not the HEAD sentinel — the same
        # collision the enclave-manifest branch guards below, on the lane the
        # first cut of that guard missed. Without it BOTH `factory` calls
        # resolve HEAD, the two views are identical, `_drift_finding` sees no
        # drift, and an import with no pin at all renders as `✓ up to date`
        # where it used to render as `[warning] producer unreachable`. That
        # also makes `bump_import` (data.py, gates on kind) silently no-op.
        #
        # Reading the pin needs no network, so this runs ABOVE the `upgrades`
        # gate: below it, plain `check` rendered an unpinned import as a clean
        # `[info] imported <path> from <repo>@` and exited 0, and `publish` /
        # `registry register` (both `check_project(upgrades=False)`) let it
        # through.
        if not dep.contract_pin.strip():
            findings.append(
                CheckFinding(
                    severity="error",
                    section="consumer",
                    message=f"import {dep.local_path} has an empty pin",
                    source=dep.source,
                    kind="pin_missing",
                    # A pin-less `.dvc` is reachable without hand-editing:
                    # `mintd data import --dvc-arg=--no-exec` writes one. Say
                    # how to get back rather than only that the state is bad.
                    hint=(
                        "the .dvc file carries no rev_lock: re-run `mintd data import "
                        "<name> --force` to rewrite it at a real commit, or set rev_lock "
                        "by hand to the producer commit this import was taken from."
                    ),
                )
            )
            continue

        if not upgrades:
            findings.append(_summary_finding(dep))
            continue

        result_pin = factory(dep.producer_repo, dep.contract_pin)
        if isinstance(result_pin, ProducerError):
            findings.append(_error_finding(dep, result_pin))
            continue

        # Compare to HEAD — empty string sentinel is a test contract.
        result_head = factory(dep.producer_repo, "")
        if isinstance(result_head, ProducerError):
            findings.append(
                _head_unreadable_finding(
                    source=dep.source, pin=dep.contract_pin, err=result_head
                )
            )
            continue

        findings.append(
            _drift_finding(dep, result_pin, result_head, fetcher=fetcher, memo=memo)
        )

    return findings


def _consumer_findings_from_enclave_manifest(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    client: CatalogClient | None,
    fetcher: Fetcher | None = None,
    memo: dict[tuple[str, ...], Any] | None = None,
) -> list[CheckFinding]:
    if fetcher is None:
        fetcher = GitArchiveFetcher()
    if memo is None:
        memo = {}
    manifest_path = project_path / "enclave_manifest.yaml"
    if not manifest_path.is_file():
        return []

    # Lazy import to break the check.py ↔ enclave.py cycle.
    from .enclave import EnclaveManifest, subscription_label

    try:
        manifest = EnclaveManifest.load(manifest_path)
    except (ValidationError, yaml.YAMLError) as e:
        return [
            CheckFinding(
                severity="error",
                section="consumer",
                message=f"enclave_manifest.yaml invalid: {e}",
                source=manifest_path,
                kind="invalid_manifest",
            )
        ]

    findings: list[CheckFinding] = []
    factory = (
        producer_view_factory
        if producer_view_factory is not None
        else ProducerView.try_at
    )

    # Memoized per (repo_url, pin) FOR THIS CALL ONLY. Before P5 a repo held
    # exactly one row, so each producer cost one HEAD round-trip; now a repo
    # with three subscriptions would pay three, and `enclave bump` (which runs
    # check_project(upgrades=True)) pays them again. `ProducerView.try_at`
    # disk-caches the pinned read but the HEAD read "always pays the
    # round-trip" by its own docstring. Same inputs give the same view within
    # one walk, so this is identical in meaning and strictly cheaper.
    _views: dict[tuple[str, str], "ProducerView | ProducerError"] = {}

    def _resolve_once(repo_url: str, pin: str) -> "ProducerView | ProducerError":
        key = (repo_url, pin)
        if key not in _views:
            _views[key] = factory(repo_url, pin)
        return _views[key]

    for ap in manifest.approved_products:
        field_path = f"approved_products[{ap.repo}]"
        if client is None:
            findings.append(
                CheckFinding(
                    severity="error",
                    section="consumer",
                    message=f"catalog client not provided; cannot resolve producer"
                    f" URL for {ap.repo} ({subscription_label(ap)})",
                    source=manifest_path,
                    field_path=field_path,
                    kind="catalog_unresolved",
                )
            )
            continue

        try:
            repo_url = _resolve_approved_product_url(client, ap)
        except (ValueError, CatalogNotFound, GitOpError) as e:
            # A GitOpError means the catalog read itself failed — the cache is
            # cloned/refreshed on every fetch. That is a documented path, not a
            # traceback, and since plain `check` resolves a client too, every
            # enclave consumer is on it. An unreachable remote and a corrupt
            # local cache are indistinguishable here, so report git's own words
            # rather than asserting a cause; the other two already carry a
            # user-facing message.
            if isinstance(e, GitOpError):
                message = (
                    f"cannot read the catalog to resolve producer URL for "
                    f"{ap.repo}: {_git_error_summary(e)}"
                )
                hint = specific_hint(e) or (
                    "check your network and `registry_url` in "
                    "~/.config/mintd/config.yaml; if both are fine, delete the "
                    "local registry cache (~/.cache/mintd/registry by default) "
                    "and retry"
                )
            else:
                message, hint = str(e), None
            findings.append(
                CheckFinding(
                    severity="error",
                    section="consumer",
                    message=f"{message} ({subscription_label(ap)})",
                    source=manifest_path,
                    field_path=field_path,
                    kind="catalog_unresolved",
                    hint=hint,
                )
            )
            continue

        # An EMPTY pin here is user data, not the HEAD sentinel. `try_at`
        # treats `""` as "resolve HEAD" so that `factory(repo_url, "")` below
        # can ask that question, but `ap.pin` comes from a hand-editable
        # manifest and `mintd enclave add <repo> --pin=""` (e.g. `--pin="$SHA"`
        # with SHA unset) writes it. Without this guard such a manifest would
        # resolve to HEAD and report a clean "up to date" for a pin that does
        # not exist, and `enclave_bump` would silently no-op instead of
        # blocking. Refuse it explicitly rather than letting the sentinel
        # swallow it.
        #
        # Above the `upgrades` gate for the same reason as the `.dvc` lane:
        # the pin is read from the manifest, not the wire.
        if not ap.pin.strip():
            findings.append(
                CheckFinding(
                    severity="error",
                    section="consumer",
                    message=f"approved product {ap.repo!r} has an empty pin"
                    f" ({subscription_label(ap)})",
                    source=manifest_path,
                    field_path=field_path,
                    kind="pin_missing",
                    hint=(
                        f"set the pin for this row: `mintd enclave add {ap.repo} "
                        "--pin=<commit>`, or edit approved_products[].pin in the "
                        "manifest. An unset shell variable (`--pin=\"$SHA\"`) writes "
                        "an empty one."
                    ),
                )
            )
            continue

        if not upgrades:
            # Summary-only finding (no upgrades path); kind stays None — never reaches a write command.
            msg = f"approved {ap.repo}@{ap.pin[:7]} (path: {subscription_label(ap)})"
            findings.append(
                CheckFinding(
                    severity="info",
                    section="consumer",
                    message=msg,
                    source=manifest_path,
                    field_path=field_path,
                )
            )
            continue

        result_pin = _resolve_once(repo_url, ap.pin)
        if isinstance(result_pin, ProducerError):
            findings.append(
                _pin_read_finding(_error_finding_for(
                    manifest_path,
                    field_path,
                    result_pin,
                    label=subscription_label(ap),
                ))
            )
            continue

        result_head = _resolve_once(repo_url, "")
        if isinstance(result_head, ProducerError):
            # Labelled (D-D): without it a multi-row repo prints N identical
            # lines here.
            findings.append(
                _head_unreadable_finding(
                    source=manifest_path,
                    field_path=field_path,
                    pin=ap.pin,
                    err=result_head,
                    label=subscription_label(ap),
                )
            )
            continue

        findings.append(
            _drift_finding_from_views(
                source=manifest_path,
                field_path=field_path,
                pin_view=result_pin,
                head_view=result_head,
                expected_output_path=ap.source_path,
                fetcher=fetcher,
                memo=memo,
                all_outputs=ap.all,
                label=subscription_label(ap),
            )
        )
    return findings


def _git_error_summary(exc: GitOpError) -> str:
    """Git's own diagnosis: its first non-blank line that is not clone
    progress, else the command.

    A failed `clone` writes progress to stderr too, so its first line is
    `Cloning into '<the registry cache path>'...` — a directory the user never
    chose, ahead of the reason they are being shown this at all. Dropping that
    one line rather than seeking `fatal:`: over SSH the cause arrives BEFORE
    git's own `fatal:`, which is the generic `Could not read from remote
    repository.` --

        Cloning into '/…/cache'...
        ssh: Could not resolve hostname r.example: nodename nor servname …
        fatal: Could not read from remote repository.

    so preferring `fatal:` would trade one useless line for another. Over
    HTTPS the two rules agree: `fatal: unable to access …` names the cause
    itself and is the first line left.
    """
    lines = [
        ln.strip()
        for ln in (exc.stderr or "").splitlines()
        if ln.strip() and not ln.strip().startswith("Cloning into")
    ]
    return lines[0] if lines else " ".join(exc.command)


def _resolve_approved_product_url(client: CatalogClient, ap: ApprovedProduct) -> str:
    """Slice-8 Decision #2α: catalog is canonical for repo identity."""
    from .data import _require_repo_url

    entry = client.fetch(ap.repo)
    return _require_repo_url(entry.model_dump(), name=ap.repo)


def _summary_finding(dep: DataDependency) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message=f"imported {dep.local_path} from {dep.producer_repo}@{dep.contract_pin[:7]} (path: {dep.output_path})",
        source=dep.source,
    )


def _uptodate_finding_for(
    *, source: Path, field_path: str | None = None, label: str | None = None
) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message=f"up to date ({label})" if label else "up to date",
        source=source,
        field_path=field_path,
        kind="up_to_date",
    )


#: `drift_unknown` covers five distinct states and only two are transport
#: failures, so the hint travels with the finding rather than being guessed
#: from the kind at render time.
NETWORK_HINT = "retry when the network is available, then 'mintd check --upgrades'"

#: `_pointer_md5` verdict for "the rev is readable and the path's pointer is
#: definitively not there" — distinct from `None`, which means "could not
#: read" and must never quietly become a verdict.
_POINTER_ABSENT = "<absent>"


def _pointer_md5(
    fetcher: Fetcher,
    repo: str,
    pin: str,
    output_path: str,
    memo: dict[tuple[str, ...], Any],
) -> str | None:
    """The producer's own DVC pointer md5 for `output_path` at `pin`, memoized.

    Returns the matching out's `md5` verbatim — `.dir` hashes INCLUDED: a
    directory manifest's hash moves when any file inside it moves, which is
    exactly the signal drift needs (and exactly what `_match_out_files`
    deliberately skips; do not reuse it). Where dvc recorded no md5 for the
    path or for a `.dir` out above it, the return is the `.files` / `.outs`
    digest `_match_out_md5` returns.

    Three states: that string, `_POINTER_ABSENT` (readable rev, no pointer
    for that path), or `None` (transport/parse failure, or a stage dropped
    for an absolute `wdir` — loud, never a verdict).
    """
    key = (repo, pin, output_path)
    if key in memo:
        return memo[key]
    clean = output_path.rstrip("/")
    absent_so_far = True  # flipped by any failure that is not PATH_MISSING
    for candidate, base_dir in _pointer_candidates(clean):
        dropped_a_stage = False
        if candidate == "dvc.lock":
            resolved = _resolved_lock(fetcher, repo, pin, memo)
            if resolved is None:
                absent_so_far = False
                continue
            data, dropped_a_stage = resolved
            if dropped_a_stage:
                # A stage whose `wdir` could not be resolved is invisible
                # here, and an invisible out is indistinguishable from an
                # absent one. Absence is evidence only when nothing was
                # dropped — otherwise a stage that resolves at HEAD and not at
                # the pin manufactures "published at HEAD but not at your pin"
                # and `bump` re-pins on it.
                absent_so_far = False
        else:
            try:
                raw = fetcher.fetch_path_at(repo, pin, candidate)
            except FetchError as e:
                if e.reason != FetchError.Reason.PATH_MISSING:
                    absent_so_far = False
                continue
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                absent_so_far = False
                continue
        md5 = _match_out_md5(data, clean, base_dir=base_dir)
        # A `.outs` digest is a SET identity over the outs beneath the path;
        # with a stage dropped the set is incomplete, and a missing member
        # would read as "vanished" — drift that `bump` acts on. An exact or
        # enclosing match is one out's own identity and is unaffected.
        if md5 is not None and not (dropped_a_stage and md5.endswith(".outs")):
            memo[key] = md5
            return md5
        # A readable document with no matching out is absence evidence.
    memo[key] = verdict = _POINTER_ABSENT if absent_so_far else None
    return verdict


def _pointer_candidates(clean: str) -> list[tuple[str, str]]:
    """`(pointer file, the directory it sits in)` to try for `clean`.

    The first two are the product's own pointer and the pipeline lock. The
    ancestors after them cover a subscription to a path INSIDE a tracked
    directory out — `data/final/b.csv` when the producer tracks `data/final/`
    — which DVC gives no pointer of its own. They are only ever fetched when
    the first two produced no match, so the common shapes still cost what
    they always did.

    The directory travels with the file because a `.dvc` records `outs[].path`
    relative to itself: `data/final.dvc` says `path: final`. Resolving against
    it is what lets one rule serve every candidate.
    """
    parts = clean.split("/")
    candidates = [(f"{clean}.dvc", "/".join(parts[:-1])), ("dvc.lock", "")]
    candidates += [
        ("/".join(parts[:i]) + ".dvc", "/".join(parts[: i - 1]))
        for i in range(len(parts) - 1, 0, -1)
    ]
    return candidates


def _resolved_lock(
    fetcher: Fetcher, repo: str, pin: str, memo: dict[tuple[str, ...], Any]
) -> tuple[Any, bool] | None:
    """The producer's `dvc.lock` at `pin`, every out's path already resolved to
    repo-relative, memoized on `(repo, pin)`.

    One lock serves every subscribed path of that repo at that rev; before this
    each path refetched and reparsed it, which is N round-trips per rev for an
    enclave subscribed to N paths of one producer.

    Returns `(document, whether a stage was dropped)`. `{}` is the document for
    "the producer has no `dvc.lock`" — readable, matches nothing — and `None`
    means it could not be read, which must never become a verdict. Same
    `{}`-is-not-`None` discipline as `_stage_wdirs`, for the same reason.
    """
    key = ("lock", repo, pin)
    if key in memo:
        return memo[key]
    result: tuple[Any, bool] | None
    try:
        data = yaml.safe_load(fetcher.fetch_path_at(repo, pin, "dvc.lock"))
    except FetchError as e:
        result = ({}, False) if e.reason == FetchError.Reason.PATH_MISSING else None
    except yaml.YAMLError:
        result = None
    else:
        # `dvc.lock` records each out relative to its stage's `wdir`, and the
        # mintd data scaffold emits `wdir: code` + `../data/final/` where the
        # consumer subscribes to `data/final/`. Normalize, or D-C is inert for
        # every scaffolded producer.
        stage_wdirs = _stage_wdirs(fetcher, repo, pin, memo)
        result = (
            None if stage_wdirs is None
            else _lock_with_resolved_paths(data, stage_wdirs)
        )
    memo[key] = result
    return result


def _stage_wdirs(
    fetcher: Fetcher, repo: str, pin: str, memo: dict[tuple[str, ...], Any]
) -> dict[str, str | None] | None:
    """The producer's `stage -> wdir` map at `pin`, memoized on `(repo, pin)` —
    one `dvc.yaml` serves every subscribed path of that repo at that rev.

    `{}` is NOT `None`. `{}` means the producer genuinely has no `dvc.yaml`,
    so every stage defaults to `wdir="."` (correct for a `dvc add` producer);
    `None` means it could not be read. Collapsing them made the scaffold shape
    (`wdir: code`, `outs: - ../data/final/`) resolve to `../data/final`, get
    dropped as escaping the root, and read back as `_POINTER_ABSENT` — a
    `drift` finding manufactured out of one network blip, which `bump`
    then acts on.
    """
    key = (repo, pin)
    if key in memo:
        return memo[key]
    resolved: dict[str, str | None] | None
    try:
        resolved = wdir_map(yaml.safe_load(fetcher.fetch_path_at(repo, pin, "dvc.yaml")))
    except FetchError as e:
        # Only "the file is not there" is evidence about the producer; every
        # other reason is evidence about the network.
        resolved = {} if e.reason == FetchError.Reason.PATH_MISSING else None
    except yaml.YAMLError:
        resolved = None
    memo[key] = resolved
    return resolved


def _lock_with_resolved_paths(
    data: Any, stage_wdirs: dict[str, str | None]
) -> tuple[Any, bool]:
    """A parsed `dvc.lock` with every out's `path` rewritten from wdir-relative
    to repo-relative, plus whether any STAGE was dropped for an unresolvable
    `wdir`. An out that escapes the repo root is dropped too but is not
    reported: it is genuinely unaddressable by a subscription, whereas a
    dropped stage merely hid outs the caller would otherwise have seen."""
    if not isinstance(data, dict) or not isinstance(data.get("stages"), dict):
        return data, False
    stages: dict[str, Any] = {}
    dropped = False
    for stage, block in data["stages"].items():
        wdir = stage_wdir(stage_wdirs, str(stage))
        if wdir is None:
            dropped = True  # absolute wdir: unresolvable against the repo root
            continue
        if not isinstance(block, dict) or not isinstance(block.get("outs"), list):
            stages[stage] = block
            continue
        outs = []
        for out in block["outs"]:
            if not isinstance(out, dict) or "path" not in out:
                outs.append(out)
                continue
            rel = resolve_out(wdir, str(out["path"]))
            if rel.startswith("../"):
                continue  # escapes the repo root; not addressable by a subscription
            outs.append({**out, "path": rel})
        stages[stage] = {**block, "outs": outs}
    return {**data, "stages": stages}, dropped


def _out_identity(out: dict[str, Any], subpath: str = "") -> str | None:
    """The content identity of one parsed out, whichever shape dvc wrote.

    `subpath` scopes the answer to one path INSIDE a directory out. In
    files-format the per-file md5 is right there in the fetched document, so
    the answer is exact and a sibling's change is correctly invisible. A
    `.dir` pointer carries only the manifest hash — the manifest itself is a
    cache object, never in git — so the whole directory's identity is the only
    signal available: conservative, costing a churn re-pin when a sibling
    moves, never a wrong "up to date".

    ``md5: <hash>`` (a file) or ``md5: <hash>.dir`` (a directory manifest), or
    else files-format — a ``files:`` list and **no top-level md5**, which dvc
    writes for a directory out on a ``version_aware`` remote. ``_init_ops``
    turns that on for every scaffolded producer, so it is the DEFAULT lab
    shape: reading only ``md5`` reported `drift_unknown` forever and blocked
    every bump.

    For files-format we digest the sorted ``(relpath, md5)`` pairs rather than
    reconstruct dvc's own directory hash — that serialization is a dvc
    internal and would degrade *silently* if it shifted on an upgrade.

    Accepted ceiling: a pointer that flips format between pin and HEAD reads
    as drift once — loud and self-correcting, unlike a silent miss.
    """
    md5 = out.get("md5")
    files = out.get("files")
    if subpath:
        if isinstance(files, list):
            beneath: list[tuple[str, str]] = []
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                rel = str(entry.get("relpath", ""))
                if rel == subpath:
                    return str(entry.get("md5") or "") or None
                if rel.startswith(subpath + "/"):
                    beneath.append((rel, str(entry.get("md5", ""))))
            # A subDIRECTORY has no entry of its own — the entries beneath it
            # answer together, as `_match_out_md5` does for lock outs. Nothing
            # at or beneath it at this rev is genuine absence, not a miss.
            if beneath:
                return f"{_digest_pairs(sorted(beneath))}.files"
            return None
        return str(md5) if md5 else None
    if md5:
        return str(md5)
    if not isinstance(files, list):
        return None
    pairs = sorted(
        (str(f.get("relpath", "")), str(f.get("md5", "")))
        for f in files
        if isinstance(f, dict)
    )
    if not pairs:
        return None
    return f"{_digest_pairs(pairs)}.files"


def _digest_pairs(pairs: list[tuple[str, str]]) -> str:
    """One identity for a SORTED list of `(path, md5)` pairs."""
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def _match_out_md5(data: Any, clean_path: str, *, base_dir: str = "") -> str | None:
    """The content identity of the out matching `clean_path` in a parsed
    `.dvc`/`dvc.lock` document.

    Every out is resolved against `base_dir` first, so one rule serves both
    shapes: a `.dvc` records `outs[].path` relative to itself (`data/final.dvc`
    says `path: final`), and a `dvc.lock` out is repo-relative ONCE
    `_lock_with_resolved_paths` has run, which `_pointer_md5` does before
    calling here. Never match on the bare leaf — a sibling at another path
    (`archive/final` for a subscribed `data/final`) would answer for a path
    that is GONE at this rev.

    An exact match wins. Failing that the NEAREST enclosing out answers, which
    is how a subscription to a path inside a tracked directory gets a verdict
    at all — see `_out_identity`'s `subpath`. Failing THAT, the outs BENEATH
    the path answer together: a declared directory (`data/final/`) that a
    stage tracks as per-file outs (`data/final/a.dta`, …) has no pointer of
    its own, and reading that as absence blocked `bump`
    for every such producer. Its identity is a digest over the sorted
    `(path, identity)` pairs, so an out moving, appearing or vanishing beneath
    the directory is drift — the whole-directory posture `.dir` pointers
    already set. Suffix `.outs` keeps it distinct from a `.files` digest.
    Ceiling: per-file `.dvc` POINTERS beneath a directory stay invisible —
    `Fetcher` has no listing call, so only `dvc.lock` outs can be walked. A
    directory tracked ONLY by such pointers still reads as absent (loud). A
    directory tracked by BOTH lock outs and a hand-added pointer gets a
    verdict from the lock outs alone, so that pointer's file moving is
    missed — silent. Swept read-only across every directory primary in the
    catalog on 2026-09-15: none is mixed; the fix must be a listing call.
    """
    if not isinstance(data, dict):
        return None
    out_lists: list[list[Any]] = []
    if isinstance(data.get("outs"), list):
        out_lists.append(data["outs"])
    stages = data.get("stages")
    if isinstance(stages, dict):
        for stage in stages.values():
            if isinstance(stage, dict) and isinstance(stage.get("outs"), list):
                out_lists.append(stage["outs"])

    enclosing: tuple[int, dict[str, Any], str] | None = None
    beneath: list[tuple[str, str]] = []
    for outs in out_lists:
        for out in outs:
            if not isinstance(out, dict):
                continue
            raw = str(out.get("path", "")).rstrip("/")
            if not raw:
                continue
            resolved = resolve_out(base_dir, raw)
            if resolved == clean_path:
                identity = _out_identity(out)
                if identity is not None:
                    return identity
            elif clean_path.startswith(resolved + "/"):
                depth = resolved.count("/")
                if enclosing is None or depth > enclosing[0]:
                    enclosing = (depth, out, clean_path[len(resolved) + 1 :])
            elif resolved.startswith(clean_path + "/"):
                identity = _out_identity(out)
                if identity is not None:
                    beneath.append((resolved, identity))
    if enclosing is not None:
        return _out_identity(enclosing[1], enclosing[2])
    if beneath:
        return f"{_digest_pairs(sorted(beneath))}.outs"
    return None


def _drift_unknown_finding(
    *, source: Path, field_path: str | None, message: str, hint: str
) -> CheckFinding:
    # `severity="warning"`, deliberately: `check` exit codes are unchanged
    # (R4). `drift_unknown` is non-actionable for bump — both write verbs
    # treat any kind outside {drift, up_to_date} as blocked.
    return CheckFinding(
        severity="warning",
        section="consumer",
        message=message,
        source=source,
        field_path=field_path,
        kind="drift_unknown",
        hint=hint,
    )


def _head_unreadable_finding(
    *,
    source: Path,
    pin: str,
    err: ProducerError,
    field_path: str | None = None,
    label: str | None = None,
) -> CheckFinding:
    """The pin resolved but the producer's HEAD did not.

    Both arms used to collapse this to `up_to_date`, so an offline or
    air-gapped consumer reported every product clean forever and `--bump`
    was a permanent no-op (`ProducerView.at` serves the pin from the
    on-disk cache; `at_head` always needs the wire). A producer whose
    metadata is broken at HEAD was hidden the same way.

    `drift_unknown` and not `unreachable`: the pin is fine and nothing is
    wrong with the consumer's own state — what failed is the COMPARISON,
    which is exactly what `drift_unknown` means everywhere else here. It
    also keeps `check`'s exit code (severity `warning`, so still 0) while
    blocking both write verbs, which already refuse any kind outside
    {drift, up_to_date}.
    """
    labelled = f" ({label})" if label else ""
    reason = f"{err.reason}: {err.detail}" if err.detail else str(err.reason)
    return _drift_unknown_finding(
        source=source,
        field_path=field_path,
        message=(
            f"cannot determine drift{labelled}: cannot read the producer's "
            f"HEAD ({reason})"
        ),
        hint=(
            f"your pin {pin[:7]} is intact; re-run 'mintd check --upgrades' "
            "when the producer's HEAD is readable"
        ),
    )


def _drift_finding_from_views(
    *,
    source: Path,
    field_path: str | None,
    pin_view: ProducerView,
    head_view: ProducerView,
    expected_output_path: str | None,
    fetcher: Fetcher,
    memo: dict[tuple[str, ...], Any],
    all_outputs: bool = False,
    label: str | None = None,
) -> CheckFinding:
    """D-C: drift = the producer's DVC pointer md5 for your path differs
    pin-vs-HEAD. Producer *metadata* carries no per-output content identity
    (`last_published` is a per-publish stamp), so bytes committed without a
    `mintd publish` still count — the pointer is the ground truth.

    An unreadable pointer is `drift_unknown`, never `up_to_date`: silent
    degradation to "up to date" is the bug class this lane exists to kill.
    """
    repo = pin_view.repo
    pin, head = pin_view.pin, head_view.pin
    labelled = f" ({label})" if label else ""

    def md5_at(rev: str, path: str) -> str | None:
        return _pointer_md5(fetcher, repo, rev, path, memo)

    if all_outputs:
        # Compare the whole {path: md5} map; any UNREADABLE member is loud.
        paths = sorted(set(pin_view.output_paths()) | set(head_view.output_paths()))
        if not paths:
            return _drift_unknown_finding(
                source=source,
                field_path=field_path,
                message=f"cannot determine drift{labelled}: producer lists no outputs",
                hint=(
                    "ask the producer to declare data_products.outputs, or "
                    "subscribe to one path with 'enclave add --source-path'"
                ),
            )
        pin_map = {p: md5_at(pin, p) for p in paths}
        head_map = {p: md5_at(head, p) for p in paths}
        # `_POINTER_ABSENT` is a comparable VALUE, not a read failure — only
        # `None` (transport/parse) is unreadable. `outputs[]` is
        # hand-maintained metadata and nothing requires an entry to carry a
        # top-level pointer, so folding absence in here let one pointer-less
        # member veto the whole map and wedge `enclave bump` for the repo
        # forever. Absent at both revs compares equal and contributes nothing;
        # absent at one is real drift, and an `--all` bump re-resolves the
        # output list at HEAD, so a member that vanished is simply not
        # imported. (The single-path lane below differs on purpose: its one
        # target vanishing leaves the bump nothing to aim at.)
        unreadable = sorted(
            p for p in paths if pin_map[p] is None or head_map[p] is None
        )
        if unreadable:
            return _drift_unknown_finding(
                source=source,
                field_path=field_path,
                message=(
                    f"cannot determine drift{labelled}: no readable pointer for "
                    f"{', '.join(unreadable)} at {pin[:7]}/{head[:7]}"
                ),
                hint=NETWORK_HINT,
            )
        changed = sorted(p for p in paths if pin_map[p] != head_map[p])
        if changed:
            return CheckFinding(
                severity="warning",
                section="consumer",
                message=(
                    f"producer's HEAD differs from your pin{labelled}: "
                    f"{', '.join(changed)} changed"
                ),
                source=source,
                field_path=field_path,
                kind="drift",
            )
        return _uptodate_finding_for(source=source, field_path=field_path, label=label)

    # The `or primary` fallback is load-bearing: `from_dvc_lock_stage` records
    # `output_path=""`, so without it every pipeline-stage import would
    # resolve an empty path and report the same verdict forever.
    path = expected_output_path or pin_view.metadata.data_products.primary
    if not path:
        return _drift_unknown_finding(
            source=source,
            field_path=field_path,
            message=(
                f"cannot determine drift{labelled}: no output path recorded "
                f"and the producer has no primary at {pin[:7]}"
            ),
            hint=(
                "re-import with --path so this pin records which output it "
                "tracks, or ask the producer to set data_products.primary"
            ),
        )

    # For a `source_path` row `subscription_label` IS the path, so the
    # parenthetical would repeat what every message in this arm already says:
    # "cannot determine drift for data/final/b.csv (data/final/b.csv)".
    if label and label.rstrip("/") == path.rstrip("/"):
        labelled = ""

    pin_md5, head_md5 = md5_at(pin, path), md5_at(head, path)
    if pin_md5 is None or head_md5 is None:
        return _drift_unknown_finding(
            source=source,
            field_path=field_path,
            message=(
                f"cannot determine drift for {path}{labelled}: no readable "
                f".dvc or dvc.lock at {pin[:7]}/{head[:7]}"
            ),
            hint=NETWORK_HINT,
        )
    if head_md5 == _POINTER_ABSENT:
        # Removed at HEAD, or tracked only by per-file `.dvc` pointers beneath
        # a directory, which the comparator cannot list. Either way a bump has
        # no target; the hint says what was checked, not what the producer did.
        return _drift_unknown_finding(
            source=source,
            field_path=field_path,
            message=(
                f"cannot determine drift for {path}{labelled}: "
                f"not published at the producer's HEAD ({head[:7]})"
            ),
            hint=(
                f"at {head[:7]} {path} is in no .dvc pointer at or above it "
                "and no dvc.lock out at, above, or beneath it; pin to an "
                "older rev, or drop the subscription"
            ),
        )
    if pin_md5 == _POINTER_ABSENT:
        # Present at HEAD, absent at your pin — drift. Whether that is an
        # upgrade needs an ancestry query the ProducerView seam does not
        # carry (a filesystem-path producer's HEAD can be BEHIND the pin),
        # so the message states only what the pointers establish.
        return CheckFinding(
            severity="warning",
            section="consumer",
            message=(
                f"producer's HEAD differs from your pin{labelled}: {path} is "
                f"published at HEAD but not at your pin {pin[:7]}"
            ),
            source=source,
            field_path=field_path,
            kind="drift",
        )
    if pin_md5 != head_md5:
        return CheckFinding(
            severity="warning",
            section="consumer",
            message=(
                f"producer's HEAD differs from your pin{labelled}: "
                f"{path} changed"
            ),
            source=source,
            field_path=field_path,
            kind="drift",
        )
    return _uptodate_finding_for(source=source, field_path=field_path, label=label)


def _drift_finding(
    dep: DataDependency,
    pin_view: ProducerView,
    head_view: ProducerView,
    *,
    fetcher: Fetcher,
    memo: dict[tuple[str, ...], Any],
) -> CheckFinding:
    return _drift_finding_from_views(
        source=dep.source,
        field_path=None,
        pin_view=pin_view,
        head_view=head_view,
        expected_output_path=dep.output_path,
        fetcher=fetcher,
        memo=memo,
    )


def _error_finding_for(
    source: Path,
    field_path: str | None,
    err: ProducerError,
    *,
    label: str | None = None,
) -> CheckFinding:
    kind: Literal[
        "unreachable",
        "pin_missing",
        "metadata_missing",
        "metadata_invalid",
        "schema_too_old",
    ] | None
    hint = None
    if err.reason == ProducerError.Reason.UNREACHABLE:
        severity: Literal["error", "warning", "info"] = "warning"
        message = f"producer unreachable: {err.detail}"
        kind = "unreachable"
        hint = unreachable_repo_hint(err.repo)
    elif err.reason == ProducerError.Reason.PIN_MISSING:
        severity = "error"
        message = f"producer pin missing: {err.pin[:7]} not found in {err.repo}"
        kind = "pin_missing"
    elif err.reason == ProducerError.Reason.METADATA_MISSING:
        severity = "error"
        message = f"producer has no metadata.json at pin {err.pin[:7]}"
        kind = "metadata_missing"
    elif err.reason == ProducerError.Reason.METADATA_INVALID:
        severity = "error"
        message = f"producer metadata invalid at pin {err.pin[:7]}: {err.detail}"
        kind = "metadata_invalid"
    elif err.reason == ProducerError.Reason.SCHEMA_TOO_OLD:
        severity = "warning"
        message = f"producer at pin {err.pin[:7]} uses schema_version {err.detail} (expected 2.0)"
        kind = "schema_too_old"
    else:
        # Reason is a closed StrEnum; this arm is defensive — kind stays None.
        severity = "error"
        message = f"producer error at pin {err.pin[:7]}: {err.detail}"
        kind = None

    if label:
        message = f"{message} ({label})"

    return CheckFinding(
        severity=severity,
        section="consumer",
        message=message,
        source=source,
        field_path=field_path,
        kind=kind,
        hint=hint,
    )


def _pin_read_finding(finding: CheckFinding) -> CheckFinding:
    """A producer read at the pin, which only `--upgrades` does (imports and
    enclave rows). A pin the producer's history no longer has is a warning
    there, as an unreachable producer is: the consumer can't fix it, so it
    mustn't turn CI red."""
    return replace(finding, severity="warning") if finding.kind == "pin_missing" else finding


def _error_finding(dep: DataDependency, err: ProducerError) -> CheckFinding:
    return _pin_read_finding(_error_finding_for(source=dep.source, field_path=None, err=err))
