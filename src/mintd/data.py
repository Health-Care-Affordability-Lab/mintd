"""Orchestration for the `mintd data ...` command family.

Slice 4: `import_product` — catalog lookup → path resolution → `dvc import`.
Slice 5: lifts the `--rev` without `--path` restriction by resolving
`data_products.primary` via `ProducerView.at(repo, rev)` (the producer's
metadata.json at the pinned commit).
Slice 7: `bump_import` — consume slice-6 `_consumer_findings`, re-resolve
`data_products.primary` at the producer's HEAD via `ProducerView.at_head`,
and overwrite the consumer's `.dvc` file with the new pin.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._dvc_ops import DvcOps
from ._fast_sync_ops import FastSyncOps, normalize_target
from ._registry_git_ops import GitOpError, RegistryGitOps
from ._templates import project_full_name
from .catalog import CatalogClient, CatalogNotFound
from .check import CheckFinding, check_project
from .data_ops import data_pull
from .imports import DataDependency, NotAnImportError
from .producer import MissingPrimaryDataProduct, ProducerError, ProducerView

if TYPE_CHECKING:
    from ._console import Reporter

__all__ = [
    "AmbiguousImport",
    "BumpBlocked",
    "BumpResult",
    "CloneResult",
    "ImportDestinationExists",
    "ImportNotFound",
    "MissingPrimaryDataProduct",
    "PrimaryRemovedAtHead",
    "ProducerError",
    "UnknownProductPath",
    "bump_import",
    "clone_and_pull_product",
    "import_product",
]


@dataclass(frozen=True)
class BumpResult:
    """Outcome of `bump_import` — for the CLI's pin-transition line (slice 38b)."""
    changed: bool
    old_pin: str
    new_pin: str | None
    dvc_path: Path | None


@dataclass(frozen=True)
class CloneResult:
    """Outcome of `clone_and_pull_product` — dest + provenance for the CLI's
    completion line (slice 38b).

    ``pull_error_count``: targets the post-clone ``data_pull`` could not
    serve (blocked + incomplete version-aware targets — see
    ``PullSummary.error_count``). Each was already reported via
    ``reporter.error`` with a targeted-retry hint; a non-zero count makes
    `mintd data clone` skip the ✓ line and exit non-zero.
    """
    dest: Path
    rev: str | None
    remote_bucket: str | None
    file_count: int = 0
    total_bytes: int = 0
    elapsed_s: float = 0.0
    pull_error_count: int = 0


class ImportDestinationExists(Exception):
    """A `.dvc` file already exists at the destination. The consumer resolves
    by passing `force=True` or removing the file first."""


class ImportNotFound(Exception):
    """`bump_import(name=...)` was called with a name that isn't imported
    in the project's `data/imports/` directory."""


class AmbiguousImport(Exception):
    """The `(product, --path)` selector matches more than one `.dvc` file.

    Two flavors, both refusing to pick silently: the product has several
    imported outputs and no `--path` was given, or two `.dvc` files in one
    namespace record the *same* producer path (a real duplicate — e.g. an
    import made before and after the layout change). Not the superseded
    two-key-space class of the same name: there is one key space here (the
    producer's output path), and this fires when it cannot name a single row.
    """


class UnknownProductPath(ValueError):
    """A requested ``--path`` is not a tracked output of the product. The
    message lists the product's `data_products.outputs[].path` values (and
    primary) so the user can pick a real target instead of decoding a raw
    DVC "no such target" stderr."""


class BumpBlocked(Exception):
    """The consumer-section finding for this dep is an error or non-actionable
    warning. Bumping is unsafe until the user resolves the underlying producer
    issue. Carries the original `CheckFinding` so a CLI layer can render the
    producer-side reason."""

    def __init__(self, name: str, finding: CheckFinding) -> None:
        super().__init__(f"bump blocked for {name!r}: {finding.message}")
        self.name = name
        self.finding = finding


class PrimaryRemovedAtHead(Exception):
    """Producer's HEAD has `data_products.primary = None`. The consumer must
    either pin to an older SHA explicitly or stop importing this producer."""

    def __init__(self, name: str, repo: str) -> None:
        super().__init__(
            f"producer {repo!r} HEAD has no data_products.primary; cannot bump {name!r}"
        )
        self.name = name
        self.repo = repo


def import_product(
    client: CatalogClient,
    dvc_ops: DvcOps,
    name: str,
    *,
    cwd: Path,
    dest_root: Path,
    path: str | list[str] | None = None,
    rev: str | None = None,
    all_outputs: bool = False,
    force: bool = False,
    extra_dvc_args: list[str] | None = None,
    producer_view_factory: Callable[[str, str], ProducerView] | None = None,
    reporter: "Reporter | None" = None,
) -> list[Path]:
    """Catalog-driven `dvc import`. Returns the list of `.dvc` files written."""

    entry = client.fetch(name)
    dumped = entry.model_dump()
    repo_url = _require_repo_url(dumped, name=name)

    if rev is not None and path is None and not all_outputs:
        factory = producer_view_factory or ProducerView.at
        view = factory(repo_url, rev)
        path = view.primary_or_raise()

    paths = _drop_nested_paths(
        _resolve_paths(dumped, path=path, all_outputs=all_outputs, name=name),
        reporter=reporter,
    )

    # Namespace the destination by the producer's full_name (e.g.
    # `data_cms-synpuf`) so importing multiple products into the same
    # `dest_root` doesn't collide on shared output names (e.g. both
    # provider-a and provider-b publishing `data/final/` would land at
    # the same `dest_root/final/` without the namespace). Falls back to
    # the catalog name if full_name is missing on the entry.
    namespace = _import_namespace(dumped, name)
    nested_root = dest_root / namespace

    # Status feedback (slice 38a). Multi-output imports relabel the spinner
    # per output. We use the spinner (not the determinate progress bar)
    # because each `dvc import` streams a subprocess; the bar's render would
    # be corrupted by the child's stderr (see data_ops.py's
    # "MUST happen OUTSIDE the progress widget" invariant). The handler
    # threads the reporter into dvc_ops so child stderr flows through
    # passthrough_stderr and refreshes the spinner.
    multi = len(paths) > 1
    status_cm = (
        reporter.status(f"Importing {name}...")
        if reporter is not None
        else nullcontext()
    )

    produced: list[Path] = []
    try:
        with status_cm:
            for i, p in enumerate(paths, 1):
                if multi and reporter is not None:
                    reporter.update_status(
                        f"Importing {Path(p.rstrip('/')).name} ({i}/{len(paths)})..."
                    )
                # Mirror the producer's own path under the namespace (D-A). The
                # basename-only form collided two outputs sharing a leaf name
                # (`data/final/` + `archive/final/`) on one dest under `--all`.
                dest = nested_root / Path(p.rstrip("/"))
                # `p` is producer-controlled and unnormalized (catalog
                # `outputs[].path` / `primary`, or an unchecked `--path`): `..`
                # escapes the project, and pathlib drops the left operand
                # entirely for an absolute path — where `shutil.rmtree` below
                # would then delete. Anchor on `nested_root`, the TIGHTER base:
                # `dest_root` alone lets `../<other-product>/data/final/` through,
                # landing on ANOTHER product's `.dvc`.
                if not dest.resolve().is_relative_to(nested_root.resolve()):
                    raise UnknownProductPath(
                        f"{name!r} resolves output {p!r} to {dest}, outside the "
                        f"import root {nested_root}; refusing to import or delete "
                        f"outside it"
                    )
                target_dvc = dest.parent / (dest.name + ".dvc")

                # Idempotence is keyed on the producer path this import RECORDS,
                # not where the pointer sits: checking `target_dvc` alone missed
                # pre-layout-change imports, so a re-import wrote a SECOND pointer
                # and a second copy of the payload, after which every `--bump`
                # died on `AmbiguousImport`. Same lookup `--bump` uses.
                existing = _imports_index(nested_root, name=name).get(p.rstrip("/"))
                if existing is not None:
                    _require_owner(existing, repo_url, name=name)
                    if not force:
                        raise ImportDestinationExists(
                            f"{existing} already imports {p!r} of {name!r}; "
                            f"pass force=True to re-import it, or remove that file"
                        )
                    # Rewrite the pointer that exists rather than adding a sibling
                    # beside it: the layout change deliberately moves nothing on
                    # disk (the plan keeps `full_name` as the folder), so a forced
                    # re-import of a legacy import stays where it is.
                    target_dvc = existing
                    dest = existing.with_suffix("")
                elif target_dvc.exists() and not force:
                    raise ImportDestinationExists(
                        f"{target_dvc} already exists; pass force=True or remove it"
                    )
                if force and target_dvc.exists() and dest.is_dir() and not dest.is_symlink():
                    # `dvc import -o <existing-dir>` treats the directory as a
                    # *container*, nests the source basename inside it, and then
                    # rejects the overlap; neither mintd's `force` nor dvc's
                    # `--force` clears it. The `.dvc` guard keeps a stray
                    # unrelated directory from being destroyed.
                    shutil.rmtree(dest)
                # `dvc import` requires the destination's parent directory to
                # already exist; it doesn't auto-create it. Create here so a
                # fresh consumer project (no `data/imports/<namespace>/` yet)
                # doesn't fail with the cryptic "stage working dir ... does not
                # exist".
                dest.parent.mkdir(parents=True, exist_ok=True)
                produced.append(
                    dvc_ops.import_(
                        repo_url=repo_url,
                        path=p,
                        dest=dest,
                        cwd=cwd,
                        rev=rev,
                        force=force,
                        extra_args=extra_dvc_args,
                    )
                )
    except BaseException:
        if produced and reporter is not None:
            # Nothing is rolled back. Say what landed, so the user is not left
            # guessing at a half-written import.
            #
            # BaseException for the same reason as `bump_import`'s restore:
            # `run_streaming` re-raises KeyboardInterrupt unchanged, and Ctrl-C
            # partway through a multi-output import is the case where this
            # message matters most -- some of the payload is already on disk
            # and only this line names it. The block is a single reporter call,
            # so it cannot stall the interrupt, and the bare `raise` leaves the
            # exit code and the CLI's rendering unchanged.
            reporter.warn(
                f"{len(produced)} of {len(paths)} outputs were imported before "
                f"this failure and remain on disk: "
                + ", ".join(str(q) for q in produced)
            )
        raise
    return produced


def _drop_nested_paths(paths: list[str], *, reporter: "Reporter | None") -> list[str]:
    """Drop any requested output that lives INSIDE another requested one.

    DVC cannot track `data/final/` and `data/final/b.csv` separately — the
    second is already in the first, and `dvc import` refuses the overlap. A
    producer listing both in `outputs[]` is publishing redundant metadata, not
    two products, but mirroring the producer's paths (D-A) put them on nested
    destinations: pointer one written, then `DvcImportDestinationExists`, exit
    1, half done. Shallowest first, so the surviving entry is always the one
    that covers the others.
    """
    unique = list(dict.fromkeys(paths))
    kept: list[str] = []
    for p in sorted(unique, key=lambda s: s.rstrip("/").count("/")):
        covering = next(
            (k for k in kept if p.rstrip("/").startswith(k.rstrip("/") + "/")), None
        )
        if covering is None:
            kept.append(p)
        elif reporter is not None:
            reporter.info(f"skipping {p}: already inside {covering}")
    return [p for p in unique if p in kept]


def _section(entry: dict[str, Any], key: str) -> dict[str, Any]:
    """One block of a catalog entry — `{}` unless the value really is a map.

    `CatalogEntry` is `extra="allow"` with no declared fields and
    `deserialize()` is `yaml.safe_load` + `model_validate`, so every value in
    a registry entry is arbitrary — and `metadata_migrate.py` documents v1
    files whose blocks are scalars. Without this, `entry.get(k) or {}`
    followed by `.get(...)` raises `AttributeError: 'str' object has no
    attribute 'get'`, which exits `data import`, `--bump` and `data clone`
    alike as a raw traceback. One reader for every block so the guard cannot
    be forgotten at the next call site.
    """
    value = entry.get(key)
    return value if isinstance(value, dict) else {}


def _resolve_paths(
    entry: dict[str, Any],
    *,
    path: str | list[str] | None,
    all_outputs: bool,
    name: str,
    missing_primary_hint: str = "pass --path or --all",
) -> list[str]:
    """Shared path resolver for `import_product` and `clone_and_pull_product`.

    Precedence: ``all_outputs`` → every `data_products.outputs[].path`;
    ``path`` (a single string or a list of them) → exactly those; otherwise
    fall back to `data_products.primary` (raising with the caller-supplied
    hint when no primary is set). One resolver for both verbs so their
    selection semantics cannot drift.
    """
    data_products = _section(entry, "data_products")

    if all_outputs:
        outputs = data_products.get("outputs") or []
        return [
            o["path"]
            for o in outputs
            if isinstance(o, dict) and isinstance(o.get("path"), str)
        ]

    if path is not None:
        return [path] if isinstance(path, str) else list(path)

    primary = data_products.get("primary")
    if not primary:
        raise MissingPrimaryDataProduct(
            f"catalog entry {name!r} has no data_products.primary; "
            f"{missing_primary_hint}"
        )
    if not isinstance(primary, str):
        # `metadata_migrate.py` documents v1 files where `primary` is a LIST.
        # Unmigrated, it reaches `p.rstrip("/")` in `import_product` (and the
        # pull targets in `clone_and_pull_product`) as an AttributeError.
        raise UnknownProductPath(
            f"catalog entry {name!r} has a non-string data_products.primary "
            f"({primary!r}); the producer's metadata.json needs migrating"
        )
    return [primary]


def _tracked_output_targets(entry: dict[str, Any]) -> list[str]:
    """The product's tracked outputs (`data_products.outputs[].path`), plus
    the primary if it isn't already listed among them."""
    data_products = _section(entry, "data_products")
    outputs = data_products.get("outputs") or []
    tracked = [o["path"] for o in outputs if isinstance(o, dict) and "path" in o]
    primary = data_products.get("primary")
    if primary and normalize_target(primary) not in {
        normalize_target(t) for t in tracked
    }:
        tracked.append(primary)
    return tracked


def _validate_requested_targets(
    entry: dict[str, Any], *, requested: list[str], name: str
) -> None:
    """Reject requested pull targets that aren't tracked outputs of the
    product — with a message listing the real outputs — instead of letting
    `dvc pull` fail later with a raw "no such target" stderr. Both sides are
    compared through `normalize_target` so `./x`, `x/`, and backslash
    spellings all match."""
    tracked = _tracked_output_targets(entry)
    known = {normalize_target(t) for t in tracked}
    unknown = [p for p in requested if normalize_target(p) not in known]
    if not unknown:
        return
    primary = _section(entry, "data_products").get("primary")
    primary_norm = normalize_target(primary) if primary else None
    listed = ", ".join(
        f"{normalize_target(t)} (primary)"
        if normalize_target(t) == primary_norm
        else normalize_target(t)
        for t in tracked
    ) or "<none>"
    unknown_desc = ", ".join(repr(p) for p in unknown)
    raise UnknownProductPath(
        f"catalog entry {name!r} has no tracked output {unknown_desc}; "
        f"tracked outputs: {listed}"
    )


def _cloned_metadata_entry(
    dest: Path, *, fallback: dict[str, Any]
) -> dict[str, Any]:
    """The cloned repo's `metadata.json` as a dict — the tracked-outputs
    source of truth at the *cloned rev*. Used to validate ``--path`` when
    ``--rev`` is pinned (the registry catalog serves HEAD, which can drift
    from an older tag). Falls back to the catalog entry when the file is
    missing, malformed, or has no usable `data_products` block."""
    try:
        data = json.loads((dest / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(data, dict) or not isinstance(
        data.get("data_products"), dict
    ):
        return fallback
    return data


def _require_repo_url(entry: dict[str, Any], *, name: str) -> str:
    url = _section(entry, "repository").get("github_url")
    if not url:
        raise ValueError(f"catalog entry {name!r} has no repository.github_url")
    return url


_NAME_FORBIDDEN = ("/", "\\", "..")


def _validate_clone_name(name: str) -> None:
    if not name or name in {".", ".."} or any(s in name for s in _NAME_FORBIDDEN):
        raise ValueError(f"invalid product name: {name!r}")


def _resolve_clone_dest(
    entry: dict[str, Any], *, name: str, dest: Path | None
) -> Path:
    if dest is not None:
        return dest
    project_type = _section(entry, "project").get("type") or "data"
    base = name
    for prefix in ("data_", "prj_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return Path.cwd() / project_full_name(project_type, base)


def clone_and_pull_product(
    client: CatalogClient,
    dvc_ops: DvcOps,
    registry_git_ops: RegistryGitOps,
    fast_sync_ops: FastSyncOps | None,
    *,
    name: str,
    dest: Path | None = None,
    rev: str | None = None,
    primary_only: bool = False,
    paths: list[str] | None = None,
    jobs: int | None = None,
    extra_dvc_args: list[str] | None = None,
    reporter: "Reporter | None" = None,
    aws_profile_name: str | None = None,
) -> "CloneResult":
    """Clone a published data product into a working directory + dvc pull it.

    Looks up `name` in the registry, full-clones the producer repo to
    `./<type>_<name>/` (or `dest` if provided), then `dvc pull`s every
    tracked output by default. Pass ``primary_only=True`` to pull only
    `data_products.primary` (useful when the full product is multi-TB
    but the user only needs the headline output), or ``paths=[...]`` to
    pull exactly those tracked outputs (files or directories) — the same
    selection model as `import_product`'s ``--path``. Precedence:
    ``paths`` → those targets; else ``primary_only`` → the primary; else
    everything. ``paths`` and ``primary_only`` together is a usage error.

    Returns a ``CloneResult`` (dest + best-effort cloned rev + remote
    bucket) so the CLI can render an informative completion line (slice
    38b). rev/bucket are best-effort (None on failure) and never block.
    ``pull_error_count`` carries the post-clone pull's failure count
    so the CLI can exit non-zero instead of
    printing a false ✓ line when targets could not be served.

    Raises:
        ValueError: invalid `name` (path-traversal characters), or
            `paths` combined with `primary_only`.
        CatalogNotFound: `name` not in registry.
        ImportDestinationExists: dest exists and is non-empty.
        ProducerError: clone failed (UNREACHABLE).
        MissingPrimaryDataProduct: `primary_only=True` and no primary set.
        UnknownProductPath: a `paths` entry is not a tracked output —
            checked against the catalog entry *before* the clone at the
            default rev, or against the cloned repo's metadata.json when
            `rev` is pinned (the clone is removed again in that case so a
            corrected retry isn't blocked by ImportDestinationExists).
        DvcOpError: dvc pull failed after clone.
    """
    _validate_clone_name(name)
    if paths and primary_only:
        raise ValueError(
            "paths and primary_only are mutually exclusive; "
            "pass --path to pull specific outputs OR --primary for the "
            "primary output, not both"
        )
    entry = client.fetch(name)
    dumped = entry.model_dump()
    repo_url = _require_repo_url(dumped, name=name)

    # Resolve + validate pull targets BEFORE the (non-shallow, potentially
    # multi-GB) clone: a typo'd --path or a missing primary must fail
    # without leaving a clone on disk that would make the corrected retry
    # trip over ImportDestinationExists.
    targets: list[str] | None
    if paths or primary_only:
        # One resolver with import_product: `paths` wins, else fall back
        # to `data_products.primary` (primary_only). No-flag clone stays
        # targets=None (pull everything) — clone's "all" is DVC's own
        # discovery, not the catalog outputs list.
        selected = _resolve_paths(
            dumped,
            path=list(paths) if paths else None,
            all_outputs=False,
            name=name,
            missing_primary_hint="drop --primary to pull all tracked outputs",
        )
        targets = [normalize_target(p) for p in selected]
        if paths and rev is None:
            # The catalog entry mirrors the producer's HEAD, so at the
            # default rev the check can run here, pre-clone. With --rev
            # pinned the tracked outputs may differ from the catalog
            # snapshot; validation is deferred until after the clone and
            # runs against the cloned metadata.json at exactly that rev.
            _validate_requested_targets(dumped, requested=targets, name=name)
    else:
        targets = None

    resolved_dest = _resolve_clone_dest(dumped, name=name, dest=dest).resolve()
    if resolved_dest.exists() and any(resolved_dest.iterdir()):
        raise ImportDestinationExists(
            f"destination {resolved_dest} exists and is non-empty"
        )

    try:
        if reporter is not None:
            with reporter.status(f"Cloning {name} repository..."):
                registry_git_ops.clone(
                    repo_url, resolved_dest, shallow=False, branch=rev,
                )
        else:
            registry_git_ops.clone(
                repo_url, resolved_dest, shallow=False, branch=rev,
            )
    except GitOpError as exc:
        raise ProducerError.unreachable(
            repo=repo_url,
            pin=rev or "HEAD",
            detail=(
                f"clone to {resolved_dest} failed; "
                f"partial clone left in place: {exc}"
            ),
        ) from exc

    if paths and rev is not None and targets is not None:
        # Deferred half of the --path validation (see above): the registry
        # catalog serves HEAD's outputs, which can drift from a pinned
        # --rev. Validate against the cloned repo's metadata.json at
        # exactly that rev, falling back to the catalog entry when it
        # can't be read.
        try:
            _validate_requested_targets(
                _cloned_metadata_entry(resolved_dest, fallback=dumped),
                requested=targets,
                name=name,
            )
        except UnknownProductPath:
            # Remove the clone this call just created so the corrected
            # retry doesn't fail with ImportDestinationExists. Safe: the
            # pre-clone guard guarantees resolved_dest was absent or
            # empty before the clone.
            shutil.rmtree(resolved_dest, ignore_errors=True)
            raise

    pull_summary = data_pull(
        project_path=resolved_dest,
        targets=targets,
        dvc_ops=dvc_ops,
        fast_sync_ops=fast_sync_ops,
        jobs=jobs,
        extra_dvc_args=extra_dvc_args,
        reporter=reporter,
        aws_profile_name=aws_profile_name,
    )

    # Best-effort provenance for the completion line (slice 38b). Neither
    # the resolved rev nor the bucket blocks the clone — both degrade to
    # None on failure.
    resolved_rev: str | None
    try:
        resolved_rev = registry_git_ops.current_commit(resolved_dest)
    except Exception:
        resolved_rev = None
    remote_bucket: str | None = None
    try:
        from ._fast_sync_ops import get_remote_config, parse_s3_url
        from .data_ops import _default_dvc_remote
        remote_name = _default_dvc_remote(resolved_dest) or "origin"
        url = get_remote_config(resolved_dest, remote_name).get("url", "")
        remote_bucket, _ = parse_s3_url(url)
    except Exception:
        remote_bucket = None

    return CloneResult(
        dest=resolved_dest,
        rev=resolved_rev,
        remote_bucket=remote_bucket,
        pull_error_count=pull_summary.error_count,
    )


def _remove_payload(path: Path) -> None:
    """Delete whatever is at `path` — directory, file, symlink or nothing.

    Neither primitive covers a payload path on its own: `shutil.rmtree` is a
    silent no-op on a file or a symlink (`ignore_errors` swallows its refusal)
    and `Path.unlink` refuses a directory. A payload is any of the three — a
    directory, a single file, or dvc's `cache.type = symlink` — so every place
    that clears one has to dispatch on the shape it finds.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def bump_import(
    client: CatalogClient,
    dvc_ops: DvcOps,
    *,
    project_path: Path,
    name: str,
    path: str | None = None,
    extra_dvc_args: list[str] | None = None,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
    check_findings: list[CheckFinding] | None = None,
) -> "BumpResult":
    """Re-resolve one import at the producer's HEAD and rewrite its `.dvc`.

    `name` is the **data product name** (the catalog key — the same
    positional `import` takes) and `path` is the producer-side output path
    (the same selector as `--path`). D-A: `--bump` has no key space of its
    own; it reads the `(product, --path)` pair `import` does.

    Slice 7 consumes slice-6 `_consumer_findings` directly — `check_project`
    is the canonical "find drift" surface; this function is the canonical
    "act on drift" surface. Walking dependencies here would duplicate
    detection in two places (the resolver-sin slice 6 retired).

    `ProducerView.at_head` returns the resolved SHA alongside the view so
    `dvc import --rev <sha>` records the concrete commit, not the symbolic
    `HEAD` — preserving the pin semantics slice 5 introduced.

    Returns a ``BumpResult`` (old pin, new pin, changed flag, rewritten
    `.dvc` path) so the CLI can render the pin transition. Raises:

    - `ImportNotFound` — the `(name, path)` pair names no import on disk.
    - `AmbiguousImport` — several imported outputs and no `path`, or two
      `.dvc` files recording the same producer path.
    - `BumpBlocked(name, finding)` — the producer is broken at the pin
      (`pin_missing` / `metadata_missing` / `metadata_invalid`) or the
      warning is non-actionable (`unreachable` / `schema_too_old` /
      `drift_unknown`). Carries the original finding so the call site can
      render the producer-side reason.
    - `PrimaryRemovedAtHead` — HEAD's `data_products.primary` is `None`
      and the import records no output path of its own.
    """
    dvc_source = _resolve_import_source(client, project_path, name, path=path)
    dep = DataDependency.from_dvc_file(dvc_source)

    findings = (
        check_findings
        if check_findings is not None
        else check_project(project_path, upgrades=True)
    )
    finding = _find_consumer_finding_for_target(findings, source=dvc_source)
    if finding is None:
        raise ImportNotFound(
            f"no consumer finding for {name!r} (source={dvc_source})"
        )

    if finding.kind is None:
        # Contract: consumer-section findings post-slice-9 always carry a kind.
        # A None here is a regression — never silently dispatch.
        raise BumpBlocked(name, finding)
    if finding.kind == "up_to_date":
        return BumpResult(changed=False, old_pin=dep.contract_pin, new_pin=None, dvc_path=None)
    if finding.kind != "drift":
        # unreachable / schema_too_old / pin_missing / metadata_missing /
        # metadata_invalid / invalid_manifest / catalog_unresolved — all non-actionable.
        raise BumpBlocked(name, finding)

    factory = producer_view_factory or ProducerView.at_head
    head_view, head_sha = factory(dep.producer_repo)
    # D-C2: bump the path this import RECORDS, not the producer's primary.
    # Re-importing the primary over a non-primary row put `final/` on disk
    # with the old `.dvc` orphaned beside a new one. The primary fallback
    # (and its PrimaryRemovedAtHead) still covers shapes that record no
    # output path of their own.
    if dep.output_path:
        target = dep.output_path
    else:
        try:
            target = head_view.primary_or_raise()
        except MissingPrimaryDataProduct as e:
            raise PrimaryRemovedAtHead(name, dep.producer_repo) from e

    # Rewrite the SAME `.dvc` file — deriving dest from the file being bumped
    # is layout-independent and guarantees nothing is orphaned.
    dest = dvc_source.with_suffix("")
    # dvc treats an existing directory dest as a CONTAINER and then refuses the
    # overlap, so it has to go — but not before the replacement exists.
    # Deleting outright meant a producer that went unreachable mid-bump left
    # the payload gone and the `.dvc` still at the old pin, from a command the
    # user ran with no `--force` anywhere. A rename inside the same directory
    # is atomic and costs no extra disk.
    backup = dest.with_name(dest.name + ".mintd-bump-backup")
    # D15: every payload shape gets the rename-aside, not just a directory.
    # `dest.is_dir() and not dest.is_symlink()` left `moved` False for the two
    # other ordinary shapes — an output that is a single file at HEAD, and
    # dvc's `cache.type = symlink` layout — which meant NO BACKUP AT ALL and
    # `--force` overwriting the payload with nothing to roll back to. The
    # `is_symlink()` conjunct was never a decision to exclude links: `is_dir()`
    # follows them, so it only separated a real directory from a link to one.
    # `rename` moves all three identically. `is_symlink()` is kept as its own
    # test because `exists()` is False for a DANGLING link (a pruned cache),
    # and that link is the only record of where the payload was.
    moved = dvc_source.exists() and (dest.exists() or dest.is_symlink())
    if moved:
        _remove_payload(backup)
        dest.rename(backup)
    try:
        dvc_path = dvc_ops.import_(
            repo_url=dep.producer_repo,
            path=target,
            dest=dest,
            cwd=project_path,
            rev=head_sha,
            force=True,
            extra_args=extra_dvc_args,
        )
    except BaseException:
        # BaseException, not Exception: `run_streaming` re-raises
        # KeyboardInterrupt unchanged, so Ctrl-C during the transfer walked
        # past this restore and left the payload in the backup directory with
        # nothing naming it. Same call, and for the same reason, as
        # `enclave_pull`'s manifest flush. The block is local filesystem work
        # only, so it cannot stall the interrupt; the bare `raise` keeps the
        # exit code and the CLI's rendering unchanged.
        if moved:
            # Whatever the failed import left at `dest` has to go first, and
            # it is not always a directory: `rmtree` is a silent no-op on a
            # file or a symlink (`ignore_errors` swallows its refusal), so
            # `backup.rename(dest)` then met the leftover and raised
            # `NotADirectoryError` — replacing the `DvcOpError` the CLI knows
            # how to render with a traceback, payload still sitting in
            # `.mintd-bump-backup`. Two ordinary shapes reach this: an output
            # that is a single file at HEAD, and dvc's `cache.type = symlink`.
            _remove_payload(dest)
            backup.rename(dest)
        raise
    _remove_payload(backup)
    return BumpResult(
        changed=True, old_pin=dep.contract_pin, new_pin=head_sha, dvc_path=dvc_path,
    )


def _import_namespace(entry: dict[str, Any], name: str) -> str:
    """The `data/imports/` folder one product's imports live under:
    `project.full_name`, falling back to the catalog name. One definition
    for the import writer and the bump reader so the two cannot drift.

    The namespace must be ONE plain path component (`_validate_clone_name`'s
    rule, reused). It is producer-controlled and `import_product`'s
    containment check cannot cover it: `full_name = "."` makes
    `nested_root == dest_root`, which IS inside the import root, and
    `_imports_index` then rglobs the whole tree and hands back ANOTHER
    product's `.dvc`.

    SHAPE ONLY — two legal namespaces can still name one directory.
    `_require_owner` is what refuses those.
    """
    project = _section(entry, "project")
    ns = project.get("full_name") or name
    try:
        _validate_clone_name(ns)
    except (TypeError, ValueError):
        fix = (
            "fix project.full_name in the producer's metadata.json"
            if project.get("full_name")
            else "check the product name"
        )
        raise UnknownProductPath(
            f"{name!r} resolves its import namespace to {ns!r}; it must be "
            f"a single folder name — {fix}"
        ) from None
    return ns


def _imports_index(namespace_dir: Path, *, name: str) -> dict[str, Path]:
    """Map each import's recorded producer path (`deps[0].path`) to its
    `.dvc` file, within ONE product's namespace folder.

    Keyed on what `--path` names, read from the `.dvc` itself — so legacy
    (basename-flattened) and new (producer-path-mirrored) layouts resolve
    identically, with no compatibility branch. Only `dvc import` shapes are
    indexed; `dvc add` files raise `NotAnImportError` and are skipped.

    Raises `AmbiguousImport` when two `.dvc` files record the same producer
    path — the last-writer-wins shadowing D-A exists to kill; never resolved
    silently.
    """
    index: dict[str, Path] = {}
    if not namespace_dir.is_dir():
        return index
    for dvc_path in sorted(namespace_dir.rglob("*.dvc")):
        try:
            dep = DataDependency.from_dvc_file(dvc_path)
        except NotAnImportError:
            continue
        key = dep.output_path.rstrip("/")
        if key in index:
            raise AmbiguousImport(
                f"two imports of {name!r} record the same producer path "
                f"{key!r}: {index[key]} and {dvc_path}; remove one"
            )
        index[key] = dvc_path
    return index


def _namespace_by_scan(imports_root: Path, name: str) -> str:
    """The import folder for `name` when the derived namespace names nothing.

    Folders are written as `project.full_name` — `<type>_<name>` for anything
    `mintd init` scaffolds — so that is the only shape to look for, NOT any
    folder that happens to hold imports. A bare output leaf must keep failing
    (D-A: one identifier), and picking the wrong folder re-pins another
    product, which `_require_owner` cannot catch on this arm. Returns `name`
    unchanged when nothing matches, so the caller raises `ImportNotFound`.
    """
    if not imports_root.is_dir():
        return name
    matches = sorted(
        d.name for d in imports_root.iterdir()
        if d.is_dir() and d.name.endswith(f"_{name}")
    )
    if len(matches) > 1:
        raise AmbiguousImport(
            f"{name!r} has no import namespace of its own, and {len(matches)} "
            f"could be it: {', '.join(matches)}; remove or rename one"
        )
    return matches[0] if matches else name


def _require_owner(dvc_path: Path, repo_url: str | None, *, name: str) -> None:
    """Refuse a pointer found under this product's namespace that records a
    DIFFERENT producer.

    `_import_namespace` checks the namespace's shape, never its identity, and
    two single-component namespaces can name ONE directory: `full_name`
    recased (APFS and NTFS are case-insensitive), copy-pasted verbatim from a
    sibling repo (bites on Linux too), or symlinked. The index then returns
    another product's `.dvc`, whose payload the force path `shutil.rmtree`s
    and whose pointer the import rewrites — exit 0, "✓ imported".

    Compares recorded URL strings, so no case or unicode normalization is
    needed. `repo_url is None` skips the check: the bump reader has no
    catalog entry to compare against for a de-listed product.
    """
    if repo_url is None:
        return
    owner = DataDependency.from_dvc_file(dvc_path).producer_repo
    if owner != repo_url:
        raise UnknownProductPath(
            f"{dvc_path} imports {owner}, not {repo_url}; refusing to "
            f"overwrite another product's import as {name!r} — if that "
            f"producer's repo moved, delete the file and re-import"
        )


def _resolve_import_source(
    client: CatalogClient, project_path: Path, name: str, *, path: str | None
) -> Path:
    """Resolve the `(product, --path)` pair to the one `.dvc` file it names."""
    owner: str | None = None
    try:
        dumped = client.fetch(name).model_dump()
        namespace = _import_namespace(dumped, name)
        # Optional, unlike `_require_repo_url`: an entry with no
        # `repository.github_url` must stay bumpable, so a missing URL means
        # "no ownership evidence", not an error on this arm.
        owner = _section(dumped, "repository").get("github_url")
    except CatalogNotFound:
        # No entry means no `full_name` — and imports live under `full_name`,
        # which `mintd init` sets to `<type>_<name>`. It therefore never
        # equals the catalog name for a data product, so falling back to the
        # name resolved NOTHING for the case this arm exists to serve: an
        # import already on disk must stay bumpable when its entry is gone.
        # Still through `_import_namespace` so a user-supplied `..` cannot aim
        # the scan at the whole `data/` tree.
        namespace = _import_namespace({}, name)
    imports_root = project_path / "data" / "imports"
    if not (imports_root / namespace).is_dir():
        # The namespace comes from `project.full_name`, and the folder on disk
        # froze it at import time. Two ways they part company, and BOTH left
        # the import unbumpable: the entry is gone (so the fallback above is
        # the catalog name, which is never `<type>_<name>`), or the producer
        # renamed `full_name` since. Find the folder rather than insist on the
        # derived name.
        namespace = _namespace_by_scan(imports_root, name)
    index = _imports_index(imports_root / namespace, name=name)

    if path is not None:
        key = path.rstrip("/")
        if key not in index:
            raise ImportNotFound(
                f"{name!r} has no import for path {path!r} in {project_path}; "
                f"imported paths: {sorted(index) if index else 'none'}"
            )
        resolved = index[key]
    elif len(index) == 1:
        resolved = next(iter(index.values()))
    elif not index:
        raise ImportNotFound(f"{name!r} not imported in {project_path}")
    else:
        # No "resolve the primary" arm on purpose: a `.dvc` does not record
        # that it *was* the primary, and a producer rename would make "the
        # primary" name two different things at pin and at HEAD.
        raise AmbiguousImport(
            f"{name!r} has {len(index)} imported outputs; pass --path to pick "
            f"one of: {sorted(index)}"
        )
    # The bump arm has no containment check, so this is its only guard
    # against acting on another product's pointer.
    _require_owner(resolved, owner, name=name)
    return resolved


def _find_consumer_findings_for_target(
    findings: list[CheckFinding], *, source: Path, field_path: str | None = None
) -> list[CheckFinding]:
    return [
        f for f in findings
        if f.section == "consumer" and f.source == source and f.field_path == field_path
    ]


def _find_consumer_finding_for_target(
    findings: list[CheckFinding], *, source: Path, field_path: str | None = None
) -> CheckFinding | None:
    matches = _find_consumer_findings_for_target(
        findings, source=source, field_path=field_path
    )
    return matches[0] if matches else None
