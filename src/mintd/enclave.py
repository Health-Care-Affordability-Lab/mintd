"""Typed view over `enclave_manifest.yaml` + `enclave_bump`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from importlib.resources import files as _files
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from ._archive_ops import ArchiveOps, TarGzArchiveOps
from ._atomic import _atomic_write_text
from .catalog import CatalogClient
from .data import (
    BumpBlocked,
    ImportNotFound,
    PrimaryRemovedAtHead,
    _find_consumer_findings_for_target,
)
from .check import CheckFinding, _resolve_approved_product_url
from .producer import MissingPrimaryDataProduct, ProducerView
from ._dvc_ops import DvcOpError, DvcOps

if TYPE_CHECKING:
    from ._console import Reporter
    from .check import CheckFinding

__all__ = [
    "AlreadyApproved",
    "AmbiguousSubscription",
    "AppendOnlyViolation",
    "ApprovedProduct",
    "DownloadedItem",
    "EnclaveManifest",
    "EnclavePullError",
    "InvalidTransferManifest",
    "NothingNewToPackage",
    "NothingToPackage",
    "DestinationExists",
    "PathTraversalDetected",
    "TransferContent",
    "TransferManifest",
    "TransferManifestNotFound",
    "TransferredItem",
    "WrongEnclave",
    "enclave_add",
    "enclave_bump",
    "enclave_package",
    "enclave_pull",
    "enclave_remove",
    "enclave_verify",
    "subscription_label",
]


class EnclavePullError(DvcOpError):
    """A single producer's `dvc import` failed during `enclave_pull`.

    Subclasses DvcOpError so a generic DVC handler still catches it; carries
    `.repo` as structured data so the CLI can name the failing producer in
    its hint without parsing the message (slice-9 convention)."""

    def __init__(self, repo: str, cause: Exception) -> None:
        super().__init__(f"failed to pull {repo!r}: {cause}")
        self.repo = repo
        self.cause = cause

class AppendOnlyViolation(Exception):
    def __init__(self, path: Path, changed_indices: list[int]) -> None:
        super().__init__(
            f"transferred[] entries changed at indices {changed_indices} in {path}"
        )
        self.path = path
        self.changed_indices = changed_indices

class AlreadyApproved(Exception):
    def __init__(self, name: str, manifest_path: Path, label: str) -> None:
        super().__init__(
            f"{name!r} ({label}) already in approved_products[] of {manifest_path}"
        )
        self.name = name
        self.manifest_path = manifest_path
        self.label = label


class AmbiguousSubscription(Exception):
    """A repo-only selector named several subscriptions of one producer.

    Since P5 a repo can hold more than one row, so `enclave remove <repo>` with
    no selector stopped being unambiguous. Typed rather than a bare ValueError:
    `pydantic.ValidationError` subclasses ValueError, so a blanket handler in
    the CLI would swallow a malformed manifest under this exception's hint.
    """

    def __init__(self, name: str, manifest_path: Path, labels: list[str]) -> None:
        super().__init__(
            f"{name!r} has {len(labels)} subscriptions in {manifest_path}: "
            + ", ".join(labels)
        )
        self.name = name
        self.manifest_path = manifest_path
        self.labels = labels

class NothingToPackage(Exception):
    """`enclave_package` filtered `downloaded[]` to an empty set."""


class NothingNewToPackage(NothingToPackage):
    """Every selected `downloaded[]` product has already crossed the gap.

    A routine, expected outcome — not a failure. Subclasses `NothingToPackage`
    so every pre-existing `except NothingToPackage` stays correct, while the CLI
    can catch this first and exit 0 with a different message; the base class's
    "run 'mintd enclave pull' first" hint is only right for a genuinely empty
    selection."""


class InvalidTransferManifest(Exception):
    """`_transfer_manifest.yaml` malformed or references a missing directory."""


class TransferManifestNotFound(InvalidTransferManifest):
    """No `_transfer_manifest.yaml` at the given path.

    Split out because it has one overwhelmingly likely cause — the user pointed
    `verify` at the `.tar.gz` instead of at the directory they extracted it into
    — and one specific fix. The base class's "the archive is malformed" hint is
    wrong for it and sends people back across the air gap for a good archive."""


class WrongEnclave(InvalidTransferManifest):
    """The transfer was built for a different enclave than this repo.

    Mirrors `land.py`'s wrong-repo guard so the two documented landing paths
    agree. Two enclave repos on one server is the mistake it catches, and
    landing product A into enclave B is not undone by re-running anything:
    `transferred[]` is append-only, so the false row can only be removed by
    hand-editing the manifest."""


class DestinationExists(InvalidTransferManifest):
    """`data/<repo>/<version_folder>` exists with no matching `transferred[]` row.

    Almost always means the product was landed by hand (the documented
    last-resort `tar` path writes no audit row), so verify's idempotence skip
    — which keys on `transferred[]` — misses and the dest-collision guard fires."""


class PathTraversalDetected(Exception):
    """A `TransferContent` member would escape the extracted dir (CVE-2007-4559)."""

    def __init__(self, member: str) -> None:
        super().__init__(
            f"transfer manifest references {member!r} which escapes the dest dir"
        )
        self.member = member


class TransferContent(BaseModel):
    model_config = ConfigDict(frozen=True)
    repo: str
    version_folder: str  # e.g. "e8f3a2b-2026-05-11"
    contract_pin: str
    artifact_pin: str


class TransferManifest(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    enclave_name: str
    transfer_date: datetime
    transfer_id: str
    contents: list[TransferContent] = []


class ApprovedProduct(BaseModel):
    model_config = ConfigDict(frozen=False)
    repo: str
    registry_entry: str
    pin: str
    source_path: str | None = None
    all: bool = False

def subscription_label(ap: ApprovedProduct) -> str:
    """How one subscription is named to a human.

    A subscription is a `(repo, source_path|all)` PAIR, not a repo — that is
    what `enclave_add`'s guard keys on since P5. Lifted from the `enclave add`
    echo line so `enclave list`, `check` and the pull status all say the same
    thing; the two that paraphrased it used to render an `--all` row
    identically to a primary one.
    """
    # `is not None`, not truthiness: `enclave_add`'s guard keys on the triple,
    # so a row with source_path "" is a DIFFERENT subscription from a bare
    # primary. Collapsing them here would render two distinct rows identically
    # on all five call sites — including the `enclave list` screen that
    # AlreadyApproved's hint and D2's refusal both send the user to.
    if ap.source_path is not None:
        return ap.source_path or "<empty source_path>"
    return "<all>" if ap.all else "<primary>"


class DownloadedItem(BaseModel):
    model_config = ConfigDict(frozen=False)
    repo: str
    output: str
    contract_pin: str
    artifact_pin: str
    fetch_strategy: Literal["dvc-import", "subtree"]
    downloaded_at: datetime
    local_path: str

class TransferredItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    repo: str
    contract_pin: str
    artifact_pin: str
    transfer_date: date
    transfer_id: str
    local_path: str

class EnclaveManifest(BaseModel):
    schema_version: Literal["2.0"] = "2.0"
    enclave_name: str
    approved_products: list[ApprovedProduct] = []
    downloaded: list[DownloadedItem] = []
    transferred: list[TransferredItem] = []

    @classmethod
    def load(cls, path: Path) -> "EnclaveManifest":
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.model_validate(data)

    def save(self, path: Path) -> None:
        if path.exists():
            existing = EnclaveManifest.load(path)
            changed = _diff_transferred(existing.transferred, self.transferred)
            if changed:
                raise AppendOnlyViolation(path, changed)
        content = yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)
        # enclave_pull flushes this manifest from its BaseException handler, so a
        # crashed/interrupted write (e.g. a second Ctrl-C mid-write) must never
        # leave a truncated file — transferred[] provenance is append-only and
        # not re-derivable. See ``_atomic._atomic_write_text`` for the temp-name
        # and fsync discipline; this was the third copy of that body.
        _atomic_write_text(path, content)

    def apply_pin_bump(self, *, repo: str, new_pin: str) -> "EnclaveManifest":
        # EVERY row of the repo, not just the first. One repo can hold several
        # subscriptions since P5, and D1 keeps them all at one pin, so a
        # first-match return would strand rows 2+ at the old pin (the spec's
        # ['new', 'old']). The membership test stays explicit — the rebuild
        # below cannot signal "no such repo" on its own, and
        # tests/test_enclave.py:117 pins that contract.
        if not any(ap.repo == repo for ap in self.approved_products):
            raise ImportNotFound(f"{repo!r} not in approved_products[] in this manifest")
        return self.model_copy(update={"approved_products": [
            ap.model_copy(update={"pin": new_pin}) if ap.repo == repo else ap
            for ap in self.approved_products
        ]})

def _diff_transferred(existing: list[TransferredItem], current: list[TransferredItem]) -> list[int]:
    overlap = min(len(existing), len(current))
    changed: list[int] = [i for i in range(overlap) if current[i] != existing[i]]
    if len(current) < len(existing):
        changed.extend(range(len(current), len(existing)))
    return changed

def enclave_add(
    client: CatalogClient,
    *,
    manifest_path: Path,
    name: str,
    pin: str | None = None,
    source_path: str | None = None,
    all_: bool = False,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
) -> Path:
    entry = client.fetch(name)
    repo_url = entry.repo_url
    if not repo_url:
        raise ValueError(f"catalog entry {name!r} has no repository.github_url")
    existing_pin: str | None = None
    if manifest_path.exists():
        manifest = EnclaveManifest.load(manifest_path)
        # A subscription is a (repo, source_path, all) TRIPLE, not a repo.
        # Keying on the repo alone let an enclave hold exactly one product per
        # producer, which is the defect this unit exists to fix (issue33).
        #
        # ORDERING IS LOAD-BEARING: AlreadyApproved must raise inside this loop,
        # i.e. BEFORE the D1 pin-conflict ValueError below.
        # tests/test_enclave_add.py:128 and tests/test_cli.py:1242 both re-add
        # the same primary with a DIFFERENT pin and assert AlreadyApproved;
        # checking the pin first flips both to ValueError (routed through
        # cli.py's generic handler, a different message) and reddens them.
        for ap in manifest.approved_products:
            if ap.repo != name:
                continue
            if ap.source_path == source_path and ap.all == all_:
                raise AlreadyApproved(name, manifest_path, subscription_label(ap))
            # Only a REAL pin is inheritable. A hand-edited manifest can carry
            # `pin: ""` (check.py reports it as pin_missing), and inheriting it
            # would silently mint a second unusable row instead of resolving
            # HEAD. Unreachable before P5, when the repo-keyed guard refused
            # every second add.
            if ap.pin.strip():
                existing_pin = ap.pin
    else:
        manifest = EnclaveManifest(enclave_name=manifest_path.parent.name)
    if existing_pin is not None:
        # D1 (user, 2026-08-21) — ONE PIN PER REPO. `check` keys its consumer
        # findings on the repo alone and `enclave bump` takes a repo, not a row,
        # so divergent per-row pins would need a row-addressable field_path AND
        # a row-addressable bump. A second add INHERITS the recorded pin; an
        # explicit --pin that disagrees is refused rather than silently split.
        # Consequence, surfaced by the CLI at add time: if the recorded pin
        # predates this path, the add succeeds and the PULL is what fails.
        if pin is not None and pin != existing_pin:
            raise ValueError(
                f"{name!r} is already pinned at {existing_pin} in this manifest; "
                f"one pin per repo. Drop --pin to inherit it, or "
                f"'mintd enclave bump {name} --force' to move every subscription."
            )
        resolved_pin = existing_pin
    elif pin is None:
        factory = producer_view_factory or ProducerView.at_head
        head_view, resolved_pin = factory(repo_url)
        if source_path is None and not all_:
            head_view.primary_or_raise()
    else:
        resolved_pin = pin
    new_ap = ApprovedProduct(
        repo=name,
        registry_entry=f"catalog/data/{name}.yaml",
        pin=resolved_pin,
        source_path=source_path,
        all=all_,
    )
    new_manifest = manifest.model_copy(
        update={"approved_products": [*manifest.approved_products, new_ap]}
    )
    new_manifest.save(manifest_path)
    return manifest_path

def _validated_head_sha(
    client: CatalogClient,
    rows: list[ApprovedProduct],
    name: str,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None,
) -> str:
    """Resolve the producer's HEAD SHA and confirm its primary still exists.

    Pure: resolves the repo URL, fetches the HEAD view via the injected
    factory (or `ProducerView.at_head`), and maps a missing primary to
    `PrimaryRemovedAtHead`. Returns the resolved SHA only — no manifest
    mutation. Shared by the `--force` and drift paths so both keep the exact
    same resolve→validate semantics.

    Takes ALL of the repo's rows, not one: repo identity is row-independent
    (`_resolve_approved_product_url` reads `ap.repo` only), but the primary
    check is not — see the gate below.
    """
    repo_url = _resolve_approved_product_url(client, rows[0])
    factory = producer_view_factory or ProducerView.at_head
    head_view, head_sha = factory(repo_url)
    # ANY primary-output subscription of this repo forces the validation.
    # Only primary subscriptions depend on data_products.primary;
    # source_path/all subscriptions do not (mirrors enclave_add, which skips
    # this check for them). Validating primary for those would wrongly block a
    # repin to a producer that legitimately has no primary.
    # Gating on `rows[0]` instead would be decided by ADD ORDER, and since
    # apply_pin_bump now moves every row together an unvalidated bump would
    # repin a primary subscription to a HEAD that has no primary — exit 0.
    if any(ap.source_path is None and not ap.all for ap in rows):
        try:
            head_view.primary_or_raise()
        except MissingPrimaryDataProduct as e:
            raise PrimaryRemovedAtHead(name, repo_url) from e
    return head_sha


def enclave_bump(
    client: CatalogClient,
    *,
    manifest_path: Path,
    project_path: Path | None = None,
    name: str,
    force: bool = False,
    producer_view_factory: Callable[[str], tuple[ProducerView, str]] | None = None,
    check_findings: list[CheckFinding] | None = None,
) -> Path | None:
    from .check import check_project
    project_path = project_path if project_path is not None else manifest_path.parent
    manifest = EnclaveManifest.load(manifest_path)
    rows = [ap for ap in manifest.approved_products if ap.repo == name]
    if not rows:
        raise ImportNotFound(f"{name!r} not in approved_products[] in {manifest_path}")
    # `--force` repins straight to the producer's validated HEAD, bypassing the
    # check_project finding gate entirely (which returns None on `up_to_date`
    # before HEAD is ever resolved). Placed AFTER the ImportNotFound guard so a
    # missing repo still errors, and BEFORE the findings computation so the
    # up_to_date early-return can't pre-empt a forced repin. Only the primary is
    # validated at HEAD — the BumpBlocked-class gates (pin_missing, drift,
    # schema/metadata) are deliberately skipped under force.
    if force:
        head_sha = _validated_head_sha(client, rows, name, producer_view_factory)
        # `all(...)`, not `rows[0].pin ==`: a hand-edited manifest whose rows
        # disagree must still converge on the bump rather than strand rows 2+.
        if all(ap.pin == head_sha for ap in rows):
            return None
        new_manifest = manifest.apply_pin_bump(repo=name, new_pin=head_sha)
        new_manifest.save(manifest_path)
        return manifest_path
    findings = (
        check_findings
        if check_findings is not None
        else check_project(project_path, upgrades=True, client=client)
    )
    repo_findings = _find_consumer_findings_for_target(
        findings, source=manifest_path, field_path=f"approved_products[{name}]"
    )
    if not repo_findings:
        raise ImportNotFound(f"no consumer finding for {name!r} (manifest={manifest_path})")
    # `check` emits one finding per ROW under a single repo-keyed field_path,
    # and rows can disagree — one path drifted while another cannot be read.
    # Reading only the first match made this WRITE verb depend on YAML row
    # order — `bump` moved the pin or printed "up to date" purely by which row
    # was added first. Precedence: any blocked row blocks the whole repo (D1 =
    # one pin, so a partial bump is not a thing), else drift wins, else up to
    # date. `kind is None` falls into the blocked arm, as it did before.
    blocked = next(
        (f for f in repo_findings if f.kind not in ("drift", "up_to_date")), None
    )
    if blocked is not None:
        raise BumpBlocked(name, blocked)
    if not any(f.kind == "drift" for f in repo_findings):
        return None
    head_sha = _validated_head_sha(client, rows, name, producer_view_factory)
    new_manifest = manifest.apply_pin_bump(repo=name, new_pin=head_sha)
    new_manifest.save(manifest_path)
    return manifest_path

def enclave_remove(
    client: CatalogClient,
    *,
    manifest_path: Path,
    name: str,
    source_path: str | None = None,
    all_: bool = False,
    primary: bool = False,
    downloads_root: Path | None = None,
) -> Path:
    del client
    # Selectors, mutually exclusive at the CLI:
    #   --source-path X  -> the row subscribed to X
    #   --primary        -> the bare-primary row (source_path None, all False)
    #   --all            -> EVERY row of the repo (what bare `remove` did before
    #                       a repo could hold more than one subscription)
    # NOTE `--all` means something different on `enclave add`, where it is the
    # all-outputs subscription. Flagged, not resolved — see BACKLOG.
    manifest = EnclaveManifest.load(manifest_path)
    def _matches_approved(ap: ApprovedProduct) -> bool:
        if ap.repo != name:
            return False
        if source_path is not None:
            return ap.source_path == source_path
        if primary:
            return ap.source_path is None and not ap.all
        return True
    repo_rows = [ap for ap in manifest.approved_products if ap.repo == name]
    if not repo_rows:
        raise ImportNotFound(f"{name!r} not in approved_products[] in {manifest_path}")
    matched = [ap for ap in repo_rows if _matches_approved(ap)]
    if not matched:
        # Repo-level absence and SELECTOR-level absence are different failures,
        # and reporting the first for the second is a lie the user can check:
        # `enclave list` shows the repo right there. Reachable straight from
        # AmbiguousSubscription's hint -- on a `[<path>, <all>]` repo the hint
        # offers `--primary`, and no primary row exists.
        raise ImportNotFound(
            f"no subscription of {name!r} matches that selector in {manifest_path}; "
            "it has: " + ", ".join(subscription_label(ap) for ap in repo_rows)
        )
    # D2 (user, 2026-08-21): bare `remove <repo>` was unambiguous only while a
    # repo held one row. Refuse rather than wipe subscriptions the user did not
    # name. Single-row repos behave exactly as before.
    if len(matched) > 1 and source_path is None and not primary and not all_:
        raise AmbiguousSubscription(
            name, manifest_path, [subscription_label(ap) for ap in matched]
        )
    new_approved = [ap for ap in manifest.approved_products if not _matches_approved(ap)]
    # downloaded[] is the provenance record for what is on disk, and
    # `enclave_package` selects straight off it — it never consults
    # approved_products. So a row left behind for an unsubscribed product goes
    # into the next transfer archive and crosses the air gap.
    #
    # WHEN IN DOUBT, DROP. A bare-primary row's resolved output is unknowable
    # here without a producer fetch, so it may or may not be the output being
    # unsubscribed, and the two errors are not symmetric:
    #   drop a row a survivor still wanted -> the next `pull` re-imports it
    #     (the fast-skip misses, `_already_downloaded` misses). Recoverable.
    #   keep a row the user revoked        -> revoked bytes ship into an
    #     enclave on a one-way transfer.   NOT recoverable.
    # An earlier revision kept every row whenever a bare primary survived, on
    # the reasoning that deleting provenance makes data un-packageable. That
    # reads the asymmetry backwards: un-packageable costs a re-fetch.
    surviving = [ap for ap in new_approved if ap.repo == name]
    claimed_by_survivor = {ap.source_path for ap in surviving if ap.source_path is not None}
    keeps_everything = any(ap.all for ap in surviving)

    def _keep_downloaded(d: DownloadedItem) -> bool:
        if d.repo != name:
            return True
        # A surviving `all` row genuinely claims every output of the repo.
        if keeps_everything:
            return True
        if source_path is not None:
            return d.output != source_path
        if primary:
            return d.output in claimed_by_survivor
        return False  # --all / bare single-row: the repo goes entirely

    new_downloaded = [d for d in manifest.downloaded if _keep_downloaded(d)]
    new_manifest = manifest.model_copy(
        update={"approved_products": new_approved, "downloaded": new_downloaded}
    )
    new_manifest.save(manifest_path)
    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    repo_downloads = downloads_root / name
    # Wipe downloads/<repo>/ only if no other manifest entry still references it.
    # Guards: no remaining approved_products[] entry for this repo, AND no
    # remaining downloaded[] entry for this repo. transferred[] entries point at
    # data/<repo>/... (different root) so they don't gate this wipe.
    if (
        repo_downloads.exists()
        and not any(ap.repo == name for ap in new_approved)
        and not any(d.repo == name for d in new_downloaded)
    ):
        shutil.rmtree(repo_downloads)
    return manifest_path

def enclave_pull(
    client: CatalogClient,
    dvc_ops: DvcOps,
    *,
    manifest_path: Path,
    repo: str | None = None,
    force: bool = False,
    downloads_root: Path | None = None,
    producer_view_factory: Callable[[str, str], ProducerView] | None = None,
    today: date | None = None,
    reporter: "Reporter | None" = None,
) -> tuple[Path, list[DownloadedItem]]:
    """Fetch every approved product into the enclave beside `manifest_path`.

    **The enclave is `manifest_path.parent`, and that is where dvc runs.** Both
    `init` and `import_` are given it as `cwd`, so this works from any process
    directory. It did not always: `import_` had no `cwd` to be given, so it ran
    wherever the caller stood, and pulling from outside the enclave cached the
    producer's restricted bytes into the enclosing repo at exit 0.

    **`downloads_root` must therefore stay INSIDE `manifest_path.parent`.** dvc
    refuses an output that falls outside the repo it is running in, so an
    override pointing elsewhere now fails loudly where it used to "work" by
    accident of the process cwd. Left as a constraint rather than a guard: no
    production caller overrides it, and the failure is dvc's own and legible.
    """
    manifest = EnclaveManifest.load(manifest_path)
    targets = [ap for ap in manifest.approved_products if repo is None or ap.repo == repo]
    if repo is not None and not targets:
        raise ImportNotFound(f"{repo!r} not in approved_products[] in {manifest_path}")
    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    # `dvc import` requires the enclave dir to be a DVC repo, but `init`
    # deliberately skips DVC *storage* wiring for enclaves (Slice 30). A bare
    # local `.dvc/` is orthogonal to that — `import` fetches from the source
    # repo's remote, not the enclave's. Lazily create it (idempotent; the op
    # also tolerates an existing repo) so a fresh enclave pulls with no manual
    # `dvc init`. No storage remote is written, so the Slice-30 invariant holds.
    if not (manifest_path.parent / ".dvc").exists():
        dvc_ops.init(cwd=manifest_path.parent)
    today_iso = (today or date.today()).isoformat()
    factory = producer_view_factory or (lambda url, pin: ProducerView.at(url, pin))
    new_downloaded: list[DownloadedItem] = list(manifest.downloaded)
    written: list[DownloadedItem] = []
    created_target_dirs: set[Path] = set()
    # (output, pin) pairs imported during THIS call — see the dedup below.
    written_this_run: set[tuple[str, str, str]] = set()

    def _save_downloaded() -> None:
        # Persist downloaded[] progress. Safe to call repeatedly:
        # EnclaveManifest.save's append-only guard (_diff_transferred) protects
        # transferred[] ONLY, and enclave_pull never mutates transferred[] — so
        # incremental saves of downloaded[] can never raise AppendOnlyViolation.
        # Reads new_downloaded at call time, so it sees the force-prune rebind
        # below (enclosing-scope late binding); a coder promoting this to a
        # module helper must pass new_downloaded in.
        manifest.model_copy(update={"downloaded": new_downloaded}).save(manifest_path)

    for i, ap in enumerate(targets, 1):
        # Per-subscription feedback (slice 38a). Fired BEFORE the idempotence
        # skip so the (i/N) count reflects every subscription, not just the
        # ones that needed fetching. N counts SUBSCRIPTIONS, not producers —
        # one repo can contribute several rows since P5, so the label is what
        # tells two of its lines apart.
        if reporter is not None:
            reporter.update_status(
                f"Fetching {ap.repo} [{subscription_label(ap)}]... ({i}/{len(targets)})"
            )
        # Idempotence: skip resolving if all outputs are already present.
        # A skip mutates nothing, so it stays outside the try/save below.
        # `manifest.approved_products`, not `targets` — targets has already been
        # filtered by the optional `repo` argument, which would hide siblings.
        if not force and _all_already_downloaded(
            manifest.downloaded, ap, manifest.approved_products
        ):
             continue

        try:
            entry = client.fetch(ap.repo)
            repo_url = entry.repo_url
            if not repo_url:
                raise ValueError(f"catalog entry {ap.repo!r} has no repository.github_url")
            outputs = _resolve_outputs(ap, repo_url, factory)
            for output in outputs:
                # `new_downloaded`, not the pre-run `manifest.downloaded`
                # snapshot: two rows of one repo can resolve to overlapping
                # outputs (e.g. `--source-path x` alongside `--all`, which
                # became CLI-reachable when the add guard widened), and against
                # the snapshot the second row re-imports x and appends a
                # duplicate downloaded[] row to a custody manifest.
                # Two guards, deliberately different:
                #   `not force` + manifest.downloaded -> idempotence across RUNS,
                #     which --force exists to override.
                #   written_this_run                  -> two rows of ONE repo
                #     resolving to the same output (e.g. `--source-path x`
                #     alongside `--all`, newly reachable since the add guard
                #     widened). Importing it twice in a single pull is never
                #     what --force asked for, and it appends a duplicate
                #     provenance row to a custody manifest.
                if (ap.repo, output, ap.pin) in written_this_run:
                    continue
                if not force and _already_downloaded(new_downloaded, ap.repo, output, ap.pin):
                    continue
                staging_dir = downloads_root / ap.repo / "_staging"
                # Defensive: clear stale _staging from a prior interrupted run.
                # Without this, dvc_ops.import_ would refuse to overwrite the
                # existing dest, breaking future pulls until manual cleanup.
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                # `dvc import` writes its stage pointer into staging_dir and
                # requires that working dir to already exist (it won't auto-create
                # it) — else it fails with "stage working dir ... does not exist".
                # Mirror the consumer-import path (data.import_product).
                staging_dir.mkdir(parents=True, exist_ok=True)
                dest = staging_dir / Path(output.rstrip("/")).name
                try:
                    dvc_path = dvc_ops.import_(
                        cwd=manifest_path.parent,
                        repo_url=repo_url,
                        path=output,
                        dest=dest,
                        rev=ap.pin,
                        force=force,
                    )
                except DvcOpError as exc:
                    raise EnclavePullError(ap.repo, exc) from exc
                artifact_pin = _read_artifact_pin(dvc_path)
                target_dir = downloads_root / ap.repo / f"{artifact_pin[:7]}-{today_iso}"
                if force and target_dir.exists() and target_dir not in created_target_dirs:
                    shutil.rmtree(target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                created_target_dirs.add(target_dir)
                # Defensive: clear any stale destination from a previous interrupted
                # run. Without this, shutil.move would nest dest inside the existing
                # target (e.g., target/dest/dest) when the prior run died after the
                # move but before manifest.save.
                final_dest = target_dir / dest.name
                if final_dest.exists():
                    if final_dest.is_dir():
                        shutil.rmtree(final_dest)
                    else:
                        final_dest.unlink()
                shutil.move(str(dest), str(final_dest))
                if staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
                # Force replaces the existing (repo, output, pin) row. Prune it
                # ONLY now — after the import succeeded — and append the new row
                # in the same step, so prune+append are atomic. Pruning earlier
                # would let the failure flush below persist a manifest missing
                # the row of a product whose re-import failed, silently dropping
                # its provenance record while its old data lingers on disk.
                if force:
                    new_downloaded = [
                        d for d in new_downloaded
                        if not (d.repo == ap.repo and d.output == output and d.contract_pin == ap.pin)
                    ]
                item = DownloadedItem(
                    repo=ap.repo,
                    output=output,
                    contract_pin=ap.pin,
                    artifact_pin=artifact_pin,
                    fetch_strategy="dvc-import",
                    downloaded_at=datetime.now(),
                    local_path=str(target_dir),
                )
                new_downloaded.append(item)
                written.append(item)
                written_this_run.add((ap.repo, output, ap.pin))
        except BaseException:
            # A producer raised (bad pin, missing repo_url ValueError, missing
            # primary via _resolve_outputs, catalog/network, dvc import ->
            # EnclavePullError) or the run was interrupted. Flush the manifest
            # reflecting every product completed so far — plus any outputs of THIS
            # product already fetched and moved — before propagating, so a partial
            # run's on-disk data is recorded and the next run skips it. Fail-loud
            # is preserved: we always re-raise UNCHANGED, so cli.py's
            # EnclavePullError hint still fires and exit codes are unchanged.
            # BaseException (not Exception) so a KeyboardInterrupt/SystemExit
            # mid-pull also flushes; the bare `raise` guarantees it is never
            # swallowed.
            _save_downloaded()
            raise
        # A fully-fetched product is persisted NOW so a later producer's failure
        # or an interrupt can't discard it (the primary Defect-1 fix:
        # SAVE-PER-PRODUCT). Covers every mutation path above, including the
        # force-prune branch that rebinds new_downloaded.
        _save_downloaded()
    return manifest_path, written

def _resolve_outputs(
    ap: ApprovedProduct,
    repo_url: str,
    factory: Callable[[str, str], ProducerView],
) -> list[str]:
    if ap.source_path is not None:
        return [ap.source_path]
    view = factory(repo_url, ap.pin)
    if ap.all:
        return view.output_paths()
    return [view.primary_or_raise()]

def _already_downloaded(
    downloaded: list[DownloadedItem], repo: str, output: str, pin: str
) -> bool:
    return any(
        d.repo == repo and d.output == output and d.contract_pin == pin
        for d in downloaded
    )

def _all_already_downloaded(
    downloaded: list[DownloadedItem],
    ap: ApprovedProduct,
    approved: list[ApprovedProduct],
) -> bool:
    # An `all` product's output set can GROW (the producer may add outputs
    # later), so it must never be fast-skipped here — the inner
    # _already_downloaded check governs per-output re-fetch instead.
    if ap.all:
        return False
    if ap.source_path is not None:
        # source_path IS the resolved output the write path records, so reuse the
        # exact-output check the inner loop uses — both idempotence checks now
        # agree on one key representation.
        return _already_downloaded(downloaded, ap.repo, ap.source_path, ap.pin)
    # Primary product: the resolved output path is unknowable without the catalog
    # fetch + producer-view resolve this fast-path exists to AVOID, so it can
    # only key on (repo, contract_pin). That key is exact ONLY while a repo holds
    # one subscription. It used to, because enclave_add rejected duplicate repos
    # — and this comment cited that guard as its correctness proof. P5 deleted
    # the guard, so a repo can now hold several rows, and a SIBLING row's
    # downloaded[] entry satisfies (repo, pin) without the primary ever having
    # been fetched: pull would exit 0 having fetched nothing.
    #
    # The check is on the row COUNT, not on the siblings' source_paths: an `all`
    # sibling has source_path None and would contribute nothing to a set of
    # sibling paths, leaving [--all row, primary row] silently skipping. A
    # multi-row repo simply falls through to the resolve, where the per-output
    # _already_downloaded check in the pull loop is exact. Costs one catalog
    # fetch + one producer resolve per multi-row repo per pull.
    if sum(1 for other in approved if other.repo == ap.repo) > 1:
        return False
    return any(d.repo == ap.repo and d.contract_pin == ap.pin for d in downloaded)

def _read_artifact_pin(dvc_path: Path) -> str:
    data = yaml.safe_load(dvc_path.read_text(encoding="utf-8"))
    outs = data.get("outs") or []
    if not outs:
        raise ValueError(f"{dvc_path} has no outs[]")
    first = outs[0]
    if not isinstance(first, dict):
        raise ValueError(f"{dvc_path} outs[0] is not a dict")
    md5 = first.get("md5")
    if not isinstance(md5, str):
        raise ValueError(f"{dvc_path} outs[0].md5 missing or non-str")
    return md5


def _next_transfer_id(manifest: EnclaveManifest, today_iso: str) -> str:
    """Pick the next sequence number for today's transfers.

    Sequence resets daily. Format: `transfer-YYYY-MM-DD-NNNNNN`.
    """
    prefix = f"transfer-{today_iso}-"
    used: set[int] = set()
    for t in manifest.transferred:
        if not t.transfer_id.startswith(prefix):
            continue
        suffix = t.transfer_id.removeprefix(prefix)
        if suffix.isdigit():
            used.add(int(suffix))
    seq = 0
    while seq in used:
        seq += 1
    return f"{prefix}{seq:06d}"


def enclave_package(
    *,
    manifest_path: Path,
    name: str | None = None,
    downloads_root: Path | None = None,
    output_archive: Path | None = None,
    output_dir: Path | None = None,
    archive_ops: ArchiveOps | None = None,
    today: date | None = None,
    resend: bool = False,
) -> tuple[Path, list[DownloadedItem]]:
    """Bundle outside-enclave `downloaded[]` into a `.tar.gz` transfer archive.

    Exactly one of `output_archive` / `output_dir` must be provided. When
    only `output_dir` is given, the archive filename is derived from the
    computed `transfer_id` (`<output_dir>/<transfer_id>.tar.gz`), which
    guarantees uniqueness across same-day runs.

    Filters `downloaded[]` to `name` if given; raises `NothingToPackage`
    when the filtered set is empty. Appends one `TransferredItem` per
    packaged entry to the outside-enclave manifest, saved through the
    slice-8 append-only seam. If `archive_ops.pack` raises, the manifest
    is never mutated (pack runs inside the `TemporaryDirectory`; save
    runs only after it exits cleanly).

    Bundles are **incremental**: products whose `(repo, artifact_pin)` is
    already in `transferred[]` are skipped, and `NothingNewToPackage` is
    raised if that leaves nothing. Pass `resend=True` to bundle them anyway
    (for a bundle that was built but never arrived).

    Every archive carries a generated `README.md` describing its own
    contents and where to land them — see `_render_bundle_readme`.

    Returns `(archive, skipped)` where `skipped` lists the `downloaded[]`
    entries left out because they had already crossed the gap.
    """
    if output_archive is None and output_dir is None:
        raise ValueError("Either output_archive or output_dir must be provided")

    manifest = EnclaveManifest.load(manifest_path)
    selected = [d for d in manifest.downloaded if name is None or d.repo == name]
    if not selected:
        raise NothingToPackage(
            f"no downloaded[] entries{' for ' + name if name else ''} in {manifest_path}"
        )

    # Incremental selection. The key is (repo, artifact_pin) — "have these bytes
    # already crossed?" — deliberately NOT the (repo, contract_pin, artifact_pin)
    # triple that `enclave_verify` keys on. A metadata-only pin bump produces a
    # new contract_pin over identical bytes; under the triple those bytes ship
    # again and land inside the gap as a byte-identical duplicate, or collide
    # with the existing dir and fail verify there — where the only fix is a
    # physical round trip. Accepted cost: a metadata-only bump ships nothing, so
    # the inside trail does not record the new producer commit.
    #
    # Also NOT local_path/version_folder: both embed the PULL date, so a re-pull
    # on a later day would silently miss and re-ship gigabytes.
    shipped = {(t.repo, t.artifact_pin) for t in manifest.transferred}
    if resend:
        fresh, skipped = selected, []
    else:
        fresh = [d for d in selected if (d.repo, d.artifact_pin) not in shipped]
        skipped = [d for d in selected if (d.repo, d.artifact_pin) in shipped]

    # Same bytes recorded twice under different contract pins (a same-day pin
    # bump: the folder name at `enclave_pull` omits contract_pin, so both rows
    # share one local_path). Copying both raises an uncaught FileExistsError in
    # `shutil.copytree` below. Copy once; a later row wins, so the newest
    # contract pin is what crosses. Dict preserves first insertion position, so
    # bundle order stays stable.
    by_bytes: dict[tuple[str, str], DownloadedItem] = {}
    for d in fresh:
        by_bytes[(d.repo, d.artifact_pin)] = d
    targets = list(by_bytes.values())

    # Residual guard: distinct artifact pins whose 7-char prefixes collide on the
    # same day. Astronomically unlikely, but it is the one remaining path to that
    # same uncaught FileExistsError.
    folders: dict[tuple[str, str], str] = {}
    for d in targets:
        vf = Path(d.local_path).name
        if folders.setdefault((d.repo, vf), d.artifact_pin) != d.artifact_pin:
            raise InvalidTransferManifest(
                f"two downloaded[] entries with different artifact pins share {d.repo}/{vf}"
            )

    # Raised BEFORE _next_transfer_id and before save(), so an up-to-date
    # `package` mints no transfer id and leaves the manifest byte-identical.
    if not targets:
        raise NothingNewToPackage(
            f"all {len(selected)} downloaded product(s)"
            f"{' for ' + name if name else ''} have already crossed the gap"
        )

    downloads_root = downloads_root or (manifest_path.parent / "downloads")
    today_iso = (today or date.today()).isoformat()
    transfer_id = _next_transfer_id(manifest, today_iso)

    if output_archive is None:
        assert output_dir is not None  # for mypy; checked above
        output_archive = output_dir / f"{transfer_id}.tar.gz"

    contents: list[TransferContent] = []
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        for d in targets:
            version_folder = Path(d.local_path).name
            src = downloads_root / d.repo / version_folder
            if not src.exists():
                raise InvalidTransferManifest(
                    f"downloaded[] entry references missing dir: {src}"
                )
            dest = tmp / d.repo / version_folder
            # `symlinks=True` preserves symlinks so the `pack` time
            # symlink-traversal guard can inspect them. Without it,
            # `copytree` dereferences hostile symlinks (e.g.,
            # `/etc/passwd`) into plain files inside the staging dir,
            # silently bypassing `TarGzArchiveOps.pack`'s check.
            shutil.copytree(src, dest, symlinks=True)
            contents.append(
                TransferContent(
                    repo=d.repo,
                    version_folder=version_folder,
                    contract_pin=d.contract_pin,
                    artifact_pin=d.artifact_pin,
                )
            )

        transfer_manifest = TransferManifest(
            enclave_name=manifest.enclave_name,
            transfer_date=datetime.now(timezone.utc),
            transfer_id=transfer_id,
            contents=contents,
        )
        (tmp / "_transfer_manifest.yaml").write_text(
            yaml.safe_dump(
                transfer_manifest.model_dump(mode="json"), sort_keys=False
            ),
            encoding="utf-8",
        )
        # Rendered from the same TransferManifest that was just built, so the
        # README cannot describe a bundle other than this one. The archive is
        # the only artifact guaranteed to cross the gap, so the answer to
        # "where do these files go" has to travel inside it.
        (tmp / "README.md").write_text(
            _render_bundle_readme(transfer_manifest, output_archive.name),
            encoding="utf-8",
        )
        # JSON sibling of the YAML manifest: `land.py` runs on a box with no
        # PyYAML, so it needs a stdlib-readable copy. Same object, dumped twice
        # three lines apart, so the two cannot drift.
        (tmp / "_transfer_manifest.json").write_text(
            json.dumps(transfer_manifest.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        # Shipped inside the archive rather than scaffolded into the enclave
        # repo: the archive is the only thing guaranteed to cross, so lander and
        # format stay version-locked and an old enclave clone gets a working
        # lander with its next bundle.
        land = tmp / "land.py"
        land.write_text(
            (_files("mintd") / "files" / "land.py.txt").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        land.chmod(0o755)

        # The staging root is 0700 (inherited from TemporaryDirectory) and
        # `pack` adds it as the archive's root member. GNU tar applies that mode
        # to an existing extraction target, silently stripping group access on a
        # shared enclave server. Cheaper and more portable than a bsdtar-
        # incompatible extract flag in the documented command.
        os.chmod(tmp, 0o755)

        ops = archive_ops or TarGzArchiveOps()
        ops.pack(tmp, output_archive)

    # `pack` succeeded — now record each packaged item in `transferred[]`.
    # `.resolve()` ensures the stored path is absolute regardless of whether
    # `downloads_root` was passed as a relative path.
    new_transferred = list(manifest.transferred)
    for content in contents:
        local_path = str(
            (downloads_root / content.repo / content.version_folder).resolve()
        )
        new_transferred.append(
            TransferredItem(
                repo=content.repo,
                contract_pin=content.contract_pin,
                artifact_pin=content.artifact_pin,
                transfer_date=date.fromisoformat(today_iso),
                transfer_id=transfer_id,
                local_path=local_path,
            )
        )
    new_manifest = manifest.model_copy(update={"transferred": new_transferred})
    new_manifest.save(manifest_path)
    return output_archive, skipped


def _render_bundle_readme(tm: TransferManifest, archive_name: str) -> str:
    """Render the per-transfer `README.md` shipped at the archive root.

    Plain markdown, readable with `cat` — the enclave researcher may have no
    markdown viewer. Pins truncated to 7 chars to keep the table narrow.

    Derived entirely from `tm`, so it cannot describe products this bundle does
    not carry. Kept as a plain f-string rather than a Jinja template: Jinja is a
    scaffold-time dependency and `enclave_package` has no template context.
    """
    rows = "\n".join(
        f"| {c.repo} | {c.version_folder} | {c.contract_pin[:7]} | {c.artifact_pin[:7]} |"
        for c in tm.contents
    )
    dests = "\n".join(f"    data/{c.repo}/{c.version_folder}/" for c in tm.contents)
    n = len(tm.contents)
    # The real filename, not one derived from `transfer_id`: `--output` can
    # name the archive anything, and the commands below have to name the file
    # that actually crossed the gap.
    archive = archive_name
    return f"""# Transfer {tm.transfer_id}

Built {tm.transfer_date.date().isoformat()} for enclave `{tm.enclave_name}`.
Contains {n} data product{"" if n == 1 else "s"}.

| Product | Version folder | Contract pin | Artifact pin |
|---------|----------------|--------------|--------------|
{rows}

The date in each version folder is the date the data was **pulled** from the
producer, not the date of this transfer. An older date does not mean stale data.

## Where these files go

Each product lands at `data/<product>/<version folder>/` in the enclave repo:

{dests}

## How to land them

    cd /path/to/{tm.enclave_name}
    mkdir -p incoming && tar -xzf {archive} -C incoming
    python3 incoming/land.py

`land.py` needs only python3 — no pip, no network, no DVC. It moves each product
into place and records it in `enclave_manifest.yaml`. Run it twice and the second
run does nothing. Use `--dry-run` to see what it would do first.

If this enclave has mintd installed (needs Python 3.11+), `mintd enclave verify
incoming` does the same job.

## If you must do it by hand

    mkdir -p data
    tar -xzf {archive} -C data
    mkdir -p transfers/received
    mv data/_transfer_manifest.yaml transfers/received/{tm.transfer_id}.yaml
    rm -f data/_transfer_manifest.json data/land.py data/README.md

This places the files but writes **no** `transferred[]` row, so the audit trail
will be missing this transfer. Running `land.py` afterwards will not silently
paper over that: it refuses, names the product, and prints the row for you to
append. `mintd enclave verify` refuses too. Prefer `land.py` from the start.
"""


def enclave_verify(
    *,
    extracted_dir: Path,
    manifest_path: Path,
    data_root: Path | None = None,
) -> tuple[Path, list[TransferredItem]]:
    """Reconcile a user-extracted transfer dir into the inside-enclave manifest.

    Path-traversal guard runs **before** any filesystem mutation. Three
    string-level pre-checks (`is_absolute()` and `..` segments on both
    `content.repo` and `content.version_folder`) plus two resolve-based
    checks (the constructed member path, and an `rglob` walk for symlinks
    inside the data) together cover the CVE-2007-4559 family. All
    `startswith` comparisons append `os.sep` to avoid sibling-directory
    false positives.

    Idempotent: entries whose `(repo, contract_pin, artifact_pin)` triple
    is already in `transferred[]` are skipped, so re-running on the same
    extracted dir is a no-op.

    Returns `(manifest_path, written)` where `written` lists only the
    newly-appended `TransferredItem`s.
    """
    manifest_yaml = extracted_dir / "_transfer_manifest.yaml"
    if not manifest_yaml.is_file():
        raise TransferManifestNotFound(
            f"_transfer_manifest.yaml not found at {manifest_yaml}"
        )

    try:
        raw = yaml.safe_load(manifest_yaml.read_text(encoding="utf-8")) or {}
        transfer = TransferManifest.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise InvalidTransferManifest(str(e)) from e

    # Load the inside-enclave manifest up front so the validation loop
    # can skip entries that are already in `transferred[]`. Without
    # this, a re-run after a successful `verify` would fail the
    # existence check (the data was moved into `data_root`) — breaking
    # the idempotence contract.
    manifest = EnclaveManifest.load(manifest_path)

    # Wrong-enclave guard, before anything moves. `land.py` refuses the same
    # mismatch; without this the mintd path would be the less safe of the two
    # landing paths the README presents as equivalent.
    if transfer.enclave_name != manifest.enclave_name:
        raise WrongEnclave(
            f"transfer was built for enclave {transfer.enclave_name!r} but "
            f"{manifest_path} is enclave {manifest.enclave_name!r}"
        )

    data_root = data_root or (manifest_path.parent / "data")
    existing_keys = {
        (t.repo, t.contract_pin, t.artifact_pin) for t in manifest.transferred
    }

    extracted_abs = extracted_dir.resolve()
    extracted_prefix = str(extracted_abs) + os.sep

    # Track destination paths seen so far in this single transfer to
    # reject manifests that would move two entries to the same dest
    # (which would surface as a `FileNotFoundError` from the second
    # `shutil.move`, leaving the first move stranded without a
    # `transferred[]` entry).
    seen_dests: set[Path] = set()
    for content in transfer.contents:
        # (a) String pre-check on `repo`. Without this, an absolute `repo`
        # would silently discard the left operand of `Path.__truediv__`
        # (e.g., `extracted_dir / "/etc" / "passwd"` → `Path("/etc/passwd")`).
        # Path-traversal pre-checks run unconditionally — even for
        # already-verified entries — so a hostile re-uploaded manifest
        # is rejected before any filesystem access. Empty string and `.`
        # are also rejected because both produce `Path(...).parts == ()`,
        # bypassing the `..` check; their effect with `Path.__truediv__`
        # is to resolve back to `extracted_dir` / `data_root`. Nested
        # paths (e.g., `A/B`) are rejected because the `dest`-collision
        # check below operates on leaf paths; a manifest pairing
        # `version_folder = "B"` and `version_folder = "B/C"` would
        # otherwise pass collision validation, then crash mid-move
        # when the second `shutil.move` finds `B/C`'s source under the
        # already-moved `B`. Repo/version_folder are flat segments by
        # design (see `_resolve_outputs` in slice 13).
        if (
            not content.repo
            or content.repo == "."
            or "/" in content.repo
            or "\\" in content.repo
            or Path(content.repo).is_absolute()
            or ".." in Path(content.repo).parts
        ):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )
        # (b) String pre-check on `version_folder`. `..` resolves
        # silently *inside* `extracted_dir` if paired with a deep
        # subpath, bypassing a pure `resolve()`-based check. Empty
        # string, `.`, and nested paths are rejected for the same
        # reasons as in the `repo` check above.
        if (
            not content.version_folder
            or content.version_folder == "."
            or "/" in content.version_folder
            or "\\" in content.version_folder
            or Path(content.version_folder).is_absolute()
            or ".." in Path(content.version_folder).parts
        ):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )

        # Skip filesystem checks for entries already in transferred[].
        # The first verify moved them out of `extracted_dir`, so the
        # existence check would falsely fail — see the idempotence
        # contract in the docstring.
        key = (content.repo, content.contract_pin, content.artifact_pin)
        if key in existing_keys:
            continue

        # (c) Existence check — safe now that string-level guards passed.
        member = extracted_dir / content.repo / content.version_folder
        if not member.exists():
            raise InvalidTransferManifest(
                f"manifest references {content.repo}/{content.version_folder} but dir not present"
            )

        # (d) Resolve check — catches symlink at the version_folder
        # itself pointing outside `extracted_dir`.
        resolved = str(member.resolve())
        if resolved != str(extracted_abs) and not resolved.startswith(extracted_prefix):
            raise PathTraversalDetected(
                f"{content.repo}/{content.version_folder}"
            )

        # (e) Symlink walk — catches symlinks inside the versioned data
        # pointing outside `extracted_dir`. Target need not exist;
        # `p.resolve()` still produces an absolute path we can check.
        for p in member.rglob("*"):
            if p.is_symlink():
                target = str(p.resolve())
                if target != str(extracted_abs) and not target.startswith(extracted_prefix):
                    raise PathTraversalDetected(str(p))

        # (f) Dest collision check — refuse to overwrite an existing
        # `data_root/<repo>/<version_folder>` (legitimate prior data)
        # and refuse two contents that target the same dest. Done in
        # the validation pass so partial moves can't strand entries on
        # disk without `transferred[]` rows.
        dest = data_root / content.repo / content.version_folder
        if dest in seen_dests:
            raise InvalidTransferManifest(
                f"transfer manifest contains duplicate destination {dest}"
            )
        seen_dests.add(dest)
        if dest.exists():
            raise DestinationExists(
                f"refusing to overwrite existing dest {dest} for new transferred[] entry"
            )

    new_transferred = list(manifest.transferred)
    written: list[TransferredItem] = []
    for content in transfer.contents:
        key = (content.repo, content.contract_pin, content.artifact_pin)
        if key in existing_keys:
            # Idempotent — already verified.
            continue
        src = extracted_dir / content.repo / content.version_folder
        dest = data_root / content.repo / content.version_folder
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Validation pass already confirmed `dest` doesn't exist; if it
        # appeared between then and now, an external process is racing
        # us and we'd rather error out than silently overwrite.
        if dest.exists():
            raise InvalidTransferManifest(
                f"dest {dest} appeared during verify (concurrent modification?)"
            )
        shutil.move(str(src), str(dest))
        item = TransferredItem(
            repo=content.repo,
            contract_pin=content.contract_pin,
            artifact_pin=content.artifact_pin,
            transfer_date=transfer.transfer_date.date(),
            transfer_id=transfer.transfer_id,
            local_path=str(dest.resolve()),
        )
        new_transferred.append(item)
        written.append(item)

    new_manifest = manifest.model_copy(update={"transferred": new_transferred})
    new_manifest.save(manifest_path)
    return manifest_path, written
