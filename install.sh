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
WITH_SCHEMA=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch) BRANCH="$2"; shift 2 ;;
        --with-schema) WITH_SCHEMA=1; shift ;;
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

# uv tool install accepts PEP 508 extras when the spec is suffixed `[extra]`.
# Wrap the whole spec so the shell doesn't glob the brackets.
if [[ "$WITH_SCHEMA" -eq 1 ]]; then
    INSTALL_SPEC="${GIT_URL}[schema]"
else
    INSTALL_SPEC="$GIT_URL"
fi

echo "Installing mintd..."
uv tool install --force --reinstall --refresh "$INSTALL_SPEC"

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
if [[ "$WITH_SCHEMA" -eq 1 ]]; then
    echo "mintd installed successfully (with bundled dvc + [schema] extra). Run 'mintd --help' to get started."
else
    echo "mintd installed successfully (with bundled dvc). Run 'mintd --help' to get started."
fi

# --- Post-install verification (non-fatal diagnostics) -----------------------
# Help users catch the common Windows/Unix failure mode where a successful
# reinstall still leaves an OLDER mintd winning on PATH (pipx / pip --user /
# another bin dir resolving before the uv tool shim).
echo ""
echo "Verifying installation..."

# (a) Resolved version of whatever 'mintd' is first on PATH.
if command -v mintd >/dev/null 2>&1; then
    RESOLVED_MINTD="$(command -v mintd)"
    echo "  mintd on PATH: ${RESOLVED_MINTD}"
    if MINTD_VERSION="$(mintd --version 2>/dev/null)"; then
        echo "  ${MINTD_VERSION}"
    else
        echo "  Warning: 'mintd --version' did not run cleanly." >&2
    fi
else
    echo "  Warning: 'mintd' is not on your PATH yet." >&2
    echo "           Open a new shell, or add the uv tool bin dir to PATH (see below)." >&2
fi

# (b) PATH-shadowing check: does the resolved 'mintd' live in uv's tool bin dir?
UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
if [[ -n "${UV_BIN_DIR}" ]]; then
    echo "  uv tool bin dir: ${UV_BIN_DIR}"
    if [[ -n "${RESOLVED_MINTD:-}" ]]; then
        # Compare the directory the resolved shim sits in against uv's bin dir.
        RESOLVED_DIR="$(cd "$(dirname "${RESOLVED_MINTD}")" 2>/dev/null && pwd -P || dirname "${RESOLVED_MINTD}")"
        UV_BIN_DIR_REAL="$(cd "${UV_BIN_DIR}" 2>/dev/null && pwd -P || echo "${UV_BIN_DIR}")"
        if [[ "${RESOLVED_DIR}" != "${UV_BIN_DIR_REAL}" ]]; then
            echo "" >&2
            echo "  WARNING: PATH shadowing detected!" >&2
            echo "    The 'mintd' on your PATH resolves to:" >&2
            echo "      ${RESOLVED_MINTD}" >&2
            echo "    but uv installed mintd into:" >&2
            echo "      ${UV_BIN_DIR_REAL}" >&2
            echo "    You are likely running an OLDER mintd (pipx / pip --user / another" >&2
            echo "    install) that wins on PATH. Remove the stale copy, or put" >&2
            echo "    '${UV_BIN_DIR_REAL}' earlier on your PATH, then re-open your shell." >&2
        fi
    fi
fi

# (c) Surface what uv knows about the install.
echo ""
echo "Installed uv tools:"
uv tool list 2>/dev/null || echo "  (could not run 'uv tool list')" >&2
