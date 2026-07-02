# Install mintd with reflink support (Windows).
#
# Usage:
#   irm https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/main/install.ps1 | iex
#
# Or from a specific branch:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/<branch>/install.ps1))) -Branch <branch>
param(
    [string]$Branch = "",
    [switch]$WithSchema
)

$ErrorActionPreference = "Stop"
$Repo = "ssh://git@github.com/Health-Care-Affordability-Lab/mintdv2.git"

# Ensure uv is installed
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    irm https://astral.sh/uv/install.ps1 | iex
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}

$GitUrl = "git+$Repo"
if ($Branch) {
    $GitUrl = "${GitUrl}@${Branch}"
}

if ($WithSchema) {
    $InstallSpec = "${GitUrl}[schema]"
} else {
    $InstallSpec = $GitUrl
}

Write-Host "Installing mintd..."
uv tool install --force --reinstall --refresh $InstallSpec
if ($LASTEXITCODE -ne 0) { throw "Failed to install mintd" }

# Locate the tool's Python interpreter
$ToolPython = Join-Path $env:LOCALAPPDATA "uv\tools\mintd\Scripts\python.exe"
if (-not (Test-Path $ToolPython)) {
    $ToolPython = Join-Path $env:USERPROFILE ".local\share\uv\tools\mintd\Scripts\python.exe"
}

if (Test-Path $ToolPython) {
    Write-Host "Installing reflink support..."
    uv pip install --python $ToolPython cffi reflink
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not install reflink. DVC will fall back to hardlink/symlink/copy."
        Write-Warning "Run 'mintd --version' to verify your installation."
    }
} else {
    Write-Warning "Could not locate mintd tool Python - skipping reflink install."
    Write-Warning "Run 'mintd --version' to verify your installation."
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
