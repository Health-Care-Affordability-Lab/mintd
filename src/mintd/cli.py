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
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from ._config import Config, ConfigError
from ._dvc_ops import DvcNotInstalled, DvcOpError, DvcOps, SubprocessDvcOps
from ._fast_sync_ops import FastSyncOps
from ._registry_git_ops import RegistryGitOps, SubprocessRegistryGitOps
from ._init_ops import InitOpError
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
    PrimaryRemovedAtHead,
    bump_import,
    import_product,
)
from .data_ops import data_add, data_pull, data_push, data_remove, data_verify
from ._archive_ops import ArchiveAlreadyExists, UnsafeArchiveMember
from .enclave import (
    AlreadyApproved,
    AppendOnlyViolation,
    EnclaveManifest,
    InvalidTransferManifest,
    NothingToPackage,
    PathTraversalDetected,
    enclave_add,
    enclave_bump,
    enclave_package,
    enclave_pull,
    enclave_remove,
    enclave_verify,
)
from .imports import scan_imports
from .init import InitDestinationExists, init_project
from .model import Metadata
from .pending_registrations import PendingRegistrations
from .producer import MissingPrimaryDataProduct, ProducerError
from .publish import (
    PublishBlocked,
    PublishError,
    WorkingTreeDirty,
    publish_project,
)

logger = logging.getLogger(__name__)



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

    p_init = subs.add_parser("init", help="Create a new mintd project")
    p_init.add_argument(
        "project_type",
        metavar="type",
        choices=["data", "code", "project", "enclave"],
    )
    p_init.add_argument("name")
    p_init.add_argument("--path", type=Path, default=Path("."))
    p_init.set_defaults(_handler=_handle_init)

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

    p_pull = p_data_sub.add_parser("pull", help="Pull DVC data")
    p_pull.add_argument("targets", nargs="*")
    p_pull.add_argument("--remote")
    p_pull.add_argument("--jobs", type=int)
    p_pull.add_argument("--path", type=Path, default=Path("."))
    p_pull.set_defaults(_handler=_handle_data_pull)

    p_push = p_data_sub.add_parser("push", help="Push DVC data")
    p_push.add_argument("targets", nargs="*")
    p_push.add_argument("--remote")
    p_push.add_argument("--jobs", type=int)
    p_push.set_defaults(_handler=_handle_data_push)

    p_add = p_data_sub.add_parser("add", help="Add DVC data")
    p_add.add_argument("path", type=Path)
    p_add.set_defaults(_handler=_handle_data_add)

    p_verify = p_data_sub.add_parser("verify", help="Verify DVC data")
    p_verify.add_argument("targets", nargs="*")
    p_verify.add_argument("--path", type=Path, default=Path("."))
    p_verify.set_defaults(_handler=_handle_data_verify)

    p_remove = p_data_sub.add_parser("remove", help="Remove DVC data")
    p_remove.add_argument("name")
    p_remove.set_defaults(_handler=_handle_data_remove)

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

    p_eadd = p_enclave_sub.add_parser("add", help="Subscribe to a producer")
    p_eadd.add_argument("repo")
    p_eadd.add_argument("--pin")
    _eadd_mutex = p_eadd.add_mutually_exclusive_group()
    _eadd_mutex.add_argument("--source-path", dest="source_path")
    _eadd_mutex.add_argument("--all", action="store_true", dest="all_outputs")
    p_eadd.add_argument(
        "--manifest", type=Path, default=Path("enclave_manifest.yaml")
    )
    p_eadd.set_defaults(_handler=_handle_enclave_add)

    p_erm = p_enclave_sub.add_parser("remove", help="Unsubscribe from a producer")
    p_erm.add_argument("repo")
    _erm_mutex = p_erm.add_mutually_exclusive_group()
    _erm_mutex.add_argument("--source-path", dest="source_path")
    _erm_mutex.add_argument("--all", action="store_true", dest="all_outputs")
    p_erm.add_argument("--manifest", type=Path, default=Path("enclave_manifest.yaml"))
    p_erm.set_defaults(_handler=_handle_enclave_remove)

    p_epull = p_enclave_sub.add_parser("pull", help="Fetch subscribed data")
    p_epull.add_argument("repo", nargs="?")
    p_epull.add_argument("--force", action="store_true")
    p_epull.add_argument("--manifest", type=Path, default=Path("enclave_manifest.yaml"))
    p_epull.set_defaults(_handler=_handle_enclave_pull)

    p_epkg = p_enclave_sub.add_parser(
        "package", help="Bundle downloads into a transfer archive"
    )
    p_epkg.add_argument("repo", nargs="?")
    p_epkg.add_argument("--output", type=Path, dest="output_archive")
    p_epkg.add_argument(
        "--manifest", type=Path, default=Path("enclave_manifest.yaml")
    )
    p_epkg.set_defaults(_handler=_handle_enclave_package)

    p_ever = p_enclave_sub.add_parser(
        "verify", help="Reconcile an extracted transfer into the manifest"
    )
    p_ever.add_argument("extracted_dir", type=Path)
    p_ever.add_argument(
        "--manifest", type=Path, default=Path("enclave_manifest.yaml")
    )
    p_ever.add_argument("--data-root", type=Path, dest="data_root")
    p_ever.set_defaults(_handler=_handle_enclave_verify)

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

    p_publish = subs.add_parser("publish", help="Publish a new version of this project")
    p_publish.add_argument("version", nargs="?")
    p_publish.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_publish.add_argument("--message", "-m")
    p_publish.add_argument("--path", type=Path, default=Path("."))
    p_publish.set_defaults(_handler=_handle_publish)

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


def _resolve_fast_sync_ops(config: Config) -> FastSyncOps | None:
    """Build production ``SubprocessFastSyncOps`` from config.
    Tests monkeypatch this function to inject fakes.

    Probes for ``boto3`` explicitly because ``_fast_sync_ops`` uses
    module-level optional-import sentinels and imports cleanly even when
    boto3 is unavailable. Without this probe we'd hand back a fast-sync
    instance whose first network call would crash.
    """
    try:
        import boto3  # noqa: F401 — availability probe only
    except ImportError as exc:
        logger.warning("fast-sync unavailable (boto3 not importable): %s", exc)
        return None
    from ._fast_sync_ops import SubprocessFastSyncOps
    return SubprocessFastSyncOps(aws_profile_name=config.aws_profile_name)


def _resolve_git_ops(config: Config) -> RegistryGitOps:
    """Build production ``SubprocessRegistryGitOps`` from config.
    Tests monkeypatch this function to inject fakes.
    """
    return SubprocessRegistryGitOps(timeout=config.git_timeout)


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


def _handle_init(args: argparse.Namespace) -> int:
    try:
        init_project(
            project_type=args.project_type,
            name=args.name,
            target_dir=args.path,
        )
    except (InitDestinationExists, InitOpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("created: metadata.json")
    print("created: .gitignore")
    print("initialized: git")
    if args.project_type in {"data", "code", "project"}:
        print("initialized: dvc")
    return 0


def _handle_data_pull(args: argparse.Namespace) -> int:
    config = Config.load()
    _, dvc_ops = _resolve_clients(config)
    fast_sync_ops = _resolve_fast_sync_ops(config)
    try:
        data_pull(
            project_path=args.path,
            targets=args.targets or None,
            dvc_ops=dvc_ops,
            fast_sync_ops=fast_sync_ops,
            remote=args.remote,
            jobs=args.jobs,
        )
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    targets = ", ".join(args.targets) if args.targets else ""
    print(f"pulled: {targets}" if targets else "pulled")
    return 0


def _handle_data_push(args: argparse.Namespace) -> int:
    config = Config.load()
    _, dvc_ops = _resolve_clients(config)
    try:
        data_push(
            project_path=Path("."),
            targets=args.targets or None,
            dvc_ops=dvc_ops,
            remote=args.remote,
            jobs=args.jobs,
        )
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("pushed")
    return 0


def _handle_data_add(args: argparse.Namespace) -> int:
    config = Config.load()
    _, dvc_ops = _resolve_clients(config)
    try:
        produced = data_add(args.path, dvc_ops=dvc_ops)
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(str(produced))
    return 0


def _handle_data_verify(args: argparse.Namespace) -> int:
    config = Config.load()
    _, dvc_ops = _resolve_clients(config)
    try:
        status_map = data_verify(
            project_path=args.path,
            targets=args.targets or None,
            dvc_ops=dvc_ops,
        )
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not status_map:
        print("clean")
        return 0
    dirty = False
    for path, status in sorted(status_map.items()):
        print(f"{path}: {status}")
        if status != "clean":
            dirty = True
    return 1 if dirty else 0


def _handle_data_remove(args: argparse.Namespace) -> int:
    config = Config.load()
    _, dvc_ops = _resolve_clients(config)
    try:
        data_remove(args.name, dvc_ops=dvc_ops)
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"removed: {args.name}")
    return 0


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
        print(f"{entry.name} ({entry.project_type}): {entry.description}")
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


def _handle_enclave_add(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        path = enclave_add(
            client,
            manifest_path=args.manifest,
            name=args.repo,
            pin=args.pin,
            source_path=args.source_path,
            all_=args.all_outputs,
        )
    except (
        CatalogNotFound,
        AlreadyApproved,
        ProducerError,
        MissingPrimaryDataProduct,
        AppendOnlyViolation,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Re-load to print the just-added entry's resolved pin.
    manifest = EnclaveManifest.load(path)
    ap = manifest.approved_products[-1]
    src = ap.source_path or ("<all>" if ap.all else "<primary>")
    print(f"subscribed: {ap.repo}@{ap.pin[:7]} (path: {src})")
    return 0


def _handle_enclave_remove(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        enclave_remove(
            client,
            manifest_path=args.manifest,
            name=args.repo,
            source_path=args.source_path,
            all_=args.all_outputs,
        )
    except (ImportNotFound, AppendOnlyViolation) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    msg = f"removed: {args.repo}"
    if args.source_path:
        msg += f" (source_path={args.source_path})"
    print(msg)
    return 0


def _handle_enclave_pull(args: argparse.Namespace) -> int:
    config = Config.load()
    client, dvc_ops = _resolve_clients(config)
    try:
        _, written = enclave_pull(
            client,
            dvc_ops,
            manifest_path=args.manifest,
            repo=args.repo,
            force=args.force,
        )
    except (
        CatalogNotFound,
        ImportNotFound,
        MissingPrimaryDataProduct,
        ProducerError,
        AppendOnlyViolation,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not written:
        print("nothing to pull")
        return 0
    for item in written:
        print(f"pulled: {item.repo}@{item.contract_pin[:7]} → {item.local_path}")
    return 0


def _handle_enclave_package(args: argparse.Namespace) -> int:
    # When --output is unset, hand `enclave_package` an output_dir; it
    # builds the filename from the computed `transfer_id` so same-day
    # re-runs produce distinct archives.
    output_dir = (
        args.manifest.parent / "transfers"
        if args.output_archive is None
        else None
    )
    try:
        archive = enclave_package(
            manifest_path=args.manifest,
            name=args.repo,
            output_archive=args.output_archive,
            output_dir=output_dir,
        )
    except (
        NothingToPackage,
        ArchiveAlreadyExists,
        UnsafeArchiveMember,
        InvalidTransferManifest,
        AppendOnlyViolation,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"packaged: {archive}")
    return 0


def _handle_enclave_verify(args: argparse.Namespace) -> int:
    try:
        _, written = enclave_verify(
            extracted_dir=args.extracted_dir,
            manifest_path=args.manifest,
            data_root=args.data_root,
        )
    except (
        InvalidTransferManifest,
        PathTraversalDetected,
        AppendOnlyViolation,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not written:
        print("nothing to verify (all entries already in transferred[])")
        return 0
    for item in written:
        print(
            f"verified: {item.repo} @ {item.contract_pin[:7]} → {item.local_path}"
        )
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


def _handle_publish(args: argparse.Namespace) -> int:
    config = Config.load()
    client, dvc_ops = _resolve_clients(config)
    git_ops = _resolve_git_ops(config)
    try:
        result = publish_project(
            project_path=args.path,
            version=args.version,
            dry_run=args.dry_run,
            client=client,
            dvc_ops=dvc_ops,
            git_ops=git_ops,
            message=args.message,
        )
    except PublishBlocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        for f in exc.findings[:5]:
            print(f"  [{f.severity}] {f.source or '<project>'}: {f.message}", file=sys.stderr)
        if len(exc.findings) > 5:
            print(f"  ... and {len(exc.findings) - 5} more", file=sys.stderr)
        return 1
    except WorkingTreeDirty as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.recovery_hint:
            print(f"note: {exc.recovery_hint}", file=sys.stderr)
        return 1
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.recovery_hint:
            print(f"note: {exc.recovery_hint}", file=sys.stderr)
        return 1
    # Success rendering.
    print(f"version: {result.version}" + (" (dry-run)" if result.dry_run else ""))
    for change in result.diff:
        print(f"  {change.field_path}: {change.before!r} → {change.after!r}")
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
