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

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .imports import DataDependency, scan_imports
from .model import Metadata
from .producer import ProducerError, ProducerView

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_project(
    path: Path,
    *,
    upgrades: bool = False,
    producer_view_factory: ProducerViewFactory | None = None,
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
            path, upgrades=upgrades, producer_view_factory=producer_view_factory
        )
    )
    return findings


# ---------------------------------------------------------------------------
# Section helpers
# ---------------------------------------------------------------------------


def _producer_findings(project_path: Path) -> list[CheckFinding]:
    """Producer-section checks: everything derivable from metadata.json alone."""
    metadata_path = project_path / "metadata.json"

    if not metadata_path.is_file():
        return [
            CheckFinding(
                severity="error",
                section="producer",
                message=f"metadata.json not found at {metadata_path}",
            )
        ]

    raw = metadata_path.read_text()

    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return [
            CheckFinding(
                severity="error",
                section="producer",
                message=f"malformed JSON in metadata.json: {e.msg} (line {e.lineno}, col {e.colno})",
            )
        ]

    try:
        Metadata.model_validate_json(raw)
    except ValidationError as e:
        return [
            CheckFinding(
                severity="error",
                section="producer",
                message=err["msg"],
                field_path=".".join(str(p) for p in err["loc"]) or None,
            )
            for err in e.errors()
        ]

    return []


def _consumer_findings(
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


def _summary_finding(dep: DataDependency) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message=f"imported {dep.local_path} from {dep.producer_repo}@{dep.contract_pin[:7]} (path: {dep.output_path})",
        source=dep.source,
    )


def _uptodate_finding(dep: DataDependency) -> CheckFinding:
    return CheckFinding(
        severity="info",
        section="consumer",
        message="up to date",
        source=dep.source,
    )


def _drift_finding(
    dep: DataDependency, pin_view: ProducerView, head_view: ProducerView
) -> CheckFinding:
    if dep.output_path and dep.output_path not in pin_view.output_paths():
        return _uptodate_finding(dep)

    head_primary = head_view.metadata.data_products.primary
    pin_primary = pin_view.metadata.data_products.primary

    if head_primary == pin_primary:
        return _uptodate_finding(dep)

    head_primary_str = head_primary if head_primary is not None else "(no primary)"
    return CheckFinding(
        severity="warning",
        section="consumer",
        message=f"upgrade available: producer now publishes {head_primary_str!r} (you have {dep.output_path!r})",
        source=dep.source,
    )


def _error_finding(dep: DataDependency, err: ProducerError) -> CheckFinding:
    if err.reason == ProducerError.Reason.UNREACHABLE:
        severity = "warning"
        message = f"producer unreachable: {err.detail}"
    elif err.reason == ProducerError.Reason.PIN_MISSING:
        severity = "error"
        message = f"producer pin missing: {err.pin[:7]} not found in {err.repo}"
    elif err.reason == ProducerError.Reason.METADATA_MISSING:
        severity = "error"
        message = f"producer has no metadata.json at pin {err.pin[:7]}"
    elif err.reason == ProducerError.Reason.METADATA_INVALID:
        severity = "error"
        message = f"producer metadata invalid at pin {err.pin[:7]}: {err.detail}"
    elif err.reason == ProducerError.Reason.SCHEMA_TOO_OLD:
        severity = "warning"
        message = f"producer at pin {err.pin[:7]} uses schema_version {err.detail} (expected 2.0)"
    else:
        severity = "error"
        message = f"producer error at pin {err.pin[:7]}: {err.detail}"

    return CheckFinding(
        severity=severity,  # type: ignore
        section="consumer",
        message=message,
        source=dep.source,
    )
