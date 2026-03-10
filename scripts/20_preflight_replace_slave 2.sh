#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (replace_slave)"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"

REPLACE_TARGET_OS="${REPLACE_TARGET_OS:-}"
REPLACE_MARIADB_VERSION="${REPLACE_MARIADB_VERSION:-}"

missing=()
for v in SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_ADMIN_USER TGT_ADMIN_PASS TGT_SSH_HOST REPLACE_TARGET_OS REPLACE_MARIADB_VERSION; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ -z "$SRC_DB" && -z "$SRC_DBS" ]]; then
  missing+=("SRC_DB_or_SRC_DBS")
fi
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi
os_check="$(printf "%s" "$REPLACE_TARGET_OS" | tr '[:upper:]' '[:lower:]' | xargs)"
case "$os_check" in
  ubuntu|debian|rocky|rhel|centos7|sles) ;;
  *)
    echo "ERROR: Unsupported REPLACE_TARGET_OS '$REPLACE_TARGET_OS'."
    echo "Supported: ubuntu|debian|rocky|rhel|centos7|sles"
    exit 8
    ;;
esac

if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: mysql client not found (MYSQL_BIN=$MYSQL_BIN)."
  exit 2
fi
if ! command -v "$MARIADB_BIN" >/dev/null 2>&1; then
  echo "ERROR: mariadb client not found (MARIADB_BIN=$MARIADB_BIN)."
  exit 3
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 4
fi

echo "Checking source admin connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null

echo "Checking source master status visibility..."
if ! MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW MASTER STATUS;" | head -n1 | grep -q .; then
  echo "ERROR: SHOW MASTER STATUS returned no rows. Ensure source is primary and admin has REPLICATION CLIENT privilege."
  exit 5
fi

echo "Checking target SSH connectivity..."
if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "echo ok" >/dev/null 2>&1; then
  echo "ERROR: Cannot connect to target SSH host: ${TGT_SSH_USER}@${TGT_SSH_HOST}"
  exit 6
fi

echo "Checking current MySQL slave status on target host..."
slave_status="$(
  MYSQL_PWD="$TGT_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
    -e "SHOW SLAVE STATUS\\G" 2>&1 || true
)"
if ! grep -Eq 'Slave_IO_Running:|Slave_SQL_Running:|Master_Host:' <<<"$slave_status"; then
  replica_status="$(
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      -e "SHOW REPLICA STATUS\\G" 2>&1 || true
  )"
  if ! grep -Eq 'Replica_IO_Running:|Replica_SQL_Running:|Source_Host:' <<<"$replica_status"; then
    echo "ERROR: Target does not appear to be a MySQL slave/replica (or status cannot be read)."
    if [[ -n "$slave_status" ]]; then
      echo "$slave_status"
    fi
    if [[ -n "$replica_status" ]]; then
      echo "$replica_status"
    fi
    if grep -q "ERROR 2002" <<<"$slave_status$replica_status"; then
      echo "Target DB port is not reachable from orchestrator (${TGT_HOST}:${TGT_PORT})."
      echo "If a previous replace attempt already stopped MySQL, resume that run or start MySQL slave before running replace_slave again."
    fi
    echo "Verify target admin privileges (REPLICATION CLIENT) and current slave setup."
    exit 7
  fi
fi

echo "Preflight complete."
