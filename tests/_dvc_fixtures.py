"""Shared on-disk DVC shapes for the delivery-gate tests.

Real files, no doubles: `check`'s delivery gate reads `.dvc` pointers,
`dvc.lock` and `.dvc/config` and nothing else, so the fixture IS the project.
Shared by test_dvc_state / test_check / test_cli / test_publish / the scaffold
journey the way `tests/_enclave_fixtures.py` is, and deliberately not under
`tests/_fakes/` (that directory owes every module a contract-test licence).

Facts the writers encode, because a fixture that gets one wrong passes for the
wrong reason:

- a `.dvc` out records `path` as the BASENAME, anchored to the pointer's own
  directory (`_fast_sync_ops.workspace_path_for`);
- a files-format entry's version ID is read ONLY from
  `cloud.<remote>.version_id` keyed by the remote's name
  (`_fast_sync_ops._extract_version_id_from_file_entry`), while a top-level
  out's comes from `version_id` or any `cloud.*.version_id`;
- a `dvc.lock` stage must also exist in `dvc.yaml`, or the orphan-stage guard
  drops it from enumeration;
- `cache: false` lives in `dvc.yaml` as the mapping form under `outs:`.
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL_METADATA = FIXTURES / "metadata_v2_minimal.json"

REMOTE = "storage"
BUCKET = "test-bucket"
PREFIX = "lab/p/"
MD5 = "a" * 32
LOCK_MD5 = "b" * 32
IMPORT_REV_LOCK = "4f7c2a1abcd1234567890abcdef0123456789abc"


def storage_block(remote: str = REMOTE) -> dict:
    """A `metadata.storage` block that matches `write_dvc_config`, so
    `inspect_storage` reads INITIALIZED. Without it a `.dvc/config` alone
    reads PARTIAL_DVC_ONLY and a second, unrelated `storage_*` error lands
    beside the one under test."""
    return {
        "provider": "s3",
        "bucket": BUCKET,
        "prefix": PREFIX,
        "endpoint": "",
        "versioning": True,
        "dvc": {"remote_name": remote},
    }


def write_dvc_config(project: Path, *, version_aware: bool = True, remote: str = REMOTE) -> Path:
    """`.dvc/config` with one default S3 remote, `version_aware = true` unless
    told otherwise (the md5-layout shape)."""
    cfg = project / ".dvc" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = f"[core]\n    remote = {remote}\n[remote \"{remote}\"]\n    url = s3://{BUCKET}/{PREFIX}\n"
    if version_aware:
        body += "    version_aware = true\n"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def write_pointer(
    project: Path,
    out_rel: str,
    *,
    is_dir: bool = False,
    version_id: str | None = "v-1",
    files: dict[str, str | None] | None = None,
    imported: bool = False,
    remote: str = REMOTE,
    md5: str = MD5,
) -> Path:
    """`<out_rel>.dvc` the way `dvc add` writes it.

    `version_id=None` leaves the `cloud:` block out (the pre-version-aware
    push shape). `files=` switches to the files-format directory out — no
    top-level md5, one entry per `{relpath: version_id-or-None}` — which is
    what dvc writes for a directory pushed to a version-aware remote.
    `imported=True` adds the `deps[].repo` block of a `dvc import` pointer.
    """
    p = project / f"{out_rel}.dvc"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["outs:", f"- path: {Path(out_rel).name}"]
    if files is not None:
        if not files:
            lines.append("  files: []")
        else:
            lines.append("  files:")
            for relpath, vid in files.items():
                lines += [f"  - relpath: {relpath}", f"    md5: {MD5}", "    size: 12"]
                if vid is not None:
                    lines += ["    cloud:", f"      {remote}:", f"        version_id: {vid}"]
    else:
        lines += [f"  md5: {md5}{'.dir' if is_dir else ''}", "  size: 12"]
        if version_id is not None:
            lines += ["  cloud:", f"    {remote}:", f"      version_id: {version_id}"]
    if imported:
        lines += [
            "deps:",
            f"- path: outputs/{Path(out_rel).name}",
            "  repo:",
            "    url: https://github.com/example-org/source-product",
            "    rev: main",
            f"    rev_lock: {IMPORT_REV_LOCK}",
        ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_lock(
    project: Path,
    stage: str,
    out_rel: str,
    *,
    files: dict[str, str | None] | None = None,
    version_id: str | None = None,
    remote: str = REMOTE,
) -> Path:
    """`dvc.lock` with ONE stage and ONE out, the way `dvc repro` records it.

    `out_rel` is spelled relative to the stage's `wdir` in dvc.yaml
    (`../data/final/` under `wdir: code`), and the stage must exist there or
    the orphan-stage guard drops it. `files=` makes a files-format directory
    out (what dvc writes for a directory on a version-aware remote), one entry
    per `{relpath: version_id-or-None}`; otherwise a single md5 out carrying a
    `cloud:` block only when `version_id` is given.
    """
    lines = ["schema: '2.0'", "stages:", f"  {stage}:", "    cmd: run", "    outs:", f"    - path: {out_rel}"]
    if files is not None:
        if not files:
            lines.append("      files: []")
        else:
            lines.append("      files:")
            for relpath, vid in files.items():
                lines += [f"      - relpath: {relpath}", f"        md5: {MD5}", "        size: 12"]
                if vid is not None:
                    lines += ["        cloud:", f"          {remote}:", f"            version_id: {vid}"]
    else:
        lines += [f"      md5: {LOCK_MD5}", "      size: 12"]
        if version_id is not None:
            lines += ["      cloud:", f"        {remote}:", f"          version_id: {version_id}"]
    p = project / "dvc.lock"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_pipeline(
    project: Path,
    out_rel: str,
    *,
    cache: bool = True,
    files: dict[str, str | None] | None = None,
    version_id: str | None = None,
) -> None:
    """One `build` stage in BOTH `dvc.yaml` and `dvc.lock` with a single out,
    `cache: false` on request (the mapping form under `outs:`), files-format
    when `files=` is given, pinned when `version_id=` is given."""
    out_line = f"    - {out_rel}:\n        cache: false" if not cache else f"    - {out_rel}"
    (project / "dvc.yaml").write_text(
        f"stages:\n  build:\n    cmd: run\n    outs:\n{out_line}\n", encoding="utf-8"
    )
    write_lock(project, "build", out_rel, files=files, version_id=version_id)


def stage_dvc_project(
    project: Path,
    *,
    declared: tuple[str, ...] = ("data/final/",),
    version_aware: bool = True,
) -> Path:
    """A dvc-initialized producer that declares `declared` (first entry is the
    primary) and tracks NOTHING yet: `metadata.json` (patched in place when the
    caller already wrote one, else from the minimal fixture) with a matching
    storage block, plus `.dvc/config`. Returns the metadata path."""
    meta_path = project / "metadata.json"
    source = meta_path if meta_path.is_file() else MINIMAL_METADATA
    meta = json.loads(source.read_text(encoding="utf-8"))
    meta["storage"] = storage_block()
    meta["data_products"] = {
        "primary": declared[0],
        "outputs": [
            {"path": p, "description": "", "primary": p == declared[0], "last_published": ""}
            for p in declared
        ],
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    write_dvc_config(project, version_aware=version_aware)
    return meta_path


def stage_undeliverable_primary(project: Path, *, version_aware: bool = True) -> Path:
    """The canonical broken product: `data/final/` declared and tracked as a
    `.dir` out that was pushed before the remote became version-aware, so the
    pointer carries no S3 version ID and a consumer's import lands nothing."""
    meta_path = stage_dvc_project(project, version_aware=version_aware)
    write_pointer(project, "data/final", is_dir=True, version_id=None)
    return meta_path
