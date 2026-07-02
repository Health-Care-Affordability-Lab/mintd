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
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn, Optional

from pydantic import ValidationError

from . import __version__, config_ops, metadata_migrate
from ._console import Reporter
from ._config import Config, ConfigError
from ._dvc_ops import (
    DvcImportPathNotFound,
    DvcNotInRepoError,
    DvcNotInstalled,
    DvcOpError,
    DvcOps,
    DvcPushError,
    SubprocessDvcOps,
)
from ._subprocess import WallTimeoutExceeded
from ._registry_git_ops import GitOpError
from ._fast_sync_ops import FastSyncOps
from ._registry_git_ops import RegistryGitOps, SubprocessRegistryGitOps  # noqa: F401
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
    clone_and_pull_product,
    import_product,
)
from .data_ops import data_add, data_pull, data_push, data_remove, data_verify
from ._s3_listing_ops import (
    BucketAccessError,
    BucketNotFound,
    S3ListingResult,
    list_product_objects,
)
from ._archive_ops import ArchiveAlreadyExists, UnsafeArchiveMember
from .enclave import (
    AlreadyApproved,
    AppendOnlyViolation,
    EnclaveManifest,
    EnclavePullError,
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
from .init import InitDestinationExists, InitNameInvalid, init_project
from .model import Metadata
from .pending_registrations import PendingRegistrations
from .producer import MissingPrimaryDataProduct, ProducerError
from .publish import (
    PublishBlocked,
    PublishError,
    PublishNonInteractive,
    PublishPreview,
    WorkingTreeDirty,
)

logger = logging.getLogger(__name__)


class MetadataSchemaTooOld(Exception):
    def __init__(self, path: Path, found: str) -> None:
        super().__init__(f"metadata.json at {path} is schema {found!r}, expected '2.0'")
        self.path = path
        self.found = found


def _load_metadata_with_schema_hint(path: Path) -> Metadata:
    """Read metadata.json. Raises ``MetadataSchemaTooOld`` when the file is
    on the v1 schema (so callers can hint at `mintd update metadata`); the
    underlying ``pydantic.ValidationError`` otherwise. ``FileNotFoundError``
    propagates verbatim."""
    raw = path.read_text(encoding="utf-8")
    try:
        peek = json.loads(raw)
    except json.JSONDecodeError:
        peek = {}
    sv = peek.get("schema_version") if isinstance(peek, dict) else None
    if sv is not None and sv != "2.0":
        raise MetadataSchemaTooOld(path=path, found=str(sv))
    return Metadata.model_validate_json(raw)


def _add_global_output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="Increase verbosity (-v: info, -vv: debug, -vvv: trace)")
    parser.add_argument("-q", "--quiet", action="count", default=0,
                        help="Reduce verbosity (-q: errors only)")
    parser.add_argument("--json", dest="json_out", action="store_true",
                        help="Emit structured JSON to stdout (read-side commands)")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable color output (also respects NO_COLOR env)")


def _build_reporter(args: argparse.Namespace) -> Reporter:
    return Reporter(
        verbose=args.verbose,
        quiet=args.quiet,
        json_mode=args.json_out,
        no_color=args.no_color or bool(os.environ.get("NO_COLOR")),
    )



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
    args._reporter = _build_reporter(args)
    args._reporter.install_log_bridge()
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except KeyboardInterrupt:
        args._reporter.error("interrupted by user")
        return 130
    except WallTimeoutExceeded as e:
        # Catches the fast-tier default (30s on dvc status / git fetch /
        # etc.) and any user-configured timeouts.transfer that fires from
        # handlers that don't catch the exception themselves (data pull /
        # push / add / verify / remove / import / publish). data clone
        # catches it locally for a richer message.
        args._reporter.error(str(e))
        return 1
    except ConfigError as e:
        args._reporter.error(str(e))
        return 1
    finally:
        args._reporter.uninstall_log_bridge()


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = _MintdArgumentParser(
        prog="mintd",
        description="mintd: Lightweight data product framework for research labs",
    )
    _add_global_output_flags(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command")

    p_init = subs.add_parser("init", help="Create a new mintd project")
    p_init.add_argument(
        "project_type",
        metavar="type",
        choices=["data", "code", "project", "enclave"],
    )
    p_init.add_argument("name")
    p_init.add_argument(
        "--path",
        type=Path,
        default=Path("."),
        help="Parent directory in which to create the project (default: cwd).",
    )
    p_init.add_argument(
        "--use-current-repo",
        action="store_true",
        help="Scaffold into --path directly instead of into a new ``{type}_{name}`` subdir.",
    )
    p_init.add_argument(
        "--lang",
        choices=["python", "r", "stata"],
        default="python",
        help="Primary programming language for scaffold (ignored for enclave type).",
    )
    p_init.set_defaults(_handler=_handle_init)

    p_check = subs.add_parser("check", help="Validate a mintd project")
    p_check.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_check.add_argument("--upgrades", action="store_true")
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
    p_import.add_argument(
        "--dvc-arg", action="append", default=[], dest="dvc_args", metavar="ARG",
        help="Append an arg to the underlying `dvc` invocation. "
             "Use `--dvc-arg=VALUE` form for hyphen-prefixed values "
             "(repeatable; ignored on fast-sync code paths).",
    )
    p_import.set_defaults(_handler=_handle_data_import, _parser=p_import)

    p_pull = p_data_sub.add_parser("pull", help="Pull DVC data")
    p_pull.add_argument("targets", nargs="*")
    p_pull.add_argument("--remote")
    p_pull.add_argument("--jobs", type=int)
    p_pull.add_argument("--path", type=Path, default=Path("."))
    p_pull.add_argument(
        "--dvc-arg", action="append", default=[], dest="dvc_args", metavar="ARG",
        help="Append an arg to the underlying `dvc` invocation. "
             "Use `--dvc-arg=VALUE` form for hyphen-prefixed values "
             "(repeatable; ignored on fast-sync code paths).",
    )
    p_pull.set_defaults(_handler=_handle_data_pull)

    p_clone = p_data_sub.add_parser(
        "clone",
        help="Clone a published data product (registry lookup + git clone + dvc pull)",
    )
    p_clone.add_argument("name", help="Registered data product name")
    p_clone.add_argument(
        "--dest", type=Path,
        help="Destination path (default: ./<type>_<name>)",
    )
    p_clone.add_argument("--rev", help="Branch or tag to clone at (default: HEAD)")
    p_clone.add_argument(
        "--primary", dest="primary_only", action="store_true",
        help="Pull only the primary data product (default: pull every tracked output)",
    )
    p_clone.add_argument("--jobs", type=int, help="DVC parallelism")
    p_clone.add_argument("--timeout", type=float, default=None,
                         help="Wall-clock cap in seconds for the clone+pull (default: unbounded)")
    p_clone.add_argument(
        "--dvc-arg", action="append", default=[], dest="dvc_args", metavar="ARG",
        help="Append an arg to the underlying `dvc` invocation. "
             "Use `--dvc-arg=VALUE` form for hyphen-prefixed values "
             "(repeatable; ignored on fast-sync code paths).",
    )
    p_clone.set_defaults(_handler=_handle_data_clone)

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

    p_schema = p_data_sub.add_parser("schema", help="Schema operations")
    p_schema_sub = p_schema.add_subparsers(dest="schema_command")
    p_schema_gen = p_schema_sub.add_parser(
        "generate",
        help="Generate Frictionless table schemas for data files (requires [schema] extra)",
    )
    p_schema_gen.add_argument("--project-dir", type=Path, default=None)
    p_schema_gen.add_argument("--data-dir", type=Path, default=None)
    p_schema_gen.add_argument("--output", type=Path, default=None)
    p_schema_gen.add_argument(
        "--recursive", action=argparse.BooleanOptionalAction, default=True,
    )
    p_schema_gen.set_defaults(_handler=_handle_data_schema_generate)

    p_data_list = p_data_sub.add_parser("list", help="List catalog entries or local imports")
    p_data_list.add_argument("--imported", action="store_true")
    p_data_list.add_argument("--detailed", action="store_true", help="Show full descriptions (no truncation).")
    p_data_list.add_argument("--width", type=int, default=80, help="Description column width (default: 80).")
    p_data_list.add_argument(
        "--type", dest="project_type",
        choices=["data", "code", "project", "enclave"],
    )
    p_data_list.set_defaults(_handler=_handle_data_list, _parser=p_data_list)

    p_data_ls = p_data_sub.add_parser(
        "ls",
        help="List S3 objects inside a registered product's bucket",
    )
    p_data_ls.add_argument("name", help="Registered data product name")
    p_data_ls.add_argument("sub_path", nargs="?", default=None,
                           help="Subdirectory inside the product prefix (e.g. data/final/)")
    p_data_ls.add_argument("--shallow", dest="recursive", action="store_false",
                           help="List one level only (default: recursive tree, truncated when long)")
    p_data_ls.add_argument("--no-truncate", dest="no_truncate", action="store_true",
                           help="Render every row (default: truncate output past 50 files; --json never truncates)")
    p_data_ls.add_argument("--versions", dest="versions", action="store_true",
                           help="Show per-key version count (version_aware buckets only)")
    p_data_ls.set_defaults(_handler=_handle_data_ls, _parser=p_data_ls, recursive=True)

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
    p_publish.add_argument("--yes", "-y", action="store_true", dest="assume_yes", help="Skip the interactive preview confirmation. Required when stdin is not a TTY.")
    p_publish.add_argument("--message", "-m")
    p_publish.add_argument("--path", type=Path, default=Path("."))
    p_publish.set_defaults(_handler=_handle_publish)

    p_config = subs.add_parser("config", help="Inspect, edit, and validate mintd config")
    p_config_sub = p_config.add_subparsers(dest="config_command")

    p_config_show = p_config_sub.add_parser("show", help="Pretty-print the current config")
    p_config_show.add_argument("--path", type=Path, default=None)
    p_config_show.set_defaults(_handler=_handle_config_show)

    p_config_setup = p_config_sub.add_parser(
        "setup",
        help="Update config fields (interactive when no flags are given)",
    )
    p_config_setup.add_argument("--path", type=Path, default=None)
    setup_group = p_config_setup.add_mutually_exclusive_group(required=False)
    setup_group.add_argument(
        "--set", action="append", dest="set_pairs", metavar="KEY=VALUE",
        help="Apply KEY=VALUE update; may be passed multiple times.",
    )
    setup_group.add_argument(
        "--from", dest="from_file", type=str, metavar="FILE",
        help="Read full config from FILE; '-' reads stdin.",
    )
    setup_group.add_argument(
        "--migrate-v1", dest="migrate_v1", type=str, metavar="FILE",
        help="Read a legacy v1 mintd config FILE and translate to v2.",
    )
    p_config_setup.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_config_setup.set_defaults(_handler=_handle_config_setup)

    p_config_validate = p_config_sub.add_parser(
        "validate", help="Schema + AWS profile + S3 connectivity check"
    )
    p_config_validate.add_argument("--path", type=Path, default=None)
    p_config_validate.add_argument(
        "--bucket", default=None,
        help="S3 bucket to test head_bucket against (auto-discovery is slice-22+).",
    )
    p_config_validate.set_defaults(_handler=_handle_config_validate)

    p_update = subs.add_parser("update", help="Migrate v1 metadata/storage to v2")
    p_update_sub = p_update.add_subparsers(dest="update_command")
    p_update_meta = p_update_sub.add_parser(
        "metadata",
        help="Migrate a v1 metadata.json (schema 1.x) to v2 (schema 2.0)",
    )
    p_update_meta.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_update_meta.add_argument("--dry-run", action="store_true", dest="dry_run")
    p_update_meta.set_defaults(_handler=_handle_update_metadata, _parser=p_update_meta)

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


def _resolve_clients(config: Config, reporter: Optional[Reporter] = None) -> tuple[CatalogClient, DvcOps]:
    """Build production ``GitCatalogClient`` + ``SubprocessDvcOps`` from
    config. Tests monkeypatch this function to inject fakes.
    """
    client = _resolve_catalog_client(config)
    dvc_ops: DvcOps = SubprocessDvcOps(
        timeouts=config.timeouts,
        reporter=reporter,
        aws_profile_name=config.aws_profile_name,
    )
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


def _resolve_s3_listing_ops(config: Config):
    """Return the listing callable; tests monkeypatch this seam.

    Probes boto3 like _resolve_fast_sync_ops. Returns None when boto3
    isn't importable so the handler can emit a clean "boto3 unavailable"
    error instead of crashing on the first network call.
    """
    try:
        import boto3  # noqa: F401
    except ImportError as exc:
        logger.warning("data ls unavailable (boto3 not importable): %s", exc)
        return None
    return list_product_objects


def _resolve_git_ops(config: Config, reporter: Optional[Reporter] = None) -> RegistryGitOps:
    """Build production ``SubprocessRegistryGitOps`` from config.
    Tests monkeypatch this function to inject fakes.
    """
    return SubprocessRegistryGitOps(timeouts=config.timeouts, reporter=reporter)


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
    from ._config import Config
    from ._console import Reporter
    from ._init_ops import InitNonInteractive
    from .init import (
        _prompt_classification,
    )

    reporter = getattr(args, "_reporter", None) or Reporter()
    # Slice 30 P1 (reviewer-flagged): enclave projects don't use DVC storage
    # wiring and must not require a TTY. Skip the classification prompt.
    classification: str | None = None
    slug: str | None = None
    if args.project_type != "enclave":
        try:
            classification, slug = _prompt_classification(reporter=reporter)
        except InitNonInteractive as exc:
            reporter.error(str(exc))
            return 1

    try:
        config = Config.load()
    except Exception:
        config = Config()  # type: ignore[call-arg]
    bucket = config.storage_bucket_prefix
    endpoint = config.storage_endpoint
    # Slice 30: write the AWS profile into .dvc/config so consumers
    # running raw `dvc pull` (outside mintd) pick up the right
    # credentials. Config.aws_profile_name returns "mintd" iff
    # ~/.aws/credentials has a [mintd] section; otherwise None.
    profile = config.aws_profile_name

    try:
        project_path, written = init_project(
            project_type=args.project_type,
            name=args.name,
            target_dir=args.path,
            language=args.lang,
            use_current_repo=args.use_current_repo,
            classification=classification,
            slug=slug,
            bucket=bucket,
            endpoint=endpoint,
            profile=profile,
        )
    except (InitDestinationExists, InitNameInvalid, InitOpError) as exc:
        reporter.error(str(exc))
        return 1
    # Render paths relative to cwd when possible so the user sees the subdir.
    cwd = Path.cwd().resolve()
    try:
        rel = project_path.resolve().relative_to(cwd)
    except ValueError:
        rel = project_path
    for p in written:
        try:
            line = p.resolve().relative_to(cwd)
        except ValueError:
            line = p
        print(f"created: {line}")
    print("initialized: git")
    if args.project_type in {"data", "code", "project"}:
        print("initialized: dvc")
    if str(rel) != ".":
        print(f"Next: cd {rel}")
    return 0


def _handle_data_pull(args: argparse.Namespace) -> int:
    # Friendly redirect when run outside a DVC project. Without this probe,
    # users hit `mintd data pull <name>` from anywhere and get the raw
    # `dvc pull failed (exit 253): ERROR: you are not inside of a DVC
    # repository` — they're confusing `pull` (refresh own data) with
    # `import` (declare and fetch from registry).
    project_path = args.path.resolve()
    if not (project_path / ".dvc").is_dir():
        name_hint = args.targets[0] if args.targets else "<name>"
        print(
            f"error: not inside a DVC project (no .dvc/ at {project_path}).\n"
            f"  mintd data pull operates on the current project's tracked data.\n"
            f"  To grab a published product: mintd data clone {name_hint}",
            file=sys.stderr,
        )
        return 1
    reporter = args._reporter
    config = Config.load()
    _, dvc_ops = _resolve_clients(config, reporter)
    fast_sync_ops = _resolve_fast_sync_ops(config)
    try:
        summary = data_pull(
            project_path=project_path,
            targets=args.targets or None,
            dvc_ops=dvc_ops,
            fast_sync_ops=fast_sync_ops,
            remote=args.remote,
            jobs=args.jobs,
            extra_dvc_args=args.dvc_args or None,
            reporter=reporter,
        )
    except DvcNotInstalled as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except DvcOpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if summary.total_bytes:
        msg = (
            f"✓ pulled {summary.file_count} file(s) "
            f"({_human_bytes(summary.total_bytes)}) in {_format_duration(summary.elapsed_s)}"
        )
    else:
        msg = f"✓ pulled {summary.file_count} file(s) in {_format_duration(summary.elapsed_s)}"
    if reporter.json_mode:
        reporter.result({
            "pulled": summary.file_count,
            "bytes": summary.total_bytes,
            "elapsed_s": round(summary.elapsed_s, 2),
        })
    else:
        reporter.success(msg)
    return 0


def _handle_data_push(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _, dvc_ops = _resolve_clients(config, reporter)
    try:
        with reporter.status("Pushing data to DVC..."):
            summary = data_push(
                project_path=Path("."),
                targets=args.targets or None,
                dvc_ops=dvc_ops,
                remote=args.remote,
                jobs=args.jobs,
            )
    except DvcNotInstalled as e:
        reporter.error(str(e), hint="install DVC: pip install 'dvc[s3]' (see notes/INSTALL.md)")
        return 2
    except DvcPushError as e:
        reporter.error(str(e), hint="check AWS credentials: mintd config validate")
        return 1
    except DvcOpError as e:
        reporter.error(str(e))
        return 1
    if reporter.json_mode:
        reporter.result(
            {
                "remote": summary.remote,
                "pushed": summary.pushed,
                "bytes": summary.bytes,
                "elapsed_s": round(summary.elapsed_s, 2),
                "up_to_date": summary.up_to_date,
            },
            pretty=_pretty_data_push,
        )
    else:
        remote_clause = f" → s3://{summary.remote}"
        duration = _format_duration(summary.elapsed_s)
        if summary.up_to_date:
            reporter.success(f"✓ already up to date{remote_clause} in {duration}")
        else:
            count_clause = (
                f" {summary.pushed} object(s)" if summary.pushed is not None else ""
            )
            size_clause = (
                f" ({_human_bytes(summary.bytes)})" if summary.bytes else ""
            )
            reporter.success(
                f"✓ pushed{count_clause}{size_clause}{remote_clause} in {duration}"
            )
    return 0


def _handle_data_add(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _, dvc_ops = _resolve_clients(config, reporter)
    try:
        produced = data_add(args.path, dvc_ops=dvc_ops)
    except DvcNotInstalled as e:
        reporter.error(str(e), hint="install DVC: pip install 'dvc[s3]' (see notes/INSTALL.md)")
        return 2
    except DvcOpError as e:
        reporter.error(str(e), hint="check the path exists inside a DVC project; 'mintd init' scaffolds one")
        return 1
    print(str(produced))
    return 0


def _handle_data_schema_generate(args: argparse.Namespace) -> int:
    from .schema_ops import (
        SchemaExtraNotInstalled,
        find_project_root,
        generate_schema_file,
    )

    reporter = getattr(args, "_reporter", None) or Reporter()
    try:
        project_dir = args.project_dir or find_project_root()
    except FileNotFoundError as e:
        reporter.error(
            str(e),
            hint="cd into a mintd project (one with metadata.json), or pass --project-dir",
        )
        return 1

    data_dir = args.data_dir or (project_dir / "data" / "final")
    output = args.output or (project_dir / "schemas" / "v1" / "schema.json")

    try:
        with reporter.status("Generating schema..."):
            generate_schema_file(data_dir, output, recursive=args.recursive)
    except SchemaExtraNotInstalled:
        reporter.error(
            "schema generation requires the [schema] extra",
            hint=(
                "Reinstall mintd with: install.sh --with-schema "
                "(or: uv tool install --force 'mintd[schema]')"
            ),
        )
        return 1
    except FileNotFoundError as e:
        reporter.error(str(e), hint=f"add supported data files under {data_dir} or pass --data-dir")
        return 1
    except (ValueError, RuntimeError) as e:
        reporter.error(str(e), hint="check file format support (.dta, .csv, .json, .parquet) and integrity")
        return 1

    reporter.success(f"Schema saved to: {output}")
    return 0


def _handle_data_verify(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _, dvc_ops = _resolve_clients(config, reporter)
    try:
        with reporter.status("Verifying DVC data..."):
            status_map = data_verify(
                project_path=args.path,
                targets=args.targets or None,
                dvc_ops=dvc_ops,
            )
    except DvcNotInstalled as e:
        reporter.error(str(e), hint="install DVC: pip install 'dvc[s3]' (see notes/INSTALL.md)")
        return 2
    except DvcOpError as e:
        reporter.error(str(e), hint="retry with --dvc-arg=-v for verbose DVC output")
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
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _, dvc_ops = _resolve_clients(config, reporter)
    try:
        data_remove(args.name, dvc_ops=dvc_ops)
    except DvcNotInstalled as e:
        reporter.error(str(e), hint="install DVC: pip install 'dvc[s3]' (see notes/INSTALL.md)")
        return 2
    except DvcOpError as e:
        reporter.error(str(e), hint="check the name; 'mintd data verify' lists tracked outputs")
        return 1
    print(f"removed: {args.name}")
    return 0


def _handle_data_ls(args: argparse.Namespace) -> int:
    reporter = args._reporter
    config = Config.load()
    client = _resolve_catalog_client(config)
    listing_fn = _resolve_s3_listing_ops(config)
    if listing_fn is None:
        reporter.error("boto3 not installed", hint="pip install 'boto3' (see notes/INSTALL.md)")
        return 2

    try:
        entry = client.fetch(args.name)
    except CatalogNotFound as exc:
        reporter.error(str(exc), hint="run 'mintd data list' to see available products")
        return 1

    dumped = entry.model_dump()
    storage = dumped.get("storage") or {}
    bucket = storage.get("bucket") or ""
    prefix = storage.get("prefix") or ""
    endpoint = storage.get("endpoint") or ""
    versioning = bool(storage.get("versioning"))

    if not bucket or not endpoint:
        reporter.error(
            f"catalog entry {args.name!r} has no usable storage block",
            hint="ask the producer to publish; see 'mintd check' on their repo",
        )
        return 1

    if not versioning:
        msg = "Not supported for md5-keyed remotes; clone the repo and read .dvc files."
        if args.json_out:
            reporter.result({"unsupported": msg, "bucket": bucket, "prefix": prefix})
        else:
            reporter.result(None, pretty=lambda _p: msg)
        return 1

    try:
        with reporter.status(f"Listing {args.name} on S3..."):
            result = listing_fn(
                bucket=bucket, prefix=prefix, endpoint=endpoint,
                sub_path=args.sub_path,
                recursive=args.recursive,
                include_versions=args.versions,
                aws_profile_name=config.aws_profile_name,
            )
    except ValueError as exc:
        reporter.error(str(exc))
        return 2
    except BucketNotFound as exc:
        reporter.error(str(exc), hint="check storage.bucket / storage.endpoint in the producer's metadata.json")
        return 1
    except BucketAccessError as exc:
        reporter.error(str(exc), hint="check AWS credentials (aws configure list / aws_profile_name)")
        return 1

    payload = _data_ls_payload(args.name, result, include_versions=args.versions)
    no_truncate = getattr(args, "no_truncate", False)
    reporter.result(
        payload,
        pretty=lambda p: _pretty_data_ls(
            p, name=args.name, versions=args.versions, no_truncate=no_truncate,
        ),
    )
    return 0

def _data_ls_payload(name: str, result: S3ListingResult, *, include_versions: bool) -> dict:
    objects = [
        {
            "key": o.key,
            "size": o.size,
            "last_modified": o.last_modified.isoformat(timespec="seconds") if o.last_modified else None,
            "version_count": o.version_count,
            "is_dir": o.is_dir,
        }
        for o in result.objects
    ]
    files = [o for o in result.objects if not o.is_dir]
    dirs = [o for o in result.objects if o.is_dir]
    payload: dict = {
        "name": name,
        "bucket": result.bucket,
        "prefix": result.prefix,
        "endpoint": result.endpoint,
        "objects": objects,
        # Counts and totals reflect FILES only (slice-31 review P1).
        "total_bytes": sum(o.size for o in files),
        "file_count": len(files),
        "dir_count": len(dirs),
    }
    # Slice-31 review P1a: hint must point at a file, not a directory.
    # Slice-31 review P1b: keys are relative to sub_path; prepend it so the
    # hint command works from the project root (where `mintd data pull`
    # expects paths).
    first_file = next((o for o in result.objects if not o.is_dir), None)
    if first_file is not None:
        sub = result.truncated_to_prefix or ""
        sub = sub.strip("/") + "/" if sub.strip("/") else ""
        full_key = sub + first_file.key
        payload["hint"] = f"mintd data pull {name} {full_key}"
    return payload

def _human_bytes(n: int) -> str:
    if n == 0:
        return "0 B"
    size: float = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}".replace(".0", "")
        size /= 1024
    return f"{size:.1f} PB"


def _format_duration(seconds: float) -> str:
    """Human-readable elapsed time: '142ms', '12s', '3m05s'."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"

_DATA_LS_ROW_CAP = 50


def _pretty_data_ls(
    payload: dict, *, name: str, versions: bool, no_truncate: bool = False,
) -> str:
    lines = [f"s3://{payload['bucket']}/{payload['prefix']}"]
    if not payload["objects"]:
        lines.append("(no objects)")
        return "\n".join(lines)

    rows = payload["objects"]
    total_rows = len(rows)
    truncated = False
    if not no_truncate and total_rows > _DATA_LS_ROW_CAP:
        rows = rows[:_DATA_LS_ROW_CAP]
        truncated = True

    table = []
    header = "  %-60s %10s   %-20s"
    if versions:
        header += "   %-8s"
    row_fmt = "  %-60s %10s   %-20s"
    if versions:
        row_fmt += "   %8d"

    header_cols = ["key", "size", "modified"]
    if versions:
        header_cols.append("versions")
    table.append(header % tuple(header_cols))

    for o in rows:
        # Slice-31 review P1: CommonPrefixes keys ALREADY end in `/`;
        # don't add another or we render `data//`.
        key = o["key"]
        size = _human_bytes(o["size"]) if not o["is_dir"] else "-"
        modified = o["last_modified"] or "-"
        args_row = [key, size, modified]
        if versions:
            args_row.append(o["version_count"])
        table.append(row_fmt % tuple(args_row))

    if truncated:
        remaining = total_rows - _DATA_LS_ROW_CAP
        table.append(
            f"  ... and {remaining} more (use --no-truncate or --json for full listing)"
        )

    lines.extend(table)

    if truncated:
        summary_parts = [
            f"{_DATA_LS_ROW_CAP} of {payload['file_count']} file(s) shown, "
            f"{_human_bytes(payload['total_bytes'])} total"
        ]
    else:
        summary_parts = [
            f"{payload['file_count']} file(s), {_human_bytes(payload['total_bytes'])} total"
        ]
    if payload.get("dir_count"):
        summary_parts.append(f"{payload['dir_count']} subdir(s)")
    lines.append("\n" + ", ".join(summary_parts) + ".")

    if "hint" in payload:
        # Use the payload's pre-computed hint (already targets the first
        # FILE, not a directory — slice-31 review P1).
        lines.append(f"\n💡 Download a single file:\n   {payload['hint']}")
    return "\n".join(lines)


def _handle_data_clone(args: argparse.Namespace) -> int:
    reporter = args._reporter
    t0 = time.monotonic()
    config = Config.load()
    if args.timeout is None:
        effective_timeouts = config.timeouts
    else:
        override = None if args.timeout == 0 else args.timeout
        effective_timeouts = config.timeouts.model_copy(update={"transfer": override})
    
    client = _resolve_catalog_client(config)
    dvc_ops = SubprocessDvcOps(
        timeouts=effective_timeouts,
        reporter=reporter,
        aws_profile_name=config.aws_profile_name,
    )
    registry_git_ops = SubprocessRegistryGitOps(timeouts=effective_timeouts, reporter=reporter)
    fast_sync_ops = _resolve_fast_sync_ops(config)
    
    reporter.debug(f"resolved registry_url={config.registry_url}")
    try:
        clone_result = clone_and_pull_product(
            client, dvc_ops, registry_git_ops, fast_sync_ops,
            name=args.name,
            dest=args.dest,
            rev=args.rev,
            primary_only=args.primary_only,
            jobs=args.jobs,
            extra_dvc_args=args.dvc_args or None,
            reporter=reporter,
        )
    except CatalogNotFound as exc:
        reporter.error(str(exc), hint="run 'mintd data list' to see available products")
        return 1
    except MissingPrimaryDataProduct as exc:
        reporter.error(str(exc), hint="drop --primary to pull every tracked output")
        return 1
    except ImportDestinationExists as exc:
        reporter.error(str(exc), hint="pass --dest <path> or remove the existing directory")
        return 1
    except DvcNotInstalled as exc:
        reporter.error(str(exc), hint="pip install 'dvc[s3]' (see notes/INSTALL.md)")
        return 2
    except ProducerError as exc:
        reporter.error(str(exc), hint="check git auth (gh auth status / ssh -T git@github.com)")
        return 1
    except GitOpError as exc:
        reporter.error(str(exc), hint="check git auth (gh auth status / ssh -T git@github.com)")
        return 1
    except WallTimeoutExceeded as exc:
        reporter.error(f"command exceeded wall timeout of {exc.seconds}s")
        return 1
    except (DvcOpError, ValueError) as exc:
        reporter.error(str(exc))
        return 1
    
    dest = clone_result.dest
    elapsed = time.monotonic() - t0
    primary = _read_primary_from_clone(dest)
    files, total_bytes = _measure_clone_result(dest)
    payload = {
        "dest": str(dest),
        "primary": primary,
        "elapsed_s": round(elapsed, 2),
        "files": files,
        "bytes": total_bytes,
        "rev": clone_result.rev,
        "remote_bucket": clone_result.remote_bucket,
    }
    rev_clause = f" @ {clone_result.rev[:7]}" if clone_result.rev else ""
    remote_clause = f" from s3://{clone_result.remote_bucket}" if clone_result.remote_bucket else ""
    size_clause = f" ({files} files, {_human_bytes(total_bytes)})" if files else ""
    if reporter.json_mode:
        reporter.result(payload, pretty=_pretty_data_clone)
    else:
        reporter.success(
            f"✓ cloned {args.name}{rev_clause}{remote_clause}{size_clause} "
            f"→ {dest.name}/ in {_format_duration(elapsed)}"
        )
    return 0


def _read_primary_from_clone(dest: Path) -> str | None:
    """Best-effort read of data_products.primary from the cloned metadata.json.
    Returns None if the file is missing or unreadable (e.g., in tests where
    `clone_and_pull_product` is stubbed and `dest` doesn't exist on disk).
    """
    try:
        meta = json.loads((dest / "metadata.json").read_text(encoding="utf-8"))
        primary = meta.get("data_products", {}).get("primary")
        return primary if isinstance(primary, str) else None
    except (OSError, json.JSONDecodeError):
        return None


def _measure_clone_result(dest: Path) -> tuple[int, int]:
    """Sum file count and total bytes under dest, skipping the .git tree.
    Returns (0, 0) if dest is unreadable (e.g., test stub)."""
    files = 0
    total = 0
    try:
        for p in dest.rglob("*"):
            if not p.is_file():
                continue
            # Skip .git internals — they're clone metadata, not data product.
            if ".git" in p.relative_to(dest).parts:
                continue
            files += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return files, total


def _pretty_data_clone(payload: dict) -> str:
    """Render the clone-result summary footer."""
    lines = []
    if payload.get("primary"):
        lines.append(
            f"  primary: {payload['primary']}  "
            f"({payload['files']} files, {_human_bytes(payload['bytes'])})"
        )
    return "\n".join(lines) if lines else ""


def _pretty_data_push(payload: dict) -> str:
    """Render the push-result summary footer (non-json pretty mode)."""
    remote_clause = f" → s3://{payload['remote']}" if payload.get("remote") else ""
    if payload.get("up_to_date"):
        return f"✓ already up to date{remote_clause}"
    pushed = payload.get("pushed")
    count_clause = f" {pushed} object(s)" if pushed is not None else ""
    size_clause = f" ({_human_bytes(payload['bytes'])})" if payload.get("bytes") else ""
    return f"✓ pushed{count_clause}{size_clause}{remote_clause}"


def _import_summary(produced: list[Path]) -> dict:
    """Derive provenance + size facts from the produced `.dvc` files for the
    `data import` completion line (slice 38b). Best-effort: a non-import-shaped
    `.dvc` degrades to a count-only summary rather than raising."""
    import yaml as _yaml
    from .imports import DataDependency

    pin: str | None = None
    repo: str | None = None
    total_bytes = 0
    file_count = 0
    for p in produced:
        try:
            dep = DataDependency.from_dvc_file(p)
            pin = pin or dep.contract_pin
            repo = repo or dep.producer_repo
        except Exception:
            pass
        try:
            raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            for out in raw.get("outs", []):
                total_bytes += int(out.get("size", 0) or 0)
                file_count += int(out.get("nfiles", 1) or 1)
        except Exception:
            file_count += 1
    dest = produced[0].parent if produced else None
    return {
        "pin": pin,
        "producer_repo": repo,
        "file_count": file_count or len(produced),
        "total_bytes": total_bytes,
        "dest": str(dest) if dest else None,
    }


def _handle_data_import(args: argparse.Namespace) -> int:
    if args.bump and (args.import_path or args.rev or args.all_outputs):
        args._parser.error("--bump cannot be combined with --path, --rev, or --all")

    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client, dvc_ops = _resolve_clients(config, reporter)

    if args.bump:
        try:
            with reporter.status(f"Bumping {args.name}..."):
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
            reporter.error(str(exc))
            return 1
        except DvcOpError as exc:
            reporter.error(str(exc), hint="check connectivity then retry: mintd config validate")
            return 1
        if not result.changed:
            old = result.old_pin[:7] if result.old_pin else "?"
            if reporter.json_mode:
                reporter.result({"name": args.name, "changed": False, "pin": result.old_pin})
            else:
                reporter.success(f"✓ {args.name} up to date ({old})")
        else:
            old = result.old_pin[:7] if result.old_pin else "?"
            new = result.new_pin[:7] if result.new_pin else "?"
            if reporter.json_mode:
                reporter.result({
                    "name": args.name, "changed": True,
                    "old_pin": result.old_pin, "new_pin": result.new_pin,
                })
            else:
                reporter.success(f"✓ bumped {args.name}: {old} → {new}")
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
            extra_dvc_args=args.dvc_args or None,
            reporter=reporter,
        )
    except CatalogNotFound as exc:
        reporter.error(str(exc), hint="run 'mintd data list' to see available products")
        return 1
    except (
        MissingPrimaryDataProduct,
        ImportDestinationExists,
        ImportNotFound,
        PrimaryRemovedAtHead,
        ProducerError,
    ) as exc:
        reporter.error(str(exc))
        return 1
    except DvcOpError as exc:
        reporter.error(
            str(exc),
            hint="retry with --dvc-arg=-v for verbose DVC output",
        )
        return 1
    summary = _import_summary(produced)
    if reporter.json_mode:
        reporter.result({**summary, "produced": [str(p) for p in produced]})
    else:
        pin = summary["pin"]
        prov = f" @ {pin[:7]}" if pin else ""
        size = f", {_human_bytes(summary['total_bytes'])}" if summary["total_bytes"] else ""
        dest = f" → {summary['dest']}/" if summary["dest"] else ""
        reporter.success(
            f"✓ imported {args.name}{prov} ({summary['file_count']} file(s){size}){dest}"
        )
    return 0


def _handle_data_list(args: argparse.Namespace) -> int:
    reporter = args._reporter
    if args.imported and args.project_type is not None:
        reporter.error("--imported cannot be combined with --type")
        return 2

    if args.imported:
        deps = scan_imports(Path("."))
        payload = [
            {"local_path": str(d.local_path),
             "producer_repo": d.producer_repo,
             "contract_pin": d.contract_pin,
             "output_path": d.output_path}
            for d in deps
        ]
        pretty = (lambda _p: "no imports") if not deps else _pretty_imports
        reporter.result(payload, pretty=pretty)
        return 0

    config = Config.load()
    client = _resolve_catalog_client(config)
    filter_ = CatalogFilter(project_type=args.project_type) if args.project_type else None
    entries = client.list(filter_)
    catalog_payload: list[dict[str, object]] = [
        {"name": e.name, "project_type": e.project_type,
         "description": (e.description or None)}
        for e in sorted(entries, key=lambda e: (e.project_type, e.name))
    ]
    # For pretty mode, use the slice-22 grouped table (grouped by project_type
    # with the canonical data → code → project → enclave order, name-column
    # truncation, "(no description)" placeholder for empty descriptions).
    if not entries:
        pretty_text = "no entries"
    else:
        pretty_text = _render_catalog_table(entries, detailed=args.detailed, width=args.width)
    reporter.result(catalog_payload, pretty=lambda _p: pretty_text)
    return 0


def _pretty_imports(payload: list[dict]) -> str:
    lines = []
    for d in payload:
        lines.append(f"{d['local_path']} ← {d['producer_repo']}@{d['contract_pin'][:7]} ({d['output_path']})")
    return "\n".join(lines)


_CATALOG_TYPE_ORDER = ("data", "code", "project", "enclave")


def _render_catalog_table(entries, *, detailed: bool, width: int) -> str:
    """Render catalog entries grouped by project_type, ASCII-only.

    Groups appear in the canonical order (data → code → project → enclave →
    anything else, alphabetical). Within a group, entries sort by name.
    Descriptions truncate to ``width`` chars (with ``...``) unless
    ``detailed`` is True. Multi-line descriptions collapse to the first line.
    """
    from collections import defaultdict

    groups: dict[str, list] = defaultdict(list)
    for entry in entries:
        groups[entry.project_type].append(entry)
    other = sorted(k for k in groups if k not in _CATALOG_TYPE_ORDER)
    ordered = [k for k in _CATALOG_TYPE_ORDER if k in groups] + other

    sections: list[str] = []
    for ptype in ordered:
        members = sorted(groups[ptype], key=lambda e: e.name)
        name_col = max(20, max(len(e.name) for e in members))
        header = f"{ptype} ({len(members)})"
        underline = ("-" * name_col) + "  " + ("-" * width)
        rows = [header, f"{'name'.ljust(name_col)}  description", underline]
        for entry in members:
            desc = (entry.description or "").splitlines()[0] if entry.description else ""
            if not desc:
                rendered = "(no description)"
            elif not detailed and len(desc) > width:
                rendered = desc[: max(0, width - 3)] + "..."
            else:
                rendered = desc
            rows.append(f"{entry.name.ljust(name_col)}  {rendered}")
        sections.append("\n".join(rows))
    return "\n\n".join(sections)


def _handle_enclave_list(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    try:
        manifest = EnclaveManifest.load(args.manifest)
    except FileNotFoundError:
        reporter.error(
            f"enclave_manifest.yaml not found at {args.manifest}",
            hint="create one with 'mintd enclave add <repo> --pin <sha>', or pass --manifest <path>",
        )
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
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        with reporter.status(f"Bumping {args.name}..."):
            result = enclave_bump(
                client,
                manifest_path=args.manifest,
                name=args.name,
                force=args.force,
            )
    except BumpBlocked as exc:
        return _render_bump_blocked(exc)
    except ImportNotFound as exc:
        reporter.error(str(exc), hint="'mintd enclave list' to see subscribed repos")
        return 1
    except PrimaryRemovedAtHead as exc:
        reporter.error(str(exc), hint="pin to an older SHA or unsubscribe with 'mintd enclave remove'")
        return 1
    except AppendOnlyViolation as exc:
        reporter.error(str(exc), hint="approved_products is append-only; edit the manifest by hand")
        return 1
    if result is None:
        print("up to date")
    else:
        print(f"bumped: {result}")
    return 0


def _handle_enclave_add(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
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
    except AlreadyApproved as exc:
        reporter.error(str(exc), hint="already subscribed; 'mintd enclave list' to review")
        return 1
    except CatalogNotFound as exc:
        reporter.error(str(exc), hint="run 'mintd data list' to see available products")
        return 1
    except MissingPrimaryDataProduct as exc:
        reporter.error(str(exc), hint="pass --path <output> or --all")
        return 1
    except AppendOnlyViolation as exc:
        reporter.error(str(exc), hint="approved_products is append-only; edit the manifest by hand")
        return 1
    except (ProducerError, ValueError) as exc:
        reporter.error(str(exc), hint="check the repo/pin arguments")
        return 1
    # Re-load to print the just-added entry's resolved pin.
    manifest = EnclaveManifest.load(path)
    ap = manifest.approved_products[-1]
    src = ap.source_path or ("<all>" if ap.all else "<primary>")
    print(f"subscribed: {ap.repo}@{ap.pin[:7]} (path: {src})")
    return 0


def _handle_enclave_remove(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
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
    except ImportNotFound as exc:
        reporter.error(str(exc), hint="'mintd enclave list' to see subscribed repos")
        return 1
    except AppendOnlyViolation as exc:
        reporter.error(str(exc), hint="approved_products is append-only; edit the manifest by hand")
        return 1
    msg = f"removed: {args.repo}"
    if args.source_path:
        msg += f" (source_path={args.source_path})"
    print(msg)
    return 0


def _handle_enclave_pull(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client, dvc_ops = _resolve_clients(config, reporter)
    try:
        with reporter.status("Pulling enclave data..."):
            _, written = enclave_pull(
                client,
                dvc_ops,
                manifest_path=args.manifest,
                repo=args.repo,
                force=args.force,
                reporter=reporter,
            )
    except EnclavePullError as exc:
        # The pin/repo retry hint only makes sense when the producer's pin/repo
        # is actually the problem. For an un-DVC-initialized enclave (now
        # auto-fixed by lazy init, but possible if `dvc` itself is broken) it
        # actively misleads — point at DVC init instead.
        if isinstance(exc.cause, DvcNotInRepoError):
            hint = (
                "enclave is not DVC-initialized; re-run `mintd enclave pull` "
                "(it initializes DVC automatically) or run `dvc init` once"
            )
        elif isinstance(exc.cause, DvcImportPathNotFound):
            hint = f"check {exc.repo}'s pin/repo, then retry: mintd enclave pull --repo {exc.repo}"
        else:
            hint = f"retry: mintd enclave pull --repo {exc.repo}"
        reporter.error(str(exc), hint=hint)
        return 1
    except (
        CatalogNotFound,
        ImportNotFound,
        MissingPrimaryDataProduct,
        ProducerError,
        AppendOnlyViolation,
        ValueError,
    ) as exc:
        reporter.error(str(exc))
        return 1
    if not written:
        reporter.info("nothing to pull")
        return 0
    if reporter.json_mode:
        reporter.result(
            {
                "pulled": [
                    {"repo": i.repo, "pin": i.contract_pin, "local_path": str(i.local_path)}
                    for i in written
                ]
            }
        )
        return 0
    by_repo: dict[str, list] = {}
    for item in written:
        by_repo.setdefault(item.repo, []).append(item)
    for repo, items in by_repo.items():
        pin = items[0].contract_pin[:7]
        reporter.success(f"✓ {repo} @ {pin} ({len(items)} output(s))")
    return 0


def _handle_enclave_package(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    # When --output is unset, hand `enclave_package` an output_dir; it
    # builds the filename from the computed `transfer_id` so same-day
    # re-runs produce distinct archives.
    output_dir = (
        args.manifest.parent / "transfers"
        if args.output_archive is None
        else None
    )
    try:
        with reporter.status("Packaging enclave archive..."):
            archive = enclave_package(
                manifest_path=args.manifest,
                name=args.repo,
                output_archive=args.output_archive,
                output_dir=output_dir,
            )
    except NothingToPackage as exc:
        reporter.error(str(exc), hint="run 'mintd enclave pull' first")
        return 1
    except ArchiveAlreadyExists as exc:
        reporter.error(str(exc), hint="remove it or pass --output <path>")
        return 1
    except (UnsafeArchiveMember, InvalidTransferManifest) as exc:
        reporter.error(str(exc), hint="re-run 'mintd enclave pull --force'")
        return 1
    except AppendOnlyViolation as exc:
        reporter.error(str(exc), hint="transferred[] is append-only; edit the manifest by hand")
        return 1
    print(f"packaged: {archive}")
    return 0


def _handle_enclave_verify(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    try:
        with reporter.status("Verifying enclave manifest..."):
            _, written = enclave_verify(
                extracted_dir=args.extracted_dir,
                manifest_path=args.manifest,
                data_root=args.data_root,
            )
    except InvalidTransferManifest as exc:
        reporter.error(str(exc), hint="the archive is malformed; re-export from source")
        return 1
    except PathTraversalDetected as exc:
        reporter.error(str(exc), hint="unsafe paths — do not extract; contact the sender")
        return 1
    except AppendOnlyViolation as exc:
        reporter.error(str(exc), hint="transferred[] is append-only; edit the manifest by hand")
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
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        metadata = _load_metadata_with_schema_hint(args.path / "metadata.json")
    except FileNotFoundError as exc:
        reporter.error(str(exc))
        return 1
    except MetadataSchemaTooOld as exc:
        reporter.error(
            str(exc),
            hint="run 'mintd update metadata' to migrate this project to v2",
        )
        return 1
    except ValidationError as exc:
        reporter.error(
            f"metadata.json failed v2 schema validation ({len(exc.errors())} field error(s))",
            hint="run 'mintd check' to see field-level details",
        )
        return 1
    findings = check_project(args.path, upgrades=False)
    error_findings = [f for f in findings if f.severity == "error"]
    if error_findings:
        return _render_findings(error_findings, json_out=False)
    with reporter.status("Registering project with the catalog…"):
        try:
            result = client.register(metadata, reporter=reporter)
        except CatalogAlreadyExists as exc:
            reporter.error(
                f"catalog entry {str(exc)!r} already exists",
                hint="use 'mintd registry update' to push changes to an existing entry",
            )
            return 1
    if result.dry_run:
        reporter.info(f"Would register {result.name!r} (dry-run; no PR opened).")
        return 0
    if result.pr_url:
        reporter.success(f"Registration PR created: {result.pr_url}")
    elif result.pr_number is not None:
        reporter.success(f"Registration PR #{result.pr_number} created.")
    else:
        reporter.success(f"Registered {result.name!r}.")
    reporter.info("The PR will be reviewed and merged by registry administrators.")
    return 0


def _handle_registry_update(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    try:
        metadata = _load_metadata_with_schema_hint(args.path / "metadata.json")
    except FileNotFoundError as exc:
        reporter.error(str(exc))
        return 1
    except MetadataSchemaTooOld as exc:
        reporter.error(
            str(exc),
            hint="run 'mintd update metadata' to migrate this project to v2",
        )
        return 1
    except ValidationError as exc:
        reporter.error(
            f"metadata.json failed v2 schema validation ({len(exc.errors())} field error(s))",
            hint="run 'mintd check' to see field-level details",
        )
        return 1
    with reporter.status("Updating project registry entry…"):

        try:
            result = client.update(metadata, dry_run=args.dry_run, reporter=reporter)
        except CatalogNotFound as exc:
            reporter.error(
                f"catalog entry {str(exc)!r} not found",
                hint="run 'mintd registry register' first to register this project",
            )
            return 1
    if not result.changes:
        reporter.info("No changes to publish." + (" (dry-run)" if result.dry_run else ""))
        return 0
    for change in result.changes:
        reporter.info(f"{change.field_path}: {change.before!r} → {change.after!r}")
    if result.dry_run:
        reporter.info("Dry-run: no PR opened.")
        return 0
    field_names = [c.field_path for c in result.changes]
    n = len(field_names)
    shown = ", ".join(field_names[:3]) + (f", +{n - 3} more" if n > 3 else "")
    name = metadata.project.name
    if result.pr_url:
        pr_clause = f" → PR {result.pr_url}"
    elif result.pr_number is not None:
        pr_clause = f" → PR #{result.pr_number}"
    else:
        pr_clause = ""
    reporter.success(f"✓ updated {name} — {n} field(s): {shown}{pr_clause}")
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
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    with reporter.status("Refreshing registry cache..."):
        count = client.sync()
    print(f"synced ({count} entries)")
    return 0


def _handle_publish(args: argparse.Namespace) -> int:
    from .publish import prepare_publish, _apply_publish
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client, dvc_ops = _resolve_clients(config)
    git_ops = _resolve_git_ops(config)
    try:
        preview = prepare_publish(
            project_path=args.path,
            version=args.version,
            dry_run=args.dry_run,
            client=client,
            git_ops=git_ops,
        )
    except PublishBlocked as exc:
        reporter.error(str(exc))
        for f in exc.findings[:5]:
            src = f.source or "<project>"
            reporter.info(f"  [{f.severity}] {src}: {f.message}")
            if f.hint:
                reporter.info(f"    💡 {f.hint}")
        if len(exc.findings) > 5:
            reporter.info(f"  ... and {len(exc.findings) - 5} more")
        return 1
    except WorkingTreeDirty as exc:
        reporter.error(str(exc), hint=exc.recovery_hint or None)
        return 1
    except PublishError as exc:
        reporter.error(str(exc), hint=exc.recovery_hint or None)
        return 1

    _render_publish_preview(reporter, preview)

    if args.dry_run:
        reporter.info("(dry-run: no side effects)")
        return 0
    if not args.assume_yes:
        try:
            ok = _prompt_publish_confirm(reporter)
        except PublishNonInteractive as exc:
            reporter.error(str(exc), hint="re-run with --yes to skip the interactive preview.")
            return 1
        if not ok:
            reporter.info("publish cancelled.")
            return 0

    with reporter.status(f"Publishing v{preview.new_version}…"):
        try:
            result = _apply_publish(
                preview,
                project_path=args.path,
                client=client,
                dvc_ops=dvc_ops,
                git_ops=git_ops,
                message=args.message,
                reporter=reporter,
            )
        except (WorkingTreeDirty, PublishError) as exc:
            reporter.error(str(exc), hint=exc.recovery_hint or None)
            return 1
    tag = f"v{result.version}"
    storage = getattr(preview.new_metadata, "storage", None)
    prefix_clause = ""
    if storage is not None and getattr(storage, "bucket", None):
        prefix = (getattr(storage, "prefix", None) or "").strip("/")
        prefix_clause = f", s3://{storage.bucket}/{prefix}/" if prefix else f", s3://{storage.bucket}/"
    pr_clause = f", PR {result.pr_url}" if result.pr_url else ", PR (local)"
    if reporter.json_mode:
        reporter.result(
            {
                "project": preview.project_name,
                "version": result.version,
                "tag": tag,
                "pr_url": result.pr_url,
            }
        )
    else:
        reporter.success(
            f"✓ published {preview.project_name} v{result.version} — tag {tag}{pr_clause}{prefix_clause}"
        )
    return 0


def _render_publish_preview(reporter: Reporter, preview: PublishPreview) -> None:
    reporter.info(f"About to publish {preview.project_name} @ v{preview.current_version} → v{preview.new_version}")
    reporter.info("")
    reporter.info(f"Working tree:    {preview.working_tree_commit} ({'clean' if preview.working_tree_clean else 'DIRTY'})")
    reporter.info(f"Primary output:  {preview.primary_path}")
    reporter.info("Outputs:")
    for out in preview.outputs:
        prefix = "[primary]" if out.path == preview.primary_path else " - "
        reporter.info(f"  {prefix} {out.path} {' - ' + out.description if out.description else ''}")
    
    catalog_diff_msg = (
        f"{len(preview.catalog_diff)} field(s) changed" if not preview.first_publish 
        else "first publish — no prior catalog entry"
    )
    reporter.info(f"Catalog diff:    {catalog_diff_msg}")
    for change in preview.catalog_diff:
        reporter.info(f"  - {change.field_path}: {change.before!r} → {change.after!r}")
    reporter.info("")


def _prompt_publish_confirm(
    reporter: Reporter, 
    prompt_fn: Callable[[str], str] = input, 
    isatty_fn: Callable[[], bool] = sys.stdin.isatty
) -> bool:
    if not isatty_fn():
        raise PublishNonInteractive("publish is interactive without --yes")
    
    try:
        resp = prompt_fn("Continue? [y/N]: ").strip().lower()
        return resp in ("y", "yes")
    except EOFError:
        return False


def _handle_config_show(args: argparse.Namespace) -> int:
    try:
        config = Config.load(args.path) if args.path is not None else Config.load()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = config_ops.render_config(config, json_out=args.json_out)
    # YAML emits a trailing newline already; JSON does not. Use print's
    # default newline only when the rendered text doesn't carry one.
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _handle_config_setup(args: argparse.Namespace) -> int:
    write = not args.dry_run
    try:
        if args.from_file is not None:
            source = None if args.from_file == "-" else args.from_file
            config = config_ops.apply_from_file(args.path, source, write=write)
        elif args.migrate_v1 is not None:
            config = config_ops.apply_migrate_v1(args.path, args.migrate_v1, write=write)
        elif args.set_pairs:
            pairs = [config_ops.parse_set_pair(s) for s in args.set_pairs]
            config = config_ops.apply_set_updates(args.path, pairs, write=write)
        else:
            # No flags → interactive walkthrough of every Config field.
            config = config_ops.interactive_setup(args.path, write=write)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("# dry-run: would write the following:")
    print(config_ops.render_config(config), end="")
    return 0


def _handle_config_validate(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    with reporter.status("Validating S3 connectivity..."):
        steps = config_ops.validate_config(args.path, bucket=args.bucket)
    text, exit_code = config_ops.render_validation(steps, json_out=args.json_out)
    print(text)
    if not args.json_out:
        s3_step = next((s for s in steps if s.name == "s3"), None)
        if s3_step is not None and s3_step.status == "ok":
            try:
                config = Config.load()
                endpoint = config.storage_endpoint or "default AWS endpoint"
                profile = config.aws_profile_name or "default"
            except Exception:
                endpoint, profile = "default AWS endpoint", "default"
            ms_clause = f" — 200 OK, {s3_step.latency_ms}ms" if s3_step.latency_ms is not None else " — 200 OK"
            reporter.success(f"✓ s3://{args.bucket} via {endpoint} (profile: {profile}){ms_clause}")
    return exit_code


def _handle_update_metadata(args: argparse.Namespace) -> int:
    """Migrate a v1 ``metadata.json`` (schema 1.x) in ``args.path`` to v2.

    Exit codes:
    - 0 — migrated cleanly (or dry-run preview produced)
    - 1 — already v2, or file missing
    - 2 — migration produced a dict that fails v2 validation (user must
      hand-fix the field path surfaced in the message)
    """
    try:
        report = metadata_migrate.apply_metadata_migration(
            args.path, dry_run=args.dry_run
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except metadata_migrate.MetadataAlreadyV2 as exc:
        print(f"already v2: {exc}")
        return 1
    except metadata_migrate.MetadataMigrateError as exc:
        print(
            f"error: migration produced invalid v2 metadata: {exc}",
            file=sys.stderr,
        )
        return 2
    if args.json_out:
        print(
            json.dumps(
                {
                    "moved": report.moved,
                    "defaulted": report.defaulted,
                    "dropped": report.dropped,
                    "schema_before": report.schema_before,
                    "schema_after": report.schema_after,
                },
                indent=2,
            )
        )
        return 0
    if args.dry_run:
        print("# dry-run: would apply the following migration")
    print(f"schema_version: {report.schema_before} → {report.schema_after}")
    for src, dst in report.moved:
        print(f"  → {src} → {dst}")
    for name in report.defaulted:
        print(f"  + {name} (defaulted)")
    for name in report.dropped:
        print(f"  - {name} (dropped)")
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
                        "hint": f.hint,
                    }
                )
            )
    else:
        for f in findings:
            prefix = _resolve_prefix(f.kind)
            loc = f.source if f.source else "<project>"
            print(f"{prefix} [{f.severity}] {loc}: {f.message}")
            if f.hint:
                print(f"    💡 {f.hint}")
        if not findings:
            print("no issues found")
        else:
            n_err = sum(1 for f in findings if f.severity == "error")
            n_warn = sum(1 for f in findings if f.severity == "warning")
            sections = sorted({f.section for f in findings if f.section})
            across = f" across {' + '.join(sections)}" if sections else ""
            print(f"{n_err} error(s), {n_warn} warning(s){across}")
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
