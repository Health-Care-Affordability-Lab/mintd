"""``mintd`` CLI entry point.

Subcommand tree wires the slice-1..9 API to argparse. No business logic
lives here — every subcommand delegates to a public function in the
library layer (``mintd.{check, data, enclave, catalog}``). The only
CLI-specific concerns are:

- argument parsing
- ``Config`` loading and client construction (``_resolve_clients``)
- rendering ``CheckFinding``s and ``BumpBlocked`` exceptions for humans
- exit-code dispatch
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from ._config import Config, ConfigError
from ._dvc_ops import DvcOps, SubprocessDvcOps
from .catalog import (
    CatalogAlreadyExists,
    CatalogClient,
    CatalogFilter,
    CatalogNotFound,
    GitCatalogClient,
)
from .check import CheckFinding, check_project
from .data import (
    BumpBlocked,
    ImportDestinationExists,
    ImportNotFound,
    MissingPrimaryDataProduct,
    PrimaryRemovedAtHead,
    bump_import,
    import_product,
)
from .enclave import AppendOnlyViolation, EnclaveManifest, enclave_bump
from .imports import scan_imports
from .model import Metadata
from .pending_registrations import PendingRegistrations
from .producer import ProducerError


# Unicode prefixes assume UTF-8 stdout. Modern terminals and CI runners
# default to UTF-8; if a non-UTF-8 locale ever surfaces, swap to ASCII
# (`+`, `^`, `!`, `x`, `.`).
_KIND_PREFIX: dict[str | None, str] = {
    "up_to_date": "✓",
    "drift": "↑",
    "unreachable": "⚠",
    "schema_too_old": "⚠",
    "pin_missing": "✗",
    "metadata_missing": "✗",
    "metadata_invalid": "✗",
    "invalid_manifest": "✗",
    "catalog_unresolved": "✗",
    None: "·",
}

_RECOVERABLE_KINDS: frozenset[str] = frozenset({"unreachable", "schema_too_old"})


class _MintdArgumentParser(argparse.ArgumentParser):
    """argparse subclass that exits 64 on misuse (instead of argparse's 2)."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(64)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = _MintdArgumentParser(
        prog="mintd",
        description="mintd: Lightweight data product framework for research labs",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.0.1")
    subs = parser.add_subparsers(dest="command")

    p_check = subs.add_parser("check", help="Validate a mintd project")
    p_check.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_check.add_argument("--upgrades", action="store_true")
    p_check.add_argument("--json", action="store_true", dest="json_out")
    p_check.set_defaults(_handler=_handle_check)

    p_data = subs.add_parser("data", help="Data commands")
    p_data_sub = p_data.add_subparsers(dest="data_command")
    p_import = p_data_sub.add_parser("import", help="Import a producer's data product")
    p_import.add_argument("name")
    p_import.add_argument("--bump", action="store_true")
    p_import.add_argument("--path", dest="import_path")
    p_import.add_argument("--rev")
    p_import.add_argument("--all", dest="all_outputs", action="store_true")
    p_import.add_argument("--force", action="store_true")
    p_import.add_argument(
        "--dest-root", type=Path, default=Path("data/imports"), dest="dest_root"
    )
    p_import.set_defaults(_handler=_handle_data_import, _parser=p_import)

    p_data_list = p_data_sub.add_parser("list", help="List catalog entries or local imports")
    p_data_list.add_argument("--imported", action="store_true")
    p_data_list.add_argument(
        "--type", dest="project_type",
        choices=["data", "code", "project", "enclave"],
    )
    p_data_list.set_defaults(_handler=_handle_data_list, _parser=p_data_list)

    p_enclave = subs.add_parser("enclave", help="Enclave commands")
    p_enclave_sub = p_enclave.add_subparsers(dest="enclave_command")
    p_ebump = p_enclave_sub.add_parser("bump", help="Bump approved_products[].pin")
    p_ebump.add_argument("name")
    p_ebump.add_argument(
        "--manifest", type=Path, default=Path("enclave_manifest.yaml")
    )
    p_ebump.add_argument("--force", action="store_true")
    p_ebump.set_defaults(_handler=_handle_enclave_bump)

    p_elist = p_enclave_sub.add_parser("list", help="List manifest entries")
    p_elist.add_argument("repo", nargs="?")
    p_elist.add_argument(
        "--manifest", type=Path, default=Path("enclave_manifest.yaml")
    )
    p_elist.set_defaults(_handler=_handle_enclave_list)

    p_registry = subs.add_parser("registry", help="Catalog commands")
    p_registry_sub = p_registry.add_subparsers(dest="registry_command")

    p_reg_reg = p_registry_sub.add_parser("register", help="Register a project")
    p_reg_reg.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_reg_reg.set_defaults(_handler=_handle_registry_register)

    p_reg_upd = p_registry_sub.add_parser("update", help="Update a registered project")
    p_reg_upd.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_reg_upd.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_reg_upd.set_defaults(_handler=_handle_registry_update)

    p_reg_status = p_registry_sub.add_parser(
        "status", help="Show registration status (or list pending)"
    )
    p_reg_status.add_argument("name", nargs="?")
    p_reg_status.set_defaults(_handler=_handle_registry_status)

    p_reg_sync = p_registry_sub.add_parser("sync", help="Refresh the registry cache")
    p_reg_sync.set_defaults(_handler=_handle_registry_sync)

    return parser


# ---------------------------------------------------------------------------
# Client construction (single monkeypatch seam for tests)
# ---------------------------------------------------------------------------


def _resolve_catalog_client(config: Config) -> CatalogClient:
    """Build a ``GitCatalogClient`` from config. Tests monkeypatch this
    function to inject fakes."""
    if not config.registry_url:
        raise ConfigError(
            "registry_url required for this command; set it in "
            "~/.config/mintd/config.yaml or $MINTD_CONFIG_DIR/config.yaml"
        )
    return GitCatalogClient(
        registry_repo_url=config.registry_url,
        work_dir=config.resolved_cache_dir() / "registry",
    )


def _resolve_clients(config: Config) -> tuple[CatalogClient, DvcOps]:
    """Build production ``GitCatalogClient`` + ``SubprocessDvcOps`` from
    config. Tests monkeypatch this function to inject fakes.
    """
    client = _resolve_catalog_client(config)
    dvc_ops: DvcOps = SubprocessDvcOps(timeout=config.dvc_timeout)
    return client, dvc_ops


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_check(args: argparse.Namespace) -> int:
    config = Config.load()
    client: CatalogClient | None = None
    if args.upgrades:
        try:
            client, _ = _resolve_clients(config)
        except ConfigError:
            # Let the manifest walker emit `catalog_unresolved` findings —
            # surface what's missing without pre-validating.
            client = None
    findings = check_project(args.path, upgrades=args.upgrades, client=client)
    return _render_findings(findings, json_out=args.json_out)


def _handle_data_import(args: argparse.Namespace) -> int:
    if args.bump and (args.import_path or args.rev or args.all_outputs):
        args._parser.error("--bump cannot be combined with --path, --rev, or --all")

    config = Config.load()
    client, dvc_ops = _resolve_clients(config)

    if args.bump:
        try:
            result = bump_import(
                client,
                dvc_ops,
                project_path=Path("."),
                name=args.name,
                force=args.force,
            )
        except BumpBlocked as exc:
            return _render_bump_blocked(exc)
        except (ImportNotFound, PrimaryRemovedAtHead) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if result is None:
            print("up to date")
        else:
            print(result)
        return 0

    try:
        produced = import_product(
            client,
            dvc_ops,
            args.name,
            dest_root=args.dest_root,
            path=args.import_path,
            rev=args.rev,
            all_outputs=args.all_outputs,
            force=args.force,
        )
    except (
        CatalogNotFound,
        MissingPrimaryDataProduct,
        ImportDestinationExists,
        ImportNotFound,
        PrimaryRemovedAtHead,
        ProducerError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p in produced:
        print(p)
    return 0


def _handle_data_list(args: argparse.Namespace) -> int:
    if args.imported and args.project_type is not None:
        args._parser.error("--imported cannot be combined with --type")

    if args.imported:
        deps = scan_imports(Path("."))
        if not deps:
            print("no imports")
            return 0
        for dep in deps:
            print(f"{dep.local_path} ← {dep.producer_repo}@{dep.contract_pin[:7]} ({dep.output_path})")
        return 0

    config = Config.load()
    client = _resolve_catalog_client(config)
    filter_ = CatalogFilter(project_type=args.project_type) if args.project_type else None
    entries = client.list(filter_)
    if not entries:
        print("no entries")
        return 0
    for entry in entries:
        dumped = entry.model_dump()
        project = dumped.get("project") or {}
        meta = dumped.get("metadata") or {}
        name = project.get("name", "<unnamed>")
        ptype = project.get("type", "?")
        desc = meta.get("description") or ""
        print(f"{name} ({ptype}): {desc}")
    return 0


def _handle_enclave_list(args: argparse.Namespace) -> int:
    try:
        manifest = EnclaveManifest.load(args.manifest)
    except FileNotFoundError:
        print(f"error: enclave_manifest.yaml not found at {args.manifest}", file=sys.stderr)
        return 1
    repo_filter: str | None = args.repo

    approved = [ap for ap in manifest.approved_products if repo_filter is None or ap.repo == repo_filter]
    downloaded = [d for d in manifest.downloaded if repo_filter is None or d.repo == repo_filter]
    transferred = [t for t in manifest.transferred if repo_filter is None or t.repo == repo_filter]

    if repo_filter is not None and not approved and not downloaded and not transferred:
        print(f"no entries for {repo_filter}")
        return 0

    print("approved_products:")
    if not approved:
        print("  (none)")
    for ap in approved:
        path = ap.source_path or "<primary>"
        print(f"  {ap.repo}@{ap.pin[:7]} (path: {path})")

    print("downloaded:")
    if not downloaded:
        print("  (none)")
    for d in downloaded:
        print(f"  {d.repo} @ {d.contract_pin[:7]} → {d.local_path} ({d.fetch_strategy})")

    print("transferred:")
    if not transferred:
        print("  (none)")
    for t in transferred:
        print(f"  {t.repo} @ {t.contract_pin[:7]} ({t.transfer_date}) → {t.local_path}")

    return 0


def _handle_enclave_bump(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        result = enclave_bump(
            client,
            manifest_path=args.manifest,
            name=args.name,
            force=args.force,
        )
    except BumpBlocked as exc:
        return _render_bump_blocked(exc)
    except (ImportNotFound, PrimaryRemovedAtHead, AppendOnlyViolation) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print("up to date")
    else:
        print(f"bumped: {result}")
    return 0


def _handle_registry_register(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        metadata = Metadata.from_json_file(args.path / "metadata.json")
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = client.register(metadata)
    except CatalogAlreadyExists as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"registered: {result.name} (dry_run={result.dry_run})")
    return 0


def _handle_registry_update(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        metadata = Metadata.from_json_file(args.path / "metadata.json")
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = client.update(metadata, dry_run=args.dry_run)
    except CatalogNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not result.changes:
        print(f"no changes (dry_run={result.dry_run})")
        return 0
    for change in result.changes:
        print(f"{change.field_path}: {change.before!r} → {change.after!r}")
    return 0


def _handle_registry_status(args: argparse.Namespace) -> int:
    config = Config.load()
    if args.name:
        # Per-name status needs the catalog client (looks at cache + PRs).
        client = _resolve_catalog_client(config)
        status = client.status(args.name)
        line = f"{args.name}: {status.state}"
        if status.pr_number is not None:
            line += f" (PR #{status.pr_number})"
        print(line)
        return 0
    # No name → just list the local pending file. No registry_url needed.
    pending_path = config.resolved_cache_dir() / "registry" / ".mintd_pending.json"
    pending = PendingRegistrations(path=pending_path)
    entries = pending.all_entries()
    if not entries:
        print("no pending registrations")
        return 0
    for entry in entries:
        print(f"{entry.name} ({entry.kind}): PR #{entry.pr_number}")
    return 0


def _handle_registry_sync(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    count = client.sync()
    print(f"synced ({count} entries)")
    return 0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _resolve_prefix(kind: str | None) -> str:
    return _KIND_PREFIX.get(kind, "·")


def _render_findings(findings: list[CheckFinding], *, json_out: bool) -> int:
    if json_out:
        for f in findings:
            print(
                json.dumps(
                    {
                        "severity": f.severity,
                        "section": f.section,
                        "message": f.message,
                        "field_path": f.field_path,
                        "source": str(f.source) if f.source else None,
                        "kind": f.kind,
                    }
                )
            )
    else:
        for f in findings:
            prefix = _resolve_prefix(f.kind)
            loc = f.source if f.source else "<project>"
            print(f"{prefix} [{f.severity}] {loc}: {f.message}")
    return 1 if any(f.severity == "error" for f in findings) else 0


def _render_bump_blocked(exc: BumpBlocked) -> int:
    """Dispatches on ``finding.kind`` only — never inspects ``finding.message``.

    Slice-9 contract: ``kind`` is the structural discriminator; ``message``
    is human-readable rendering. Hint dispatch + exit-code dispatch both
    read ``kind``. If a future error class wants different UX, add a
    ``kind`` value — don't parse messages.
    """
    print(f"error: {exc}", file=sys.stderr)
    kind = exc.finding.kind
    if kind == "unreachable":
        print("hint: retry when the network is available", file=sys.stderr)
    elif kind == "schema_too_old":
        print(
            "hint: ask the producer to update their metadata schema to 2.0",
            file=sys.stderr,
        )
    elif kind == "catalog_unresolved":
        print(
            "hint: check that registry_url is set in ~/.config/mintd/config.yaml",
            file=sys.stderr,
        )
    if kind in _RECOVERABLE_KINDS:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
