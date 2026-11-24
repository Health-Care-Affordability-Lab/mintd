"""install.sh and install.ps1 install a release's own wheel, checked, with its constraints.

The scripts run here only with stub `curl` and `uv` first on a minimal PATH and
a temporary HOME, so the machine's own uv and mintd are never touched.
"""

from __future__ import annotations

import hashlib
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



_WHEEL = "mintd-9.9.9-py3-none-any.whl"
# Serves a URL's last path segment from $STUB_ASSETS, to -o or stdout.
_STUB_CURL = """#!/bin/bash
out=""; url=""; fail=0
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; -*f*) fail=1; shift ;; -*) shift ;; *) url="$1"; shift ;; esac
done
echo "curl $url" >> "$LOG"
f="$STUB_ASSETS/${url##*/}"
if [ ! -f "$f" ]; then  # like real curl: an HTTP 404 is an error only with -f
  [ "$fail" = 1 ] && exit 22
  f=/dev/null; echo "Not Found" > "${out:-/dev/stdout}"; exit 0
fi
if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
"""
_STUB_UV = """#!/bin/bash
echo "uv $*" >> "$LOG"
if [ "$1" = pip ] && [ -n "${REAL_UV:-}" ]; then exec "$REAL_UV" "$@"; fi  # the reflink step, for real
case "$*" in
  --version) echo "${STUB_UV_SAYS-uv 0.13.9}"; exit "${STUB_UV_EXIT:-0}" ;;
  "tool dir") echo "${STUB_UV_TOOLS:-$HOME/.local/share/uv/tools}" ;;
  "tool dir --bin") echo "${STUB_UV_BIN:-$HOME/bin}" ;;
esac
"""


def _run_sh(
    tmp_path: Path, script: str, *args: str, assets: dict[str, bytes], uv_dir: Path | None = None,
    cwd: Path | None = None, **env: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Runs `script` with stub curl, and stub uv in `uv_dir` (default: the stubs, first on PATH)."""
    stubs, served, home = tmp_path / "stubs", tmp_path / "assets", tmp_path / "home"
    uv_dir = uv_dir or stubs
    for d in (stubs, uv_dir, served, home / ".local/share/uv/tools/mintd/bin"):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".local/share/uv/tools/mintd/bin/python").touch()
    for path, body in ((stubs / "curl", _STUB_CURL), (uv_dir / "uv", _STUB_UV)):
        path.write_text(body)
        path.chmod(0o755)
    for name, data in assets.items():
        (served / name).write_bytes(data)
    log = tmp_path / "log"
    log.touch()
    full_env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "LOG": str(log), "STUB_ASSETS": str(served), **env}
    result = subprocess.run(
        ["bash", "-c", script, "install.sh", *args], env=full_env, capture_output=True, text=True, cwd=cwd
    )
    return result, log.read_text()


def _release_assets(sha256: str | None = None) -> dict[str, bytes]:
    wheel = b"not really a wheel"
    digest = hashlib.sha256(wheel).hexdigest() if sha256 is None else sha256
    return {_WHEEL: wheel, f"{_WHEEL}.sha256": f"{digest}  {_WHEEL}\n".encode() if digest else b"", "constraints.txt": b"pyyaml==6.0\n"}


_STAMPED_SH = SH.replace('\nMINTD_TAG=""\n', '\nMINTD_TAG="v9.9.9"\n')


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize("schema", [False, True])
def test_stamped_install_sh_checks_the_wheel_then_installs_it_with_the_constraints(tmp_path: Path, schema: bool) -> None:
    result, log = _run_sh(tmp_path, _STAMPED_SH, *(["--with-schema"] if schema else []), assets=_release_assets())
    assert result.returncode == 0, result.stderr
    tmp = re.search(r"--constraints (\S+)/constraints\.txt", log)
    assert tmp, log
    spec = f"mintd[schema] @ file://{tmp[1]}/{_WHEEL}" if schema else f"{tmp[1]}/{_WHEEL}"
    assert f"uv tool install --force --reinstall --refresh --python 3.13 --with-executables-from dvc --constraints {tmp[1]}/constraints.txt {spec}\n" in log
    assert re.search(
        rf"uv pip install --no-config --python \S+/mintd/bin/python --require-hashes "
        rf"--constraints {re.escape(tmp[1])}/constraints\.txt --requirements {re.escape(tmp[1])}/reflink\.txt\n",
        log,
    )
    # Every file from the same release: the tag's own, never latest.
    assert [line for line in log.splitlines() if line.startswith("curl ")] == [
        f"curl https://github.com/Health-Care-Affordability-Lab/mintd/releases/download/v9.9.9/{name}"
        for name in (_WHEEL, f"{_WHEEL}.sha256", "constraints.txt")
    ]


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize(("sha256", "no_hasher"), [("0" * 64, False), ("", False), ("", True)])
def test_stamped_install_sh_installs_nothing_when_the_wheel_does_not_match(
    tmp_path: Path, sha256: str, no_hasher: bool
) -> None:
    if no_hasher:  # sha256sum and shasum both fail: an empty hash must not match an empty file
        (tmp_path / "stubs").mkdir()
        for name in ("sha256sum", "shasum"):
            (tmp_path / "stubs" / name).write_text("#!/bin/sh\nexit 1\n")
            (tmp_path / "stubs" / name).chmod(0o755)
    result, log = _run_sh(tmp_path, _STAMPED_SH, assets=_release_assets(sha256))
    assert result.returncode != 0
    assert "nothing was installed" in result.stderr
    assert "tool install" not in log


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_stamped_install_sh_stops_when_a_release_file_is_missing(tmp_path: Path) -> None:
    assets = _release_assets()
    del assets["constraints.txt"]
    result, log = _run_sh(tmp_path, _STAMPED_SH, assets=assets)
    assert result.returncode != 0
    assert "tool install" not in log


_SELF_UPDATE = (
    "Upgrade it with 'uv self update', then run the install line again. Nothing was installed.\n"
    "If uv refuses to update itself (installed with pip, pipx, conda or a distro package), "
    "upgrade it with the tool that installed it.\n"
)
_BREW_UPGRADE = "Upgrade it with 'brew upgrade uv', then run the install line again. Nothing was installed.\n"


def _homebrew_uv(tmp_path: Path, layout: str) -> str:
    """A stub Homebrew uv 0.12.0, reached on PATH the way `layout` says; returns
    the PATH entry to put first."""
    brew = tmp_path / "brew"
    (brew / "Cellar" / "uv" / "0.12.0" / "bin").mkdir(parents=True)
    shutil.copy(tmp_path / "stubs" / "uv", brew / "Cellar" / "uv" / "0.12.0" / "bin" / "uv")
    (brew / "bin").mkdir()
    (brew / "bin" / "uv").symlink_to("../Cellar/uv/0.12.0/bin/uv")  # what brew links
    (brew / "opt").mkdir()
    (brew / "opt" / "uv").symlink_to("../Cellar/uv/0.12.0")
    if layout == "brew bin":
        return str(brew / "bin")
    if layout == "brew opt":
        return str(brew / "opt" / "uv" / "bin")
    (tmp_path / "mybin").mkdir()  # a link of the user's own to brew's link
    (tmp_path / "mybin" / "uv").symlink_to(brew / "bin" / "uv")
    return str(tmp_path / "mybin")


_OLDER = "this installer needs uv 0.13.9 or newer, and you have 0.12.0."
_UNREAD = "could not read your uv's version from 'uv --version'; this installer needs uv 0.13.9 or newer."


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize(
    ("layout", "says", "exit_code", "first", "advice"),
    [
        ("standalone", "uv 0.12.0", "0", _OLDER, _SELF_UPDATE),
        ("brew bin", "uv 0.12.0", "0", _OLDER, _BREW_UPGRADE),
        ("brew opt", "uv 0.12.0", "0", _OLDER, _BREW_UPGRADE),
        ("link to brew", "uv 0.12.0", "0", _OLDER, _BREW_UPGRADE),
        ("standalone", "", "1", _UNREAD, _SELF_UPDATE),
        ("standalone", "", "0", _UNREAD, _SELF_UPDATE),
        ("standalone", "uv-0.12.0", "0", _UNREAD, _SELF_UPDATE),
        ("brew bin", "", "1", _UNREAD, _BREW_UPGRADE),
    ],
)
def test_stamped_install_sh_stops_on_a_uv_older_than_the_pin_or_of_unknown_version(
    tmp_path: Path, layout: str, says: str, exit_code: str, first: str, advice: str
) -> None:
    _run_sh(tmp_path, "true", assets={})  # writes the stubs
    path = f"{tmp_path / 'stubs'}:/usr/bin:/bin"
    if layout != "standalone":
        path = f"{_homebrew_uv(tmp_path, layout)}:{path}"
    result, log = _run_sh(
        tmp_path, _STAMPED_SH, assets=_release_assets(), PATH=path, STUB_UV_SAYS=says, STUB_UV_EXIT=exit_code
    )
    assert result.returncode == 1
    assert result.stderr == f"error: {first}\n{advice}"
    assert log == "uv --version\n"


def _exe(folder: Path, name: str, says: str) -> Path:
    """An executable `name` in `folder` that prints `says`: a .cmd on Windows."""
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = folder / f"{name}.cmd"
        path.write_text(f"@echo {says}\n")
    else:
        path = folder / name
        path.write_text(f"#!/bin/sh\necho '{says}'\n")
        path.chmod(0o755)
    return path


_NOT_ON_PATH = "  - '{name}' is not on your PATH yet: open a new shell, or add {bin} to your PATH."
_SHADOWED = "  - 'dvc' runs {other}, not the dvc installed with mintd (see the warning above)."
# (mintd in uv's bin dir, another dvc first on PATH, a dvc in uv's bin dir, the closing lines)
_WHICH_DVC = [
    (True, False, True, ["mintd installed successfully (with {with_}, both on your PATH). Run 'mintd --help' to get started."]),
    (True, False, False, ["mintd installed (with {with_}), but:", _NOT_ON_PATH.replace("{name}", "dvc")]),
    (True, True, True, ["mintd installed (with {with_}), but:", _SHADOWED]),
    # A mintd problem is kept when dvc has one too.
    (False, True, True, ["mintd installed (with {with_}), but:", _NOT_ON_PATH.replace("{name}", "mintd"), _SHADOWED]),
]


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize("schema", [False, True])
@pytest.mark.parametrize(("has_mintd", "other_dvc", "uv_dvc", "closing"), _WHICH_DVC)
def test_stamped_install_sh_says_which_dvc_runs(
    tmp_path: Path, has_mintd: bool, other_dvc: bool, uv_dvc: bool, closing: list[str], schema: bool
) -> None:
    bin_dir, other_dir = tmp_path / "home" / "bin", tmp_path / "other"
    mintd = _exe(bin_dir, "mintd", "mintd 9.9.9") if has_mintd else None
    ours = _exe(bin_dir, "dvc", "3.67.1") if uv_dvc else None
    other = _exe(other_dir, "dvc", "3.50.0") if other_dvc else None
    path = f"{tmp_path / 'stubs'}:{other_dir}:{bin_dir}:/usr/bin:/bin"
    result, _ = _run_sh(tmp_path, _STAMPED_SH, *(["--with-schema"] if schema else []), assets=_release_assets(), PATH=path)
    assert result.returncode == 0, result.stderr
    with_ = "dvc + [schema] extra" if schema else "dvc"
    want = [line.format(with_=with_, bin=bin_dir, other=other) for line in closing]
    assert result.stdout.splitlines()[-len(want):] == want, result.stdout
    runs = other or ours
    if runs:
        assert f"  dvc on PATH: {runs} ({'3.50.0' if other else '3.67.1'})\n" in result.stdout
    assert ("WARNING: PATH shadowing detected!" in result.stderr) is other_dvc
    assert (f"  mintd on PATH: {mintd} (mintd 9.9.9)\n" in result.stdout) is has_mintd


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_stamped_install_sh_takes_a_link_to_uvs_own_dvc_for_that_dvc(tmp_path: Path) -> None:
    bin_dir, other_dir = tmp_path / "home" / "bin", tmp_path / "other"
    _exe(bin_dir, "mintd", "mintd 9.9.9")
    _exe(bin_dir, "dvc", "3.67.1")
    other_dir.mkdir()
    (other_dir / "dvc").symlink_to(bin_dir / "dvc")
    path = f"{tmp_path / 'stubs'}:{other_dir}:{bin_dir}:/usr/bin:/bin"
    result, _ = _run_sh(tmp_path, _STAMPED_SH, assets=_release_assets(), PATH=path)
    assert result.returncode == 0, result.stderr
    assert f"  dvc on PATH: {other_dir / 'dvc'} (3.67.1)\n" in result.stdout
    assert "shadowing" not in result.stderr
    assert result.stdout.splitlines()[-1].startswith("mintd installed successfully"), result.stdout


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_install_sh_that_installs_uv_checks_the_callers_path(tmp_path: Path) -> None:
    """With no uv, the script installs one and puts ~/.local/bin on its own PATH
    only: the caller's shell finds mintd and dvc there after a new shell."""
    local_bin = tmp_path / "home" / ".local" / "bin"
    _exe(local_bin, "mintd", "mintd 9.9.9")
    _exe(local_bin, "dvc", "3.67.1")
    uv_src = tmp_path / "uv-release"
    installer = f'mkdir -p "$HOME/.local/bin" && cp "{uv_src}/uv" "$HOME/.local/bin/uv"\n'.encode()
    result, log = _run_sh(
        tmp_path, _STAMPED_SH, assets={**_release_assets(), "install.sh": installer}, uv_dir=uv_src,
        STUB_UV_BIN=str(local_bin),
    )
    assert result.returncode == 0, result.stderr
    assert log.startswith("curl https://astral.sh/uv/0.13.9/install.sh\n"), log
    assert "uv tool install " in log
    assert result.stdout.splitlines()[-3:] == [
        "mintd installed (with dvc), but:",
        _NOT_ON_PATH.format(name="mintd", bin=local_bin),
        _NOT_ON_PATH.format(name="dvc", bin=local_bin),
    ], result.stdout


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_install_sh_reads_the_callers_path_without_globbing(tmp_path: Path) -> None:
    """A PATH entry is a directory name, never a pattern: 'globt/*' is not globt/sub."""
    bin_dir, sub_dir = tmp_path / "home" / "bin", tmp_path / "globt" / "sub"
    _exe(sub_dir, "mintd", "mintd 9.9.9")
    _exe(sub_dir, "dvc", "3.67.1")
    path = f"{tmp_path / 'stubs'}:{tmp_path / 'globt'}/*:/usr/bin:/bin"
    result, _ = _run_sh(tmp_path, _STAMPED_SH, assets=_release_assets(), PATH=path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-3:] == [
        "mintd installed (with dvc), but:",
        _NOT_ON_PATH.format(name="mintd", bin=bin_dir),
        _NOT_ON_PATH.format(name="dvc", bin=bin_dir),
    ], result.stdout


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize("entry", ["a newline in a name", "a trailing colon"])
def test_install_sh_reads_every_entry_of_the_callers_path(tmp_path: Path, entry: str) -> None:
    """A newline is part of a directory name, and an empty entry (a trailing ':') is the current directory."""
    bin_dir = tmp_path / ("new\nline" if entry == "a newline in a name" else "bin")  # uv's bin dir
    _exe(bin_dir, "mintd", "mintd 9.9.9")
    _exe(bin_dir, "dvc", "3.67.1")
    if entry == "a newline in a name":
        path, cwd = f"{tmp_path / 'stubs'}:{bin_dir}:/usr/bin:/bin", None
    else:
        path, cwd = f"{tmp_path / 'stubs'}:/usr/bin:/bin:", bin_dir
    result, _ = _run_sh(
        tmp_path, _STAMPED_SH, assets=_release_assets(), cwd=cwd, PATH=path, STUB_UV_BIN=str(bin_dir)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1].startswith("mintd installed successfully"), result.stdout


class _Index:
    """A package index on localhost that records every request. It answers 404,
    except for the paths in `pages` (path: (content type, body))."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.pages: dict[str, tuple[str, bytes]] = {}
        index = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                index.requests.append(self.path)
                page = index.pages.get(self.path)
                self.send_response(200 if page else 404)
                if page:
                    self.send_header("Content-Type", page[0])
                    self.send_header("Content-Length", str(len(page[1])))
                self.end_headers()
                if page and self.command == "GET":
                    self.wfile.write(page[1])

            do_HEAD = do_GET

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.root = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.url = f"{self.root}/simple"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


_REAL_UV = shutil.which("uv")
# Where the install line runs is tmp_path/work/a/b; each plant adds an index there or above.
_PLANTS = {
    "uv.toml where it runs": ("work/a/b/uv.toml", 'extra-index-url = ["{url}"]\n'),
    "uv.toml two folders up": ("work/uv.toml", 'extra-index-url = ["{url}"]\n'),
    "pyproject.toml with [tool.uv.pip]": (
        "work/a/b/pyproject.toml",
        '[project]\nname = "planted"\nversion = "0"\n\n[tool.uv.pip]\nextra-index-url = ["{url}"]\n',
    ),
}


def _plant(tmp_path: Path, plant: str | None, url: str = "") -> Path:
    """Plants `plant` (if any) with index `url`, makes a real venv for the tool's Python
    under tmp_path/tools/mintd, and returns the folder the install line runs in."""
    run_in = tmp_path / "work" / "a" / "b"
    run_in.mkdir(parents=True)
    if plant is not None:
        rel, text = _PLANTS[plant]
        (tmp_path / rel).write_text(text.format(url=url))
    subprocess.run(
        [_REAL_UV, "venv", "--no-config", "-q", "--python", sys.executable, str(tmp_path / "tools" / "mintd")],
        check=True, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
    )
    return run_in


def _real_uv_env(tmp_path: Path, default: _Index) -> dict[str, str]:
    """The real uv for the reflink step: its own cache, and the local default index, never the network."""
    assert _REAL_UV
    return {
        "REAL_UV": _REAL_UV, "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_DEFAULT_INDEX": default.url,
        "UV_PYTHON_DOWNLOADS": "never", "UV_NO_PROGRESS": "1",
    }


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.skipif(_REAL_UV is None, reason="needs a real uv")
@pytest.mark.parametrize("plant", list(_PLANTS))
def test_stamped_install_sh_never_asks_an_index_planted_where_it_runs(tmp_path: Path, plant: str) -> None:
    """A uv.toml or pyproject.toml where the install line runs, or above it, adds no
    index to the reflink step: real uv asks the default index and never the planted one."""
    planted, default = _Index(), _Index()
    try:
        run_in = _plant(tmp_path, plant, planted.url)
        result, log = _run_sh(
            tmp_path, _STAMPED_SH, assets=_release_assets(), cwd=run_in,
            STUB_UV_TOOLS=str(tmp_path / "tools"), **_real_uv_env(tmp_path, default),
        )
    finally:
        planted.close()
        default.close()
    assert result.returncode == 0, result.stderr
    assert planted.requests == [], planted.requests
    assert default.requests, result.stderr  # real uv did look for reflink, on the default index


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.skipif(_REAL_UV is None, reason="needs a real uv")
def test_stamped_install_sh_refuses_a_reflink_file_whose_hash_isnt_pinned(tmp_path: Path) -> None:
    """An index (PyPI itself, compromised) serving another reflink-0.2.2 sdist, whose
    setup.py would leave a mark: uv refuses it on its hash, so the setup.py never runs."""
    import io
    import tarfile

    mark = tmp_path / "setup-py-ran"
    files = {
        "reflink-0.2.2/PKG-INFO": "Metadata-Version: 2.1\nName: reflink\nVersion: 0.2.2\n",
        "reflink-0.2.2/setup.py": f"open({str(mark)!r}, 'w').close()\n"
        "from setuptools import setup\nsetup(name='reflink', version='0.2.2')\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            tar.addfile(info, io.BytesIO(text.encode()))
    default = _Index()
    sdist = f"{default.root}/files/reflink-0.2.2.tar.gz"
    default.pages = {
        "/simple/reflink/": ("text/html", f'<a href="{sdist}">reflink-0.2.2.tar.gz</a>'.encode()),
        "/files/reflink-0.2.2.tar.gz": ("application/gzip", buf.getvalue()),
    }
    try:
        run_in = _plant(tmp_path, None)
        result, _ = _run_sh(
            tmp_path, _STAMPED_SH, assets=_release_assets(), cwd=run_in,
            STUB_UV_TOOLS=str(tmp_path / "tools"), **_real_uv_env(tmp_path, default),
        )
    finally:
        default.close()
    assert result.returncode == 0, result.stderr
    assert "/files/reflink-0.2.2.tar.gz" in default.requests, default.requests
    assert "Hash mismatch" in result.stderr, result.stderr
    assert not mark.exists()
    assert "Warning: reflink install failed" in result.stderr


@pytest.mark.skipif(_REAL_UV is None, reason="needs a real uv")
def test_the_releases_constraints_hash_reflinks_dependencies() -> None:
    """The reflink step takes cffi's and pycparser's hashes from constraints.txt, which
    release.yml exports from the lock exactly so; without them --require-hashes fails."""
    assert _REAL_UV
    out = subprocess.run(
        [_REAL_UV, "export", "--locked", "--no-dev", "--no-emit-project", "--extra", "schema", "-q", "--no-config"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
    ).stdout
    for name in ("cffi", "pycparser"):
        assert re.search(rf"^{name}==\S+.*\\\n\s+--hash=sha256:[0-9a-f]{{64}}", out, re.M), name


def test_both_scripts_pin_reflink_to_the_same_hashes() -> None:
    sh = "".join(re.findall(r'^REFLINK_REQ\+?="([^"]*)"$', SH, re.M))
    ps1 = "".join(re.findall(r'^(?:\$ReflinkReq = )?\s*"((?:reflink==| --hash=)[^"]*)"', PS1, re.M))
    assert re.fullmatch(r"reflink==0\.2\.2( --hash=sha256:[0-9a-f]{64}){3}", sh), sh
    assert ps1 == sh


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize("python_there", [True, False])
def test_stamped_install_sh_finds_the_tool_python_where_uv_keeps_its_tools(tmp_path: Path, python_there: bool) -> None:
    tools = tmp_path / "mytools"  # UV_TOOL_DIR, as `uv tool dir` reports it
    if python_there:
        (tools / "mintd" / "bin").mkdir(parents=True)
        (tools / "mintd" / "bin" / "python").touch()
    result, log = _run_sh(tmp_path, _STAMPED_SH, assets=_release_assets(), STUB_UV_TOOLS=str(tools))
    assert result.returncode == 0, result.stderr
    assert (f"uv pip install --no-config --python {tools}/mintd/bin/python " in log) is python_there
    assert ("could not locate mintd tool Python" in result.stderr) is not python_there
    assert "mintd installed (with dvc), but:" in result.stdout  # the closing line, either way


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize("before", ["none", "mintd's", "uv tool", "file"])
def test_stamped_install_sh_names_a_dvc_it_replaces(tmp_path: Path, before: str) -> None:
    bin_dir, tools = tmp_path / "home" / "bin", tmp_path / "home" / ".local" / "share" / "uv" / "tools"
    bin_dir.mkdir(parents=True)
    if before == "mintd's":
        (bin_dir / "dvc").symlink_to(tools / "mintd" / "bin" / "dvc")
    elif before == "uv tool":
        (bin_dir / "dvc").symlink_to(tools / "dvc" / "bin" / "dvc")
    elif before == "file":  # what `pip install --user dvc` leaves in ~/.local/bin
        _exe(bin_dir, "dvc", "3.50.0")
    result, _ = _run_sh(tmp_path, _STAMPED_SH, assets=_release_assets())
    assert result.returncode == 0, result.stderr
    was = {"uv tool": f"a link to {tools / 'dvc' / 'bin' / 'dvc'}", "file": "a file of its own"}.get(before)
    replaced = [line for line in result.stdout.splitlines() if line.startswith("Replaced ")]
    assert replaced == ([f"Replaced {bin_dir / 'dvc'}, {was}, with the dvc installed with mintd."] if was else [])


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_a_stamped_install_sh_cut_short_runs_nothing(tmp_path: Path) -> None:
    """`curl | bash` runs what has arrived. Cut at every line, the script must
    download and install nothing: everything runs from main, called last."""
    _run_sh(tmp_path, "true", assets=_release_assets())  # sets up the stubs
    env = {"PATH": f"{tmp_path / 'stubs'}:/usr/bin:/bin", "HOME": str(tmp_path / "home"),
           "LOG": str(tmp_path / "log"), "STUB_ASSETS": str(tmp_path / "assets")}
    lines = _STAMPED_SH.splitlines(keepends=True)
    for cut in range(len(lines)):  # every prefix but the whole script
        subprocess.run(["bash", "-s"], input="".join(lines[:cut]), env=env, capture_output=True, text=True)
        assert (tmp_path / "log").read_text() == "", f"cut after line {cut} ran something"


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
def test_unstamped_install_sh_runs_the_newest_releases_script(tmp_path: Path) -> None:
    # The handoff exports the loop guard, so an unstamped newest script stops instead of looping.
    asset = b'echo forwarded "$@" "guard=$MINTD_FORWARDED"\n'
    result, log = _run_sh(tmp_path, SH, "--with-schema", assets={"install.sh": asset})
    assert (result.returncode, result.stdout) == (0, "forwarded --with-schema guard=1\n"), result.stderr
    assert log == "curl https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.sh\n"


@pytest.mark.skipif(os.name == "nt", reason="install.sh is the POSIX installer")
@pytest.mark.parametrize(
    ("assets", "env", "error"),
    [
        ({}, {}, "no mintd release is out yet"),
        ({"install.sh": b"echo never"}, {"MINTD_FORWARDED": "1"}, "has no tag in it"),
    ],
)
def test_unstamped_install_sh_stops_without_a_release_or_a_tag(tmp_path: Path, assets: dict[str, bytes], env: dict[str, str], error: str) -> None:
    result, _ = _run_sh(tmp_path, SH, assets=assets, **env)
    assert result.returncode == 1
    assert error in result.stderr


_PWSH = shutil.which("pwsh") or shutil.which("powershell")
_NEEDS_PWSH = pytest.mark.skipif(_PWSH is None, reason="needs PowerShell")

# `irm | iex` runs the script in the caller's own session. Each run below sets
# the caller's preferences to non-defaults first, then reports whether the
# script changed them or left variables behind (LASTEXITCODE is the stub uv's
# doing, as real uv's; `snap`, and `_`/`PSItem` from the harness's own catch,
# are this harness's).
_SESSION_BEFORE = """
$ErrorActionPreference = 'SilentlyContinue'; $ProgressPreference = 'Continue'
$snap = @((Get-Variable).Name)
"""
_SESSION_AFTER = """
$left = @((Get-Variable).Name | Where-Object { $snap -notcontains $_ -and $_ -notin @('LASTEXITCODE', 'snap', '_', 'PSItem') })
"session: eap=$ErrorActionPreference progress=$ProgressPreference left=[$($left -join ',')] guard=[$env:MINTD_FORWARDED]"
"""
_SESSION_UNCHANGED = "session: eap=SilentlyContinue progress=Continue left=[] guard=[]"


def _pwsh(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_PWSH, "-NoProfile", "-Command", script], capture_output=True, text=True)


@_NEEDS_PWSH
def test_unstamped_install_ps1_hands_off_and_leaves_the_session_as_it_was() -> None:
    """The handoff passes -WithSchema on and sets the loop guard while the
    release's script runs; afterwards the session is as it was, also when
    that script fails, so a second install in the same window works."""
    script = (
        # A function shadows the cmdlet, also when called as `irm`.
        'function Invoke-RestMethod { $global:served }\n'
        '$global:served = \'param([switch]$WithSchema) "forwarded schema=$WithSchema guard=$env:MINTD_FORWARDED"\'\n'
        f"$t = Get-Content -Raw '{REPO / 'install.ps1'}'\n"
        + _SESSION_BEFORE
        + "& ([scriptblock]::Create($t)) -WithSchema\n"
        "Invoke-Expression $t\n"
        '$global:served = \'throw "the install failed"\'\n'
        "try { Invoke-Expression $t } catch { \"caught: $_\" }\n"
        + _SESSION_AFTER
    )
    result = _pwsh(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "forwarded schema=True guard=1",
        "forwarded schema=False guard=1",
        "caught: the install failed",
        _SESSION_UNCHANGED,
    ]


@_NEEDS_PWSH
@pytest.mark.parametrize(
    ("setup", "error"),
    [
        ('$env:MINTD_FORWARDED = "1"', "has no tag in it"),  # the newest release's script is unstamped
        ('function Invoke-RestMethod { throw "404" }', "no mintd release is out yet"),
    ],
)
def test_unstamped_install_ps1_stops_without_a_release_or_a_tag(setup: str, error: str) -> None:
    script = (
        'function Invoke-RestMethod { "Write-Output forwarded" }\n'
        f"{setup}\n"
        f"try {{ Invoke-Expression (Get-Content -Raw '{REPO / 'install.ps1'}') }} catch {{ \"caught: $_\" }}\n"
    )
    result = _pwsh(script)
    assert result.stdout.splitlines()[0].startswith("caught: ") and error in result.stdout, result.stdout + result.stderr
    assert "forwarded" not in result.stdout


def _ps(text: str) -> str:
    """`text` as a PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def _run_stamped_ps1(
    tmp_path: Path,
    *,
    sha256: str | None = None,
    uv_says: str = "uv 0.13.9",
    uv_fails: bool = False,
    path: tuple[Path, ...] = (),
    bin_name: str = "bin",
    tools_name: str = "tools",
    tool_python: bool = True,
    temp: Path | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """The stamped install.ps1 under `irm | iex`, from a session whose
    preferences are not the defaults. Downloads come from tmp_path; `uv` only
    records what it was asked, and says `uv_says` for --version, tmp_path/`bin_name`
    for `tool dir --bin` and tmp_path/`tools_name` for `tool dir`; PATH is `path` alone.
    The tool's python.exe is under the latter when `tool_python`, and always at
    the %LOCALAPPDATA% guess the script must not use. `temp` is the session's temp folder,
    `cwd` the folder it runs in, and `env` more variables (REAL_UV: `uv pip` runs that uv).
    Returns the run, its last line of output the session check, and the log."""
    for name, data in _release_assets(sha256).items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "install.ps1").write_text(PS1.replace('\n$MintdTag = ""\n', '\n$MintdTag = "v9.9.9"\n'))
    for tools in [tmp_path / "local" / "uv" / "tools", *([tmp_path / tools_name] if tool_python else [])]:
        (tools / "mintd" / "Scripts").mkdir(parents=True, exist_ok=True)
        (tools / "mintd" / "Scripts" / "python.exe").touch()
    log = tmp_path / "log"
    script = (
        f"$env:LOCALAPPDATA = {_ps(str(tmp_path / 'local'))}\n"
        f"$env:PATH = {_ps(os.pathsep.join(map(str, path)))}\n"
        # GetTempPath reads TMPDIR on macOS and Linux, and TMP before TEMP on Windows.
        + (f"$env:TMPDIR = {_ps(str(temp))}; $env:TMP = {_ps(str(temp))}; $env:TEMP = {_ps(str(temp))}\n" if temp else "")
        + "".join(f"$env:{k} = {_ps(v)}\n" for k, v in (env or {}).items())
        + (f"Set-Location -LiteralPath {_ps(str(cwd))}\n" if cwd else "")
        + "function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing) "
        f"Add-Content {_ps(str(log))} \"get $Uri\"; Copy-Item (Join-Path {_ps(str(tmp_path))} ($Uri -split '/')[-1]) $OutFile }}\n"
        f"function uv {{ Add-Content {_ps(str(log))} \"uv $($args -join ' ')\"; $global:LASTEXITCODE = 0\n"
        "  if ($args[0] -eq 'pip' -and $env:REAL_UV) { & $env:REAL_UV @args; return }\n"
        f"  if ($args[0] -eq '--version') {{ {_ps(uv_says)} }}\n"
        f"  elseif ($args[0] -eq 'tool' -and $args[1] -eq 'dir') {{ if ($args -contains '--bin') {{ {_ps(str(tmp_path / bin_name))} }}"
        f" else {{ {_ps(str(tmp_path / tools_name))} }} }}\n"
        f"  elseif ($args[0] -eq 'tool' -and $args[1] -eq 'install') {{ $global:LASTEXITCODE = {int(uv_fails)} }} }}\n"
        + _SESSION_BEFORE
        + f"try {{ Invoke-Expression (Get-Content -Raw {_ps(str(tmp_path / 'install.ps1'))}) *>&1 | ForEach-Object {{ \"$_\" }} }}"
        " catch { \"caught: $_\" }\n"
        + _SESSION_AFTER
    )
    result = _pwsh(script)
    return result, log.read_text() if log.exists() else ""


@_NEEDS_PWSH
@pytest.mark.parametrize("case", ["installs", "wrong hash", "uv fails", "old uv"])
def test_stamped_install_ps1_installs_only_a_matching_wheel_and_leaves_the_session_as_it_was(
    tmp_path: Path, case: str
) -> None:
    result, ran = _run_stamped_ps1(
        tmp_path,
        sha256="0" * 64 if case == "wrong hash" else None,
        uv_says="uv 0.12.0" if case == "old uv" else "uv 0.13.9",
        uv_fails=case == "uv fails",
    )
    out = result.stdout
    assert out.splitlines()[-1] == _SESSION_UNCHANGED, out + result.stderr
    # Every file from the tag's own release, never latest; none for a uv too old.
    assert [line for line in ran.splitlines() if line.startswith("get ")] == [] if case == "old uv" else [
        f"get https://github.com/Health-Care-Affordability-Lab/mintd/releases/download/v9.9.9/{name}"
        for name in (_WHEEL, f"{_WHEEL}.sha256", "constraints.txt")
    ]
    install = re.compile(r"^uv tool install --force --reinstall --refresh --python 3\.13 --with-executables-from dvc --constraints \S+constraints\.txt \S+mintd-9\.9\.9-py3-none-any\.whl$", re.M)
    reflink = re.compile(
        r"^uv pip install --no-config --python \S+python\.exe --require-hashes --constraints \S+constraints\.txt"
        r" --requirements \S+reflink\.txt$", re.M,
    )
    if case == "installs":
        assert "caught" not in out
        assert install.search(ran) and reflink.search(ran), ran
    elif case == "wrong hash":
        assert "does not match its SHA-256; nothing was installed" in out
        assert "tool install" not in ran
    elif case == "old uv":
        assert "caught: this installer needs uv 0.13.9 or newer, and you have 0.12.0. " in out
        assert ran == "uv --version\n"
    else:
        assert "caught: Failed to install mintd" in out
        assert install.search(ran) and not reflink.search(ran)


_PS1_UPGRADE = (
    "Upgrade it with 'uv self update', then run the install line again. Nothing was installed. "
    "If uv refuses to update itself (installed with pip, pipx, winget, scoop or conda), upgrade it with the tool that installed it."
)


@_NEEDS_PWSH
@pytest.mark.parametrize(
    ("says", "stop"),
    [
        ("uv 0.12.19", "this installer needs uv 0.13.9 or newer, and you have 0.12.19."),
        ("uv 0.13.8", "this installer needs uv 0.13.9 or newer, and you have 0.13.8."),
        ("uv 0.13.9", None),
        ("uv 0.13.10", None),  # newer by number, older as a string
        ("uv 1.0.0", None),
        ("", "could not read your uv's version from 'uv --version'; this installer needs uv 0.13.9 or newer."),
        ("uv", "could not read your uv's version from 'uv --version'; this installer needs uv 0.13.9 or newer."),
    ],
)
def test_stamped_install_ps1_compares_uv_versions_by_number(tmp_path: Path, says: str, stop: str | None) -> None:
    result, ran = _run_stamped_ps1(tmp_path, uv_says=says)
    if stop:
        assert f"caught: {stop} {_PS1_UPGRADE}" in result.stdout, result.stdout + result.stderr
        assert ran == "uv --version\n"
    else:
        assert "caught" not in result.stdout, result.stdout + result.stderr
        assert "uv tool install " in ran


@_NEEDS_PWSH
@pytest.mark.parametrize(("has_mintd", "other_dvc", "uv_dvc", "closing"), _WHICH_DVC)
def test_stamped_install_ps1_says_which_dvc_runs(
    tmp_path: Path, has_mintd: bool, other_dvc: bool, uv_dvc: bool, closing: list[str]
) -> None:
    bin_dir, other_dir = tmp_path / "bin", tmp_path / "other"
    mintd = _exe(bin_dir, "mintd", "mintd 9.9.9") if has_mintd else None
    ours = _exe(bin_dir, "dvc", "3.67.1") if uv_dvc else None
    other = _exe(other_dir, "dvc", "3.50.0") if other_dvc else None
    result, _ = _run_stamped_ps1(tmp_path, path=(other_dir, bin_dir))
    lines = result.stdout.splitlines()[:-1]  # the last is the session check
    assert "caught" not in result.stdout, result.stdout + result.stderr
    want = [line.format(with_="dvc", bin=bin_dir, other=other) for line in closing]
    assert lines[-len(want):] == want, result.stdout
    runs = other or ours
    if runs:
        assert f"  dvc on PATH: {runs} ({'3.50.0' if other else '3.67.1'})" in lines
    assert ("PATH shadowing detected!" in result.stdout) is other_dvc
    assert (f"  mintd on PATH: {mintd} (mintd 9.9.9)" in lines) is has_mintd


@_NEEDS_PWSH
@pytest.mark.parametrize(
    ("in_bin", "mintds", "replaced"),
    [
        (None, b"mintd's dvc", False),
        (b"mintd's dvc", b"mintd's dvc", False),  # a copy of mintd's own, as uv makes on Windows
        # `uv tool install --force dvc` after mintd: uv tool list shows dvc under both tools.
        (b"dvc 3.60.0's dvc", b"mintd's dvc", True),
        (b"a dvc of its own", None, True),
    ],
)
def test_stamped_install_ps1_names_a_dvc_it_replaces(
    tmp_path: Path, in_bin: bytes | None, mintds: bytes | None, replaced: bool
) -> None:
    (tmp_path / "bin").mkdir()
    if in_bin is not None:
        (tmp_path / "bin" / "dvc.exe").write_bytes(in_bin)
    if mintds is not None:
        (tmp_path / "tools" / "mintd" / "Scripts").mkdir(parents=True)
        (tmp_path / "tools" / "mintd" / "Scripts" / "dvc.exe").write_bytes(mintds)
    result, _ = _run_stamped_ps1(tmp_path)
    assert "caught" not in result.stdout, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("Replaced ")]
    want = f"Replaced {tmp_path / 'bin' / 'dvc.exe'}, a dvc that was not mintd's, with the dvc installed with mintd."
    assert lines == ([want] if replaced else [])


@_NEEDS_PWSH
@pytest.mark.parametrize(
    "which",
    [
        pytest.param("uv's bin", marks=pytest.mark.skipif(os.name == "nt", reason="chmod 000 locks nothing on Windows")),
        pytest.param("mintd's", marks=pytest.mark.skipif(os.name == "nt", reason="chmod 000 locks nothing on Windows")),
        "a folder in uv's bin",
    ],
)
def test_stamped_install_ps1_installs_when_a_dvc_cant_be_read(tmp_path: Path, which: str) -> None:
    """A dvc.exe that can't be read (antivirus, another installer) or a folder in its
    place never stops the install: it counts as not mintd's."""
    bin_dvc, mintds = tmp_path / "bin" / "dvc.exe", tmp_path / "tools" / "mintd" / "Scripts" / "dvc.exe"
    mintds.parent.mkdir(parents=True)
    mintds.write_bytes(b"mintd's dvc")
    bin_dvc.parent.mkdir()
    if which == "a folder in uv's bin":
        bin_dvc.mkdir()
    else:
        bin_dvc.write_bytes(b"mintd's dvc")
        (bin_dvc if which == "uv's bin" else mintds).chmod(0)
    try:
        result, ran = _run_stamped_ps1(tmp_path)
    finally:
        for f in (bin_dvc, mintds):
            if f.is_file():
                f.chmod(0o644)
    assert "caught" not in result.stdout, result.stdout + result.stderr
    assert "uv tool install " in ran
    want = f"Replaced {bin_dvc}, a dvc that was not mintd's, with the dvc installed with mintd."
    assert want in result.stdout.splitlines(), result.stdout


@_NEEDS_PWSH
@pytest.mark.parametrize(("in_bin", "replaced"), [(b"a dvc of its own", True), (b"mintd's dvc", False)])
def test_stamped_install_ps1_reads_folders_with_brackets_literally(tmp_path: Path, in_bin: bytes, replaced: bool) -> None:
    """'b[1]' and 't[1]' are folder names, not wildcards: both dvc.exe files are found and compared."""
    bin_dir, scripts = tmp_path / "b[1]", tmp_path / "t[1]" / "mintd" / "Scripts"
    bin_dir.mkdir()
    scripts.mkdir(parents=True)
    (bin_dir / "dvc.exe").write_bytes(in_bin)
    (scripts / "dvc.exe").write_bytes(b"mintd's dvc")
    result, ran = _run_stamped_ps1(tmp_path, bin_name="b[1]", tools_name="t[1]")
    assert "caught" not in result.stdout, result.stdout + result.stderr
    assert f"uv pip install --no-config --python {scripts / 'python.exe'} " in ran, ran  # the tool's Python, found too
    lines = [line for line in result.stdout.splitlines() if line.startswith("Replaced ")]
    want = f"Replaced {bin_dir / 'dvc.exe'}, a dvc that was not mintd's, with the dvc installed with mintd."
    assert lines == ([want] if replaced else []), result.stdout


@_NEEDS_PWSH
def test_stamped_install_ps1_checks_the_wheel_in_a_temp_folder_with_brackets(tmp_path: Path) -> None:
    """A temp folder like 'tmp[2]' is a name, not a wildcard: the wheel's hash is still checked, and it installs."""
    temp = tmp_path / "tmp[2]"
    temp.mkdir()
    result, ran = _run_stamped_ps1(tmp_path, temp=temp)
    assert "caught" not in result.stdout, result.stdout + result.stderr
    assert re.search(r"^uv tool install .*tmp\[2\].*mintd-9\.9\.9-py3-none-any\.whl$", ran, re.M), ran
    assert list(temp.iterdir()) == []  # and its own folder there is removed afterwards


@_NEEDS_PWSH
@pytest.mark.parametrize("python_there", [True, False])
def test_stamped_install_ps1_finds_the_tool_python_where_uv_keeps_its_tools(tmp_path: Path, python_there: bool) -> None:
    """`uv tool dir` locates the tool's Python, as it locates mintd's dvc.exe; the
    %LOCALAPPDATA% guess is never used."""
    result, ran = _run_stamped_ps1(tmp_path, tool_python=python_there)
    assert "caught" not in result.stdout, result.stdout + result.stderr
    python = tmp_path / "tools" / "mintd" / "Scripts" / "python.exe"
    assert (f"uv pip install --no-config --python {python} " in ran) is python_there, ran
    assert ("uv pip install " in ran) is python_there, ran
    assert ("Could not locate mintd tool Python" in result.stdout) is not python_there, result.stdout


@_NEEDS_PWSH
@pytest.mark.skipif(_REAL_UV is None, reason="needs a real uv")
@pytest.mark.parametrize("plant", list(_PLANTS))
def test_stamped_install_ps1_never_asks_an_index_planted_where_it_runs(tmp_path: Path, plant: str) -> None:
    """install.ps1's reflink step, as install.sh's: real uv asks the default index, never the planted one."""
    planted, default = _Index(), _Index()
    try:
        run_in = _plant(tmp_path, plant, planted.url)
        scripts = tmp_path / "tools" / "mintd" / "Scripts"
        if os.name != "nt":  # a POSIX venv has bin/python; install.ps1 looks for Scripts\python.exe
            scripts.mkdir()
            (scripts / "python.exe").symlink_to(tmp_path / "tools" / "mintd" / "bin" / "python")
        result, ran = _run_stamped_ps1(tmp_path, tool_python=False, cwd=run_in, env=_real_uv_env(tmp_path, default))
    finally:
        planted.close()
        default.close()
    assert "caught" not in result.stdout, result.stdout + result.stderr
    assert planted.requests == [], planted.requests
    assert default.requests, result.stdout + result.stderr  # real uv did look for reflink, on the default index

