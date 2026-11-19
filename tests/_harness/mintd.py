"""The installed mintd, run the way a researcher runs it.

A story never imports mintd. It runs the binary at `$MINTD_BIN`, or, when that
is unset, one built from a clean clone of this checkout's HEAD: the wheel,
installed with exactly the locked dependencies into a throwaway venv and run by
its full path. Not `uv tool install`, which puts a `mintd` in the user's own bin
folder. A checkout with uncommitted changes to what the wheel is built from is
refused rather than quietly tested at HEAD.

Each call gets stdin from /dev/null, a timeout, and an explicit environment.
A timeout or a Ctrl-C kills mintd's whole process group. The environment is
`PATH` with mintd's bin folder first, a temp `HOME` holding a git identity,
`TMPDIR`, `MINTD_CONFIG_DIR`, the S3 test keys and region, AWS credential and
config paths that do not exist, and dvc's config and site-cache dirs. Nothing
else is inherited, so real AWS credentials, `AWS_PROFILE` and the developer's
git and dvc config are out of reach.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests._harness.git import _git

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
        # Its own process group, so a timeout also kills the dvc and git
        # children, which would otherwise outlive mintd and hold the run. The
        # new session also keeps the terminal's Ctrl-C from reaching them, so
        # an interrupt has to kill the group here too.
        with subprocess.Popen(
            argv, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        ) as proc:
            try:
                out, err = proc.communicate(timeout=TIMEOUT)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):  # the group already exited
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _uv(*args: str) -> None:
    """From uv's cache when it can serve the call, so a warm cache never reaches
    the package index; from the index when it cannot."""
    if subprocess.run(["uv", "--offline", *args], capture_output=True, check=False).returncode:
        subprocess.run(["uv", *args], check=True)


@pytest.fixture(scope="session")
def mintd_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    given = os.environ.get("MINTD_BIN")
    if given:
        # A bare name is looked up on PATH, as a shell would; a path is made absolute.
        found = shutil.which(given) if os.sep not in given else given
        if found is None:
            pytest.fail(f"MINTD_BIN={given!r} is not on PATH")
        return Path(found).resolve()
    dirty = _git(["status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock"], cwd=REPO)
    if dirty:
        pytest.fail(
            "the stories build mintd from HEAD, which lacks these changes:\n"
            f"{dirty}commit them, or set MINTD_BIN to a mintd built with them"
        )
    root = tmp_path_factory.mktemp("mintd-bin")
    src, venv, locked = root / "src", root / "venv", root / "locked.txt"
    # Built in the checkout, setuptools writes build/ and src/mintd.egg-info/
    # there, and a stale build/lib ships modules HEAD has deleted.
    _git(["clone", "-q", "--shared", "--no-checkout", str(REPO), str(src)])
    _git(["checkout", "-q", "--detach", _git(["rev-parse", "HEAD"], cwd=REPO).strip()], cwd=src)
    subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "-q", "-o", str(locked)],
        cwd=src, check=True,
    )
    # The lock pins setuptools for the build too; setuptools-scm is not in it.
    _uv("build", "--wheel", "-q", "--build-constraints", str(locked), "-o", str(root), str(src))
    subprocess.run(["uv", "venv", "-q", "--python", sys.executable, str(venv)], check=True)
    (wheel,) = root.glob("*.whl")
    _uv("pip", "install", "-q", "--no-deps", "--python", str(venv), "-r", str(locked), str(wheel))
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
        "PATH": f"{mintd_bin.parent}{os.pathsep}{os.environ['PATH']}",
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
