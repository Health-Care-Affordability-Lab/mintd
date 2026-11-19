#!/usr/bin/env bash
# Install mintd's newest release, with reflink support.
#
# Usage:
#   curl -fsSL https://github.com/Health-Care-Affordability-Lab/mintd/releases/latest/download/install.sh | bash
#
# A given release:
#   curl -fsSL https://github.com/Health-Care-Affordability-Lab/mintd/releases/download/vX.Y.Z/install.sh | bash
#
# It downloads that release's wheel, checks it against the release's SHA-256,
# and installs it with the dependency versions in the release's
# constraints.txt. Nothing is built from mintd's source.
set -euo pipefail

# The uv this script installs when none is found, and the oldest it accepts
# without a warning. To bump: set it to the newest release
# (`gh api repos/astral-sh/uv/releases/latest --jq .tag_name`) here and in
# install.ps1; tests/test_install_scripts.py checks the two match.
UV_VERSION="0.13.9"

# True when version $1 is older than version $2.
version_lt() {
    [[ "$1" != "$2" && "$(printf '%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}

# .github/workflows/release.yml writes the release's tag on the next line;
# it is empty in the repository's own copy.
MINTD_TAG=""
RELEASES="https://github.com/Health-Care-Affordability-Lab/mintd/releases"

# True when file $1's SHA-256 is the first field of file $2.
sha256_matches() {
    local have
    have=$( { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; } | awk '{print $1}')
    [[ -n "$have" && "$have" == "$(awk '{print $1; exit}' "$2")" ]]
}

# Everything runs from main, called on the last line: a download cut short
# defines nothing, runs nothing, and so installs nothing.
main() {

# The repository's own copy (an old install line) runs the newest release's
# install.sh instead. Fetching it is kept apart from running it, so a failed
# install is never reported as "no release".
if [[ -z "$MINTD_TAG" ]]; then
    [[ -z "${MINTD_FORWARDED:-}" ]] || { echo "error: the newest release's install.sh has no tag in it" >&2; exit 1; }
    script=$(curl -fsSL "$RELEASES/latest/download/install.sh") || {
        echo "error: no mintd release is out yet; see $RELEASES" >&2; exit 1; }
    export MINTD_FORWARDED=1
    exec bash -c "$script" install.sh "$@"
fi

WITH_SCHEMA=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-schema) WITH_SCHEMA=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Ensure uv is installed
if ! command -v uv &>/dev/null; then
    echo "Installing uv ${UV_VERSION}..."
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    UV_HAVE="$(uv --version | awk '{print $2}')"
    if version_lt "$UV_HAVE" "$UV_VERSION"; then
        echo "Warning: uv ${UV_HAVE} is older than ${UV_VERSION}; upgrade it with 'uv self update'." >&2
    fi
fi

WHEEL="mintd-${MINTD_TAG#v}-py3-none-any.whl"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Downloading mintd ${MINTD_TAG}..."
for f in "$WHEEL" "$WHEEL.sha256" constraints.txt; do
    curl -fsSL -o "$TMP/$f" "$RELEASES/download/$MINTD_TAG/$f"
done
sha256_matches "$TMP/$WHEEL" "$TMP/$WHEEL.sha256" || {
    echo "error: $WHEEL does not match its SHA-256; nothing was installed." >&2; exit 1; }

# A direct reference carries the extra; quoted so the shell doesn't glob it.
if [[ "$WITH_SCHEMA" -eq 1 ]]; then
    INSTALL_SPEC="mintd[schema] @ file://$TMP/$WHEEL"
else
    INSTALL_SPEC="$TMP/$WHEEL"
fi

echo "Installing mintd ${MINTD_TAG}..."
# Pin Python to the newest version CI tests (.github/workflows/test.yml).
# Unpinned, uv uses the first Python it finds or downloads the newest, which
# can outrun wheels for dvc's compiled deps (pygit2 had none for 3.15 at
# first, and its source build needs libgit2).
uv tool install --force --reinstall --refresh --python 3.13 \
    --constraints "$TMP/constraints.txt" "$INSTALL_SPEC"

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
# With the release's constraints, so cffi is the version the release pins.
uv pip install --python "$TOOL_PYTHON" --constraints "$TMP/constraints.txt" cffi reflink || {
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

}

main "$@"
