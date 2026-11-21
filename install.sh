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

# The uv this script installs when none is found, and the oldest it accepts.
# To bump: set it to the newest release
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
        # A Homebrew uv is a link into brew's Cellar and can't update itself.
        UV_PATH="$(command -v uv)"
        if [[ "$UV_PATH" == */Cellar/uv/* || "$(readlink "$UV_PATH" || true)" == */Cellar/uv/* ]]; then
            UV_UPGRADE="brew upgrade uv"
        else
            UV_UPGRADE="uv self update"
        fi
        echo "error: this installer needs uv ${UV_VERSION} or newer, and you have ${UV_HAVE}." >&2
        echo "Upgrade it with '${UV_UPGRADE}', then run the install line again. Nothing was installed." >&2
        exit 1
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
# --with-executables-from puts dvc on PATH beside mintd, so nobody installs
# DVC separately.
uv tool install --force --reinstall --refresh --python 3.13 --with-executables-from dvc \
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

# --- Post-install verification (non-fatal diagnostics) -----------------------
# Help users catch the common failure mode where a successful reinstall still
# leaves another mintd or dvc winning on PATH (pipx / pip --user / brew /
# another bin dir resolving before the uv tool bin dir), or neither on PATH yet.
echo ""
echo "Verifying installation..."
UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
UV_BIN_DIR_REAL="$(cd "${UV_BIN_DIR}" 2>/dev/null && pwd -P || echo "${UV_BIN_DIR}")"
[[ -z "${UV_BIN_DIR}" ]] || echo "  uv tool bin dir: ${UV_BIN_DIR}"
PROBLEMS=""   # one "  - " line per problem, for the closing line

# Says which $1 runs and warns when it is not the one uv installed; $2 says
# what that means for $1.
check_on_path() {
    local found version dir
    if ! found="$(command -v "$1")"; then
        echo "  Warning: '$1' is not on your PATH yet." >&2
        PROBLEMS+=$'\n'"  - '$1' is not on your PATH yet: open a new shell, or add ${UV_BIN_DIR:-the uv tool bin dir} to your PATH."
        return 0
    fi
    if version="$("$1" --version 2>/dev/null)"; then
        echo "  $1 on PATH: ${found} (${version})"
    else
        echo "  $1 on PATH: ${found}"
        echo "  Warning: '$1 --version' did not run cleanly." >&2
    fi
    dir="$(cd "$(dirname "${found}")" 2>/dev/null && pwd -P || dirname "${found}")"
    if [[ -n "${UV_BIN_DIR}" && "${dir}" != "${UV_BIN_DIR_REAL}" ]]; then
        echo "" >&2
        echo "  WARNING: PATH shadowing detected!" >&2
        echo "    The '$1' on your PATH resolves to:" >&2
        echo "      ${found}" >&2
        echo "    but uv installed $1 into:" >&2
        echo "      ${UV_BIN_DIR_REAL}" >&2
        echo "    $2" >&2
        echo "    Remove the other copy, or put '${UV_BIN_DIR_REAL}' earlier on your PATH," >&2
        echo "    then re-open your shell." >&2
        PROBLEMS+=$'\n'"  - '$1' runs ${found}, not the $1 installed with mintd (see the warning above)."
    fi
}
check_on_path mintd "You are likely running an OLDER mintd (pipx / pip --user / another install)."
check_on_path dvc "Typing 'dvc' (as in 'dvc repro') runs that one; mintd itself runs the dvc installed with it."

# Surface what uv knows about the install.
echo ""
echo "Installed uv tools:"
uv tool list 2>/dev/null || echo "  (could not run 'uv tool list')" >&2

echo ""
if [[ "$WITH_SCHEMA" -eq 1 ]]; then WITH="dvc + [schema] extra"; else WITH="dvc"; fi
if [[ -z "$PROBLEMS" ]]; then
    echo "mintd installed successfully (with ${WITH}, both on your PATH). Run 'mintd --help' to get started."
else
    echo "mintd installed (with ${WITH}), but:${PROBLEMS}"
fi

}

main "$@"
