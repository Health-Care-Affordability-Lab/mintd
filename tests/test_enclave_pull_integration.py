"""Real-DVC end-to-end test for `enclave_pull` (slice 47).

The unit suite uses a fake `DvcOps` that can't reproduce the three failures a
fresh enclave hit in production (no `.dvc/`, git-ignored staging pointer,
missing stage working dir). This test drives the *real* bundled `dvc` against a
fully local, offline producer so all three fixes are exercised in sequence.

Skipped by default. Run with `MINTD_RUN_INTEGRATION=1 pytest -m integration`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from mintd._config import Timeouts
from mintd._dvc_ops import SubprocessDvcOps
from mintd._templates import render_scaffold
from mintd.enclave import ApprovedProduct, EnclaveManifest, enclave_pull

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("MINTD_RUN_INTEGRATION") != "1",
        reason="set MINTD_RUN_INTEGRATION=1 to run",
    ),
]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _dvc(args: list[str], cwd: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "dvc", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


def _build_producer(root: Path) -> tuple[Path, str]:
    """A local git+DVC producer with one DVC-tracked output and an offline
    (local-dir) DVC remote so `dvc import` can fetch the bytes without network.
    """
    prod = root / "producer"
    prod.mkdir()
    _git(["init", "-q", "-b", "main"], prod)
    _dvc(["init", "-q"], prod)
    # Absolute remote path so the import's transient clone resolves it too.
    remote = root / "dvc-remote"
    _dvc(["remote", "add", "-d", "store", str(remote)], prod)
    (prod / "outputs").mkdir()
    (prod / "outputs" / "data.csv").write_text("a,b\n1,2\n")
    _dvc(["add", "outputs/data.csv"], prod)
    _dvc(["push"], prod)
    _git(["add", "-A"], prod)
    _git(["commit", "-q", "-m", "seed"], prod)
    pin = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(prod), capture_output=True, text=True, check=True,
    ).stdout.strip()
    return prod, pin


class _Client:
    def __init__(self, repo_url: str) -> None:
        self._repo_url = repo_url

    def fetch(self, name):  # noqa: ANN001 - structural CatalogClient stand-in
        repo_url = self._repo_url

        class _Entry:
            pass

        e = _Entry()
        e.repo_url = repo_url
        return e


def test_enclave_pull_end_to_end_real_dvc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prod, pin = _build_producer(tmp_path)

    enclave = tmp_path / "enclave_foo"
    enclave.mkdir()
    render_scaffold(
        project_type="enclave", name="foo", language="python", target_dir=enclave
    )
    _git(["init", "-q", "-b", "main"], enclave)
    # Failure 1 precondition: a fresh enclave has no DVC repo.
    assert not (enclave / ".dvc").exists()

    m_path = enclave / "enclave_manifest.yaml"
    EnclaveManifest(
        enclave_name="foo",
        approved_products=[
            ApprovedProduct(
                repo="provider-x",
                registry_entry="provider-x",
                pin=pin,
                source_path="outputs/data.csv",
            )
        ],
    ).save(m_path)

    # Production runs `mintd enclave pull` from inside the enclave dir; the real
    # `dvc import` inherits cwd, so mirror that.
    monkeypatch.chdir(enclave)

    _, written = enclave_pull(
        _Client(str(prod)),
        SubprocessDvcOps(timeouts=Timeouts()),
        manifest_path=m_path,
    )

    # Failure 1: lazy `dvc init` created a bare DVC repo...
    assert (enclave / ".dvc").exists()
    # ...with NO storage remote (Slice-30 invariant intact).
    config = (enclave / ".dvc" / "config").read_text()
    assert "remote" not in config

    # Failures 2 + 3: the import wrote its pointer (not git-ignored) and the
    # staging dir existed, so the data landed in the versioned download dir.
    assert len(written) == 1
    landed = Path(written[0].local_path) / "data.csv"
    assert landed.exists()
    assert landed.read_text() == "a,b\n1,2\n"
