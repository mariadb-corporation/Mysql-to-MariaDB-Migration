#!/usr/bin/env bash
set -euo pipefail

echo "==> Backup existing MySQL slave host"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"
REPLACE_BACKUP_CMD="${REPLACE_BACKUP_CMD:-sudo tar -czf /tmp/mysql_slave_backup_$(date +%Y%m%d_%H%M%S).tgz /var/lib/mysql /etc/mysql /etc/my.cnf 2>/dev/null || true}"

if [[ -z "$TGT_SSH_HOST" ]]; then
  echo "ERROR: TGT_SSH_HOST is required."
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 2
fi

echo "Running backup command on target host..."
ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "$REPLACE_BACKUP_CMD"

echo "Backup step completed."
