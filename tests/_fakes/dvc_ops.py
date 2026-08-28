"""Fake `DvcOps` for tests.

Records every `import_` call and writes a parseable stub `.dvc` file to disk
so downstream `scan_imports()` can pick it up. The stub mirrors the real
`dvc import` shape closely enough that `DataDependency.from_dvc_file` parses
it cleanly (see tests/test_dvc_ops.py for the round-trip).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from mintd._dvc_ops import (
    DvcImportDestinationExists,
    DvcOpError,
    DvcPullError,
    DvcPushResult,
)


class DvcInitCall(NamedTuple):
    cwd: Path


class DvcImportCall(NamedTuple):
    cwd: Path
    repo_url: str
    path: str
    dest: Path
    rev: str | None
    force: bool
    extra_args: list[str] | None = None


class DvcPushCall(NamedTuple):
    cwd: Path
    remote: str | None
    jobs: int | None
    targets: list[str] | None = None


class DvcPullCall(NamedTuple):
    cwd: Path
    targets: list[str] | None
    remote: str | None
    jobs: int | None
    extra_args: list[str] | None = None


class DvcAddCall(NamedTuple):
    cwd: Path
    path: Path


class DvcStatusCall(NamedTuple):
    cwd: Path
    targets: list[str] | None


class DvcRemoveCall(NamedTuple):
    cwd: Path
    name: str


class DvcCheckoutCall(NamedTuple):
    cwd: Path
    targets: list[str] | None


class _FakeDvcOps:
    """Implements `mintd._dvc_ops.DvcOps` structurally."""

    def __init__(self) -> None:
        self.init_calls: list[DvcInitCall] = []
        self.calls: list[DvcImportCall] = []
        self.import_raises: BaseException | None = None
        self.push_calls: list[DvcPushCall] = []
        self.push_raises: Exception | None = None
        self.push_result: DvcPushResult = DvcPushResult(pushed=1, up_to_date=False)
        self.pull_calls: list[DvcPullCall] = []
        self.pull_raises: Exception | None = None
        # Per-target pull failure: raised (before recording) when a pull's
        # ``targets`` argv contains a matching key. Models dvc's field
        # behavior where a specific import cannot be materialized while other
        # targets pull fine. ``pull_raises`` (global) semantics are untouched.
        self.pull_raises_for: dict[str, Exception] = {}
        self.add_calls: list[DvcAddCall] = []
        self.add_raises: Exception | None = None
        self.status_calls: list[DvcStatusCall] = []
        self.status_raises: Exception | None = None
        self.status_result: dict[str, str] = {}
        self.remove_calls: list[DvcRemoveCall] = []
        self.remove_raises: Exception | None = None
        self.checkout_calls: list[DvcCheckoutCall] = []
        self.checkout_raises: Exception | None = None
        # Post-checkout verification (data_pull) stats workspace paths, so a
        # fake checkout must be able to MATERIALIZE its targets. Set
        # ``workspace`` to any non-None value to enable it.
        #
        # It is a SWITCH, not a location. WHERE a checkout materializes is the
        # ``cwd`` of the call, exactly as it is for real dvc -- there is one
        # "which repo" concept now, and it is the protocol's. Before unit A
        # there were two, and they could silently disagree. Setting this to the
        # project root is still the conventional spelling, and every existing
        # site does, but only its not-None-ness is read.
        #
        # Knobs model the dvc 3.67.1 index_from_targets bug:
        # - checkout_materializes=False: checkout exits 0 having written
        #   nothing (the silent multi-target no-op);
        # - checkout_single_target_only=True: only single-target invocations
        #   materialize (the cluster shape — bulk no-ops, retries work).
        self.workspace: Path | None = None
        self.checkout_materializes: bool = True
        self.checkout_single_target_only: bool = False
        # Targets checkout NEVER materializes (even single-target retries)
        # — models a target whose cache blobs are unusable/corrupt.
        self.checkout_never_materializes: set[str] = set()
        # Opt-in target validation. OFF by default: 142 assert-lines across
        # seven modules read this fake's `*_calls`, and they assert "the
        # handler called the seam with X", not "X was legal". Flipping the
        # default would redden them for a reason unrelated to what they
        # test. ON, `pull()` rejects what real dvc rejects -- see
        # `_reject_unknown_targets` for the measured boundary and
        # `test_strict_fake_agrees_with_real_dvc_on_one_graph` for its
        # licence.
        self.strict_targets: bool = False

    def init(self, *, cwd: Path) -> None:
        self.init_calls.append(DvcInitCall(cwd=cwd))

    def import_(
        self,
        *,
        repo_url: str,
        path: str,
        dest: Path,
        cwd: Path,
        rev: str | None = None,
        force: bool = False,
        extra_args: list[str] | None = None,
    ) -> Path:
        if self.import_raises:
            raise self.import_raises
        self.calls.append(
            DvcImportCall(
                cwd=cwd, repo_url=repo_url, path=path, dest=dest, rev=rev,
                force=force, extra_args=extra_args,
            )
        )
        # Mirror real `dvc import`: the destination's parent (the stage working
        # dir) must already exist. The caller is responsible for creating it;
        # do NOT mkdir here, or we mask the "stage working dir does not exist"
        # failure that bit enclave_pull (slice 47).
        if not dest.parent.exists():
            raise DvcOpError(
                f"dvc import failed (exit 1): stage working dir "
                f"'{dest.parent}' does not exist"
            )
        # Mirror real `dvc import -o <existing-dir>`: dvc treats the
        # directory as a container, nests the source basename inside it, and
        # refuses the overlap — mapped by SubprocessDvcOps to
        # DvcImportDestinationExists. Without this the fake returns canned
        # success and the force-clears-the-destination bug class is
        # structurally untestable.
        if dest.is_dir():
            raise DvcImportDestinationExists(
                f"destination '{dest}' already exists; remove the "
                f"directory or pass force=True"
            )
        dvc_file = dest.parent / (dest.name + ".dvc")
        # Stub shape: enough for DataDependency.from_dvc_file to parse.
        rev_lock = rev if (rev and len(rev) == 40) else "fake0pin" + "0" * 32
        dvc_file.write_text(
            "outs:\n"
            f"  - md5: {'f' * 32}\n"
            "    size: 0\n"
            f"    path: {dest.name}\n"
            "deps:\n"
            f"  - path: {path}\n"
            "    repo:\n"
            f"      url: {repo_url}\n"
            f"      rev: {rev or 'main'}\n"
            f"      rev_lock: {rev_lock}\n"
        )
        return dvc_file

    def push(
        self,
        *,
        cwd: Path,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
    ) -> DvcPushResult:
        if self.push_raises:
            raise self.push_raises
        self.push_calls.append(
            DvcPushCall(cwd=cwd, remote=remote, jobs=jobs, targets=targets)
        )
        return self.push_result

    def pull(
        self,
        *,
        cwd: Path,
        targets: list[str] | None = None,
        remote: str | None = None,
        jobs: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        if self.pull_raises:
            raise self.pull_raises
        for t in targets or []:
            if t in self.pull_raises_for:
                raise self.pull_raises_for[t]
        if self.strict_targets:
            self._reject_unknown_targets(targets, root=cwd)
        self.pull_calls.append(
            DvcPullCall(
                cwd=cwd, targets=targets, remote=remote, jobs=jobs,
                extra_args=extra_args,
            )
        )

    def add(self, path: Path, *, cwd: Path) -> Path:
        if self.add_raises:
            raise self.add_raises
        self.add_calls.append(DvcAddCall(cwd=cwd, path=path))
        dvc_file = path.parent / (path.name + ".dvc")
        dvc_file.parent.mkdir(parents=True, exist_ok=True)
        # A REAL `.dvc` body. This used to be `""`, which is valid YAML
        # (it parses to None) and so never failed loudly -- it just made
        # every downstream reader see an out-less pointer. Real `dvc add`
        # always writes an `outs` block, and
        # `test_dvc_ops_contract.py::test_add_writes_a_parseable_dvc_file`
        # is what caught the gap: it passed on the real arm and failed on
        # the fake with `no outs block: {}`.
        dvc_file.write_text(
            "outs:\n"
            f"  - md5: {'a' * 32}\n"
            "    size: 0\n"
            f"    path: {path.name}\n"
        )
        return dvc_file

    def status(self, targets: list[str] | None = None, *, cwd: Path) -> dict[str, str]:
        if self.status_raises:
            raise self.status_raises
        self.status_calls.append(DvcStatusCall(cwd=cwd, targets=targets))
        return self.status_result.copy()

    def remove(self, name: str, *, cwd: Path) -> None:
        if self.remove_raises:
            raise self.remove_raises
        self.remove_calls.append(DvcRemoveCall(cwd=cwd, name=name))

    def checkout(self, *, cwd: Path, targets: list[str] | None = None) -> None:
        if self.checkout_raises:
            raise self.checkout_raises
        self.checkout_calls.append(DvcCheckoutCall(cwd=cwd, targets=targets))
        if (
            self.workspace is not None
            and self.checkout_materializes
            and (not self.checkout_single_target_only or len(targets or []) == 1)
        ):
            for t in targets or []:
                if t not in self.checkout_never_materializes:
                    self._materialize_target(t, root=cwd)

    def _materialize_target(self, target: str, *, root: Path) -> None:
        """Write what a real `dvc checkout` would: the target's workspace
        path(s). Out shapes (file vs dir vs files-format dir) come from the
        on-disk .dvc / dvc.lock, same as production's verification pass; a
        target with neither is materialized as a plain file.

        Path resolution and shape dispatch are imported from production
        (`workspace_path_for`, `DvcOut.materializes_as_dir`,
        `EMPTY_DIR_MD5`) — the fake WRITES the paths production STATS, so
        writer and reader must agree on the address by construction. Only
        the stand-in file CONTENT below is fake-specific."""
        from mintd._fast_sync_ops import (
            EMPTY_DIR_MD5,
            cache_path_for,
            outs_for_target,
            parse_dvc_lock_outs,
            read_cached_dir_manifest,
            workspace_path_for,
        )

        outs = outs_for_target(root, target, "origin")
        if not outs:
            outs = [o for o in parse_dvc_lock_outs(root, "origin") if o.target == target]
        if not outs:
            dest = root / (target[:-4] if target.endswith(".dvc") else target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("materialized")
            return
        for out in outs:
            dest = workspace_path_for(root, out)
            if out.materializes_as_dir:
                dest.mkdir(parents=True, exist_ok=True)
                if out.files is not None:
                    # files-format: exactly the pinned entries — an empty
                    # files: [] list yields an EMPTY directory, like real dvc.
                    rels = [fe.relpath for fe in out.files]
                elif out.md5 == EMPTY_DIR_MD5:
                    rels = []  # empty-manifest md5 dir: real dvc makes it empty
                else:
                    # md5-keyed dir. When the cached .dir manifest IS present
                    # (e.g. the import-rescue lane seeded it plus the blobs),
                    # materialize the REAL entries by copying each cached blob
                    # to dest/relpath — byte-correct, like real dvc over a
                    # populated cache. Otherwise stand in one file.
                    cache_dir = root / ".dvc" / "cache"
                    manifest = read_cached_dir_manifest(cache_dir, out.md5)
                    if manifest:
                        for fe in manifest:
                            blob = cache_path_for(cache_dir, fe.md5)
                            p = dest / fe.relpath
                            p.parent.mkdir(parents=True, exist_ok=True)
                            if blob.is_file():
                                p.write_bytes(blob.read_bytes())
                            else:
                                p.write_text("materialized")
                        continue
                    rels = [".materialized"]
                for rel in rels:
                    p = dest / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("materialized")
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("materialized")

    # -- strict target validation --------------------------------------------

    def _declared_targets(self, root: Path) -> tuple[set[str], list[str]]:
        """What this workspace declares, as (names, out-paths).

        Anchored on ``dvc.yaml`` and ``.dvc`` files, deliberately NOT on
        ``dvc.lock``. A lock can carry a stage no ``dvc.yaml`` declares, and
        real dvc rejects that stage's out -- measured at dvc 3.67.1:

            data/built.csv (dvc.yaml stage out)  rc=0
            data/ghost.csv (dvc.lock only)       rc=1  "does not exist as an
                                                        output or a stage name
                                                        in 'dvc.yaml'"

        Reading the lock for accepts is exactly how a fake starts accepting
        orphans, which is the failure this flag exists to catch.

        **Why not ``parse_dvc_lock_outs``**, which does this parse already:
        it sets ``target=rel`` (`_fast_sync_ops.py:536`), i.e. it throws the
        stage name away and keeps only the path. The stage name is precisely
        what separates a declared out from an orphan, so that function cannot
        answer this question -- the first attempt here used it and the oracle
        caught the fake rejecting `data/built`, a stage out real dvc accepts.
        The wdir handling below mirrors it on purpose.
        """
        import posixpath

        import yaml

        names: set[str] = set()
        paths: list[str] = []

        for dvc_file in sorted(root.rglob("*.dvc")):
            # `is_file()` is load-bearing: `*.dvc` also matches the `.dvc`
            # DIRECTORY every dvc repo has. Without this the list gains a ``""``
            # entry (``".dvc"`` minus its suffix), and the subpath test below —
            # ``nt.startswith(f"{p}/")`` — degenerates to ``nt.startswith("/")``,
            # accepting every absolute path including ``/etc/passwd``. Pinned by
            # ``test_strict_fake_rejects_an_absolute_path_outside_the_graph``.
            if not dvc_file.is_file():
                continue
            rel = dvc_file.relative_to(root).as_posix()
            names.add(rel)
            # The out path comes from ``outs[].path``, NEVER from the filename.
            # `dvc add` happens to make the two agree; `dvc import` does not —
            # it writes ``<name>.dvc`` whose out is the LOCAL path, which is
            # exactly what ``tests/_harness/consumer.py::write_import`` emits
            # (``alpha.dvc`` carrying ``path: final``). Deriving the out from
            # the stem made this fake wrong in BOTH directions on that shape.
            # Measured, dvc 3.67.1, on `alpha.dvc` with `path: final`:
            #
            #     data/imports/final      rc=0   (stem-fake REJECTED it)
            #     data/imports/alpha      rc=1   (stem-fake ACCEPTED it)
            #     data/imports/alpha.dvc  rc=0
            #
            # Stricter than dvc on the real out and more lenient on a name dvc
            # refuses — the same double failure that killed `GraphAwareDvcOps`.
            # ``names.add(rel)`` stays: dvc accepts the pointer file by name
            # whatever its out path is (measured rc=0 above).
            try:
                body = yaml.safe_load(dvc_file.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(body, dict):
                # A malformed or empty pointer declares no outs. Skipping it
                # keeps the pointer's own NAME accepted (dvc reads the file
                # lazily) without inventing an out from the filename.
                continue
            wdir = dvc_file.parent.relative_to(root).as_posix()
            for out in body.get("outs") or []:
                raw = (out or {}).get("path") if isinstance(out, dict) else None
                if raw:
                    paths.append(posixpath.normpath(posixpath.join(wdir, raw)))

        yaml_path = root / "dvc.yaml"
        if not yaml_path.is_file():
            return names, paths
        names.add("dvc.yaml")

        try:
            stages = (yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}).get(
                "stages"
            ) or {}
            lock = (
                yaml.safe_load((root / "dvc.lock").read_text(encoding="utf-8")) or {}
            ).get("stages") or {}
        except (OSError, yaml.YAMLError):
            return names, paths
        names.update(stages)

        for stage, body in lock.items():
            # `foreach` splits the name: `dvc.yaml` says `base`, `dvc.lock`
            # says `base@a`. Match on the base so foreach instances are
            # accepted while genuine orphans stay rejected.
            base = stage.split("@", 1)[0]
            if base not in stages:
                continue
            # The INSTANCE name is itself a legal target: `dvc pull base@a` is
            # the only way real dvc lets you target one instance of a `foreach`
            # stage, and it exits 0 (measured, dvc 3.67.1, fresh clone per
            # target: base@a rc=0 fetching only a.parquet, base@zzz rc=1).
            # Adding it here rather than beside `names.update(stages)` is what
            # keeps orphan rejection intact — a lock-only stage is skipped by
            # the guard above before it can be added.
            names.add(stage)
            wdir = (stages[base] or {}).get("wdir", ".")
            for out in (body or {}).get("outs") or []:
                raw = out.get("path")
                if not raw:
                    continue
                try:
                    resolved = (root / wdir / raw).resolve()
                    paths.append(resolved.relative_to(root.resolve()).as_posix())
                except (ValueError, OSError):
                    continue
        return names, paths

    def _reject_unknown_targets(self, targets: list[str] | None, *, root: Path) -> None:
        """Raise on a target real dvc would refuse.

        The accept side is as load-bearing as the reject side and is where
        the previous attempt at this (`GraphAwareDvcOps`) died: it was
        STRICTER than dvc, rejecting bare stage names and directory-out
        subpaths, which would have false-failed issues 06 and 07. Measured
        against dvc 3.67.1 on one graph, all rc=0:

            data/final.csv.dvc   a .dvc pointer
            data/final.csv       the out path itself
            build                a bare stage name
            data/built           a pipeline stage's dir out
            data/built/out.csv   a path UNDER a dir out
            dvc.yaml             the pipeline file

        and rc=1 for `nope.dvc` / `data/nothing.csv`. A fake that rejects
        anything in the first list is a second source of truth, not a
        double.
        """
        from mintd._fast_sync_ops import normalize_target

        names, paths = self._declared_targets(root)

        for target in targets or []:
            # dvc accepts an empty target (measured: `dvc pull ""` → rc=0); it
            # simply contributes no filter. Rejecting it would be stricter than
            # dvc, which is the one direction this flag must never be.
            if not target.strip():
                continue
            nt = normalize_target(target)
            # An ABSOLUTE path inside the workspace is legal — measured rc=0
            # against a declared out. `normalize_target` passes absolute paths
            # through unchanged, so re-anchor here. Deliberately NOT
            # `resolve()`d on either side: dvc compares physical paths without
            # following symlinks, so resolving would accept a symlinked
            # absolute target dvc refuses — leniency, the one direction this
            # flag must never be. (An earlier comment here blamed macOS `/var`
            # vs `/private/var`. That was wrong: pytest hands out an already
            # resolved `tmp_path`, so the two never disagreed and neither
            # `resolve()` was pinned by any test.)
            if Path(nt).is_absolute():
                try:
                    nt = Path(nt).relative_to(root).as_posix()
                except ValueError:
                    # Genuinely outside the workspace (`/etc/passwd`). dvc
                    # rejects these — measured rc=1 — so fall through.
                    pass
            if target in names or nt in names or nt in paths:
                continue
            # Under a directory out: dvc resolves into the `.dir` manifest.
            if any(nt.startswith(f"{p}/") for p in paths):
                continue
            raise DvcPullError(
                f"dvc pull failed (exit 1): ERROR: failed to pull data from "
                f"the cloud - '{target}' does not exist as an output or a "
                f"stage name in 'dvc.yaml'"
            )
