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
# The uv to install when none is found, and the oldest accepted. Bump it
# together with UV_VERSION in install.sh (see there).
$UvVersion = "0.13.9"

# Ensure uv is installed, and new enough for --with-executables-from below.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv $UvVersion..."
    irm "https://astral.sh/uv/$UvVersion/install.ps1" | iex
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
} else {
    $UvHave = if ("$(uv --version)" -match '(\d+\.\d+\.\d+)') { $Matches[1] }
    $Upgrade = "Upgrade it with 'uv self update', then run the install line again. Nothing was installed. " +
        "If uv refuses to update itself (installed with pip, pipx, winget, scoop or conda), upgrade it with the tool that installed it."
    if (-not $UvHave) {
        throw "could not read your uv's version from 'uv --version'; this installer needs uv $UvVersion or newer. $Upgrade"
    }
    if ([version]$UvHave -lt [version]$UvVersion) {
        throw "this installer needs uv $UvVersion or newer, and you have $UvHave. $Upgrade"
    }
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
    $Want = ((Get-Content -LiteralPath "$Whl.sha256") -split '\s+')[0]
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Whl).Hash -ne $Want) {   # -ne ignores case
        throw "$Wheel does not match its SHA-256; nothing was installed."
    }

    # A direct reference carries the extra; "$Whl[schema]" would index the string.
    if ($WithSchema) {
        $InstallSpec = "mintd[schema] @ $(([Uri]$Whl).AbsoluteUri)"
    } else {
        $InstallSpec = $Whl
    }

    # --force below replaces a dvc already in uv's bin dir. One that isn't
    # mintd's own is named below. On Windows uv copies each executable from the
    # tool's Scripts folder into its bin dir, so mintd's own dvc.exe is a copy
    # of mintd\Scripts\dvc.exe. -LiteralPath: a folder name like 'b[1]' is not
    # a wildcard. A dvc.exe that can't be read (locked by antivirus or another
    # installer, or a folder in its place) counts as not mintd's: the compare
    # must never stop the install.
    $ToolsDir = "$(uv tool dir)".Trim()
    $OldDvc = Join-Path "$(uv tool dir --bin)".Trim() "dvc.exe"
    $MintdDvc = Join-Path $ToolsDir "mintd\Scripts\dvc.exe"
    try {
        $ReplacedDvc = (Test-Path -LiteralPath $OldDvc) -and -not (
            (Test-Path -LiteralPath $MintdDvc) -and
            (Get-FileHash -LiteralPath $OldDvc).Hash -eq (Get-FileHash -LiteralPath $MintdDvc).Hash)
    } catch { $ReplacedDvc = $true }

    Write-Host "Installing mintd $MintdTag..."
    # Pin Python to the newest version CI tests, and put dvc on PATH beside
    # mintd (see install.sh).
    uv tool install --force --reinstall --refresh --python 3.13 --with-executables-from dvc --constraints $Constraints $InstallSpec
    if ($LASTEXITCODE -ne 0) { throw "Failed to install mintd" }
    if ($ReplacedDvc) { Write-Host "Replaced $OldDvc, a dvc that was not mintd's, with the dvc installed with mintd." }

    # The tool's Python, where uv keeps its tools (UV_TOOL_DIR when set).
    $ToolPython = Join-Path $ToolsDir "mintd\Scripts\python.exe"
    if (Test-Path -LiteralPath $ToolPython) {
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
    Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Post-install verification (non-fatal diagnostics) -----------------------
# Catch the common Windows failure mode where a successful reinstall still
# leaves another mintd or dvc winning on PATH (pipx / pip --user / winget /
# another bin dir resolving before the uv tool shim), or neither on PATH yet.
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
$UvBinDir = (& uv tool dir --bin 2>$null)
if ($LASTEXITCODE -eq 0 -and $UvBinDir) {
    $UvBinDir = "$UvBinDir".Trim()
    Write-Host "  uv tool bin dir: $UvBinDir"
    try { $UvBinDirFull = (Resolve-Path -LiteralPath $UvBinDir -ErrorAction Stop).Path } catch { $UvBinDirFull = $UvBinDir }
} else {
    $UvBinDir = $null
}

# Says which $Name runs and warns when it is not the one uv installed; $Means
# says what that means for $Name. Returns the problem, if any, for the last line.
function Test-OnPath($Name, $Means) {
    $Cmd = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Cmd) {
        Write-Warning "'$Name' is not on your PATH yet."
        $AddDir = if ($UvBinDir) { $UvBinDir } else { "the uv tool bin dir" }
        return "'$Name' is not on your PATH yet: open a new shell, or add $AddDir to your PATH."
    }
    $Version = (& $Cmd.Source --version 2>$null)
    if ($LASTEXITCODE -eq 0 -and $Version) {
        Write-Host "  $Name on PATH: $($Cmd.Source) ($Version)"
    } else {
        Write-Host "  $Name on PATH: $($Cmd.Source)"
        Write-Warning "'$Name --version' did not run cleanly."
    }
    if (-not $UvBinDir) { return }
    # Normalize both paths for a case-insensitive, separator-tolerant compare.
    $Dir = Split-Path -Parent $Cmd.Source
    try { $DirFull = (Resolve-Path -LiteralPath $Dir -ErrorAction Stop).Path } catch { $DirFull = $Dir }
    if ($DirFull.TrimEnd('\').ToLowerInvariant() -ne $UvBinDirFull.TrimEnd('\').ToLowerInvariant()) {
        Write-Host ""
        Write-Warning "PATH shadowing detected!"
        Write-Warning "  The '$Name' on your PATH resolves to:"
        Write-Warning "    $($Cmd.Source)"
        Write-Warning "  but uv installed $Name into:"
        Write-Warning "    $UvBinDirFull"
        Write-Warning "  $Means"
        Write-Warning "  Remove the other copy, or put '$UvBinDirFull' earlier on your PATH,"
        Write-Warning "  then open a new shell. Every '$Name' on your PATH:"
        foreach ($Each in (Get-Command $Name -CommandType Application -All)) { Write-Warning "    $($Each.Source)" }
        return "'$Name' runs $($Cmd.Source), not the $Name installed with mintd (see the warning above)."
    }
}
$Problems = @(
    Test-OnPath mintd "You are likely running an OLDER mintd (pipx / pip --user / another install)."
    Test-OnPath dvc "Typing 'dvc' (as in 'dvc repro') runs that one; mintd itself runs the dvc installed with it."
)

# Surface what uv knows about the install.
Write-Host ""
Write-Host "Installed uv tools:"
& uv tool list
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not run 'uv tool list'." }

$With = if ($WithSchema) { "dvc + [schema] extra" } else { "dvc" }
Write-Host ""
if (-not $Problems) {
    Write-Host "mintd installed successfully (with $With, both on your PATH). Run 'mintd --help' to get started."
} else {
    Write-Host "mintd installed (with $With), but:"
    foreach ($Problem in $Problems) { Write-Host "  - $Problem" }
}

}
