"""install.sh and install.ps1 install one pinned uv, not whatever is newest.

The scripts themselves are not run here: they `uv tool install --force` mintd
over the machine's own install.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SH = (REPO / "install.sh").read_text(encoding="utf-8")
PS1 = (REPO / "install.ps1").read_text(encoding="utf-8")


def test_both_scripts_install_the_same_pinned_uv() -> None:
    sh_pin = re.search(r'^UV_VERSION="(\d+\.\d+\.\d+)"$', SH, re.M)
    ps1_pin = re.search(r'^\$UvVersion = "(\d+\.\d+\.\d+)"$', PS1, re.M)
    assert sh_pin and ps1_pin
    assert sh_pin[1] == ps1_pin[1]
    assert 'curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh' in SH
    assert 'irm "https://astral.sh/uv/$UvVersion/install.ps1" | iex' in PS1


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize(
    ("have", "want", "older"),
    [
        ("0.12.19", "0.13.9", True),
        ("0.13.8", "0.13.9", True),
        ("0.13.9", "0.13.9", False),
        ("0.13.10", "0.13.9", False),
        ("1.0.0", "0.13.9", False),
    ],
)
def test_version_lt(have: str, want: str, older: bool) -> None:
    func = re.search(r"^version_lt\(\) \{\n.*?^\}$", SH, re.M | re.S)
    assert func
    rc = subprocess.run(["bash", "-c", f'{func[0]}\nversion_lt "$1" "$2"', "_", have, want]).returncode
    assert (rc == 0) is older
