"""Scaffold rendering orchestrator.

``render_scaffold`` is the slice-19 public surface: takes a project type +
language + name + target_dir, builds the canonical context dict, walks the
scaffold file list, and writes each rendered file to disk. Returns the
list of written paths so the CLI can render `created:` lines.
"""

from __future__ import annotations

import getpass
import importlib.metadata
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .engine import render_template
from .scaffolds import dispatch


_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# Static files that should be copied as-is rather than Jinja-rendered.
# These ship under `src/mintd/files/` next to the .j2 templates.
_STATIC_FILES: frozenset[str] = frozenset({"gitignore.txt", "dvcignore.txt"})

# Templates that map to v1 schema 1.1 — render via the v2 Pydantic model
# instead, so the rendered JSON validates. Recorded as slice-19's binding-
# question hit: 51/52 templates vendored cleanly; metadata.json needed
# this special case because v1 and v2 use different schema versions.
_METADATA_TEMPLATES: frozenset[str] = frozenset(
    {"metadata.json.j2", "metadata_code.json.j2"}
)


class InitNameInvalid(Exception):
    """Project name did not match the alphanumeric / dash / underscore regex."""


def validate_project_name(name: str) -> None:
    """Raise ``InitNameInvalid`` if ``name`` isn't a safe filesystem segment."""
    if not _NAME_RE.match(name):
        raise InitNameInvalid(
            f"invalid project name {name!r}; must match {_NAME_RE.pattern}"
        )


def _get_mint_hash() -> str:
    """Slice 19 records ``mint_hash`` in the Jinja context for parity with
    the legacy templates, but the v2 Pydantic ``metadata.json`` generation
    (``_render_metadata_json``) hardcodes ``mint.commit_hash=""``. Returning
    the constant ``"unknown"`` here avoids the wheel-install footgun where
    walking ``__file__.parent**4`` could resolve to an unrelated enclosing
    git repo and silently corrupt provenance. A follow-up slice can replace
    this with a build-time-baked hash once any template actually consumes it.
    """
    return "unknown"


def _current_user() -> str:
    """Best-effort user lookup. Falls back to ``"unknown"`` when no USER/
    LOGNAME env var is set and the UID has no passwd entry (CI, distroless)."""
    try:
        return getpass.getuser()
    except OSError:
        return "unknown"


def _detect_platform_os() -> str:
    import platform as _platform
    system = _platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def _build_context(
    *,
    project_type: str,
    name: str,
    language: str,
    source_dir: str = "code",
) -> dict[str, object]:
    try:
        version = importlib.metadata.version("mintd")
    except importlib.metadata.PackageNotFoundError:
        version = "0.0.0"
    # Load the user's config lazily; slice 21 lets users seed these fields
    # via `mintd config setup`. Absent fields fall back to safe defaults
    # — empty strings for cosmetic vars, sensible literals for the rest.
    # See `notes/V1-PORT-AUDIT.md` for the legacy→v2 mapping.
    try:
        from .._config import Config
        cfg = Config.load()
    except Exception:
        cfg = None

    def _cfg(name: str, default: object) -> object:
        if cfg is None:
            return default
        value = getattr(cfg, name, None)
        return default if value is None else value

    platform_os = _detect_platform_os()
    command_sep = "&" if platform_os == "windows" else "&&"

    # Empty-string defaults for every variable the vendored legacy templates
    # reference. Slice-21 absorbs the v1-config fields that users actually
    # set; the rest stay deferred until a downstream slice surfaces a need.
    # Keys are grouped by source for readability.
    return {
        # Set by slice-19 init flow.
        "project_name": name,
        "full_project_name": f"{project_type}_{name}",
        "package_name": name,
        "project_type": project_type,
        "language": language,
        "language_version": "",
        "source_dir": source_dir,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "created_by": _current_user(),
        "mint_version": version,
        "mint_hash": _get_mint_hash(),

        # Platform — auto-detected (slice 21). Windows shell scripts not yet
        # vendored — see notes/V1-PORT-AUDIT.md and the windows-followup memory.
        "platform_os": platform_os,
        "command_sep": command_sep,
        "stata_executable": _cfg("stata_executable", "stata"),

        # Absorbed from v1 config in slice 21.
        "author": _cfg("author", ""),
        "organization": _cfg("organization", ""),

        # Deferred to governance-flag slice (--public/--contract/--private/...).
        "classification": "private",
        "contract_info": "",
        "description": "",
        "tags": [],
        "methods": "",
        "data_kind": "",

        # Team fields absorbed in slice 21. `team` itself remains deferred —
        # legacy didn't model a single team key.
        "team": "",
        "admin_team": _cfg("admin_team", ""),
        "researcher_team": _cfg("researcher_team", ""),

        # Storage fields absorbed in slice 21.
        "bucket_name": _cfg("storage_bucket_prefix", ""),
        "storage_endpoint": _cfg("storage_endpoint", ""),
        "storage_prefix": _cfg("storage_bucket_prefix", ""),
        "storage_provider": "s3",
        "storage_versioning": True,
        "dvc_remote_name": "origin",

        # Registry fields — registry_url and registry_org both absorbed in slice 21.
        "registry_org": _cfg("registry_org", ""),
        "registry_url": _cfg("registry_url", ""),
        "mirror_url": "",
        "mirror_purpose": "",

        # Deferred (pipeline definition / catalog imports).
        "data_dependencies": [],
        "data_products_primary": "",
        "configurations": [],
    }


def _team_entries(context: dict[str, object]) -> list[dict[str, str]]:
    """Build the `access_control.teams` list from admin_team / researcher_team
    context values. Empty strings are skipped so the resulting list only
    contains entries the user actually configured."""
    entries: list[dict[str, str]] = []
    admin = str(context.get("admin_team") or "")
    researcher = str(context.get("researcher_team") or "")
    if admin:
        entries.append({"name": admin, "permission": "admin"})
    if researcher:
        entries.append({"name": researcher, "permission": "read"})
    return entries


def _render_metadata_json(context: dict[str, object]) -> str:
    """Generate v2-shaped metadata.json via the Pydantic ``Metadata`` model.

    Bypasses the legacy ``metadata.json.j2`` template, which targets v1
    schema 1.1 and would not validate against v2's 2.0 model. Slice-19
    binding-question hit; documented in retro.
    """
    from ..model import Metadata  # local import to avoid cycles at module load
    project_type = str(context["project_type"])
    name = str(context["project_name"])
    created_at = str(context["created_at"])
    created_by = str(context["created_by"]) or "unknown"
    mint_version = str(context.get("mint_version") or "0.0.0")
    data = {
        "schema_version": "2.0",
        "mint": {"version": mint_version, "commit_hash": ""},
        "project": {
            "type": project_type,
            "name": name,
            "full_name": f"{project_type}_{name}",
            "created_at": created_at,
            "created_by": created_by,
        },
        "metadata": {"description": "", "tags": []},
        "ownership": {"team": "", "maintainers": [created_by]},
        "access_control": {"teams": _team_entries(context)},
        "governance": {
            "classification": str(context.get("classification") or "private"),
            "contract_info": str(context.get("contract_info") or ""),
        },
        "data_products": {"primary": None, "outputs": []},
        "repository": {
            "github_url": "",
            "default_branch": "main",
            "visibility": "private",
            "mirror": {
                "url": str(context.get("mirror_url") or ""),
                "purpose": str(context.get("mirror_purpose") or ""),
            },
        },
        "status": {
            "state": "active",
            "last_updated": created_at,
            "last_published_version": "",
        },
    }
    return Metadata.model_validate(data).model_dump_json(indent=2) + "\n"


def _write_file(out_path: Path, template_name: str, context: dict[str, object]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if template_name in _STATIC_FILES:
        # Copy verbatim. Use importlib.resources for installed-package safety.
        from importlib.resources import files as _files
        out_path.write_text(
            (_files("mintd") / "files" / template_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return
    if template_name in _METADATA_TEMPLATES:
        out_path.write_text(_render_metadata_json(context), encoding="utf-8")
        return
    out_path.write_text(render_template(template_name, context), encoding="utf-8")


def render_scaffold(
    *,
    project_type: Literal["data", "project", "code", "enclave"],
    name: str,
    language: Literal["python", "r", "stata"],
    target_dir: Path,
    context_overrides: dict[str, object] | None = None,
) -> list[Path]:
    """Render the full scaffold for a typed project into ``target_dir``.

    Caller is responsible for ensuring ``target_dir`` exists. ``name`` is
    validated; raises ``InitNameInvalid`` on a bad name. Returns the list
    of files written (in scaffold order), so the CLI can print one
    ``created:`` line per file.
    """
    validate_project_name(name)
    context = _build_context(project_type=project_type, name=name, language=language)
    if context_overrides:
        context.update(context_overrides)

    full_name = f"{project_type}_{name}"
    dirs, files = dispatch(project_type)(language, name, full_name)

    for rel_dir in dirs:
        (target_dir / rel_dir).mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for rel_path, template_name in files:
        out_path = target_dir / rel_path
        _write_file(out_path, template_name, context)
        written.append(out_path)

        # Enclave shell scripts must be executable for the legacy workflow.
        if rel_path.startswith("scripts/") and rel_path.endswith(".sh"):
            out_path.chmod(0o755)

    return written


__all__ = [
    "InitNameInvalid",
    "render_scaffold",
    "render_template",
    "validate_project_name",
]


