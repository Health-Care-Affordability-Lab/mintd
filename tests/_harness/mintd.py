"""The installed mintd, run the way a researcher runs it.

A story never imports mintd. It runs the binary at `$MINTD_BIN`, or, when that
is unset, one built from this checkout: the wheel, installed with the locked
dependencies into a throwaway venv and run by its full path. Not `uv tool
install`, which puts a `mintd` in the user's own bin folder.

Each call gets stdin from /dev/null, a timeout, and an explicit environment:
`PATH`, a temp `HOME`, a git identity, the S3 test keys, and dvc's config and
site-cache dirs moved under the test's temp dir. Nothing else is inherited, so
real AWS credentials, `AWS_PROFILE` and the developer's git and dvc config are
out of reach.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TIMEOUT = 300  # seconds, per call


@dataclasses.dataclass
class Mintd:
    bin: Path
    env: dict[str, str]
    #: the config.yaml the binary reads; a story writes it
    config: Path

    def __call__(self, *args: str, cwd: Path, json: bool = True) -> subprocess.CompletedProcess[str]:
        """`mintd --json <args>`, or `mintd <args>` with `json=False` for the
        run that shows the ✓ lines, warnings and hints JSON mode leaves out."""
        argv = [str(self.bin), *(["--json"] if json else []), *args]
        return subprocess.run(
            argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=TIMEOUT, check=False,
        )


@pytest.fixture(scope="session")
def mintd_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if os.environ.get("MINTD_BIN"):
        return Path(os.environ["MINTD_BIN"])
    root = tmp_path_factory.mktemp("mintd-bin")
    venv = root / "venv"
    for argv in (
        ["uv", "build", "--wheel", "-q", "-o", str(root), str(REPO)],
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "-q", "-o", str(root / "locked.txt")],
        ["uv", "venv", "-q", "--python", sys.executable, str(venv)],
    ):
        subprocess.run(argv, cwd=REPO, check=True)
    (wheel,) = root.glob("*.whl")
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(venv), "-r", str(root / "locked.txt"), str(wheel)],
        cwd=REPO, check=True,
    )
    return venv / "bin" / "mintd"


@pytest.fixture
def mintd(mintd_bin: Path, tmp_path: Path) -> Mintd:
    home = tmp_path / "mintd-home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\temail = test@mintd\n\tname = test\n"
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "MINTD_CONFIG_DIR": str(home / "mintd"),
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_SHARED_CREDENTIALS_FILE": str(home / "no-aws-credentials"),
        "AWS_CONFIG_FILE": str(home / "no-aws-config"),
        "DVC_GLOBAL_CONFIG_DIR": str(home / "dvc-global"),
        "DVC_SYSTEM_CONFIG_DIR": str(home / "dvc-system"),
        "DVC_SITE_CACHE_DIR": str(home / "dvc-site"),
        "DVC_NO_ANALYTICS": "1",
    }
    (home / "mintd").mkdir()
    return Mintd(mintd_bin, env, home / "mintd" / "config.yaml")
