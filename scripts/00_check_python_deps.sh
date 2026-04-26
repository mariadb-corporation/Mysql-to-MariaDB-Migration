#!/usr/bin/env bash

check_environment() {
    echo "==> Validating execution environment..."

    # 1. Check for Python 3
    if ! command -v python3 &> /dev/null; then
        echo "[ERROR] Python 3 is not installed. Please install Python 3.9+."
        exit 1
    fi

    # 2. Check for Requirements File
    REQ_FILE="orchestrator/requirements.txt"
    if [ ! -f "$REQ_FILE" ]; then
        echo "[ERROR] Migration tool is incomplete: $REQ_FILE is missing."
        exit 1
    fi

    # 3. Check Dependencies using a Python one-liner
    # We use importlib.metadata (Python 3.8+) or pkg_resources as fallback
    MISSING_PKGS=$(python3 -c "
import sys
try:
    from importlib.metadata import version, PackageNotFoundError
    def check(p): version(p)
except ImportError:
    import pkg_resources
    def check(p): pkg_resources.require(p)

missing = []
with open('$REQ_FILE', 'r') as f:
    for line in f:
        pkg = line.split('>=')[0].split('==')[0].strip()
        if not pkg or pkg.startswith('#'): continue
        try:
            check(pkg)
        except Exception:
            missing.append(pkg)
if missing:
    print(' '.join(missing))
    sys.exit(1)
" 2>/dev/null)

    if [ $? -ne 0 ]; then
        echo "--------------------------------------------------------"
        echo "MISSING DEPENDENCIES: $MISSING_PKGS"
        echo "To install, run: pip install -r $REQ_FILE"
        echo "--------------------------------------------------------"

        # Optional: Ask user if they want to install automatically
        read -p "Would you like to install them now? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            python3 -m pip install -r "$REQ_FILE"
        else
            echo ""
            echo "==> Manual installation required to proceed."
            echo "    Run the following command:"
            echo "    pip install -r $(realpath "$REQ_FILE")"
            echo ""
            echo "Exiting program."
            exit 1 # Ensures the script stops here
        fi
    fi
    echo "==> Environment ready."
}

# Run the check before anything else
check_environment

# Launch the orchestrator
#python3 orchestrator/migrationctl.py "$@"
