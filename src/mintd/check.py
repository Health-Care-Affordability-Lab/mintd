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

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError

from .catalog import CatalogClient, CatalogNotFound
from .imports import DataDependency, scan_imports
from .model import Metadata
from .producer import ProducerError, ProducerView

if TYPE_CHECKING:
    # Avoid module-level import of enclave.py — enclave.py imports from this
    # module (CheckFinding) and from data.py, but data.py is imported here.
    # Lazy import inside the manifest walker breaks the cycle for runtime.
    from .enclave import ApprovedProduct

ProducerViewFactory = Callable[[str, str], "ProducerView | ProducerError"]

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

    try:
        Metadata.model_validate_json(raw)
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

    return findings


def _consumer_findings(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    client: CatalogClient | None = None,
    imports_under: str = "data/imports",
) -> list[CheckFinding]:
    findings = _consumer_findings_from_dvc(
        project_path,
        upgrades=upgrades,
        producer_view_factory=producer_view_factory,
        imports_under=imports_under,
    )
    findings.extend(
        _consumer_findings_from_enclave_manifest(
            project_path,
            upgrades=upgrades,
            producer_view_factory=producer_view_factory,
            client=client,
        )
    )
    return findings


def _consumer_findings_from_dvc(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    imports_under: str = "data/imports",
) -> list[CheckFinding]:
    deps = scan_imports(project_path, under=imports_under)
    if not deps:
        return []

    findings: list[CheckFinding] = []
    factory = producer_view_factory if producer_view_factory is not None else ProducerView.try_at

    for dep in deps:
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

        findings.append(_drift_finding(dep, result_pin, result_head))

    return findings


def _consumer_findings_from_enclave_manifest(
    project_path: Path,
    *,
    upgrades: bool,
    producer_view_factory: ProducerViewFactory | None,
    client: CatalogClient | None,
) -> list[CheckFinding]:
    manifest_path = project_path / "enclave_manifest.yaml"
    if not manifest_path.is_file():
        return []

    # Lazy import to break the check.py ↔ enclave.py cycle.
    from .enclave import EnclaveManifest

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
        except (ValueError, CatalogNotFound) as e:
            findings.append(
                CheckFinding(
                    severity="error",
                    section="consumer",
                    message=str(e),
                    source=manifest_path,
                    field_path=field_path,
                    kind="catalog_unresolved",
                )
            )
            continue

        if not upgrades:
            # Summary-only finding (no upgrades path); kind stays None — never reaches a write command.
            msg = f"approved {ap.repo}@{ap.pin[:7]} (path: {ap.source_path or '<primary>'})"
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

        result_pin = factory(repo_url, ap.pin)
        if isinstance(result_pin, ProducerError):
            findings.append(_error_finding_for(manifest_path, field_path, result_pin))
            continue

        result_head = factory(repo_url, "")
        if isinstance(result_head, ProducerError):
            findings.append(_uptodate_finding_for(source=manifest_path, field_path=field_path))
            continue

        findings.append(
            _drift_finding_from_views(
                source=manifest_path,
                field_path=field_path,
                pin_view=result_pin,
                head_view=result_head,
                expected_output_path=ap.source_path,
            )
        )
    return findings


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


def _uptodate_finding_for(*, source: Path, field_path: str | None = None) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=source,
        field_path=field_path,
        kind="up_to_date",
    )


def _uptodate_finding(dep: DataDependency) -> CheckFinding:
    return _uptodate_finding_for(source=dep.source)


def _drift_finding_from_views(
    *,
    source: Path,
    field_path: str | None,
    pin_view: ProducerView,
    head_view: ProducerView,
    expected_output_path: str | None,
) -> CheckFinding:
    if expected_output_path and expected_output_path not in pin_view.output_paths():
        return _uptodate_finding_for(source=source, field_path=field_path)

    head_primary = head_view.metadata.data_products.primary
    pin_primary = pin_view.metadata.data_products.primary

    if head_primary == pin_primary:
        return _uptodate_finding_for(source=source, field_path=field_path)

    head_primary_str = head_primary if head_primary is not None else "(no primary)"
    return CheckFinding(
        severity="warning",
        section="consumer",
        message=f"upgrade available: producer now publishes {head_primary_str!r} (you have {expected_output_path!r})",
        source=source,
        field_path=field_path,
        kind="drift",
    )


def _drift_finding(
    dep: DataDependency, pin_view: ProducerView, head_view: ProducerView
) -> CheckFinding:
    return _drift_finding_from_views(
        source=dep.source,
        field_path=None,
        pin_view=pin_view,
        head_view=head_view,
        expected_output_path=dep.output_path,
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
