#!/usr/bin/env bash
#
# 00_check_python_deps.sh
# Ensures the orchestrator's Python dependencies are available before launch.
#
# Behaviour:
#   - If a virtualenv is already active (VIRTUAL_ENV set), install into it and
#     leave it alone otherwise.
#   - If no venv is active, create (when absent) and use a project-local .venv,
#     so the host's system Python is never touched. This sidesteps PEP 668
#     "externally-managed-environment" errors entirely and needs no root.
#
# NOTE ON ACTIVATION: a child process cannot mutate its parent's environment,
# so this script does NOT (and cannot usefully) activate the venv for the
# launcher. It only guarantees the venv EXISTS and is POPULATED. The launcher
# (mariadb-migrator) activates ./.venv after this script returns. Keep the two
# in sync.
#
# Opt out of auto-creation with MIGRATOR_NO_AUTO_VENV=1 (the script will then
# print manual steps and exit non-zero instead of creating a venv).

set -uo pipefail

REQ_FILE="orchestrator/requirements.txt"
VENV_DIR=".venv"

check_environment() {
    echo "==> Validating execution environment..."

    # 1. Python 3 must exist to build the venv and run the orchestrator.
    if ! command -v python3 >/dev/null 2>&1; then
        echo "[ERROR] Python 3 is not installed. Please install Python 3.9+."
        exit 1
    fi

    # 2. The requirements manifest must ship with the tool.
    if [[ ! -f "$REQ_FILE" ]]; then
        echo "[ERROR] Migration tool is incomplete: $REQ_FILE is missing."
        exit 1
    fi

    # 3. Decide which interpreter we install into / check against.
    local pybin
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        # Operator already activated a venv -- respect it, install there.
        pybin="${VIRTUAL_ENV}/bin/python"
        echo "==> Using active virtual environment: $VIRTUAL_ENV"
    elif [[ -d "$VENV_DIR" ]]; then
        echo "==> Using existing project virtual environment: ./$VENV_DIR"
        pybin="$VENV_DIR/bin/python"
    else
        if [[ "${MIGRATOR_NO_AUTO_VENV:-0}" == "1" ]]; then
            echo "[ERROR] No active virtualenv and auto-creation is disabled"
            echo "        (MIGRATOR_NO_AUTO_VENV=1). Create one manually:"
            echo ""
            echo "            python3 -m venv $VENV_DIR"
            echo "            source $VENV_DIR/bin/activate"
            echo "            pip install -r $REQ_FILE"
            echo ""
            exit 1
        fi
        echo "==> No active virtualenv detected. Creating one at ./$VENV_DIR ..."
        if ! python3 -m venv "$VENV_DIR"; then
            echo "[ERROR] Failed to create virtual environment at ./$VENV_DIR."
            echo "        On Debian/Ubuntu the venv module ships separately:"
            echo "            sudo apt-get install -y python3-venv"
            exit 1
        fi
        pybin="$VENV_DIR/bin/python"
    fi

    # 4. Check installed vs. required using THAT interpreter, so the result
    #    reflects the environment the orchestrator will actually run in.
    #    The requirements path is passed as argv (not interpolated into the
    #    Python source), and the heredoc is quoted so the shell leaves it alone.
    local missing rc
    missing="$("$pybin" - "$REQ_FILE" <<'PY'
import sys
req = sys.argv[1]
try:
    from importlib.metadata import version
    def check(p): version(p)
except ImportError:
    import pkg_resources
    def check(p): pkg_resources.require(p)

missing = []
with open(req) as f:
    for line in f:
        name = line.split('>=')[0].split('==')[0].strip()
        if not name or name.startswith('#'):
            continue
        try:
            check(name)
        except Exception:
            missing.append(name)
if missing:
    print(' '.join(missing))
    sys.exit(1)
PY
)"
    rc=$?

    # 5. Install if anything is missing, and STOP if the install fails
    #    (the old script printed "Environment ready" regardless, which let the
    #    orchestrator start and then crash with an import error).
    if [[ $rc -ne 0 ]]; then
        if [[ -z "$missing" ]]; then
            echo "[ERROR] Could not verify Python dependencies (interpreter: $pybin)."
            echo "        Re-run with the heredoc 2>/dev/null removed to see the error."
            exit 1
        fi
        echo "--------------------------------------------------------"
        echo "Installing missing dependencies: $missing"
        echo "Target environment: $pybin"
        echo "--------------------------------------------------------"
        if ! "$pybin" -m pip install -r "$REQ_FILE"; then
            echo "[ERROR] Dependency installation failed. See pip output above."
            exit 1
        fi
    fi

    echo "==> Environment ready."
}

check_environment
