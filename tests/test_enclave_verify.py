"""Tests for `mintd.enclave.enclave_verify` — slice 16.

Verify operates on an already-extracted directory (user extracted with
`tar xzf` or similar). Tests build the extracted layout directly in
`tmp_path` rather than going through a real tarball — the path-traversal
guard is the load-bearing contract, not tar handling.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
import yaml

from mintd.enclave import (
    EnclaveManifest,
    InvalidTransferManifest,
    PathTraversalDetected,
    TransferredItem,
    enclave_verify,
)


def _make_extracted(
    tmp_path: Path,
    *,
    repo: str = "ds-alpha",
    version_folder: str = "aaabbb1-2026-05-15",
    contract_pin: str = "c" * 40,
    artifact_pin: str = "a" * 32,
    enclave_name: str = "lab-enclave",
    create_data_dir: bool = True,
    manifest_repo: str | None = None,
    manifest_version_folder: str | None = None,
) -> Path:
    """Stage an extracted dir at `tmp_path / "extracted"`.

    When `manifest_repo` / `manifest_version_folder` differ from `repo`
    / `version_folder`, the *manifest* claims one path but the
    filesystem holds another — used to exercise the traversal pre-checks.
    """
    extracted = tmp_path / "extracted"
    extracted.mkdir(parents=True)
    if create_data_dir:
        data_dir = extracted / repo / version_folder
        data_dir.mkdir(parents=True)
        (data_dir / "data.csv").write_text("col1,col2\n1,2\n")
    manifest_data = {
        "schema_version": "2.0",
        "enclave_name": enclave_name,
        "transfer_date": "2026-05-15T12:00:00+00:00",
        "transfer_id": "transfer-2026-05-15-000000",
        "contents": [
            {
                "repo": manifest_repo if manifest_repo is not None else repo,
                "version_folder": (
                    manifest_version_folder
                    if manifest_version_folder is not None
                    else version_folder
                ),
                "contract_pin": contract_pin,
                "artifact_pin": artifact_pin,
            }
        ],
    }
    (extracted / "_transfer_manifest.yaml").write_text(yaml.safe_dump(manifest_data))
    return extracted


def _new_inside_manifest(
    tmp_path: Path, *, transferred: list[TransferredItem] | None = None
) -> Path:
    m_path = tmp_path / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="lab-enclave",
        transferred=list(transferred or []),
    ).save(m_path)
    return m_path


def test_verify_happy_path(tmp_path: Path) -> None:
    extracted = _make_extracted(tmp_path)
    m_path = _new_inside_manifest(tmp_path)
    returned_path, written = enclave_verify(
        extracted_dir=extracted,
        manifest_path=m_path,
        data_root=tmp_path / "data",
    )
    assert returned_path == m_path
    assert len(written) == 1
    assert written[0].repo == "ds-alpha"


def test_verify_moves_data_to_data_root(tmp_path: Path) -> None:
    extracted = _make_extracted(tmp_path)
    m_path = _new_inside_manifest(tmp_path)
    data_root = tmp_path / "data"
    enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=data_root
    )
    moved = data_root / "ds-alpha" / "aaabbb1-2026-05-15"
    assert moved.is_dir()
    assert (moved / "data.csv").is_file()
    # Source dir was moved (not copied).
    assert not (extracted / "ds-alpha" / "aaabbb1-2026-05-15").exists()


def test_verify_idempotent_skips_existing_entry(tmp_path: Path) -> None:
    extracted = _make_extracted(tmp_path)
    pre_seeded = TransferredItem(
        repo="ds-alpha",
        contract_pin="c" * 40,
        artifact_pin="a" * 32,
        transfer_date=date(2026, 5, 15),
        transfer_id="transfer-2026-05-15-000000",
        local_path="/some/path",
    )
    m_path = _new_inside_manifest(tmp_path, transferred=[pre_seeded])
    _, written = enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=tmp_path / "data"
    )
    assert written == []
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 1


def test_verify_idempotent_on_back_to_back_runs(tmp_path: Path) -> None:
    """Regression: the first verify `shutil.move`s the data out of
    `extracted_dir`; a naive existence check on the second run would
    raise `InvalidTransferManifest("dir not present")`. Idempotence
    requires the existing-key check to short-circuit the validation
    loop *before* the existence check fires."""
    extracted = _make_extracted(tmp_path)
    m_path = _new_inside_manifest(tmp_path)
    data_root = tmp_path / "data"
    _, first_written = enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=data_root
    )
    assert len(first_written) == 1
    # Second call must be a clean no-op even though the source dir has
    # been moved out of `extracted_dir`.
    _, second_written = enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=data_root
    )
    assert second_written == []
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 1


def test_verify_rejects_invalid_manifest_yaml(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "_transfer_manifest.yaml").write_text(
        "schema_version: 2.0\nenclave_name: x\n  bad: : :\n"
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(InvalidTransferManifest):
        enclave_verify(
            extracted_dir=extracted, manifest_path=m_path
        )


def test_verify_rejects_traversal_via_relative_path(tmp_path: Path) -> None:
    """`version_folder = "../escape"` is caught by the string-level
    pre-check, before any filesystem access — so the test does not need
    an `escape` directory."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_version_folder="../escape",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_traversal_via_symlink(tmp_path: Path) -> None:
    """A symlink inside `version_folder` pointing outside `extracted_dir`
    is caught by the `rglob` walk."""
    extracted = _make_extracted(tmp_path)
    outside_target = tmp_path / "outside"
    outside_target.mkdir()
    (outside_target / "secret").write_text("top")
    data_dir = extracted / "ds-alpha" / "aaabbb1-2026-05-15"
    os.symlink(str(outside_target), str(data_dir / "evil_link"))
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_absolute_path_in_manifest(tmp_path: Path) -> None:
    """`version_folder = "/etc/passwd"` is caught by the `is_absolute()`
    pre-check."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_version_folder="/etc/passwd",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_absolute_repo_in_manifest(tmp_path: Path) -> None:
    """`repo = "/etc"`, `version_folder = "passwd"` — without a pre-check
    on `repo`, `Path.__truediv__` silently discards `extracted_dir` (the
    left operand) because `/etc` is absolute. Pinned by the string
    pre-check on `content.repo`."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_repo="/etc",
        manifest_version_folder="passwd",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_preserves_other_transferred_entries(tmp_path: Path) -> None:
    extracted = _make_extracted(tmp_path)
    existing = [
        TransferredItem(
            repo="other-repo-1",
            contract_pin="1" * 40,
            artifact_pin="1" * 32,
            transfer_date=date(2026, 1, 1),
            transfer_id="transfer-2026-01-01-000000",
            local_path="/abs/data/other-repo-1/v1",
        ),
        TransferredItem(
            repo="other-repo-2",
            contract_pin="2" * 40,
            artifact_pin="2" * 32,
            transfer_date=date(2026, 2, 2),
            transfer_id="transfer-2026-02-02-000000",
            local_path="/abs/data/other-repo-2/v2",
        ),
    ]
    m_path = _new_inside_manifest(tmp_path, transferred=existing)
    before_dump = [t.model_dump() for t in existing]
    enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=tmp_path / "data"
    )
    reloaded = EnclaveManifest.load(m_path)
    assert len(reloaded.transferred) == 3
    assert [t.model_dump() for t in reloaded.transferred[:2]] == before_dump


def test_verify_handles_missing_data_dir_creates_it(tmp_path: Path) -> None:
    extracted = _make_extracted(tmp_path)
    m_path = _new_inside_manifest(tmp_path)
    data_root = tmp_path / "does-not-yet-exist"
    enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=data_root
    )
    assert data_root.is_dir()
    assert (data_root / "ds-alpha" / "aaabbb1-2026-05-15" / "data.csv").is_file()


def test_verify_rejects_dot_repo(tmp_path: Path) -> None:
    """`repo = "."` produces `Path(".").parts == ()`, bypassing the
    `..`-segment check. Without an explicit reject, `dest = data_root /
    "." / "..."` would resolve to inside `data_root` and let a hostile
    transfer wipe the enclave data root via `shutil.rmtree`."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_repo=".",
        manifest_version_folder=".",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_empty_version_folder(tmp_path: Path) -> None:
    """`version_folder = ""` has empty `parts`, bypassing the `..` check.
    With a valid `repo`, the resulting `dest = data_root / repo / ""`
    resolves to `data_root / repo`, which `rmtree` would wipe."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_version_folder="",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_nested_path_in_version_folder(tmp_path: Path) -> None:
    """`version_folder = "B/C"` would pass the `..`/`is_absolute()`
    checks and the leaf `dest`-collision check, but pairing it with a
    sibling `version_folder = "B"` in the same manifest would crash
    mid-move (the second shutil.move's src has already been moved
    inside the first). Reject path separators outright."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_version_folder="aaabbb1-2026-05-15/nested",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_nested_path_in_repo(tmp_path: Path) -> None:
    """Same rationale as nested `version_folder`: `repo = "A/B"`
    interacts badly with leaf collision checks. Reject."""
    extracted = _make_extracted(
        tmp_path,
        create_data_dir=False,
        manifest_repo="ds-alpha/nested",
    )
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(PathTraversalDetected):
        enclave_verify(extracted_dir=extracted, manifest_path=m_path)


def test_verify_rejects_duplicate_dest_in_manifest(tmp_path: Path) -> None:
    """Two `TransferContent` entries targeting the same `<repo>/<vf>`
    would race in the move loop — the first would succeed, the second
    would `FileNotFoundError` when its src is missing. Reject up front
    so the manifest can't strand a partial move."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    repo, version_folder = "ds-alpha", "aaabbb1-2026-05-15"
    data_dir = extracted / repo / version_folder
    data_dir.mkdir(parents=True)
    (data_dir / "data.csv").write_text("x\n")
    # Two contents with identical dest but different pins.
    manifest_data = {
        "schema_version": "2.0",
        "enclave_name": "lab",
        "transfer_date": "2026-05-15T12:00:00+00:00",
        "transfer_id": "transfer-2026-05-15-000000",
        "contents": [
            {
                "repo": repo,
                "version_folder": version_folder,
                "contract_pin": "a" * 40,
                "artifact_pin": "a" * 32,
            },
            {
                "repo": repo,
                "version_folder": version_folder,
                "contract_pin": "b" * 40,
                "artifact_pin": "b" * 32,
            },
        ],
    }
    (extracted / "_transfer_manifest.yaml").write_text(yaml.safe_dump(manifest_data))
    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(InvalidTransferManifest):
        enclave_verify(
            extracted_dir=extracted, manifest_path=m_path, data_root=tmp_path / "data"
        )
    # No partial move: nothing in data_root yet.
    assert not (tmp_path / "data" / repo / version_folder).exists()


def test_verify_refuses_to_overwrite_existing_dest(tmp_path: Path) -> None:
    """A new `(repo, contract_pin, artifact_pin)` whose `version_folder`
    happens to collide with an already-populated `data_root/<repo>/<vf>`
    must NOT overwrite the legitimate data. The idempotence skip only
    fires when the *pins* match; a collision with different pins must
    raise instead of `rmtree`-ing the existing dest."""
    extracted = _make_extracted(
        tmp_path,
        contract_pin="d" * 40,  # different from the pre-populated entry
        artifact_pin="d" * 32,
    )
    # Pre-populate the destination as if a prior verify wrote here.
    legitimate = tmp_path / "data" / "ds-alpha" / "aaabbb1-2026-05-15"
    legitimate.mkdir(parents=True)
    (legitimate / "old_data.csv").write_text("legitimate\n")

    m_path = _new_inside_manifest(tmp_path)
    with pytest.raises(InvalidTransferManifest):
        enclave_verify(
            extracted_dir=extracted, manifest_path=m_path, data_root=tmp_path / "data"
        )
    # The legitimate file is still there — nothing was wiped.
    assert (legitimate / "old_data.csv").is_file()


def test_verify_multi_repo_extracted_dir(tmp_path: Path) -> None:
    """Three repos in one transfer manifest are all moved + appended;
    all `local_path` values must be absolute (validates the `.resolve()`
    fix in Step 5)."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    contents = []
    for letter in ("a", "b", "c"):
        version_folder = f"{letter * 7}-2026-05-15"
        repo = f"ds-{letter}"
        data_dir = extracted / repo / version_folder
        data_dir.mkdir(parents=True)
        (data_dir / "data.csv").write_text(f"col\n{letter}\n")
        contents.append(
            {
                "repo": repo,
                "version_folder": version_folder,
                "contract_pin": letter * 40,
                "artifact_pin": letter * 32,
            }
        )
    manifest_data = {
        "schema_version": "2.0",
        "enclave_name": "lab",
        "transfer_date": "2026-05-15T12:00:00+00:00",
        "transfer_id": "transfer-2026-05-15-000000",
        "contents": contents,
    }
    (extracted / "_transfer_manifest.yaml").write_text(yaml.safe_dump(manifest_data))

    m_path = _new_inside_manifest(tmp_path)
    _, written = enclave_verify(
        extracted_dir=extracted, manifest_path=m_path, data_root=tmp_path / "data"
    )
    assert len(written) == 3
    for item in written:
        # All local_paths must be absolute regardless of how
        # `manifest_path` / `data_root` were passed. os.path.isabs is the
        # portable check (a Windows absolute path is 'C:\\...', not '/...').
        assert os.path.isabs(item.local_path)
