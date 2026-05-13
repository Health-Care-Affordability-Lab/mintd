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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .model import Metadata


# ---------------------------------------------------------------------------
# Finding type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckFinding:
    severity: Literal["error", "warning", "info"]
    section: Literal["producer", "consumer", "environment"]
    message: str
    field_path: str | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_project(path: Path) -> list[CheckFinding]:
    """Validate a mintd project at `path` (the project directory).

    Returns a list of findings. Empty list means clean.

    Slice 1 behavior — producer section only:
      - metadata.json missing → 1 error finding
      - metadata.json malformed JSON → 1 error finding
      - metadata.json fails Pydantic → 1 error finding per ValidationError entry
      - valid → []

    Slice 4 will add: imports.yaml validation, pin resolution.
    Slice 6 will add: env hygiene (dvc/git/gh), --upgrades network checks.
    """
    return _producer_findings(path)


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


# Slice 4 will add: _consumer_findings(project_path) -> list[CheckFinding]
# Slice 6 will add: _environment_findings() -> list[CheckFinding]
