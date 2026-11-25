"""``mintd`` CLI entry point.

Subcommand tree wires the library API to argparse. Handlers delegate the
work to the library layer (``mintd.check``, ``mintd.data``, ``mintd.enclave``,
``mintd.publish``, ...) and keep to:

- argument parsing
- ``Config`` loading and client construction (the ``_resolve_*`` factories)
- rendering results and errors (``CheckFinding``s, ``BumpBlocked``, ...) for
  humans and ``--json``
- exit-code dispatch
- a few guards no library function owns, such as ``registry register``
  refusing an uncommitted project and ``enclave pull`` refusing to run
  outside the enclave
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn, Optional

from pydantic import ValidationError

from . import __version__, config_ops, metadata_migrate
from ._console import Reporter
from ._config import Config, ConfigError
from ._dvc_ops import (
    DvcOpError,
    DvcOps,
    SubprocessDvcOps,
)
from ._registry_git_ops import (
    GhAuthError,
    GhNotInstalled,
    GitOpError,
    PRConflictError,
    RegistryBranchExists,
    specific_hint,
)
from ._dvc_invoke import dvc_root
from ._fast_sync_ops import FastSyncOps
from . import _s3
from ._s3 import S3AccessDenied
from .errors import MintdError, strip_userinfo, unreachable_repo_hint
from ._push_verify import judge as judge_push
from ._registry_git_ops import RegistryGitOps, SubprocessRegistryGitOps
from .catalog import (
    CatalogAlreadyExists,
    CatalogClient,
    CatalogFilter,
    CatalogNotFound,
    GitCatalogClient,
)
from ._dvc_state import abbreviate, gitignored_pointers
from .check import CheckFinding, check_project
from .data import (
    BumpBlocked,
    ImportDestinationExists,
    ImportNotFound,
    PrimaryRemovedAtHead,
    UnknownProductPath,
    _cloned_metadata_entry,
    bump_import,
    clone_and_pull_product,
    import_product,
)
from .data_ops import (
    COMMIT_MESSAGE,
    PointerCommit,
    commit_pushed_pointers,
    data_add,
    data_pull,
    data_push,
    data_remove,
    data_verify,
)
from ._s3_listing_ops import (
    BucketNotFound,
    S3ListingResult,
    list_product_objects,
)
from ._archive_ops import UnsafeArchiveMember
from ._share_ops import (
    neutralize_control_chars,
    resolve_share_user,
    share_get,
    share_put,
)
from ._cache_ops import (
    CachePullSummary,
    CachePushSummary,
    cache_pull,
    cache_push,
    resolve_repo_remote,
    select_cache_objects,
)
from .enclave import (
    EnclaveManifest,
    InvalidTransferManifest,
    NothingNewToPackage,
    enclave_add,
    enclave_bump,
    enclave_package,
    enclave_pull,
    enclave_remove,
    enclave_verify,
    missing_downloads,
    subscription_label,
)
from .imports import scan_imports
from .init import init_project
from .model import Metadata
from .pending_registrations import PendingRegistrations
from .producer import MissingPrimaryDataProduct, ProducerError
from .publish import (
    PublishBlocked,
    PublishNonInteractive,
    PublishPreview,
)

logger = logging.getLogger(__name__)

_RELEASES = "https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download"


def _install_line(*, with_schema: bool = False, windows: bool = os.name == "nt") -> str:
    """The one line that installs, or updates to, the newest promoted release.

    README.md shows exactly these lines (tests/test_docs.py checks it).
    """
    if windows:
        if with_schema:
            return f"& ([scriptblock]::Create((irm {_RELEASES}/install.ps1))) -WithSchema"
        return f"irm {_RELEASES}/install.ps1 | iex"
    return f"curl -fsSL {_RELEASES}/install.sh | bash" + (" -s -- --with-schema" if with_schema else "")


def _reinstall_hint(*, windows: bool = os.name == "nt") -> str:
    """The [schema] hint. On Windows it says the line is for PowerShell, not cmd."""
    where = ", in PowerShell" if windows else ""
    return f"reinstall mintd with the [schema] extra{where}: {_install_line(with_schema=True, windows=windows)}"


def _release_notice(args: argparse.Namespace) -> None:
    """On stderr, when a newer release is out or this one was withdrawn.

    Never under --json or -q, and never an error: `mintd --version` must work
    with a broken config and offline. stdout stays exactly `mintd <version>`.
    """
    # No stderr (`2>&-`): rich would fall back to stdout, which must stay one line.
    if args.json_out or args.quiet or sys.stderr is None:
        return
    from ._release_check import is_release, newest_release, release_notice

    try:
        cache_dir = Config.load().resolved_cache_dir()
    except (ConfigError, OSError):
        return
    notice = release_notice(__version__, newest_release(cache_dir, __version__))
    if notice and is_release(__version__):
        where = ", in PowerShell" if os.name == "nt" else ""
        notice += f"\n  update{where}: {_install_line()}"
    if notice:  # a dev build gets no update line: the release would be older
        args._reporter.info(notice)  # every reporter line is soft-wrapped, so it copies whole


class MetadataSchemaTooOld(MintdError):
    hint = "run 'mintd update metadata' to migrate this project to v2"

    def __init__(self, path: Path, found: str) -> None:
        super().__init__(f"metadata.json at {path} is schema {found!r}, expected '2.0'")
        self.path = path
        self.found = found


def _load_metadata_with_schema_hint(path: Path) -> Metadata:
    """Read metadata.json for `registry register` and `registry update`.
    Every failure is a ``MintdError``: a missing file, the v1 schema
    (``MetadataSchemaTooOld``, hinting at `mintd update metadata`), or a v2
    file pydantic rejects."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MintdError(str(exc)) from exc
    try:
        peek = json.loads(raw)
    except json.JSONDecodeError:
        peek = {}
    sv = peek.get("schema_version") if isinstance(peek, dict) else None
    if sv is not None and sv != "2.0":
        raise MetadataSchemaTooOld(path=path, found=str(sv))
    try:
        return Metadata.model_validate_json(raw)
    except ValidationError as exc:
        raise MintdError(
            f"metadata.json failed v2 schema validation ({len(exc.errors())} field error(s))",
            hint="run 'mintd check' to see field-level details",
        ) from exc


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
    "drift_unknown": "⚠",
    "unreachable": "⚠",
    "schema_too_old": "⚠",
    "pin_missing": "✗",
    "metadata_missing": "✗",
    "metadata_invalid": "✗",
    "invalid_manifest": "✗",
    "catalog_unresolved": "✗",
    None: "·",
}

_RECOVERABLE_KINDS: frozenset[str] = frozenset(
    {"unreachable", "schema_too_old", "drift_unknown"}
)


class _MintdArgumentParser(argparse.ArgumentParser):
    """argparse subclass that exits 64 on misuse (instead of argparse's 2)."""

    def error(self, message: str) -> NoReturn:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(64)


class _Removed(argparse.Action):
    """A flag a release removed. Typing it says where its job went, with the
    value given, and exits 64 while parsing, before any handler, so no code can
    read the value. The next removal is one ``add_argument(..., action=_Removed)``."""

    def __init__(self, option_strings: list[str], dest: str, *, instead: str, **_kw: Any) -> None:
        super().__init__(option_strings, dest, nargs="?", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        self.instead = instead

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: Any, option_string: str | None = None) -> NoReturn:
        shown = shlex.quote(neutralize_control_chars(values)) if values else "<path>"
        parser.exit(64, f"{option_string} was removed; {self.instead.format(value=shown)}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args._reporter = _build_reporter(args)
    if args.show_version:
        print(f"mintd {__version__}")
        _release_notice(args)
        return 0
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        if args.command is None:  # bare `mintd`, not `mintd data`
            _release_notice(args)
        return 0
    # Only where the finally below removes it: a bridge left installed keeps
    # writing to whatever stderr it captured.
    args._reporter.install_log_bridge()
    try:
        return handler(args)
    except KeyboardInterrupt:
        args._reporter.error("interrupted by user")
        return 130
    except MintdError as e:
        # The one place an error is rendered: what failed, what to do, the exit code.
        args._reporter.error(str(e), hint=e.hint)
        return e.exit_code
    finally:
        args._reporter.uninstall_log_bridge()


@contextmanager
def _hint(
    errors: type[BaseException] | tuple[type[BaseException], ...],
    hint: str | Callable[[Any], str | None] | None = None,
    *,
    exit_code: int | None = None,
    message: str | None = None,
) -> Iterator[None]:
    """Re-raise ``errors`` from the block as a ``MintdError`` carrying this
    verb's advice, for ``main`` to render.

    For an error whose raise site several verbs share, so the advice depends
    on the verb. ``hint`` (or ``hint(error)``, for advice built from the
    error) applies only where the error has none of its own;
    ``exit_code`` overrides the error's own (else 1); ``message`` replaces
    ``str(error)``. The result is a plain ``MintdError``, so an outer
    ``_hint`` on a base class (``ValueError``, say) no longer matches it.
    """
    try:
        yield
    except errors as e:
        raise MintdError(
            message or str(e),
            hint=getattr(e, "hint", None) or (hint(e) if callable(hint) else hint),
            exit_code=exit_code or getattr(e, "exit_code", None),
        ) from e


def _producer_unreachable(e: ProducerError) -> str | None:
    """The shared advice when a producer's repository can't be reached: for
    `data import --rev` with no `--path` (reading its metadata) and `data clone`.
    Other reasons get none. `--bump` meets an unreachable producer in its
    `check` preflight, whose finding carries the same hint."""
    return unreachable_repo_hint(e.repo) if e.reason == ProducerError.Reason.UNREACHABLE else None


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def _add_schema_parser(subs: Any) -> None:
    """Wire ``data schema generate``. Split out of ``_build_parser``, which is
    at the statement cap."""
    p_schema = subs.add_parser("schema", help="Schema operations")
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
    p_schema_gen.add_argument(
        "--allow-numeric-column-names", action="store_true",
        help="accept column names with digits and no letters (2019, 09001); put it in dvc.yaml's cmd to commit it",
    )
    p_schema_gen.set_defaults(_handler=_handle_data_schema_generate)


def _add_registry_rebuild_parser(subs: Any) -> None:
    p = subs.add_parser(
        "rebuild", help="Rebuild each catalog entry from its product's default branch (run in a registry checkout)"
    )
    p.add_argument("--check", action="store_true", help="Show what would change; write nothing")
    p.add_argument("--registry", type=Path, default=Path("."), help="The registry checkout (default: .)")
    p.set_defaults(_handler=_handle_registry_rebuild)


def _add_cache_parser(subs: Any) -> None:
    """Wire the ``cache push/pull/ls`` subparsers. Split out of ``_build_parser``
    so the naming-discipline grep test can scope to exactly the cache surface's
    argparse help strings (SLICE-37: help text rots under prose-only norms)."""
    p_cache = subs.add_parser(
        "cache", help="Durable repo file cache (S3): mirror untracked repo files to the repo's prefix"
    )
    p_cache_sub = p_cache.add_subparsers(dest="cache_command")

    p_cache_push = p_cache_sub.add_parser(
        "push", help="Upload untracked working-tree files to the repo file cache (S3)"
    )
    p_cache_push.add_argument(
        "paths", nargs="+",
        help="Files or directories in the working tree (anything not DVC-tracked)",
    )
    p_cache_push.add_argument("--remote", help="DVC remote name (default: the project's)")
    p_cache_push.add_argument("--jobs", type=int, default=8, help="Upload parallelism (default: 8)")
    p_cache_push.add_argument(
        "--dry-run", action="store_true", dest="dry_run",
        help="Report the would-upload/unchanged split without moving bytes "
             "(still performs the LIST + HEAD precheck)",
    )
    p_cache_push.add_argument("--path", type=Path, default=Path("."),
                              help="Project root (default: current directory)")
    p_cache_push.set_defaults(_handler=_handle_cache_push)

    p_cache_pull = p_cache_sub.add_parser(
        "pull", help="Download objects from the repo file cache (S3) to their repo paths"
    )
    p_cache_pull.add_argument(
        "paths", nargs="*",
        help="Files or folders to pull, by their path in the project (default: all of the repo file cache (S3))",
    )
    p_cache_pull.add_argument("--prefix", action=_Removed, instead="pass the path: mintd cache pull {value}")
    p_cache_pull.add_argument("--remote", help="DVC remote name (default: the project's)")
    p_cache_pull.add_argument("--jobs", type=int, default=8, help="Download parallelism (default: 8)")
    p_cache_pull.add_argument(
        "--force", action="store_true",
        help="Overwrite existing working-tree files (default: skip-and-warn on a "
             "local file with different content)",
    )
    p_cache_pull.add_argument("--path", type=Path, default=Path("."),
                              help="Project root (default: current directory)")
    p_cache_pull.set_defaults(_handler=_handle_cache_pull)

    p_cache_ls = p_cache_sub.add_parser(
        "ls", help="List objects in the repo file cache (S3)"
    )
    p_cache_ls.add_argument(
        "paths", nargs="*",
        help="Files or folders to list, by their path in the project (default: all of the repo file cache (S3))",
    )
    p_cache_ls.add_argument("--prefix", action=_Removed, instead="pass the path: mintd cache ls {value}")
    p_cache_ls.add_argument("--remote", help="DVC remote name (default: the project's)")
    p_cache_ls.add_argument("--no-truncate", dest="no_truncate", action="store_true",
                            help="Render every row (default: truncate past 50 files)")
    p_cache_ls.add_argument("--path", type=Path, default=Path("."),
                            help="Project root (default: current directory)")
    p_cache_ls.set_defaults(_handler=_handle_cache_ls)


#: What each form of `import` and `clone` brings, shown in their --help and
#: checked word for word against README's "Command | Brings" tables.
_IMPORT_BRINGS = [
    ("<name>", ["the primary output"]),
    ("<name> --path <path>", ["only that dataset or folder, declared", "as an output or not (repeatable)"]),
    ("<name> --all", ["everything the product's DVC tracks,", "raw and intermediate data included"]),
]
_CLONE_BRINGS = [
    ("<name>", ["a full git clone of the producer's repo,", "with its declared outputs and primary", "pulled"]),
    ("<name> --all", ["the same clone, with everything DVC tracks", "pulled, raw and intermediate data", "included"]),
    ("<name> --primary", ["the same clone, with only the primary", "output pulled; --path pulls only declared",
                          "outputs"]),
]


def _brings_epilog(prefix: str, verb: str, rows: list[tuple[str, list[str]]], width: int, other: str) -> str:
    """The "what each form brings" table for `<prefix><verb> --help`, aligned at
    ``width`` whichever spelling it is shown under."""
    lines = ["what each form brings:"]
    for form, brings in rows:
        lines.append(f"  {prefix}{verb} {form}".ljust(width) + brings[0])
        lines.extend(" " * width + more for more in brings[1:])
    return "\n".join(lines) + "\n\n" + other.format(prefix=prefix)


def _add_import_parser(subs: Any, prefix: str) -> argparse.ArgumentParser:
    """`import` under `prefix` ("mintd data " or "mintd "): the same arguments,
    defaults and handler under both spellings, so the two can't drift apart."""
    p_import = subs.add_parser(
        "import",
        help="Bring a product's primary output into this project, pinned (--path, --all for more)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_brings_epilog(
            prefix, "import", _IMPORT_BRINGS, 42,
            "To get the producer's whole project, code and data, use `{prefix}clone`.",
        ),
    )
    p_import.add_argument(
        "name",
        help="Registered data product name (the catalog key) — the same "
             "identifier for import and for --bump",
    )
    p_import.add_argument(
        "--bump", action="store_true",
        help="Re-pin an existing import to the producer's HEAD; selects the "
             "import with the same name/--path pair a plain import uses",
    )
    p_import.add_argument(
        "--path", dest="import_path", action="append", default=[], metavar="PATH",
        help="Import only this path — any file or directory the producer tracks, "
             "declared in data_products.outputs or not (repeatable). Default: the "
             "primary output.",
    )
    p_import.add_argument(
        "--rev", help="Producer commit/tag/branch to import at (default: HEAD)"
    )
    p_import.add_argument(
        "--all", dest="all_outputs", action="store_true",
        help="Import everything the product's DVC tracks, raw and intermediate "
             "data included. It can be large; licensed data still needs access.",
    )
    p_import.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing import of the same output",
    )
    p_import.add_argument(
        "--dest-root", type=Path, default=Path("data/imports"), dest="dest_root",
        help="Directory imports land under (default: data/imports)",
    )
    p_import.add_argument("--jobs", type=int, help="DVC parallelism")
    p_import.add_argument(
        "--timeout", type=float, default=None,
        help="Wall-clock cap in seconds for the import (default: unbounded)",
    )
    p_import.add_argument(
        "--dvc-arg", action="append", default=[], dest="dvc_args", metavar="ARG",
        help="Append an arg to the underlying `dvc` invocation. "
             "Use `--dvc-arg=VALUE` form for hyphen-prefixed values "
             "(repeatable; ignored on fast-sync code paths).",
    )
    p_import.set_defaults(_handler=_handle_data_import, _parser=p_import)
    return p_import


def _add_clone_parser(subs: Any, prefix: str) -> argparse.ArgumentParser:
    """`clone` under `prefix` ("mintd data " or "mintd "): the same arguments,
    defaults and handler under both spellings, so the two can't drift apart."""
    p_clone = subs.add_parser(
        "clone",
        help="Get a product's whole project in its own folder, with its declared outputs pulled (--all for everything)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_brings_epilog(
            prefix, "clone", _CLONE_BRINGS, 37,
            "To bring only a product's data into your project, use `{prefix}import`.",
        ),
    )
    p_clone.add_argument("name", help="Registered data product name")
    p_clone.add_argument(
        "--dest", type=Path,
        help="Destination path (default: ./<type>_<name>)",
    )
    p_clone.add_argument("--rev", help="Branch or tag to clone at (default: HEAD)")
    clone_scope = p_clone.add_mutually_exclusive_group()
    clone_scope.add_argument(
        "--all", dest="all_outputs", action="store_true",
        help="Pull everything the product's DVC tracks, raw and intermediate "
             "data included. It can be large; licensed data still needs access.",
    )
    clone_scope.add_argument(
        "--primary", dest="primary_only", action="store_true",
        help="Pull only the primary data product (default: the declared outputs and the primary)",
    )
    clone_scope.add_argument(
        "--path", dest="paths", action="append", metavar="PATH",
        help="Pull only this tracked output — a file or directory from the "
             "product's data_products.outputs (repeatable). Default: the "
             "declared outputs and the primary.",
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
    return p_clone


def _build_parser() -> argparse.ArgumentParser:
    parser = _MintdArgumentParser(
        prog="mintd",
        description="mintd: Lightweight data product framework for research labs",
    )
    _add_global_output_flags(parser)
    # Not dest="version": publish's positional `version` shares the namespace.
    parser.add_argument("--version", dest="show_version", action="store_true",
                        help="show mintd's version, and say on stderr when a newer release is out")
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
        help=(
            "Scaffold into --path directly instead of into a new subdir "
            "(named data_<name>, prj_<name>, enclave_<name>, or the bare "
            "name for code repos)."
        ),
    )
    p_init.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing files that collide with the scaffold. Without "
            "this, init refuses and names every colliding file."
        ),
    )
    p_init.add_argument(
        "--lang",
        choices=["python", "r", "stata"],
        default="python",
        help="Primary programming language for scaffold (ignored for enclave type).",
    )
    p_init.set_defaults(_handler=_handle_init)

    # The front door: the same entries as `data import` and `data clone`.
    _add_import_parser(subs, "mintd ")
    _add_clone_parser(subs, "mintd ")

    p_check = subs.add_parser("check", help="Validate a mintd project")
    p_check.add_argument("path", nargs="?", type=Path, default=Path("."))
    p_check.add_argument("--upgrades", action="store_true")
    remote_or_staged = p_check.add_mutually_exclusive_group()
    remote_or_staged.add_argument(
        "--remote", action="store_true",
        help="also ask S3 whether every pinned file version the working tree and HEAD record is there",
    )
    remote_or_staged.add_argument(
        "--staged", action="store_true",
        help="only check the staged .dvc and dvc.lock files for version IDs dropped from unchanged files",
    )
    p_check.add_argument(
        "--all", action="store_true", dest="all_outs",
        help="with --remote, check every output dvc pushes, not only the declared ones",
    )
    p_check.set_defaults(_handler=_handle_check)

    p_data = subs.add_parser("data", help="Data commands")
    p_data_sub = p_data.add_subparsers(dest="data_command")
    _add_import_parser(p_data_sub, "mintd data ")

    p_pull = p_data_sub.add_parser("pull", help="Pull DVC data")
    p_pull.add_argument(
        "targets", nargs="*",
        help="Limit pull to these tracked files/directories, .dvc files or "
             "stage names (default: pull everything tracked)",
    )
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

    _add_clone_parser(p_data_sub, "mintd data ")

    p_push = p_data_sub.add_parser(
        "push", help="Upload DVC data, check every pinned S3 version, then commit and push the .dvc / dvc.lock files it rewrote",
    )
    p_push.add_argument(
        "targets", nargs="*",
        help="tracked files or folders (each pushes the whole output containing it), "
             ".dvc files or stage names (default: push everything tracked)",
    )
    p_push.add_argument("--remote")
    p_push.add_argument("--jobs", type=int)
    p_push.add_argument(
        "--no-commit", action="store_true",
        help="upload only; leave the .dvc / dvc.lock files uncommitted",
    )
    p_push.set_defaults(_handler=_handle_data_push)

    p_add = p_data_sub.add_parser("add", help="Add DVC data")
    p_add.add_argument("path", type=Path)
    p_add.set_defaults(_handler=_handle_data_add)

    p_verify = p_data_sub.add_parser("verify", help="Verify DVC data")
    p_verify.add_argument("targets", nargs="*")
    p_verify.add_argument("--path", type=Path, default=Path("."))
    p_verify.set_defaults(_handler=_handle_data_verify)

    p_remove = p_data_sub.add_parser(
        "remove", help="Stop tracking a file or folder with DVC; the data stays on disk")
    p_remove.add_argument("name", help="the .dvc file (or stage) to stop tracking")
    p_remove.set_defaults(_handler=_handle_data_remove)

    _add_schema_parser(p_data_sub)

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

    p_share = subs.add_parser("share", help="Ephemeral handoff lane (drop-zone)")
    p_share_sub = p_share.add_subparsers(dest="share_command")
    p_share_put = p_share_sub.add_parser(
        "put", help="Upload a file to the share drop-zone"
    )
    p_share_put.add_argument("file", type=Path, help="Local file to share")
    p_share_put.add_argument(
        "--as", dest="as_value", metavar="SUBPATH",
        help="Sub-path or target name under share/<user>/ "
             "(trailing '/' drops the file into that folder)",
    )
    p_share_put.set_defaults(_handler=_handle_share_put)
    p_share_get = p_share_sub.add_parser(
        "get", help="Download a shared file by ref"
    )
    p_share_get.add_argument("ref", help="Share ref: <user>/<sub>/<file>")
    p_share_get.add_argument(
        # Plain str (not type=Path): pathlib strips a trailing separator at
        # construction, so type=Path would silently discard the "into this
        # folder" intent of e.g. --out inbox/. _resolve_get_dest tests the raw
        # string for a trailing separator before converting to Path.
        "--out", default=None, metavar="PATH",
        help="Destination file or directory (trailing '/' drops the file into "
             "that directory; default: current directory)",
    )
    p_share_get.set_defaults(_handler=_handle_share_get)

    _add_cache_parser(subs)

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
    _erm_mutex.add_argument(
        "--source-path", dest="source_path",
        help="remove the subscription to this path",
    )
    _erm_mutex.add_argument(
        "--primary", action="store_true", dest="primary",
        help="remove the subscription to the producer's primary product",
    )
    _erm_mutex.add_argument(
        "--all", action="store_true", dest="all_outputs",
        help="remove EVERY subscription to this repo (on 'enclave add', "
             "--all means an all-outputs subscription instead)",
    )
    p_erm.add_argument("--manifest", type=Path, default=Path("enclave_manifest.yaml"))
    p_erm.set_defaults(_handler=_handle_enclave_remove)

    p_epull = p_enclave_sub.add_parser("pull", help="Fetch subscribed data")
    p_epull.add_argument("repo", nargs="?")
    p_epull.add_argument("--force", action="store_true")
    p_epull.add_argument(
        "--manifest", type=Path, default=Path(_DEFAULT_ENCLAVE_MANIFEST),
    )
    p_epull.set_defaults(_handler=_handle_enclave_pull)

    p_epkg = p_enclave_sub.add_parser(
        "package", help="Bundle downloads into a transfer archive"
    )
    p_epkg.add_argument("repo", nargs="?")
    p_epkg.add_argument("--output", type=Path, dest="output_archive")
    p_epkg.add_argument(
        "--resend",
        action="store_true",
        help="Re-bundle products already recorded in transferred[] (default: "
             "skip them). Use when a bundle was built but never arrived.",
    )
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
    _add_registry_rebuild_parser(p_registry_sub)

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
        help="the S3 bucket to check (default: the config's storage_bucket_prefix)",
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


def _timeouts_for(config: Config, timeout: float | None):
    """Effective timeouts for a `--timeout` flag: `None` keeps the config's,
    `0` lifts the transfer cap, anything else overrides it. One definition
    for `data clone` and `data import` so the flag cannot drift."""
    if timeout is None:
        return config.timeouts
    override = None if timeout == 0 else timeout
    return config.timeouts.model_copy(update={"transfer": override})


def _resolve_dvc_ops(config: Config, reporter: Optional[Reporter] = None) -> DvcOps:
    """Build a production ``SubprocessDvcOps`` from config. Handlers that only
    wrap dvc use this directly, so a machine with no ``registry_url`` is not
    refused work that never touches the catalog. Tests monkeypatch this
    function to inject fakes."""
    return SubprocessDvcOps(
        timeouts=config.timeouts,
        reporter=reporter,
        aws_profile_name=config.aws_profile_name,
    )


def _resolve_fast_sync_ops(config: Config) -> FastSyncOps | None:
    """Build production ``SubprocessFastSyncOps`` from config.
    Tests monkeypatch this function to inject fakes.

    Probes ``_s3.available()`` because ``mintd._s3`` imports cleanly even
    when boto3 is unavailable. Without this probe we'd hand back a fast-sync
    instance whose first network call would crash.
    """
    if not _s3.available():
        logger.warning("fast-sync unavailable: boto3 is not importable")
        return None
    from ._fast_sync_ops import SubprocessFastSyncOps
    return SubprocessFastSyncOps(aws_profile_name=config.aws_profile_name)


def _require_fast_sync_ops(config: Config) -> FastSyncOps:
    """Resolve fast-sync for `data pull` / `data clone`, or fail.

    Fails ONLY when boto3 is not importable -- the one degraded case
    `_resolve_fast_sync_ops` has. boto3 is a required dependency, so that
    state is a broken install and never a supported configuration; both
    callers exit 2, the posture a missing bundled `dvc` already takes.

    There used to be a third option. `data_ops.data_pull` carried a
    `fast_sync_ops is None` branch that issued one blanket `dvc pull` and
    then reported the number of targets REQUESTED as the number landed --
    `pulled 0` on a pull-all that fetched a file, `pulled 1` on a targeted
    pull that fetched nothing -- with no warning at all under `--json`. It
    was DELETED, not flag-gated: it also skipped pull-all discovery,
    per-target classification, checkout-before-pull and the entire error
    accounting, and its blanket `dvc pull` re-entered two DVC bugs
    `data_ops` carries explicit workarounds for. No lab environment runs
    mintd without boto3, so there is nothing for a
    flag to serve.

    An error, not a warning: `warn` is json-suppressed, which is how the
    deleted lane stayed completely invisible to a `--json` consumer.
    """
    fast_sync_ops = _resolve_fast_sync_ops(config)
    if fast_sync_ops is None:
        raise MintdError(
            "mintd's boto3 dependency is missing — reinstall mintd",
            hint="pip install 'dvc[s3]'",
            exit_code=2,
        )
    return fast_sync_ops


def _resolve_s3_listing_ops(config: Config):
    """Return the listing callable; tests monkeypatch this seam.

    Probes boto3 like _resolve_fast_sync_ops. Returns None when boto3
    isn't importable so the handler can emit a clean "boto3 unavailable"
    error instead of crashing on the first network call.
    """
    if not _s3.available():
        logger.warning("data ls unavailable: boto3 is not importable")
        return None
    return list_product_objects


def _resolve_cache_ops(config: Config):
    """Return the S3 client factory for the repo file cache (S3) verbs; tests
    monkeypatch this seam to inject a moto client. Probes boto3 like
    ``_resolve_fast_sync_ops`` so a missing dependency yields a clean
    ``reporter.error`` + exit 2 instead of a first-call crash."""
    if not _s3.available():
        logger.warning("cache unavailable: boto3 is not importable")
        return None
    return _s3.create_client


def _resolve_git_ops(config: Config, reporter: Optional[Reporter] = None) -> RegistryGitOps:
    """Build production ``SubprocessRegistryGitOps`` from config.
    Tests monkeypatch this function to inject fakes.
    """
    return SubprocessRegistryGitOps(timeouts=config.timeouts, reporter=reporter)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_check(args: argparse.Namespace) -> int:
    if args.all_outs and not args.remote:
        raise SystemExit("mintd check: --all goes with --remote")
    if args.staged:  # offline and fast: no catalog, no S3, no dvc
        if args.upgrades:  # a flag that did nothing would read as checked
            raise SystemExit("mintd check: --upgrades doesn't go with --staged")
        from .check import check_staged

        return _render_findings(check_staged(args.path), json_out=args.json_out)
    config = Config.load()
    client: CatalogClient | None = None
    # Resolved for every check, not just --upgrades: an enclave consumer's
    # approved products need the catalog to validate, and publish/register
    # already validate them. Leaving plain check weaker would mean "check is
    # green" and "publish will not be blocked" stop meaning the same thing.
    try:
        client = _resolve_catalog_client(config)
    except ConfigError:
        # Let the manifest walker emit `catalog_unresolved` findings —
        # surface what's missing without pre-validating.
        client = None
    findings = check_project(args.path, upgrades=args.upgrades, client=client, remote=args.remote,
                             all_outs=args.all_outs, aws_profile=config.aws_profile_name)
    return _render_findings(findings, json_out=args.json_out)


def _handle_init(args: argparse.Namespace) -> int:
    from ._config import Config
    from ._console import Reporter
    from .init import (
        _prompt_classification,
    )

    reporter = getattr(args, "_reporter", None) or Reporter()
    # Slice 30 P1 (reviewer-flagged): enclave projects don't use DVC storage
    # wiring and must not require a TTY. Skip the classification prompt.
    classification: str | None = None
    slug: str | None = None
    if args.project_type != "enclave":
        classification, slug = _prompt_classification(reporter=reporter)

    try:
        config = Config.load()
    except Exception:
        config = Config()  # type: ignore[call-arg]
    bucket = config.storage_bucket_prefix
    endpoint = config.storage_endpoint
    # Slice 30: write the AWS profile into .dvc/config so consumers
    # running raw `dvc pull` (outside mintd) pick up the right
    # credentials. Config.aws_profile_name returns "mintd" iff the shared
    # credentials file ($AWS_SHARED_CREDENTIALS_FILE when set, else
    # ~/.aws/credentials) has a [mintd] section; otherwise None.
    profile = config.aws_profile_name

    project_path, written = init_project(
        project_type=args.project_type,
        name=args.name,
        target_dir=args.path,
        language=args.lang,
        use_current_repo=args.use_current_repo,
        force=args.force,
        classification=classification,
        slug=slug,
        bucket=bucket,
        endpoint=endpoint,
        profile=profile,
        reporter=reporter,
    )
    # Render paths relative to cwd when possible so the user sees the subdir.
    from ._paths import inside

    cwd = Path.cwd()
    rel = inside(cwd, project_path, read="follow", root_ok=True) or project_path
    for p in written:
        print(f"created: {inside(cwd, p, read='follow', root_ok=True) or p}")
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
    name_hint = args.targets[0] if args.targets else "<name>"
    project_path = _require_project(
        args.path,
        hint=f"mintd data pull refreshes this project's tracked data\n"
             f"to get a published product: mintd clone {name_hint}",
    )
    reporter = args._reporter
    config = Config.load()
    dvc_ops = _resolve_dvc_ops(config, reporter)
    fast_sync_ops = _require_fast_sync_ops(config)
    summary = data_pull(
        project_path=project_path,
        targets=args.targets or None,
        dvc_ops=dvc_ops,
        fast_sync_ops=fast_sync_ops,
        remote=args.remote,
        jobs=args.jobs,
        extra_dvc_args=args.dvc_args or None,
        reporter=reporter,
        aws_profile_name=config.aws_profile_name,
    )
    if summary.error_count:
        # Targets data_pull did not land in the workspace. Each was already
        # reported via reporter.error with its own hint inside data_pull;
        # summarize and exit non-zero so scripts/CI can't mistake a partial
        # pull for success.
        if reporter.json_mode:
            reporter.result({
                "pulled": summary.targets_pulled,
                "bytes": summary.total_bytes,
                "elapsed_s": round(summary.elapsed_s, 2),
                "errors": summary.error_count,
            })
        else:
            reporter.error(
                f"pull incomplete: {summary.error_count} target(s) failed, "
                f"{summary.targets_pulled} file(s) pulled",
                hint="each failed target's error above says what to do next",
            )
        return 1
    if summary.total_bytes:
        msg = (
            f"✓ pulled {summary.targets_pulled} file(s) "
            f"({_human_bytes(summary.total_bytes)}) in {_format_duration(summary.elapsed_s)}"
        )
    else:
        msg = f"✓ pulled {summary.targets_pulled} file(s) in {_format_duration(summary.elapsed_s)}"
    if reporter.json_mode:
        reporter.result({
            "pulled": summary.targets_pulled,
            "bytes": summary.total_bytes,
            "elapsed_s": round(summary.elapsed_s, 2),
        })
    else:
        reporter.success(msg)
    return 0


def _skipped_line(skipped: list[str]) -> str:
    """The warning naming every pointer DVC skips because of .gitignore."""
    it = "it" if len(skipped) == 1 else "them"
    return f"skipped {', '.join(skipped)}: DVC never pushes or pulls {it}, because .gitignore rules exclude {it}"


def _handle_data_push(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    dvc_ops = _resolve_dvc_ops(config, reporter)
    # dvc runs from the project it finds by walking up to `.dvc/`; targets are
    # read relative to where the user typed them.
    root = dvc_root(Path.cwd())
    try:
        with reporter.status("Pushing data to DVC..."):
            summary = data_push(
                project_path=root,
                cwd=Path.cwd(),
                targets=args.targets or None,
                dvc_ops=dvc_ops,
                remote=args.remote,
                jobs=args.jobs,
                aws_profile=config.aws_profile_name,
                memo_path=config.resolved_cache_dir() / "proven-versions.sqlite",
            )
    except S3AccessDenied as e:
        raise MintdError(
            f"S3 refused to show the pinned versions, so the push can't be checked: {e}",
            hint="check AWS credentials: mintd config validate",
        ) from e
    # Importers read the producer's pushed commit, never this working tree,
    # and a version-aware push writes S3 version IDs into exactly these files,
    # so a pointer that differs from HEAD is one no importer can see yet.
    # None, not []: a JSON reader must not mistake "git could not answer"
    # (outside a repo, say) for a clean tree, and `warn` is silent under --json.
    git_ops = _resolve_git_ops(config)
    # DVC never collects a .dvc file that .gitignore excludes, so it never
    # pushes or pulls one: say which ones were left out, and keep them out of
    # the check and the commit, which read git status rather than DVC's view.
    skipped = gitignored_pointers(root)
    if skipped:
        reporter.warn(_skipped_line(skipped))
    pointers: list[str] | None
    try:
        pointers = [p for p in git_ops.uncommitted_pointers(root) if p not in skipped]
    except GitOpError as exc:
        reporter.warn(f"could not check .dvc / dvc.lock files against HEAD: {exc.stderr.strip() or exc}")
        pointers = None
    found = pointers
    outcome: PointerCommit | None = None
    unpushed: tuple[int, str] | None = None
    gate = None
    prepared = summary.verification
    if prepared is not None:
        # The only verdict that may print a ✓ on a version-aware remote: every
        # pinned version this push answers for resolves and holds its bytes.
        for line in prepared.report:
            reporter.info(line)
        try:
            head: dict[str, str | None] = {}
            for p in pointers or []:
                try:
                    head[p] = git_ops.head_text(root, p)
                except GitOpError:
                    head[p] = None  # unreadable: judge every out it records
            gate = judge_push(prepared, pointers, head=head, dvc_uploaded_nothing=summary.up_to_date)
        except S3AccessDenied as e:
            raise MintdError(f"S3 refused to show the pinned versions: {e}",
                             hint="check AWS credentials: mintd config validate") from e
        if not gate.ok:
            if reporter.json_mode:
                reporter.result({"remote": summary.remote, "verify": gate.counts, "errors": gate.lines,
                                 "uncommitted_pointers": pointers})
                return 1
            reporter.error(
                f"the push isn't verified on {gate.location}: " + "; ".join(gate.lines),
                hint="nothing was committed. Re-run mintd data push; if a file is named as not in "
                     "this push, push it too",
            )
            return 1
    if pointers and not args.no_commit:
        with reporter.status("Committing and pushing pointer files..."):
            outcome = commit_pushed_pointers(
                root, pointers, git_ops=git_ops, remote=args.remote,
                selection=summary.selection, cwd=Path.cwd(),
                frozen=gate.frozen if gate is not None else None,
            )
        if outcome.state not in ("left", "blocked"):
            try:
                pointers = [p for p in git_ops.uncommitted_pointers(root) if p not in skipped]
            except GitOpError:
                pointers = None
    elif pointers == []:
        # Nothing to commit, but a pointer commit a rejected push left local
        # still reaches no importer: no ✓ while the branch is ahead of its upstream.
        try:
            info = git_ops.branch_info(root)
        except GitOpError:
            info = None
        if info and info.ahead:
            same_name = info.merge_ref is not None and info.merge_ref.removeprefix("refs/heads/") == info.branch
            unpushed = (info.ahead, f"{info.remote}/{info.branch}" if same_name else "any remote")

    if found is None:
        state = "unchecked"
    elif not found:
        state = "unpushed" if unpushed else "clean"
    elif outcome is None:
        state = "skipped"
    else:
        state = outcome.state
    failed = state in ("blocked", "commit_failed", "push_failed")
    # Re-check the judged bytes before any ✓ or advice to commit by hand,
    # --no-commit and nothing-to-commit included: what was verified is what
    # is on disk. A commit made checks its own blobs instead.
    drifted = gate.drifted(root) if gate is not None and (outcome is None or outcome.state == "left") else []
    if drifted:
        reporter.error(
            f"{abbreviate(drifted)} changed while the push was being checked",
            hint="re-run mintd data push",
        )
        return 1

    if reporter.json_mode:
        reporter.result(
            {
                "remote": summary.remote,
                "pushed": summary.pushed,
                "bytes": summary.bytes,
                "elapsed_s": round(summary.elapsed_s, 2),
                "up_to_date": summary.up_to_date,
                "uncommitted_pointers": pointers,
                **({"verify": gate.counts} if gate is not None else {}),
                "git": {
                    "state": state,
                    "commit": outcome.commit if outcome else None,
                    "pushed_to": outcome.pushed_to if outcome else None,
                    "reason": (outcome.reason if outcome else None)
                    or (f"{unpushed[0]} commit(s) not on {unpushed[1]}" if unpushed else None),
                    "blocked": list(outcome.blocked) if outcome else [],
                    "note": outcome.note if outcome else None,
                },
            },
            pretty=_pretty_data_push,
        )
        return 1 if failed else 0

    remote_clause = f" → s3://{summary.remote}"
    duration = _format_duration(summary.elapsed_s)
    if gate is not None:
        line = f"{gate.success_line} in {duration}"
    elif summary.up_to_date:
        line = f"already up to date{remote_clause} in {duration}"
    else:
        count_clause = (
            f" {summary.pushed} object(s)" if summary.pushed is not None else ""
        )
        size_clause = (
            f" ({_human_bytes(summary.bytes)})" if summary.bytes else ""
        )
        line = f"pushed{count_clause}{size_clause}{remote_clause} in {duration}"
    if found is None:
        # The warning above says why: no ✓ over pointers nobody checked.
        reporter.success(line)
    elif not found:
        if unpushed:
            reporter.warn(
                f"{line}, but {unpushed[0]} commit(s) on this branch are not on {unpushed[1]} yet "
                "— git push; importers read your pushed commit"
            )
        else:
            reporter.success(f"✓ {line}")
    elif outcome is None or outcome.state == "left":
        reporter.warn(
            f"{line}, but these pointer files differ from the last commit: "
            f"{', '.join(found)} — commit and push them; importers read your pushed commit, not these files"
        )
        if outcome is not None:
            reporter.warn(f"not committed: {outcome.reason}")
    elif outcome.state == "blocked":
        reporter.error(
            f"{line}, but nothing was committed: importers could not pull what these "
            f"pointer files record: {abbreviate(list(outcome.blocked))}",
            hint="the data is on S3. Push the named outputs too (mintd data push <path>); "
                 "re-record a damaged folder with `dvc commit -f <its .dvc file or stage>` "
                 "and push again; or pass --no-commit to leave git alone",
        )
    elif outcome.state == "commit_failed":
        reporter.error(
            f"{line}; the data is on S3, but git commit failed: {outcome.reason}",
            hint=(
                f"git -C {shlex.quote(str(root))} commit -m {shlex.quote(COMMIT_MESSAGE)} -- "
                f"{' '.join(shlex.quote(p) for p in found)} && git -C {shlex.quote(str(root))} push"
            ),
        )
    elif outcome.state == "push_failed":
        reporter.error(
            f"{line}; the data is on S3 and committed locally as {outcome.commit}, "
            f"but git push failed: {outcome.reason}",
            hint=outcome.hint,
        )
    else:
        earlier = f" with {outcome.earlier} earlier unpushed commit(s)" if outcome.earlier else ""
        reporter.success(
            f"✓ {line}; committed {outcome.commit} ({abbreviate(list(outcome.pointers))}) "
            f"and pushed it to {outcome.pushed_to}{earlier}"
        )
        if outcome.note:
            reporter.info(outcome.note)
    return 1 if failed else 0


def _share_boto3_available() -> None:
    """Probe ``_s3.available()`` with the same posture as ``_resolve_fast_sync_ops``:
    a clean error + exit 2, never a traceback from a later
    ``_s3.create_client`` call whose ``boto3`` is ``None``."""
    if not _s3.available():
        logger.warning("share unavailable: boto3 is not importable")
        raise MintdError(
            "share requires boto3, which is not installed",
            hint="install it: pip install 'dvc[s3]'",
            exit_code=2,
        )


def _handle_share_put(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _share_boto3_available()
    user, source = resolve_share_user(config)
    if source == "author":
        reporter.info(
            f"💡 sharing as '{user}' — set share_user in "
            "~/.config/mintd/config.yaml to pin this"
        )
    result = share_put(
        local_path=args.file,
        user=user,
        config=config,
        reporter=reporter,
        as_value=args.as_value,
    )
    if reporter.json_mode:
        reporter.result(
            {
                "shared": result.name,
                "key": result.key,
                "ref": result.ref,
                "bytes": result.bytes,
                "elapsed_s": round(result.elapsed_s, 2),
            }
        )
    else:
        reporter.success(
            f"✓ shared {result.name} ({_human_bytes(result.bytes)}) → "
            f"{result.key} in {_format_duration(result.elapsed_s)}"
        )
        # The line a teammate runs to grab it — the point of the share.
        # reporter.success (not info): visible at default verbosity, still
        # suppressed under --json (the ref rides the json payload above).
        reporter.success(f"  fetch it with: mintd share get {result.ref}")
    return 0


def _handle_share_get(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    _share_boto3_available()
    result = share_get(
        ref=args.ref,
        config=config,
        reporter=reporter,
        out=args.out,
    )
    if reporter.json_mode:
        reporter.result(
            {
                "got": result.name,
                "ref": result.ref,
                "dest": str(result.dest),
                "bytes": result.bytes,
                "elapsed_s": round(result.elapsed_s, 2),
            }
        )
    else:
        reporter.success(
            f"✓ got {result.name} ({_human_bytes(result.bytes)}) → "
            f"{result.dest} in {_format_duration(result.elapsed_s)}"
        )
    return 0


def _require_project(project_path: Path, *, hint: str, or_above: bool = False) -> Path:
    """Stop a verb that works on a project before it reads the catalog or runs
    dvc, when ``project_path`` isn't one: a mintd project has ``.dvc/`` at its
    root. Pull and the cache verbs take their ``--path`` as that root. With
    ``or_above`` (import, which runs anywhere in a project, as dvc does), the
    project may be any folder above, found by ``dvc_root`` as dvc finds it.
    Returns the project's root."""
    resolved = project_path.resolve()
    root = dvc_root(resolved) if or_above else resolved
    if not (root / ".dvc").is_dir():
        where = f"{resolved} or above" if or_above else str(resolved)
        raise MintdError(f"not inside a mintd project (no .dvc/ at {where})", hint=hint)
    return root


def _cache_project_preflight(args: argparse.Namespace) -> tuple[Reporter, Path, Config, Any]:
    """Shared preflight for the three cache verbs: project probe + config +
    boto3-backed factory. Returns ``(reporter, project_path, config, factory)``."""
    reporter = args._reporter
    project_path = _require_project(
        args.path,
        hint="the repo file cache (S3) lives in a project's own bucket prefix\ncd into the project\n"
             "for an ad-hoc handoff: mintd share put",
    )
    config = Config.load()
    factory = _resolve_cache_ops(config)
    if factory is None:
        raise MintdError(
            "the repo file cache (S3) requires boto3, which is not installed",
            hint="install it: pip install 'dvc[s3]'",
            exit_code=2,
        )
    return reporter, project_path, config, factory


def _handle_cache_push(args: argparse.Namespace) -> int:
    reporter, project_path, config, factory = _cache_project_preflight(args)
    summary = cache_push(
        project_path=project_path,
        paths=args.paths,
        config=config,
        reporter=reporter,
        remote=args.remote,
        jobs=args.jobs,
        dry_run=args.dry_run,
        s3_client_factory=factory,
    )
    return _render_cache_push(summary, reporter)


def _render_cache_push(summary: CachePushSummary, reporter: Reporter) -> int:
    failed = summary.failed
    for o in failed:
        reporter.error(
            f"upload failed for {o.rel}: {o.reason}",
            hint=o.hint,
        )
    if reporter.json_mode:
        reporter.result(
            {
                "cached": summary.total,
                "uploaded": summary.uploaded,
                "unchanged": summary.unchanged,
                "failed": len(failed),
                "bytes": summary.uploaded_bytes,
                "prefix": summary.key_prefix,
                "dry_run": summary.dry_run,
                "elapsed_s": round(summary.elapsed_s, 2),
            }
        )
        return 1 if failed else 0
    if failed:
        reporter.error(
            f"push incomplete: {len(failed)} file(s) failed, "
            f"{summary.uploaded} uploaded, {summary.unchanged} unchanged",
            hint="each failed file has a targeted retry command above",
        )
        return 1
    verb = "would upload" if summary.dry_run else "uploaded"
    dur = _format_duration(summary.elapsed_s)
    dest = f" → s3://{summary.bucket}/{summary.key_prefix}/"
    reporter.success(
        f"✓ pushed {summary.total} file(s) to the repo file cache (S3) "
        f"({_human_bytes(summary.uploaded_bytes)} · {summary.uploaded} {verb}, "
        f"{summary.unchanged} unchanged){'' if summary.dry_run else dest} in {dur}"
    )
    if not summary.dry_run and summary.uploaded:
        reporter.success(
            "💡 the repo file cache (S3) is durable but unpinned — for versioned, "
            "citable outputs use: mintd data push"
        )
    return 0


def _handle_cache_pull(args: argparse.Namespace) -> int:
    reporter, project_path, config, factory = _cache_project_preflight(args)
    summary = cache_pull(
        project_path=project_path,
        config=config,
        reporter=reporter,
        paths=args.paths,
        remote=args.remote,
        jobs=args.jobs,
        force=args.force,
        s3_client_factory=factory,
    )
    return _render_cache_pull(summary, reporter)


def _render_cache_pull(summary: CachePullSummary, reporter: Reporter) -> int:
    failed = summary.failed
    for o in failed:
        reporter.error(f"pull failed for {o.rel}: {o.reason}", hint=o.hint)
    skipped = summary.skipped_existing
    for o in skipped:
        reporter.warn(f"kept local (differs, not overwritten): {o.rel} — {o.hint}")
    if reporter.json_mode:
        reporter.result(
            {
                "pulled": summary.pulled,
                "unchanged": summary.unchanged,
                "skipped_existing": len(skipped),
                "failed": len(failed),
                "bytes": summary.pulled_bytes,
                "paths": summary.paths,
                "elapsed_s": round(summary.elapsed_s, 2),
            }
        )
        return 1 if failed else 0
    if failed:
        reporter.error(
            f"pull incomplete: {len(failed)} file(s) failed, "
            f"{summary.pulled} pulled, {summary.unchanged} unchanged, "
            f"{len(skipped)} kept-local",
            hint="a refused key is an admin conversation; retry after it is cleaned up",
        )
        return 1
    if summary.pulled == 0 and summary.unchanged == 0 and not skipped:
        # empty listing already warned + returned exit 0 in the ops layer
        return 0
    skip_note = f", {len(skipped)} kept-local (use --force to overwrite)" if skipped else ""
    reporter.success(
        f"✓ pulled {summary.pulled} file(s) from the repo file cache (S3) "
        f"({_human_bytes(summary.pulled_bytes)}) to their repo paths"
        f"{skip_note} in {_format_duration(summary.elapsed_s)}"
    )
    reporter.success(
        "💡 the repo file cache (S3) is not DVC-tracked — 'mintd data pull' "
        "handles the versioned outputs"
    )
    return 0


def _handle_cache_ls(args: argparse.Namespace) -> int:
    reporter, project_path, config, factory = _cache_project_preflight(args)
    repo = resolve_repo_remote(project_path, args.remote)
    with reporter.status("Listing the repo file cache (S3)..."):
        _keys, result = select_cache_objects(
            project_path, repo, args.paths, verb="ls",
            aws_profile_name=config.aws_profile_name, factory=factory,
        )

    payload = _data_ls_payload("cache", result, include_versions=False)
    # Overwrite the built-in `mintd data pull` hint with the cache-pull one. Keys
    # are full repo paths and cache pull takes a file's path, so name the file,
    # quoted so a path with a space pastes as one.
    first_file = next((o for o in result.objects if not o.is_dir), None)
    if first_file is not None:
        payload["hint"] = f"mintd cache pull {shlex.quote(first_file.key)}"
    no_truncate = getattr(args, "no_truncate", False)
    reporter.result(
        payload,
        pretty=lambda p: _pretty_data_ls(
            p, name="cache", versions=False, no_truncate=no_truncate,
        ),
    )
    return 0


def _handle_data_add(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    dvc_ops = _resolve_dvc_ops(config, reporter)
    with _hint(DvcOpError, "check the path exists inside a DVC project; 'mintd init' scaffolds one"):
        produced = data_add(args.path, project_path=Path("."), dvc_ops=dvc_ops)
    print(str(produced))
    return 0


def _handle_data_schema_generate(args: argparse.Namespace) -> int:
    from .schema_ops import (
        ColumnNamesLookLikeData,
        SchemaExtraNotInstalled,
        find_project_root,
        generate_schema_file,
    )

    reporter = getattr(args, "_reporter", None) or Reporter()
    with _hint(FileNotFoundError, "cd into a mintd project (one with metadata.json), or pass --project-dir"):
        project_dir = args.project_dir or find_project_root()

    data_dir = args.data_dir or (project_dir / "data" / "final")
    output = args.output or (project_dir / "schemas" / "v1" / "schema.json")

    with (
        _hint((ValueError, RuntimeError), "check file format support (.dta, .csv, .json, .parquet) and integrity"),
        _hint(ColumnNamesLookLikeData),  # a ValueError, but it names its own fix
        _hint(FileNotFoundError, f"add supported data files under {data_dir} or pass --data-dir"),
        _hint(SchemaExtraNotInstalled, _reinstall_hint()),
        reporter.status("Generating schema..."),
    ):
        generate_schema_file(
            data_dir, output, recursive=args.recursive,
            allow_numeric_column_names=args.allow_numeric_column_names,
        )

    reporter.success(f"Schema saved to: {output}")
    return 0


def _handle_data_verify(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    dvc_ops = _resolve_dvc_ops(config, reporter)
    with _hint(DvcOpError, "retry with --dvc-arg=-v for verbose DVC output"), reporter.status("Verifying DVC data..."):
        status_map = data_verify(
            project_path=args.path,
            targets=args.targets or None,
            dvc_ops=dvc_ops,
        )
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
    dvc_ops = _resolve_dvc_ops(config, reporter)
    with _hint(DvcOpError, "check the name; 'mintd data verify' lists tracked outputs"):
        left = data_remove(args.name, project_path=Path("."), dvc_ops=dvc_ops)
    print(f"removed: {args.name}")
    if not left:
        return 0
    def shown(paths: list[str]) -> str:
        return ", ".join(f"{p}/" if Path(p).is_dir() else p for p in paths)

    reporter.info(f"DVC no longer tracks it; the data stays on disk: {shown(left)}")
    # dvc remove also drops the pointer's .gitignore line, so git may now see
    # the data as untracked, ready for the next `git add` to commit it.
    # Untracked: what git lists, less what it ignores and what HEAD tracks.
    try:
        git = _resolve_git_ops(config, reporter)
        tracked = set(git.head_files(Path("."))) if git.has_commit(Path(".")) else set()
        untracked = set(git.listed_files(Path("."), left)) - set(git.ignored_files(Path("."), left)) - tracked
    except GitOpError:  # not a git repository, or git failed: nothing to warn about
        untracked = set()
    seen = [p for p in left if any(f == p or f.startswith(f"{p}/") for f in untracked)]
    if seen:
        reporter.warn(f"git now sees {len(untracked)} file(s) in {shown(seen)} as untracked: a `git add` would commit them")
        reporter.warn(f"to keep them out: delete them, or add {shown(seen)} to .gitignore")
    return 0


def _handle_data_ls(args: argparse.Namespace) -> int:
    reporter = args._reporter
    config = Config.load()
    client = _resolve_catalog_client(config)
    listing_fn = _resolve_s3_listing_ops(config)
    if listing_fn is None:
        raise MintdError("boto3 not installed", hint="pip install 'boto3'", exit_code=2)

    entry = client.fetch(args.name)

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

    with (
        _hint(ValueError, exit_code=2),
        _hint(BucketNotFound, "check storage.bucket / storage.endpoint in the producer's metadata.json"),
        reporter.status(f"Listing {args.name} on S3..."),
    ):
        result = listing_fn(
            bucket=bucket, prefix=prefix, endpoint=endpoint,
            sub_path=args.sub_path,
            recursive=args.recursive,
            include_versions=args.versions,
            aws_profile_name=config.aws_profile_name,
        )

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
        # Cache-redteam P2: object keys are remote-controlled (any teammate /
        # `aws s3 cp` can plant one); neutralize control chars before writing
        # to stdout so a key can't forge table rows or inject ANSI. JSON mode
        # is already safe (json.dumps escapes control chars).
        key = neutralize_control_chars(o["key"])
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
        # Cache-redteam P2: the hint embeds a remote-controlled key; neutralize
        # so the suggested command can't carry a smuggled newline/ANSI.
        lines.append(
            f"\n💡 Download a single file:\n   {neutralize_control_chars(payload['hint'])}"
        )
    return "\n".join(lines)


def _handle_data_clone(args: argparse.Namespace) -> int:
    reporter = args._reporter
    t0 = time.monotonic()
    config = Config.load()
    effective_timeouts = _timeouts_for(config, args.timeout)

    client = _resolve_catalog_client(config)
    dvc_ops = SubprocessDvcOps(
        timeouts=effective_timeouts,
        reporter=reporter,
        aws_profile_name=config.aws_profile_name,
    )
    registry_git_ops = SubprocessRegistryGitOps(timeouts=effective_timeouts, reporter=reporter)
    fast_sync_ops = _require_fast_sync_ops(config)

    reporter.debug(f"resolved registry_url={config.registry_url}")
    with (
        _hint(ValueError),
        _hint(MissingPrimaryDataProduct, "drop --primary to pull the declared outputs"),
        _hint(
            UnknownProductPath,
            "pass --path with one of the tracked outputs listed above\n"
            "or pass --all to pull everything DVC tracks",
        ),
        _hint(ImportDestinationExists, "pass --dest <path> or remove the existing directory"),
        _hint(ProducerError, _producer_unreachable),
    ):
        clone_result = clone_and_pull_product(
            client, dvc_ops, registry_git_ops, fast_sync_ops,
            name=args.name,
            dest=args.dest,
            rev=args.rev,
            primary_only=args.primary_only,
            paths=args.paths or None,
            all_outputs=args.all_outputs,
            jobs=args.jobs,
            extra_dvc_args=args.dvc_args or None,
            reporter=reporter,
            aws_profile_name=config.aws_profile_name,
        )

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
    if clone_result.pull_error_count:
        # The clone succeeded but the post-clone pull could not serve
        # every target — mirror
        # _handle_data_pull: no ✓ line, exit non-zero so scripts/CI can't
        # mistake a partial clone for success. Each failed target was
        # already reported via reporter.error with its own hint.
        if reporter.json_mode:
            payload["errors"] = clone_result.pull_error_count
            reporter.result(payload, pretty=_pretty_data_clone)
        else:
            reporter.error(
                f"cloned {args.name} → {dest.name}/ but pull incomplete: "
                f"{clone_result.pull_error_count} target(s) failed",
                hint="each failed target's error above says what to do next; "
                     f"run any retry command it gives inside {dest.name}/",
            )
        return 1
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
    primary = _cloned_metadata_entry(dest, fallback={}).get("data_products", {}).get("primary")
    return primary if isinstance(primary, str) else None


def _measure_clone_result(dest: Path) -> tuple[int, int]:
    """Sum file count and total bytes under dest, skipping the .git and
    .dvc trees. Returns (0, 0) if dest is unreadable (e.g., test stub).

    .dvc/ holds the local cache: every pulled byte exists there AND as its
    workspace materialization, so counting both doubled the ✓-line's
    files/bytes (e.g. 37 GB of product reported as 74.3 GB)."""
    from ._paths import rel as rel_to

    files = 0
    total = 0
    try:
        for p in dest.rglob("*"):
            if not p.is_file():
                continue
            # Skip clone metadata: .git internals and the .dvc dir
            # (cache/tmp/config) — neither is data product content.
            parts = rel_to(dest, p).split("/")
            if ".git" in parts or ".dvc" in parts:
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
    # `--rev` contradicts "move to HEAD"; bumping every output at once is
    # not a thing. `--path` IS allowed with --bump (it selects the row),
    # but only one — silently bumping the first of several is the exact
    # last-writer-wins defect D-A killed.
    if args.bump and (args.rev or args.all_outputs):
        args._parser.error("--bump cannot be combined with --rev or --all")
    if args.bump and len(args.import_path) > 1:
        args._parser.error("--bump takes at most one --path")
    # Import adds a product to the project it runs in, from any folder inside
    # it (it has no --path), so outside one it stops here, before the catalog
    # lookup or any dvc call. It still runs from ".", as before.
    _require_project(
        Path("."),
        or_above=True,
        hint=f"mintd import adds a product to a project\nto get the whole product: mintd clone {args.name}\n"
             f"to start a project: mintd init",
    )

    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    config = config.model_copy(update={"timeouts": _timeouts_for(config, args.timeout)})
    client = _resolve_catalog_client(config)
    dvc_ops = _resolve_dvc_ops(config, reporter)
    dvc_args = list(args.dvc_args)
    if args.jobs:
        # `dvc import` takes `-j`; riding extra_args keeps the DvcOps
        # protocol (and its fakes) untouched.
        dvc_args.extend(["-j", str(args.jobs)])

    if args.bump:
        try:
            with (
                _hint(DvcOpError, "check connectivity then retry: mintd config validate"),
                reporter.status(f"Bumping {args.name}..."),
            ):
                result = bump_import(
                    client,
                    dvc_ops,
                    project_path=Path("."),
                    name=args.name,
                    path=args.import_path[0] if args.import_path else None,
                    extra_dvc_args=dvc_args or None,
                )
        except BumpBlocked as exc:
            return _render_bump_blocked(exc)
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

    with (
        # `_require_repo_url`'s bare ValueError: `CatalogEntry` is
        # `extra="allow"` with no required fields, so an entry with no
        # `repository.github_url` reaches it.
        _hint(ValueError),
        # The containment guard's raise site: a leading slash in `--path`, or
        # a catalog entry carrying an absolute/`..` output path.
        _hint(UnknownProductPath, "pass --path relative to the producer's repo root"),
        # `--rev` with no `--path` reads the producer's metadata before dvc runs.
        _hint(ProducerError, _producer_unreachable),
        _hint(DvcOpError, "retry with --dvc-arg=-v for verbose DVC output"),
    ):
        produced = import_product(
            client,
            dvc_ops,
            args.name,
            cwd=Path("."),
            dest_root=args.dest_root,
            # `or None` is load-bearing: an empty list is not None, so
            # without it `_resolve_paths` returns [] and a plain import
            # exits 0 having written nothing (`data clone` carries the
            # same guard).
            path=args.import_path or None,
            rev=args.rev,
            all_outputs=args.all_outputs,
            force=args.force,
            extra_dvc_args=dvc_args or None,
            reporter=reporter,
            registry_git_ops=_resolve_git_ops(config),
        )
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
    # For pretty mode, use the grouped table (grouped by project_type with
    # the canonical data → code → project → enclave order, description
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
    with _hint(
        FileNotFoundError,
        "create one with 'mintd enclave add <repo> --pin <sha>', or pass --manifest <path>",
        message=f"enclave_manifest.yaml not found at {args.manifest}",
    ):
        manifest = EnclaveManifest.load(args.manifest)
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
        path = subscription_label(ap)
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
        with (
            # `--force` skips the check_project reachability gate and resolves
            # the producer's HEAD directly, so an unreachable/broken producer or
            # a malformed catalog entry (no github_url) surfaces here rather
            # than as a BumpBlocked finding.
            _hint(
                (ProducerError, ValueError),
                lambda e: (_producer_unreachable(e) if isinstance(e, ProducerError) else None)
                or "check the producer's catalog entry (repo URL/pin) and that it's reachable at HEAD",
            ),
            # A pydantic ValidationError is a ValueError: inside the clause
            # above, so it keeps this hint. The manifest is human-editable.
            _hint(ValidationError, "enclave_manifest.yaml is malformed; fix the reported field"),
            _hint(ImportNotFound, "'mintd enclave list' to see subscribed repos"),
            _hint(PrimaryRemovedAtHead, "pin to an older SHA or unsubscribe with 'mintd enclave remove'"),
            reporter.status(f"Bumping {args.name}..."),
        ):
            result = enclave_bump(
                client,
                manifest_path=args.manifest,
                name=args.name,
                force=args.force,
            )
    except BumpBlocked as exc:
        return _render_bump_blocked(exc)
    if result is None:
        print("up to date")
    else:
        print(f"bumped: {result}")
    return 0


def _handle_enclave_add(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    with (
        _hint(
            (ProducerError, ValueError),
            lambda e: (_producer_unreachable(e) if isinstance(e, ProducerError) else None)
            or "check the repo/pin arguments",
        ),
        # `--path` is not a flag on this parser; following the old hint exited
        # 64 with `unrecognized arguments`.
        _hint(MissingPrimaryDataProduct, "pass --source-path <output> or --all"),
    ):
        path = enclave_add(
            client,
            manifest_path=args.manifest,
            name=args.repo,
            pin=args.pin,
            source_path=args.source_path,
            all_=args.all_outputs,
        )
    # Re-load to print the just-added entry's resolved pin.
    manifest = EnclaveManifest.load(path)
    ap = manifest.approved_products[-1]
    print(f"subscribed: {ap.repo}@{ap.pin[:7]} (path: {subscription_label(ap)})")
    # "this add INHERITED a pin", not "the repo now has >1 row": those differ
    # whenever the sibling rows carry a different pin — a hand-edited manifest,
    # or the empty-pin case, where enclave_add deliberately resolves HEAD
    # instead of inheriting. Warning there names a pin that was never recorded.
    inherited = any(
        a is not ap and a.repo == ap.repo and a.pin == ap.pin
        for a in manifest.approved_products
    )
    if inherited:
        # D1: this add INHERITED the repo's recorded pin, it did not resolve
        # HEAD. If the path did not exist at that pin the add still succeeds and
        # `pull` is what fails, so name the remedy now rather than in the notes.
        # Bare `bump` suffices since the md5 drift rule: a path published at
        # HEAD but absent at the pin reports as drift, so the bump moves the
        # pin without `--force` (which skips every guard and was only ever a
        # workaround for the retired short-circuit).
        reporter.warn(
            f"inherited the pin recorded for {ap.repo} ({ap.pin[:7]}) - one pin per repo. "
            f"If {subscription_label(ap)} is newer than that pin, "
            f"'mintd enclave pull' will fail; run "
            f"'mintd enclave bump {ap.repo}' to move every subscription."
        )
    return 0


def _handle_enclave_remove(args: argparse.Namespace) -> int:
    config = Config.load()
    client = _resolve_catalog_client(config)
    with _hint(ImportNotFound, "'mintd enclave list' to see subscribed repos"):
        enclave_remove(
            client,
            manifest_path=args.manifest,
            name=args.repo,
            source_path=args.source_path,
            all_=args.all_outputs,
            primary=args.primary,
        )
    msg = f"removed: {args.repo}"
    if args.source_path:
        msg += f" (source_path={args.source_path})"
    elif args.primary:
        msg += " (primary)"
    print(msg)
    return 0


_DEFAULT_ENCLAVE_MANIFEST = "enclave_manifest.yaml"


def _handle_enclave_pull(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    # Resolved ONCE and used for the comparison, the message and the hint.
    # `Path.cwd()` is physical by definition (POSIX getcwd resolves symlinks),
    # so printing an unresolved `enclave_dir` beside it showed the user two
    # roots that look unrelated -- "(/tmp/x/enclave), not from /private/tmp/x"
    # on any macOS, where /tmp IS a symlink.
    enclave_dir = args.manifest.parent.resolve()
    if enclave_dir != Path.cwd():
        # Deliberately narrow. `dvc import` acts on the repo it runs in, and
        # this is the custody lane: `--manifest` names a file, it is not a
        # licence to reach into another directory. The library is correct from
        # anywhere now; the CLI still declines to widen the flag.
        #
        # The hint is BUILT AS ARGV and joined by shlex, never concatenated:
        # a list makes a dropped flag visible and `shlex.join` makes an
        # unquoted path (cloud drives hand out "My Drive") impossible.
        # `--manifest` carries only when it is not the default, and as a NAME,
        # because the hint has already `cd`'d there.
        retry = ["mintd", "enclave", "pull"]
        if args.repo:
            retry.append(args.repo)
        if args.manifest.name != _DEFAULT_ENCLAVE_MANIFEST:
            retry += ["--manifest", args.manifest.name]
        if args.force:
            retry.append("--force")
        reporter.error(
            f"`mintd enclave pull` must run from inside the enclave "
            f"({enclave_dir}), not from {Path.cwd()}",
            hint=f"cd {shlex.quote(str(enclave_dir))} && {shlex.join(retry)}",
        )
        return 1
    config = Config.load()
    client = _resolve_catalog_client(config)
    dvc_ops = _resolve_dvc_ops(config, reporter)
    with (
        _hint(ValueError),
        # The enclave manifest itself is missing. The cwd guard above funnels
        # every user into this path.
        _hint(
            FileNotFoundError,
            "run this from the enclave repo root, or pass --manifest <path>",
            message=f"{args.manifest.name} not found at {args.manifest}",
        ),
        reporter.status("Pulling enclave data..."),
    ):
        _, written = enclave_pull(
            client,
            dvc_ops,
            manifest_path=args.manifest,
            repo=args.repo,
            force=args.force,
            reporter=reporter,
        )
    # Computed ONCE for both lanes. Text mode already got the D7 report as
    # warns from enclave_pull, but it used to print "nothing to pull" straight
    # after them — the line the whole branch exists to stop saying, since what
    # is recorded as downloaded is precisely NOT on disk.
    missing = missing_downloads(manifest_path=args.manifest, repo=args.repo)
    if reporter.json_mode:
        # BEFORE the not-written early return, and with a "missing" key:
        # Reporter.warn is a no-op in json_mode, so the D7 missing report
        # (enclave_pull's warns) never reaches a `--json` caller — a fresh
        # clone used to print ZERO bytes at exit 0, byte-identical to a
        # healthy up-to-date enclave. The result is emitted even when both
        # lists are empty so json output is never "bare nothing".
        reporter.result(
            {
                "pulled": [
                    {"repo": i.repo, "pin": i.contract_pin, "local_path": str(i.local_path)}
                    for i in written
                ],
                "missing": [
                    {
                        "repo": d.repo,
                        "output": d.output,
                        "pin": d.contract_pin,
                        "local_path": str(d.local_path),
                    }
                    for d in missing
                ],
            }
        )
        return 0
    if not written:
        # Exit 0 either way (D7: a missing path is indistinguishable from a
        # deliberate prune-after-transfer, so it is a report, not a failure) —
        # only the contradicting line goes.
        if not missing:
            reporter.info("nothing to pull")
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
        with (
            _hint((UnsafeArchiveMember, InvalidTransferManifest), "re-run 'mintd enclave pull --force'"),
            reporter.status("Packaging enclave archive..."),
        ):
            archive, skipped = enclave_package(
                manifest_path=args.manifest,
                name=args.repo,
                output_archive=args.output_archive,
                output_dir=output_dir,
                resend=args.resend,
            )
    except NothingNewToPackage as exc:
        # Everything already crossed the gap — the routine steady state, not a
        # failure. Exit 0 so a scripted `pull && package` loop is not a red run.
        # Caught before NothingToPackage, whose hint ("run pull first") is only
        # right for a genuinely empty selection.
        reporter.info(f"nothing new to package: {exc}")
        reporter.info(
            "a bundle that was built is recorded as transferred even if it never "
            "arrived — to re-ship:\n"
            f"  mintd enclave package --resend{' ' + args.repo if args.repo else ''}"
        )
        return 0
    if skipped:
        # Count repos, not `downloaded[]` rows: one product with several
        # outputs is several rows, and "skipped 3 products: my-repo" reads as
        # three products that share a name.
        repos = sorted({d.repo for d in skipped})
        names = ", ".join(repos)
        reporter.info(
            f"skipped {len(repos)} already-transferred product"
            f"{'' if len(repos) == 1 else 's'}: {names} "
            f"(pass --resend to ship {'it' if len(repos) == 1 else 'them'} again)"
        )
    # `result` (not `print`) so the archive path lands on stdout AND is flushed
    # before the stderr hint below — otherwise a piped stdout is block-buffered
    # and the hint surfaces first. Also gives --json mode the skipped list.
    reporter.result(
        {"archive": str(archive), "skipped": sorted({d.repo for d in skipped})},
        pretty=lambda p: f"packaged: {p['archive']}",
    )
    reporter.info(
        "land it inside the enclave with:\n"
        f"  mkdir -p incoming && tar -xzf {archive.name} -C incoming\n"
        "  python3 incoming/land.py"
    )
    return 0


def _handle_enclave_verify(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    with (
        # The enclave manifest itself is missing.
        _hint(
            FileNotFoundError,
            "run this from the enclave repo root, or pass --manifest <path>",
            message=f"enclave_manifest.yaml not found at {args.manifest}",
        ),
        # The base class's hint; its subclasses carry their own.
        _hint(InvalidTransferManifest, "the archive is malformed; re-export from source"),
        reporter.status("Verifying enclave manifest..."),
    ):
        _, written = enclave_verify(
            extracted_dir=args.extracted_dir,
            manifest_path=args.manifest,
            data_root=args.data_root,
        )
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
    metadata = _load_metadata_with_schema_hint(args.path / "metadata.json")
    findings = check_project(args.path, upgrades=False, client=client)
    error_findings = [f for f in findings if f.severity == "error"]
    if error_findings:
        return _render_findings(error_findings, json_out=False)
    # After check, so a finding whose fix is another push is fixed before the
    # one commit. Here and not in `check_project`: `publish` re-checks between
    # its own push and its commit, while the pointers are dirty by design.
    # No exception for a directory outside git: importers read a git history,
    # so a project without one cannot be imported by anyone.
    # The whole tree, as `publish` requires: importers read the commit, and a
    # list of the files they need (pointers, dvc.yaml, the vars files it
    # loads, metadata.json, ...) is never complete. `mintd init` makes no
    # commit, so a first registration would otherwise pass uncommitted.
    try:
        unseen = _resolve_git_ops(config).uncommitted_files(args.path)
    except GitOpError as exc:
        raise MintdError(
            f"could not check the project against HEAD: {exc.stderr.strip() or exc}",
            hint=(
                "importers read this project's git history, so register needs a working git "
                "repository: fix git (or 'git init' and commit), then rerun 'mintd registry register'"
            ),
        ) from exc
    if unseen:
        shown = ", ".join(unseen[:8]) + (f" … ({len(unseen)} in all)" if len(unseen) > 8 else "")
        reporter.error(
            f"the project has uncommitted changes: {shown}",
            hint=(
                "importers read the commit on your git remote, not your disk: commit and push what "
                "belongs to the project, put data in DVC ('mintd data add') or .gitignore, then "
                "rerun 'mintd registry register'"
            ),
        )
        return 1
    with reporter.status("Registering project with the catalog…"):
        try:
            result = client.register(metadata, reporter=reporter)
        except CatalogAlreadyExists as exc:
            raise MintdError(
                f"catalog entry {str(exc)!r} already exists",
                hint="use 'mintd registry update' to push changes to an existing entry",
            ) from exc
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
    metadata = _load_metadata_with_schema_hint(args.path / "metadata.json")
    with reporter.status("Updating project registry entry…"):

        try:
            result = client.update(metadata, dry_run=args.dry_run, reporter=reporter)
        except CatalogNotFound as exc:
            raise MintdError(
                f"catalog entry {str(exc)!r} not found",
                hint="run 'mintd registry register' first to register this project",
            ) from exc
        # Specific clause first — RegistryBranchExists is a GitOpError.
        except (RegistryBranchExists, PRConflictError) as exc:
            raise MintdError(
                f"the registry rejected branch {exc.branch} for {metadata.project.name!r}",
                hint=(
                    "an earlier catalog PR is probably still open on it — check "
                    f"`gh pr list --head {exc.branch}` on the registry, then rerun "
                    "`mintd registry update` (safe to retry; nothing in this project changed)"
                ),
            ) from exc
        except (GitOpError, GhAuthError, GhNotInstalled) as exc:
            raise MintdError(
                f"could not update the registry entry for {metadata.project.name!r}: {exc}",
                hint=specific_hint(exc) or (
                    "check `gh auth status` and network access to the registry, then rerun "
                    "`mintd registry update` — nothing in this project changed"
                ),
            ) from exc
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
    pr_label = "updated PR" if result.pr_reused else "PR"
    if result.pr_url:
        pr_clause = f" → {pr_label} {result.pr_url}"
    elif result.pr_number is not None:
        pr_clause = f" → {pr_label} #{result.pr_number}"
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


def _handle_registry_rebuild(args: argparse.Namespace) -> int:
    """Rebuild the catalog in a registry checkout. Exits 1 when an entry
    couldn't be read (its repository unreachable, or the entry itself) or is
    held by mintd's own error, the other entries written; under --check, also
    when any entry would change or is held, so a dry run can gate."""
    from .catalog import rebuild_catalog

    reporter = getattr(args, "_reporter", None) or Reporter()
    if not (args.registry / "catalog").is_dir():
        reporter.error(f"{args.registry} has no catalog/ folder", hint="run it in a registry checkout, or pass --registry")
        return 1
    results = rebuild_catalog(args.registry, git_ops=_resolve_git_ops(Config.load()), write=not args.check)
    if reporter.json_mode:
        reporter.result([
            {"name": r.name, "entry": r.entry, "verdict": r.verdict, "commit": r.commit, "why": r.why,
             "changes": [c.field_path for c in r.changes], "reports": list(r.reports), "mintd_error": r.mintd_error}
            for r in results
        ])
    else:
        for r in results:
            changed = f" ({', '.join(c.field_path for c in r.changes)})" if r.changes else ""
            line = f"{r.entry}: {r.verdict}{changed}" + (f": {r.why}" if r.why else "")
            (reporter.warn if r.verdict in ("held", "unreachable", "skipped") else reporter.info)(line)
            for report in r.reports:
                reporter.warn(f"{r.entry}: {report}")
    unread = any(r.verdict in ("unreachable", "skipped") or r.mintd_error for r in results)
    return 1 if unread or (args.check and any(r.verdict in ("updated", "held") for r in results)) else 0


def _handle_publish(args: argparse.Namespace) -> int:
    from .publish import prepare_publish, _apply_publish
    reporter = getattr(args, "_reporter", None) or Reporter()
    config = Config.load()
    client = _resolve_catalog_client(config)
    dvc_ops = _resolve_dvc_ops(config, reporter)
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
            reporter.info(f"  [{f.severity}] {f.location}: {f.message}")
            if f.hint:
                reporter.info(_finding_hint(f.hint))
        if len(exc.findings) > 5:
            reporter.info(f"  ... and {len(exc.findings) - 5} more")
        return 1

    _render_publish_preview(reporter, preview)

    if args.dry_run:
        reporter.info("(dry-run: no side effects)")
        return 0
    if not args.assume_yes:
        ok = _prompt_publish_confirm(reporter)
        if not ok:
            reporter.info("publish cancelled.")
            return 0

    with reporter.status(f"Publishing v{preview.new_version}…"):
        result = _apply_publish(
            preview,
            project_path=args.path,
            client=client,
            dvc_ops=dvc_ops,
            git_ops=git_ops,
            message=args.message,
            reporter=reporter,
            aws_profile=config.aws_profile_name,
            memo_path=config.resolved_cache_dir() / "proven-versions.sqlite",
        )
    tag = f"v{result.version}"
    storage = getattr(preview.new_metadata, "storage", None)
    prefix_clause = ""
    if storage is not None and getattr(storage, "bucket", None):
        prefix = (getattr(storage, "prefix", None) or "").strip("/")
        prefix_clause = f", s3://{storage.bucket}/{prefix}/" if prefix else f", s3://{storage.bucket}/"
    pr_label = "updated PR" if result.pr_reused else "PR"
    pr_clause = f", {pr_label} {result.pr_url}" if result.pr_url else ", PR (local)"
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
    if preview.unpinned:
        reporter.info("Not yet pinned (this publish's dvc push must record the S3 version IDs, or the release stops after it):")
        for message in preview.unpinned:
            reporter.info(f"  - {message}")
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
    config = Config.load(args.path) if args.path is not None else Config.load()
    # A registry_url can carry a token; the shown config never does.
    text = strip_userinfo(config_ops.render_config(config, json_out=args.json_out))
    # YAML emits a trailing newline already; JSON does not. Use print's
    # default newline only when the rendered text doesn't carry one.
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _handle_config_setup(args: argparse.Namespace) -> int:
    write = not args.dry_run
    if args.from_file is not None:
        source = None if args.from_file == "-" else args.from_file
        config = config_ops.apply_from_file(args.path, source, write=write)
    elif args.migrate_v1 is not None:
        config = config_ops.apply_migrate_v1(args.path, args.migrate_v1, write=write)
    elif args.set_pairs:
        pairs = [config_ops.parse_set_pair(s) for s in args.set_pairs]
        config = config_ops.apply_set_updates(args.path, pairs, write=write)
    else:
        # No flags → interactive walkthrough of the Config fields
        # (minus config_ops._SETUP_SKIP_FIELDS).
        config = config_ops.interactive_setup(args.path, write=write)
    if args.dry_run:
        print("# dry-run: would write the following:")
    print(config_ops.render_config(config), end="")
    return 0


def _handle_config_validate(args: argparse.Namespace) -> int:
    reporter = getattr(args, "_reporter", None) or Reporter()
    with reporter.status("Validating S3 connectivity..."):
        steps = config_ops.validate_config(args.path, bucket=args.bucket)
    text, exit_code = config_ops.render_validation(steps, json_out=args.json_out)
    print(strip_userinfo(text))  # an endpoint or an error's URL can carry credentials
    if not args.json_out:
        s3_step = next((s for s in steps if s.name == "s3"), None)
        if s3_step is not None and s3_step.status == "ok":
            try:
                config = Config.load(args.path)
                endpoint = config.storage_endpoint or "default AWS endpoint"
                profile = _s3.resolve_profile({}, config.aws_profile_name)[0] or "default"
            except Exception:
                endpoint, profile = "default AWS endpoint", "default"
            ms_clause = f" — 200 OK, {s3_step.latency_ms}ms" if s3_step.latency_ms is not None else " — 200 OK"
            # The bucket the step checked: --bucket, or the config's storage_bucket_prefix.
            reporter.success(f"✓ s3://{s3_step.bucket or args.bucket} via {endpoint} (profile: {profile}){ms_clause}")
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
        raise MintdError(str(exc)) from exc
    except metadata_migrate.MetadataAlreadyV2 as exc:
        print(f"already v2: {exc}")  # stdout, as a report: not an error line
        return 1
    except metadata_migrate.MetadataMigrateError as exc:
        raise MintdError(f"migration produced invalid v2 metadata: {exc}", exit_code=2) from exc
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


def _finding_hint(hint: str) -> str:
    """A finding's hint under it: the first line after the 💡, the rest aligned beneath."""
    return "    💡 " + hint.replace("\n", "\n       ")


def _render_findings(findings: list[CheckFinding], *, json_out: bool) -> int:
    if json_out:
        for f in findings:
            print(
                json.dumps(
                    {
                        "severity": f.severity,
                        "section": f.section,
                        "message": strip_userinfo(f.message),
                        "field_path": f.field_path,
                        "source": str(f.source) if f.source else None,
                        "kind": f.kind,
                        "hint": strip_userinfo(f.hint) if f.hint else None,
                    }
                )
            )
    else:
        for f in findings:
            prefix = _resolve_prefix(f.kind)
            print(strip_userinfo(f"{prefix} [{f.severity}] {f.location}: {f.message}"))
            if f.hint:
                print(strip_userinfo(_finding_hint(f.hint)))
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
    print(strip_userinfo(f"error: {exc}"), file=sys.stderr)
    kind = exc.finding.kind
    if kind == "unreachable" and exc.finding.hint:
        # check's finding carries the shared unreachable-producer hint.
        print(strip_userinfo(f"hint: {exc.finding.hint}"), file=sys.stderr)
    elif kind == "pin_missing":
        # An empty pin in the user's own file carries its own fix.
        print(
            strip_userinfo("hint: " + (
                exc.finding.hint
                or "the producer's history no longer has your pinned commit; "
                "ask the producer, or re-import at a commit that exists"
            )),
            file=sys.stderr,
        )
    elif kind == "drift_unknown":
        # Five states reach this kind and only two are transport failures, so
        # a fixed "check the network" misdiagnosed three of them at a network
        # that was working. Prefer the finding's own hint, as
        # `catalog_unresolved` below already does. Still `kind` dispatch.
        print(
            strip_userinfo("hint: " + (
                exc.finding.hint
                or "'mintd check --upgrades' shows the row and the reason"
            )),
            file=sys.stderr,
        )
    elif kind == "schema_too_old":
        print(
            "hint: ask the producer to update their metadata schema to 2.0",
            file=sys.stderr,
        )
    elif kind == "catalog_unresolved":
        # Prefer the finding's own hint. This kind now covers both "no catalog
        # client was provided" and "the catalog read failed", and only the
        # finding knows which — a fixed string would misdirect the second case
        # at a setting that is already correct. Still `kind` dispatch, not
        # message parsing.
        print(
            strip_userinfo("hint: " + (
                exc.finding.hint
                or "check that registry_url is set in ~/.config/mintd/config.yaml"
            )),
            file=sys.stderr,
        )
    if kind in _RECOVERABLE_KINDS:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
