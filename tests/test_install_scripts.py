"""install.sh and install.ps1 install one pinned uv, not whatever is newest.

The scripts themselves are not run here: they `uv tool install --force` mintd
over the machine's own install. One line of install.sh, the reflink step, is
run with a real uv against indexes on localhost.
"""

from __future__ import annotations

import http.server
import os
import re
import shutil
import subprocess
import sys
import threading
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


def _reflink_line(script: str) -> str:
    """The script's one `uv pip install … reflink` line."""
    [line] = [ln.strip() for ln in script.splitlines() if ln.strip().startswith("uv pip install") and "reflink" in ln]
    return line


def test_both_scripts_reflink_steps_ignore_uv_settings_files() -> None:
    for script in (SH, PS1):
        assert _reflink_line(script).startswith("uv pip install --no-config "), _reflink_line(script)


class _Index:
    """A package index on localhost that records every request and answers 404."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        index = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index.requests.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/simple"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


_UV = shutil.which("uv")


@pytest.mark.skipif(os.name == "nt" or _UV is None, reason="runs install.sh's line in bash, with a real uv")
def test_install_shs_reflink_step_ignores_a_uv_toml_in_the_folder_it_runs_from(tmp_path: Path) -> None:
    """install.sh's reflink line, run by a real uv from a folder whose uv.toml names
    another index: uv asks only the default index (both on localhost), never that one."""
    assert _UV
    home, venv, run_in = tmp_path / "home", tmp_path / "venv", tmp_path / "run"
    home.mkdir()
    run_in.mkdir()
    env = {"PATH": f"{Path(_UV).parent}:/usr/bin:/bin", "HOME": str(home), "UV_CACHE_DIR": str(tmp_path / "cache"),
           "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1"}
    subprocess.run([_UV, "venv", "--no-config", "-q", "--python", sys.executable, str(venv)], env=env, check=True)
    named, default = _Index(), _Index()
    try:
        (run_in / "uv.toml").write_text(f'extra-index-url = ["{named.url}"]\n')
        line = _reflink_line(SH).removesuffix(" || {")
        subprocess.run(
            ["bash", "-c", line], cwd=run_in, capture_output=True, text=True,
            env={**env, "TOOL_PYTHON": str(venv / "bin" / "python"), "UV_DEFAULT_INDEX": default.url},
        )
    finally:
        named.close()
        default.close()
    assert named.requests == [], named.requests
    assert default.requests  # uv did look for the packages, on the default index

