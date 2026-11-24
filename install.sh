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

# reflink isn't in uv.lock, so the hashes of its PyPI files (0.2.2's sdist and
# two wheels) are pinned here; cffi and pycparser take theirs from the
# release's constraints.txt. Bump it together with install.ps1; a test checks
# the two match.
REFLINK_REQ="reflink==0.2.2"
REFLINK_REQ+=" --hash=sha256:8435c7153af4d6e66dc8acb48a9372c8ec6f978a09cdf7b57cd6656d969e343a"
REFLINK_REQ+=" --hash=sha256:882375ee7319275ae5f6a6a1287406365dac1e9643b91ad10e5187d3f84253bd"
REFLINK_REQ+=" --hash=sha256:be4787c6208faf7fc892390909cf01e34e650ea67c37bf345addefd597ed90e1"

# True when version $1 is older than version $2.
version_lt() {
    [[ "$1" != "$2" && "$(printf '%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}

# .github/workflows/release.yml writes the release's tag on the next line;
# it is empty in the repository's own copy.
MINTD_TAG=""
RELEASES="https://github.com/Health-Care-Affordability-Lab/mintd/releases"

# Prints file $1's physical path: every link in the chain followed, and every
# directory resolved.
physical() {
    local p="$1" link
    while link="$(readlink "$p")"; do
        [[ "$link" == /* ]] || link="$(dirname "$p")/$link"
        p="$link"
    done
    echo "$(cd -P "$(dirname "$p")" 2>/dev/null && pwd -P)/$(basename "$p")"
}

# Prints where command $1 is on the caller's own PATH, which installing uv
# below changes only for this script. (`PATH=... command -v` would answer
# from bash's hash table.)
on_caller_path() {
    # Split on ':' by hand: no globbing, a newline is part of a name, and an
    # empty entry (a trailing ':' included) is the current directory.
    local dir rest="$CALLER_PATH:"
    while [[ -n "$rest" ]]; do
        dir="${rest%%:*}"
        rest="${rest#*:}"
        if [[ -f "${dir:-.}/$1" && -x "${dir:-.}/$1" ]]; then
            echo "${dir:-.}/$1"
            return 0
        fi
    done
    return 1
}

# Says how to upgrade the uv on PATH. A Homebrew uv, one that resolves into
# brew's Cellar, refuses `uv self update`.
uv_upgrade_advice() {
    if [[ "$(physical "$(command -v uv)")" == */Cellar/uv/* ]]; then
        echo "Upgrade it with 'brew upgrade uv', then run the install line again. Nothing was installed." >&2
    else
        echo "Upgrade it with 'uv self update', then run the install line again. Nothing was installed." >&2
        echo "If uv refuses to update itself (installed with pip, pipx, conda or a distro package), upgrade it with the tool that installed it." >&2
    fi
}

# True when file $1's SHA-256 is the first field of file $2.
sha256_matches() {
    local have
    have=$( { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; } | awk '{print $1}')
    [[ -n "$have" && "$have" == "$(awk '{print $1; exit}' "$2")" ]]
}

# Everything runs from main, called on the last line: a download cut short
# defines nothing, runs nothing, and so installs nothing.
main() {

CALLER_PATH="$PATH"

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

# Ensure uv is installed, and new enough for --with-executables-from below.
if ! command -v uv &>/dev/null; then
    echo "Installing uv ${UV_VERSION}..."
    curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    UV_HAVE="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
    if [[ ! "$UV_HAVE" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
        echo "error: could not read your uv's version from 'uv --version'; this installer needs uv ${UV_VERSION} or newer." >&2
        uv_upgrade_advice
        exit 1
    elif version_lt "$UV_HAVE" "$UV_VERSION"; then
        echo "error: this installer needs uv ${UV_VERSION} or newer, and you have ${UV_HAVE}." >&2
        uv_upgrade_advice
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

# --force below replaces whatever dvc is already in uv's bin dir. One that
# isn't mintd's own (a pip --user dvc, or another uv tool's) is named below.
UV_BIN_DIR="$(uv tool dir --bin 2>/dev/null || true)"
TOOLS_DIR="$(uv tool dir 2>/dev/null || true)"
REPLACED_DVC=""
if [[ -n "$UV_BIN_DIR" && ( -e "$UV_BIN_DIR/dvc" || -L "$UV_BIN_DIR/dvc" ) ]]; then
    OLD_LINK="$(readlink "$UV_BIN_DIR/dvc" || true)"
    if [[ -z "$OLD_LINK" ]]; then
        REPLACED_DVC="a file of its own"
    elif [[ "$OLD_LINK" != "$TOOLS_DIR/mintd/bin/dvc" ]]; then
        REPLACED_DVC="a link to $OLD_LINK"
    fi
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
[[ -z "$REPLACED_DVC" ]] || echo "Replaced ${UV_BIN_DIR}/dvc, ${REPLACED_DVC}, with the dvc installed with mintd."

# The tool's own Python, wherever uv keeps its tools (UV_TOOL_DIR moves them).
TOOL_PYTHON="${TOOLS_DIR}/mintd/bin/python"
REFLINK_REFUSED=""
if [[ -f "$TOOL_PYTHON" ]]; then
    echo "Installing reflink support (halves DVC disk usage on APFS/XFS/Btrfs)..."
    # Only files whose hashes are pinned: reflink's above, cffi's and pycparser's
    # in the release's constraints. --no-config: a uv.toml or pyproject.toml in
    # the folder this runs in, or a parent, can't add an index or a setting.
    echo "$REFLINK_REQ" > "$TMP/reflink.txt"
    # uv's messages go to a file first, so a refused hash can be told from a
    # network failure (uv 0.13.9 says "Hash mismatch for ...").
    if uv pip install --no-config --python "$TOOL_PYTHON" --require-hashes \
        --constraints "$TMP/constraints.txt" --requirements "$TMP/reflink.txt" 2>"$TMP/reflink.err"; then
        cat "$TMP/reflink.err" >&2
    else
        cat "$TMP/reflink.err" >&2
        if grep -q "Hash mismatch" "$TMP/reflink.err"; then
            REFLINK_REFUSED=1
            echo "Warning: reflink was refused: a download didn't match its pinned hash, so it may have been tampered with. mintd works without it; DVC falls back to hardlink/symlink/copy." >&2
        else
            echo "Warning: reflink install failed. DVC will fall back to hardlink/symlink/copy." >&2
            echo "  This step ignores uv settings files (uv.toml, pyproject.toml), your own included. If you reach PyPI only through a mirror set in one, set UV_DEFAULT_INDEX to it and run the install line again." >&2
        fi
    fi
else
    echo "Warning: could not locate mintd tool Python — skipping reflink install." >&2
fi

# --- Post-install verification (non-fatal diagnostics) -----------------------
# Help users catch the common failure mode where a successful reinstall still
# leaves another mintd or dvc winning on PATH (pipx / pip --user / brew /
# another bin dir resolving before the uv tool bin dir), or neither on PATH yet.
# Both are looked up on the caller's own PATH.
echo ""
echo "Verifying installation..."
UV_BIN_DIR_REAL="$(cd "${UV_BIN_DIR}" 2>/dev/null && pwd -P || echo "${UV_BIN_DIR}")"
[[ -z "${UV_BIN_DIR}" ]] || echo "  uv tool bin dir: ${UV_BIN_DIR}"
PROBLEMS=""   # one "  - " line per problem, for the closing line

# Says which $1 runs and warns when it is not the one uv installed; $2 says
# what that means for $1.
check_on_path() {
    local found version
    if ! found="$(on_caller_path "$1")"; then
        echo "  Warning: '$1' is not on your PATH yet." >&2
        PROBLEMS+=$'\n'"  - '$1' is not on your PATH yet: open a new shell, or add ${UV_BIN_DIR:-the uv tool bin dir} to your PATH."
        return 0
    fi
    if version="$("$found" --version 2>/dev/null)"; then
        echo "  $1 on PATH: ${found} (${version})"
    else
        echo "  $1 on PATH: ${found}"
        echo "  Warning: '$1 --version' did not run cleanly." >&2
    fi
    # The same file is not shadowing, also when reached through another link.
    if [[ -n "${UV_BIN_DIR}" && "$(physical "${found}")" != "$(physical "${UV_BIN_DIR}/$1")" ]]; then
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
[[ -z "$REFLINK_REFUSED" ]] || PROBLEMS+=$'\n'"  - reflink was refused: a download didn't match its pinned hash, so it may have been tampered with. mintd works without it; DVC falls back to hardlink/symlink/copy."
if [[ -z "$PROBLEMS" ]]; then
    echo "mintd installed successfully (with ${WITH}, both on your PATH). Run 'mintd --help' to get started."
else
    echo "mintd installed (with ${WITH}), but:${PROBLEMS}"
fi

}

main "$@"
