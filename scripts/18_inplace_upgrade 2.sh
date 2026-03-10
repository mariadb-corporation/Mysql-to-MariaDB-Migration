#!/usr/bin/env bash
set -euo pipefail

echo "==> In-place upgrade: execute"

INPLACE_EXECUTE="${INPLACE_EXECUTE:-0}"
INPLACE_STOP_CMD="${INPLACE_STOP_CMD:-sudo systemctl stop mysql || sudo systemctl stop mysqld}"
INPLACE_INSTALL_CMD="${INPLACE_INSTALL_CMD:-}"
INPLACE_START_CMD="${INPLACE_START_CMD:-sudo systemctl start mariadb || sudo systemctl start mysql}"
INPLACE_UPGRADE_CMD="${INPLACE_UPGRADE_CMD:-mariadb-upgrade}"

echo "Planned stop command: $INPLACE_STOP_CMD"
echo "Planned install command: ${INPLACE_INSTALL_CMD:-<not set>}"
echo "Planned start command: $INPLACE_START_CMD"
echo "Planned upgrade command: $INPLACE_UPGRADE_CMD"

if [[ "$INPLACE_EXECUTE" != "1" ]]; then
  echo "INPLACE_EXECUTE is not 1. Dry-run only."
  echo "Set INPLACE_EXECUTE=1 to execute the in-place commands."
  exit 0
fi

if [[ -z "$INPLACE_INSTALL_CMD" ]]; then
  echo "ERROR: INPLACE_INSTALL_CMD is required when INPLACE_EXECUTE=1"
  exit 2
fi

echo "Stopping MySQL service..."
bash -lc "$INPLACE_STOP_CMD"

echo "Installing MariaDB..."
bash -lc "$INPLACE_INSTALL_CMD"

echo "Starting MariaDB service..."
bash -lc "$INPLACE_START_CMD"

echo "Running upgrade command..."
bash -lc "$INPLACE_UPGRADE_CMD"

echo "In-place execution completed."
