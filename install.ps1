# Install mintd's newest release, with reflink support (Windows).
#
# Usage:
#   irm https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.ps1 | iex
#
# With the [schema] extra:
#   & ([scriptblock]::Create((irm https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.ps1))) -WithSchema
#
# A given release: the same, with releases/download/vX.Y.Z/ in the URL.
# It downloads that release's wheel, checks it against the release's SHA-256,
# and installs it with the dependency versions in the release's
# constraints.txt. Nothing is built from mintd's source.
param(
    [switch]$WithSchema
)

# All of it in a block of its own (left unindented): under irm | iex this
# script runs in the caller's own session, and the block keeps every variable
# and preference it sets out of that session, whether the install works or fails.
& {

# .github/workflows/release.yml writes the release's tag on the next line;
# it is empty in the repository's own copy.
$MintdTag = ""
$Releases = "https://github.com/Health-Care-Affordability-Lab/mintd/releases"

# The repository's own copy (an old install line) runs the newest release's
# install.ps1 instead. Fetching it is kept apart from running it, so a failed
# install is never reported as "no release".
if (-not $MintdTag) {
    if ($env:MINTD_FORWARDED) { throw "the newest release's install.ps1 has no tag in it" }
    try { $text = Invoke-RestMethod "$Releases/latest/download/install.ps1" -ErrorAction Stop } catch { throw "no mintd release is out yet; see $Releases" }
    # irm | iex runs in the caller's own session, so the guard must not outlive this install.
    try {
        $env:MINTD_FORWARDED = "1"
        $sb = [scriptblock]::Create($text)
        if ($WithSchema) { & $sb -WithSchema } else { & $sb }
    } finally { Remove-Item Env:MINTD_FORWARDED -ErrorAction SilentlyContinue }
    return   # never exit: under irm | iex it would close the caller's window
}

$ErrorActionPreference = "Stop"
# The uv to install when none is found, and the oldest accepted without a
# warning. Bump it together with UV_VERSION in install.sh (see there).
$UvVersion = "0.13.9"

# Ensure uv is installed
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv $UvVersion..."
    irm "https://astral.sh/uv/$UvVersion/install.ps1" | iex
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
} elseif ((uv --version) -match '(\d+\.\d+\.\d+)' -and [version]$Matches[1] -lt [version]$UvVersion) {
    Write-Warning "uv $($Matches[1]) is older than $UvVersion; upgrade it with 'uv self update'."
}

$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is slow with a progress bar
$Wheel = "mintd-$($MintdTag.Substring(1))-py3-none-any.whl"
$Tmp = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $Tmp | Out-Null
try {
    Write-Host "Downloading mintd $MintdTag..."
    foreach ($f in @($Wheel, "$Wheel.sha256", "constraints.txt")) {
        Invoke-WebRequest -UseBasicParsing -Uri "$Releases/download/$MintdTag/$f" -OutFile (Join-Path $Tmp $f)
    }
    $Whl = Join-Path $Tmp $Wheel
    $Constraints = Join-Path $Tmp "constraints.txt"
    $Want = ((Get-Content "$Whl.sha256") -split '\s+')[0]
    if ((Get-FileHash -Algorithm SHA256 $Whl).Hash -ne $Want) {   # -ne ignores case
        throw "$Wheel does not match its SHA-256; nothing was installed."
    }

    # A direct reference carries the extra; "$Whl[schema]" would index the string.
    if ($WithSchema) {
        $InstallSpec = "mintd[schema] @ $(([Uri]$Whl).AbsoluteUri)"
    } else {
        $InstallSpec = $Whl
    }

    Write-Host "Installing mintd $MintdTag..."
    # Pin Python to the newest version CI tests (see install.sh).
    uv tool install --force --reinstall --refresh --python 3.13 --constraints $Constraints $InstallSpec
    if ($LASTEXITCODE -ne 0) { throw "Failed to install mintd" }

    # Locate the tool's Python interpreter
    $ToolPython = Join-Path $env:LOCALAPPDATA "uv\tools\mintd\Scripts\python.exe"
    if (-not (Test-Path $ToolPython)) {
        $ToolPython = Join-Path $env:USERPROFILE ".local\share\uv\tools\mintd\Scripts\python.exe"
    }

    if (Test-Path $ToolPython) {
        Write-Host "Installing reflink support..."
        # With the release's constraints, so cffi is the version the release pins.
        uv pip install --python $ToolPython --constraints $Constraints cffi reflink
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not install reflink. DVC will fall back to hardlink/symlink/copy."
            Write-Warning "Run 'mintd --version' to verify your installation."
        }
    } else {
        Write-Warning "Could not locate mintd tool Python - skipping reflink install."
        Write-Warning "Run 'mintd --version' to verify your installation."
    }
} finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

Write-Host ""
if ($WithSchema) {
    Write-Host "mintd installed successfully (with bundled dvc + [schema] extra). Run 'mintd --help' to get started."
} else {
    Write-Host "mintd installed successfully (with bundled dvc). Run 'mintd --help' to get started."
}

# --- Post-install verification (non-fatal diagnostics) -----------------------
# Catch the common Windows failure mode where a successful reinstall still
# leaves an OLDER mintd winning on PATH (pipx / pip --user / another bin dir
# resolving before the uv tool shim).
#
# Everything below is diagnostics only, so relax the script-wide EAP=Stop:
# in Windows PowerShell 5.1, `2>$null` on a native command turns each stderr
# line into an ErrorRecord, and under Stop the first one terminates the
# script — e.g. a broken stale shim would abort the install output right
# before the PATH-shadowing warning it exists to trigger. (PS 7.2+ no longer
# does this.) Nothing fatal runs after this point.
$ErrorActionPreference = "Continue"
Write-Host ""
Write-Host "Verifying installation..."

# (a) Resolved 'mintd' first on PATH + its version.
$ResolvedCmd = Get-Command mintd -ErrorAction SilentlyContinue
if ($ResolvedCmd) {
    $ResolvedMintd = $ResolvedCmd.Source
    Write-Host "  mintd on PATH: $ResolvedMintd"
    $MintdVersion = (& mintd --version 2>$null)
    if ($LASTEXITCODE -eq 0 -and $MintdVersion) {
        Write-Host "  $MintdVersion"
    } else {
        Write-Warning "'mintd --version' did not run cleanly."
    }
} else {
    Write-Warning "'mintd' is not on your PATH yet."
    Write-Warning "Open a new shell, or add the uv tool bin dir to PATH (see below)."
}

# (b) PATH-shadowing check against uv's tool bin dir.
$UvBinDir = (& uv tool dir --bin 2>$null)
if ($LASTEXITCODE -eq 0 -and $UvBinDir) {
    $UvBinDir = $UvBinDir.Trim()
    Write-Host "  uv tool bin dir: $UvBinDir"
    if ($ResolvedMintd) {
        $ResolvedDir = Split-Path -Parent $ResolvedMintd
        # Normalize both paths for a case-insensitive, separator-tolerant compare.
        try { $ResolvedDirFull = (Resolve-Path -LiteralPath $ResolvedDir -ErrorAction Stop).Path } catch { $ResolvedDirFull = $ResolvedDir }
        try { $UvBinDirFull = (Resolve-Path -LiteralPath $UvBinDir -ErrorAction Stop).Path } catch { $UvBinDirFull = $UvBinDir }
        $ResolvedNorm = $ResolvedDirFull.TrimEnd('\').ToLowerInvariant()
        $UvBinNorm = $UvBinDirFull.TrimEnd('\').ToLowerInvariant()
        if ($ResolvedNorm -ne $UvBinNorm) {
            Write-Host ""
            Write-Warning "PATH shadowing detected!"
            Write-Warning "  The 'mintd' on your PATH resolves to:"
            Write-Warning "    $ResolvedMintd"
            Write-Warning "  but uv installed mintd into:"
            Write-Warning "    $UvBinDirFull"
            Write-Warning "  You are likely running an OLDER mintd (pipx / pip --user / another"
            Write-Warning "  install) that wins on PATH. Inspect duplicates with:"
            Write-Warning "    where.exe mintd"
            Write-Warning "  Remove the stale copy, or put '$UvBinDirFull' earlier on your PATH,"
            Write-Warning "  then open a new shell."
            # Surface every mintd on PATH so the user can see the duplicates.
            $AllMintd = (& where.exe mintd 2>$null)
            if ($LASTEXITCODE -eq 0 -and $AllMintd) {
                Write-Warning "  All 'mintd' entries on PATH:"
                foreach ($line in $AllMintd) { Write-Warning "    $line" }
            }
        }
    }
}

# (c) Surface what uv knows about the install.
Write-Host ""
Write-Host "Installed uv tools:"
& uv tool list
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not run 'uv tool list'." }

}
