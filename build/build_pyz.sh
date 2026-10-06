#!/usr/bin/env bash
# Build a universal (arch- and ABI-independent) zipapp of the orchestrator.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- repo-specific knobs -----------------------------------------------------
ORCH_DIR="orchestrator"          # python sources
PHASE_DIR="${PHASE_DIR:-scripts}" # numbered bash phase scripts
SQL_DIR="${SQL_DIR:-sql}"        # check SQL consumed by phase scripts
APP_NAME="mariadb-migrator"
VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo 0.0.0-dev)}"
VERSION="${VERSION#v}"
# -----------------------------------------------------------------------------

BUILD_DIR="build/pyz"
DIST_DIR="dist"
OUT="${DIST_DIR}/${APP_NAME}-${VERSION}.pyz"

# 3.9 is the floor; building on a newer interpreter can emit 3.10+ bytecode
# assumptions into the vendored tree.
py_minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
if [[ "$py_minor" -ne 9 ]]; then
    echo "build requires python3.9 (found 3.${py_minor})" >&2
    exit 1
fi

[[ -d "$ORCH_DIR" ]] || { echo "missing $ORCH_DIR" >&2; exit 1; }
[[ -d "$PHASE_DIR" ]] || { echo "missing $PHASE_DIR (set PHASE_DIR=)" >&2; exit 1; }
[[ -d "$SQL_DIR" ]] || { echo "missing $SQL_DIR (set SQL_DIR=)" >&2; exit 1; }
[[ -f "${ORCH_DIR}/step_map.yaml" ]] || { echo "missing step_map.yaml" >&2; exit 1; }

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$DIST_DIR"

# Dependencies. PYYAML_FORCE_LIBYAML=0 + --no-binary=PyYAML keeps the C
# accelerator out, which is what makes the artifact arch-independent.
PYYAML_FORCE_LIBYAML=0 python3 -m pip install \
    --no-binary=PyYAML \
    --no-compile \
    --target "$BUILD_DIR" \
    -r "${ORCH_DIR}/requirements.lock"

# Orchestrator sources, kept as a package so relative imports resolve.
mkdir -p "${BUILD_DIR}/${ORCH_DIR}"
cp "${ORCH_DIR}"/*.py "${BUILD_DIR}/${ORCH_DIR}/"
[[ -f "${BUILD_DIR}/${ORCH_DIR}/__init__.py" ]] || : > "${BUILD_DIR}/${ORCH_DIR}/__init__.py"

# Phase scripts travel as payload; they are extracted at runtime, never
# exec'd from inside the zip.
mkdir -p "${BUILD_DIR}/_payload/orchestrator"
cp -R "${PHASE_DIR}" "${BUILD_DIR}/_payload/"
cp -R "${SQL_DIR}" "${BUILD_DIR}/_payload/"
cp "${ORCH_DIR}/step_map.yaml" "${BUILD_DIR}/_payload/orchestrator/"

# Entry point.
cp build/pyz_main.py "${BUILD_DIR}/__main__.py"

# Stamp the version so the runtime shim knows which share dir to use.
printf '%s\n' "$VERSION" > "${BUILD_DIR}/_version.txt"

# Drop build noise.
find "$BUILD_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUILD_DIR" -name '*.dist-info' -type d -prune -exec rm -rf {} +

# The guard that actually matters. A native extension cannot be imported from
# a zip, and would also pin the artifact to one arch + CPython ABI.
if find "$BUILD_DIR" -name '*.so' -print -quit | grep -q .; then
    echo "native extension found in bundle:" >&2
    find "$BUILD_DIR" -name '*.so' >&2
    exit 1
fi

python3 -m zipapp "$BUILD_DIR" \
    --output "$OUT" \
    --python '/usr/bin/env python3' \
    --compress

chmod +x "$OUT"
echo "built $OUT"
