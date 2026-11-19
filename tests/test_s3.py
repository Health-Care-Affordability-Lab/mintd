"""`mintd._s3`: the one module that imports boto3 or botocore."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("missing", [["boto3"], ["boto3", "botocore"]], ids=["boto3", "boto3-and-botocore"])
def test_a_broken_install_says_reinstall(tmp_path: Path, missing: list[str]) -> None:
    """boto3 is a hard dependency, but an install without it must still import
    mintd and say "reinstall", not crash. A fresh process: in this one,
    `mintd.cli` is already imported, so patching `sys.modules` here proves
    nothing about import time.

    Mutation that must redden this: an unguarded `from botocore... import` on
    `mintd.cli`'s import path (as `_s3_listing_ops.py` had)."""
    (tmp_path / ".dvc").mkdir()
    patch = "; ".join(f"sys.modules[{m!r}] = None" for m in missing)
    code = (
        f"import sys; {patch}; from mintd import cli; "
        f"raise SystemExit(cli.main(['data', 'pull', '--path', {str(tmp_path)!r}]))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    err = " ".join(r.stderr.split())
    assert r.returncode == 2, r.stderr
    assert "mintd's boto3 dependency is missing" in err
    assert "pip install 'dvc[s3]'" in err
    assert "Traceback" not in r.stderr
