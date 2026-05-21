# Install mintd with reflink support (Windows).
#
# Usage:
#   irm https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/main/install.ps1 | iex
#
# Or from a specific branch:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/<branch>/install.ps1))) -Branch <branch>
param(
    [string]$Branch = ""
)

$ErrorActionPreference = "Stop"
$Repo = "https://github.com/Health-Care-Affordability-Lab/mintdv2.git"

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

Write-Host "Installing mintd..."
uv tool install --force $GitUrl
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
Write-Host "mintd installed successfully. Run 'mintd --help' to get started."
