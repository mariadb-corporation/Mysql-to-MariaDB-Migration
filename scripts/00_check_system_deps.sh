#!/usr/bin/env bash
#
# 00_check_system_deps.sh
# Detects required system binaries and offers to install the ones that live in
# OS package repositories.
#
# Unlike the Python deps (installed into a local .venv, no root), system
# binaries need sudo and are platform-specific, so installs are ALWAYS opt-in:
# the script detects the platform, builds the correct command, SHOWS it, then
# asks before running. If you decline, the printed command is your manual step.
#
# Scope:
#   - mariadb / mysql client : REQUIRED for all live modes.  Assisted install.
#   - pv (pipe viewer)       : OPTIONAL dump progress meter.  Assisted, non-blocking.
#   - sqldata                : REQUIRED for two_step only, NOT in any repo ->
#                              left to its point-of-use gate in mariadb-migrator
#                              (download + SQLINESDATA_BIN), since it can't be
#                              installed via a package manager.
#
# Env flags:
#   MIGRATOR_ASSUME_YES=1         accept install prompts automatically (CI).
#   MIGRATOR_NO_SYSTEM_INSTALL=1  never run installs; print the commands only.

set -uo pipefail

# Filled in by detect_platform().
PLATFORM_LABEL=""
INSTALL_CLIENT=""
INSTALL_PV=""
USES_SUDO="no"

detect_platform() {
    if [[ "$(uname -s 2>/dev/null || echo unknown)" == "Darwin" ]]; then
        PLATFORM_LABEL="macOS (Homebrew)"
        INSTALL_CLIENT="brew install mariadb"
        INSTALL_PV="brew install pv"
        USES_SUDO="no"
        return 0
    fi

    if command -v dnf >/dev/null 2>&1; then
        PLATFORM_LABEL="RHEL/Fedora family (dnf)"
        INSTALL_CLIENT="sudo dnf install -y mariadb"
        INSTALL_PV="sudo dnf install -y pv"
        USES_SUDO="yes"
    elif command -v yum >/dev/null 2>&1; then
        PLATFORM_LABEL="RHEL family (yum)"
        INSTALL_CLIENT="sudo yum install -y mariadb"
        INSTALL_PV="sudo yum install -y pv"
        USES_SUDO="yes"
    elif command -v apt-get >/dev/null 2>&1; then
        PLATFORM_LABEL="Debian/Ubuntu (apt)"
        INSTALL_CLIENT="sudo apt-get update && sudo apt-get install -y mariadb-client"
        INSTALL_PV="sudo apt-get install -y pv"
        USES_SUDO="yes"
    elif command -v zypper >/dev/null 2>&1; then
        PLATFORM_LABEL="SLES/openSUSE (zypper)"
        INSTALL_CLIENT="sudo zypper install -y mariadb-client"
        INSTALL_PV="sudo zypper install -y pv"
        USES_SUDO="yes"
    elif command -v pacman >/dev/null 2>&1; then
        PLATFORM_LABEL="Arch (pacman)"
        INSTALL_CLIENT="sudo pacman -S --noconfirm mariadb-clients"
        INSTALL_PV="sudo pacman -S --noconfirm pv"
        USES_SUDO="yes"
    else
        PLATFORM_LABEL="unknown"
    fi
    return 0
}

have_client() { command -v mariadb >/dev/null 2>&1 || command -v mysql >/dev/null 2>&1; }

# offer_install <human-name> <install-command> <required:yes|no>
# Shows the command, prompts, runs it on accept. Returns non-zero only when a
# REQUIRED dependency is left unmet.
offer_install() {
    local name="$1" cmd="$2" required="$3"

    if [[ -z "$cmd" ]]; then
        echo "  [ERROR] No install command known for '$name' on this platform"
        echo "          ($PLATFORM_LABEL). Install it manually, then re-run."
        echo "          MariaDB client downloads: https://mariadb.com/downloads/"
        [[ "$required" == "yes" ]] && return 1 || return 0
    fi

    echo ""
    echo "  Install command for $PLATFORM_LABEL:"
    echo "      $cmd"
    [[ "$USES_SUDO" == "yes" ]] && echo "  (uses sudo; you may be prompted for your password)"
    echo ""

    if [[ "${MIGRATOR_NO_SYSTEM_INSTALL:-0}" == "1" ]]; then
        echo "  Auto-install disabled (MIGRATOR_NO_SYSTEM_INSTALL=1). Run the command above manually."
        [[ "$required" == "yes" ]] && return 1 || return 0
    fi

    local reply="n"
    if [[ "${MIGRATOR_ASSUME_YES:-0}" == "1" ]]; then
        reply="y"
    elif [[ -t 0 ]]; then
        read -rp "  Run this command now? [y/N] " reply
    else
        echo "  Non-interactive shell; not running automatically. Run the command above, then re-run."
        [[ "$required" == "yes" ]] && return 1 || return 0
    fi

    if [[ "$reply" =~ ^[Yy]$ ]]; then
        echo "  ==> Installing $name ..."
        if ! bash -c "$cmd"; then
            echo "  [ERROR] Install command failed (see output above)."
            [[ "$required" == "yes" ]] && return 1 || return 0
        fi
    else
        echo "  Skipped. Run the command above when ready."
        [[ "$required" == "yes" ]] && return 1 || return 0
    fi
    return 0
}

check_system_deps() {
    echo "==> Checking system dependencies..."
    detect_platform
    echo "==> Platform: $PLATFORM_LABEL"

    # 1. DB client -- required for every live mode.
    if have_client; then
        echo "==> Database client found: $(command -v mariadb 2>/dev/null || command -v mysql)"
    else
        echo "[MISSING] No 'mariadb' or 'mysql' client on PATH (needed to test"
        echo "          connectivity and run the schema/data steps)."
        offer_install "MariaDB client" "$INSTALL_CLIENT" "yes" || return 1
        if ! have_client; then
            echo "[ERROR] Client still not on PATH after install. Open a fresh shell"
            echo "        (to refresh PATH) or install manually, then re-run."
            return 1
        fi
        echo "==> Database client now available: $(command -v mariadb 2>/dev/null || command -v mysql)"
    fi

    # 2. pv -- optional progress meter for the dump pipeline. Never blocks.
    if command -v pv >/dev/null 2>&1; then
        echo "==> pv found (dump progress will be shown)."
    else
        echo "==> pv not installed (optional). Dump phases will run without a progress meter."
        offer_install "pv" "$INSTALL_PV" "no" || true
    fi

    echo "==> System dependencies ready."
    return 0
}

check_system_deps
