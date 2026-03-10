#!/usr/bin/env bash
set -euo pipefail

echo "==> Optional cleanup of old MySQL data on replaced slave host"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"
REPLACE_DELETE_OLD_MYSQL_DATA="${REPLACE_DELETE_OLD_MYSQL_DATA:-0}"
REPLACE_CLEANUP_CMD="${REPLACE_CLEANUP_CMD:-}"

if [[ "$REPLACE_DELETE_OLD_MYSQL_DATA" != "1" ]]; then
  echo "Cleanup not requested (REPLACE_DELETE_OLD_MYSQL_DATA != 1); skipping."
  exit 0
fi

if [[ -z "$TGT_SSH_HOST" ]]; then
  echo "ERROR: TGT_SSH_HOST is required for cleanup."
  exit 1
fi
if [[ -z "$REPLACE_CLEANUP_CMD" ]]; then
  echo "ERROR: REPLACE_CLEANUP_CMD is required when REPLACE_DELETE_OLD_MYSQL_DATA=1."
  exit 2
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 3
fi

echo "Running cleanup command on target host..."
ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "$REPLACE_CLEANUP_CMD"

echo "Cleanup step completed."
