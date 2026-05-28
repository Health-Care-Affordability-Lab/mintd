#!/usr/bin/env bash
# Install mintd with reflink support.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/main/install.sh | bash
#
# Or from a specific branch:
#   curl -fsSL https://raw.githubusercontent.com/Health-Care-Affordability-Lab/mintdv2/<branch>/install.sh | bash -s -- --branch <branch>
set -euo pipefail

REPO="ssh://git@github.com/Health-Care-Affordability-Lab/mintdv2.git"
BRANCH=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Ensure uv is installed
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

GIT_URL="git+${REPO}"
if [[ -n "$BRANCH" ]]; then
    GIT_URL="${GIT_URL}@${BRANCH}"
fi

echo "Installing mintd..."
uv tool install --force "$GIT_URL"

# Locate the tool's Python interpreter
TOOL_PYTHON="$HOME/.local/share/uv/tools/mintd/bin/python"
if [[ ! -f "$TOOL_PYTHON" ]]; then
    # XDG_DATA_HOME override
    TOOL_PYTHON="${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools/mintd/bin/python"
fi

if [[ ! -f "$TOOL_PYTHON" ]]; then
    echo "Warning: could not locate mintd tool Python — skipping reflink install." >&2
    echo "Run 'mintd --version' to verify your installation." >&2
    exit 0
fi

echo "Installing reflink support (halves DVC disk usage on APFS/XFS/Btrfs)..."
uv pip install --python "$TOOL_PYTHON" cffi reflink || {
    echo "Warning: reflink install failed. DVC will fall back to hardlink/symlink/copy." >&2
}

echo ""
echo "mintd installed successfully (with bundled dvc). Run 'mintd --help' to get started."
