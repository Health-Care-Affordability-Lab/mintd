"""Unified project validation.

This is the single source of validation findings for any mintd project.
Other modules (validate_publishable, mintd registry update preflight, etc.)
query check_project() instead of re-implementing checks.

Findings are split into three sections, by which artifact they read:

  - producer: derivable from metadata.json alone — shape, required fields,
    Owner × Audience consistency, storage config sanity. This is what a
    project owner is responsible for getting right before publishing.
  - consumer: derivable from imports.yaml and the resolved producer metadata
    of upstream projects — pin resolvability, version compatibility. This is
    what a project owner is responsible for keeping current as upstreams move.
  - environment: derivable from the local machine — dvc/git/gh availability,
    versions, auth state. Not the project's fault; affects whether commands
    can actually run.

Findings carry one of three severities:

  - error: blocks publish / blocks `mintd registry update`. The project is
    not in a valid state.
  - warning: surfaced to the user but does not block. Something is unusual
    or likely-wrong (e.g., a USER-owned field that looks tool-generated).
  - info: purely informational. Used sparingly.

Slice 1 scope:
  - Producer section: Pydantic validation of metadata.json only.
  - Consumer section: returns [] (added in slice 4 with imports.yaml).
  - Environment section: returns [] (added in slice 6 with --upgrades).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError

from ._fast_sync_ops import resolve_out, stage_wdir, wdir_map
from ._registry_git_ops import GitOpError
from .catalog import CatalogClient, CatalogNotFound
from .imports import DataDependency, scan_imports
from .model import Metadata
from .producer import FetchError, Fetcher, GitArchiveFetcher, ProducerError, ProducerView

if TYPE_CHECKING:
    # Avoid module-level import of enclave.py — enclave.py imports from this
    # module (CheckFinding) and from data.py, but data.py is imported here.
    # Lazy import inside the manifest walker breaks the cycle for runtime.
    from .enclave import ApprovedProduct

ProducerViewFactory = Callable[[str, str], "ProducerView | ProducerError"]

# Project types for which a `data_products.primary` is mandatory. Other types
# (code/project/enclave) may declare a primary but are not required to — a
# code/project repo publishes no consumable data product, and an enclave
# resolves *upstream* producers' primaries rather than publishing its own.
PRIMARY_REQUIRED_TYPES: frozenset[str] = frozenset({"data"})

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
) -> list[CheckFinding]:
    """Validate a mintd project at `path` (the project directory).

    Returns a list of findings. Empty list means clean.

    Slice 1 behavior — producer section only:
      - metadata.json missing → 1 error finding
      - metadata.json malformed JSON → 1 error finding
      - metadata.json fails Pydantic → 1 error finding per ValidationError entry
      - valid → []

    Slice 4 added: imports.yaml validation, pin resolution.
    Slice 6 added: env hygiene (dvc/git/gh), --upgrades network checks.
    """
    findings = _producer_findings(path)
    findings.extend(
        _consumer_findings(
            path,
            upgrades=upgrades,
            producer_view_factory=producer_view_factory,
            client=client,
            fetcher=fetcher,
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _producer_findings(project_path: Path) -> list[CheckFinding]:
    """Producer-section checks: everything derivable from metadata.json alone."""
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

    return findings


def _check_repository_identity(meta: Metadata, metadata_path: Path) -> list[CheckFinding]:
    """An entry with no repository.github_url is unusable to every consumer.

    `_require_repo_url` (data.py:300-304) raises on it, so `mintd data clone`
    against such a catalog entry exits 1. Presence only -- the derived
    `{org}/{full_name}` shape is a scaffold default a human may legitimately
    override (the lab's `skills` entry really lives at `hcal-agent-skills`).
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
        # also makes `data_bump` (data.py:568, gates on kind) silently no-op.
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
            # We could resolve the pin but not HEAD — degrade to "up to date"
            findings.append(_uptodate_finding(dep))
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
                    message=f"catalog client not provided; cannot resolve producer URL for {ap.repo}",
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
                hint = (
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
                    message=message,
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
                    message=f"approved product {ap.repo!r} has an empty pin",
                    source=manifest_path,
                    field_path=field_path,
                    kind="pin_missing",
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
            findings.append(_error_finding_for(manifest_path, field_path, result_pin))
            continue

        result_head = _resolve_once(repo_url, "")
        if isinstance(result_head, ProducerError):
            # HEAD-unreachable degrade. Labelled too (D-D): without it a
            # multi-row repo prints N identical unlabeled lines here.
            findings.append(
                _uptodate_finding_for(
                    source=manifest_path,
                    field_path=field_path,
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
    """First non-blank line of git's own stderr; the command when it is empty."""
    lines = (ln.strip() for ln in (exc.stderr or "").splitlines() if ln.strip())
    return next(lines, " ".join(exc.command))


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


def _uptodate_finding(dep: DataDependency) -> CheckFinding:
    return _uptodate_finding_for(source=dep.source)


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
    deliberately skips; do not reuse it).

    Three states: the md5 string, `_POINTER_ABSENT` (readable rev, no pointer
    for that path), or `None` (transport/parse failure — loud, never a
    verdict).
    """
    key = (repo, pin, output_path)
    if key in memo:
        return memo[key]
    clean = output_path.rstrip("/")
    absent_so_far = True  # flipped by any failure that is not PATH_MISSING
    for candidate, base_dir in _pointer_candidates(clean):
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
        if md5 is not None:
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
    dropped as escaping the root, and read back as `_POINTER_ABSENT` — an
    "upgrade available" manufactured out of one network blip, which `bump`
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
            for entry in files:
                if isinstance(entry, dict) and str(entry.get("relpath", "")) == subpath:
                    return str(entry.get("md5") or "") or None
            # Not in the directory at this rev — genuine absence, not a miss.
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
    digest = hashlib.sha256(
        json.dumps(pairs, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{digest}.files"


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
    at all — see `_out_identity`'s `subpath`.
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
    if enclosing is None:
        return None
    return _out_identity(enclosing[1], enclosing[2])


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
                    f"upgrade available{labelled}: "
                    f"{', '.join(changed)} changed at the producer's HEAD"
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
        # Removed at HEAD (or never published there): a bump has no target.
        return _drift_unknown_finding(
            source=source,
            field_path=field_path,
            message=(
                f"cannot determine drift for {path}{labelled}: "
                f"not published at the producer's HEAD ({head[:7]})"
            ),
            hint=(
                "the producer no longer publishes this output; pin to an "
                "older rev, or drop the subscription"
            ),
        )
    if pin_md5 == _POINTER_ABSENT:
        # Published after the pin — a real, bumpable upgrade.
        return CheckFinding(
            severity="warning",
            section="consumer",
            message=(
                f"upgrade available{labelled}: {path} is published at the "
                f"producer's HEAD but not at your pin {pin[:7]}"
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
                f"upgrade available{labelled}: {path} changed at the "
                f"producer's HEAD"
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
    source: Path, field_path: str | None, err: ProducerError
) -> CheckFinding:
    kind: Literal[
        "unreachable",
        "pin_missing",
        "metadata_missing",
        "metadata_invalid",
        "schema_too_old",
    ] | None
    if err.reason == ProducerError.Reason.UNREACHABLE:
        severity: Literal["error", "warning", "info"] = "warning"
        message = f"producer unreachable: {err.detail}"
        kind = "unreachable"
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

    return CheckFinding(
        severity=severity,
        section="consumer",
        message=message,
        source=source,
        field_path=field_path,
        kind=kind,
    )


def _error_finding(dep: DataDependency, err: ProducerError) -> CheckFinding:
    return _error_finding_for(source=dep.source, field_path=None, err=err)
