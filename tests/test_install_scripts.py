"""install.sh and install.ps1 install a release's own wheel, checked, with its constraints.

The scripts run here only with stub `curl` and `uv` first on a minimal PATH and
a temporary HOME, so the machine's own uv and mintd are never touched.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
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
case "$*" in
  --version) echo "uv 0.13.9" ;;
  "tool dir --bin") echo "$HOME/bin" ;;
esac
"""


def _run_sh(tmp_path: Path, script: str, *args: str, assets: dict[str, bytes], **env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    stubs, served, home = tmp_path / "stubs", tmp_path / "assets", tmp_path / "home"
    for d in (stubs, served, home / ".local/share/uv/tools/mintd/bin"):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".local/share/uv/tools/mintd/bin/python").touch()
    for name, body in (("curl", _STUB_CURL), ("uv", _STUB_UV)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    for name, data in assets.items():
        (served / name).write_bytes(data)
    log = tmp_path / "log"
    log.touch()
    full_env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(home), "LOG": str(log), "STUB_ASSETS": str(served), **env}
    result = subprocess.run(["bash", "-c", script, "install.sh", *args], env=full_env, capture_output=True, text=True)
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
    assert f"uv tool install --force --reinstall --refresh --python 3.13 --constraints {tmp[1]}/constraints.txt {spec}\n" in log
    assert re.search(rf"uv pip install --python \S+/mintd/bin/python --constraints {re.escape(tmp[1])}/constraints\.txt cffi reflink\n", log)
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


@_NEEDS_PWSH
@pytest.mark.parametrize("case", ["installs", "wrong hash", "uv fails"])
def test_stamped_install_ps1_installs_only_a_matching_wheel_and_leaves_the_session_as_it_was(
    tmp_path: Path, case: str
) -> None:
    for name, data in _release_assets("0" * 64 if case == "wrong hash" else None).items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "install.ps1").write_text(PS1.replace('\n$MintdTag = ""\n', '\n$MintdTag = "v9.9.9"\n'))
    tool_python = tmp_path / "local" / "uv" / "tools" / "mintd" / "Scripts" / "python.exe"
    tool_python.parent.mkdir(parents=True)
    tool_python.touch()
    log = tmp_path / "log"
    fail = "$true" if case == "uv fails" else "$false"
    script = (
        f"$env:LOCALAPPDATA = '{tmp_path / 'local'}'\n"
        # Downloads come from tmp_path; `uv` only records what it was asked.
        f"function Invoke-WebRequest {{ param($Uri, $OutFile, [switch]$UseBasicParsing) "
        f"Add-Content '{log}' \"get $Uri\"; Copy-Item (Join-Path '{tmp_path}' ($Uri -split '/')[-1]) $OutFile }}\n"
        f"function uv {{ Add-Content '{log}' \"uv $($args -join ' ')\"; if ($args[0] -eq '--version') {{ 'uv 0.13.9' }}; "
        f"$global:LASTEXITCODE = [int]({fail} -and $args[0] -eq 'tool' -and $args[1] -eq 'install') }}\n"
        + _SESSION_BEFORE
        + f"try {{ Invoke-Expression (Get-Content -Raw '{tmp_path / 'install.ps1'}') *> $null }} catch {{ \"caught: $_\" }}\n"
        + _SESSION_AFTER
    )
    result = _pwsh(script)
    out, ran = result.stdout, log.read_text() if log.exists() else ""
    assert out.splitlines()[-1] == _SESSION_UNCHANGED, out + result.stderr
    # Every file from the tag's own release, never latest.
    assert [line for line in ran.splitlines() if line.startswith("get ")] == [
        f"get https://github.com/Health-Care-Affordability-Lab/mintd/releases/download/v9.9.9/{name}"
        for name in (_WHEEL, f"{_WHEEL}.sha256", "constraints.txt")
    ]
    install = re.compile(r"^uv tool install --force --reinstall --refresh --python 3\.13 --constraints \S+constraints\.txt \S+mintd-9\.9\.9-py3-none-any\.whl$", re.M)
    reflink = re.compile(r"^uv pip install --python \S+python\.exe --constraints \S+constraints\.txt cffi reflink$", re.M)
    if case == "installs":
        assert "caught" not in out
        assert install.search(ran) and reflink.search(ran), ran
    elif case == "wrong hash":
        assert "does not match its SHA-256; nothing was installed" in out
        assert "tool install" not in ran
    else:
        assert "caught: Failed to install mintd" in out
        assert install.search(ran) and not reflink.search(ran)
