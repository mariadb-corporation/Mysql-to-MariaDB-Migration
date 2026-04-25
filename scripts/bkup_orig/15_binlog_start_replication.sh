#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: configure and start replication"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

REPL_USER="${REPL_USER:-}"
REPL_PASS="${REPL_PASS:-}"
BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-artifacts/binlog_coords.env}"
SRC_BINLOG_FILE="${SRC_BINLOG_FILE:-}"
SRC_BINLOG_POS="${SRC_BINLOG_POS:-}"
BINLOG_CREATE_REPL_USER="${BINLOG_CREATE_REPL_USER:-1}"
BINLOG_MASTER_SSL="${BINLOG_MASTER_SSL:-0}"
BINLOG_MASTER_SSL_VERIFY_SERVER_CERT="${BINLOG_MASTER_SSL_VERIFY_SERVER_CERT:-0}"
BINLOG_AUTO_FIX_SERVER_ID="${BINLOG_AUTO_FIX_SERVER_ID:-1}"
TGT_SERVER_ID="${TGT_SERVER_ID:-}"

missing=()
for v in SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_ADMIN_USER TGT_ADMIN_PASS REPL_USER REPL_PASS; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi

if [[ -z "$SRC_BINLOG_FILE" || -z "$SRC_BINLOG_POS" ]]; then
  if [[ -f "$BINLOG_COORD_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$BINLOG_COORD_FILE"
  fi
fi

echo "Checking source/target server_id..."
src_server_id="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
tgt_server_id="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
if [[ -z "$src_server_id" || -z "$tgt_server_id" ]]; then
  echo "ERROR: Could not read source/target server_id."
  exit 10
fi

if [[ "$src_server_id" == "$tgt_server_id" ]]; then
  if [[ "$BINLOG_AUTO_FIX_SERVER_ID" != "1" ]]; then
    echo "ERROR: Source and target server_id are equal ($src_server_id). Set distinct IDs before replication."
    exit 11
  fi
  new_tgt_id="$TGT_SERVER_ID"
  if [[ -z "$new_tgt_id" ]]; then
    host_hash="$(printf "%s" "$TGT_HOST" | tr -cd '0-9')"
    if [[ -z "$host_hash" ]]; then host_hash="200"; fi
    new_tgt_id="$(( (10#$host_hash % 2147483000) + 1000 ))"
    if [[ "$new_tgt_id" == "$src_server_id" ]]; then
      new_tgt_id="$((new_tgt_id + 1))"
    fi
  fi
  if ! [[ "$new_tgt_id" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Computed target server_id is invalid: $new_tgt_id"
    exit 12
  fi
  echo "Source/target server_id collision detected ($src_server_id). Setting target server_id=$new_tgt_id"
  MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
    --batch --skip-column-names -e "SET GLOBAL server_id=${new_tgt_id};"
  tgt_server_id="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
    --batch --skip-column-names -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
  if [[ "$tgt_server_id" != "$new_tgt_id" ]]; then
    echo "ERROR: Failed to apply target server_id change."
    exit 13
  fi
  echo "Target server_id updated to $tgt_server_id"
fi
if [[ -z "$SRC_BINLOG_FILE" || -z "$SRC_BINLOG_POS" ]]; then
  echo "ERROR: Missing binlog coordinates. Set SRC_BINLOG_FILE/SRC_BINLOG_POS or generate $BINLOG_COORD_FILE in seed step."
  exit 2
fi

echo "Ensuring replication user exists on source..."
if [[ "$BINLOG_CREATE_REPL_USER" == "1" ]]; then
  repl_user_esc="${REPL_USER//\'/\'\'}"
  repl_pass_esc="${REPL_PASS//\'/\'\'}"
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "CREATE USER IF NOT EXISTS '${repl_user_esc}'@'%' IDENTIFIED WITH mysql_native_password BY '${repl_pass_esc}';" || true
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "ALTER USER '${repl_user_esc}'@'%' IDENTIFIED WITH mysql_native_password BY '${repl_pass_esc}';" || true
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '${repl_user_esc}'@'%';" || true
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "FLUSH PRIVILEGES;"
fi

repl_ssl_sql=", MASTER_SSL=0, MASTER_SSL_VERIFY_SERVER_CERT=0"
if [[ "$BINLOG_MASTER_SSL" == "1" ]]; then
  repl_ssl_sql=", MASTER_SSL=1, MASTER_SSL_VERIFY_SERVER_CERT=${BINLOG_MASTER_SSL_VERIFY_SERVER_CERT}"
fi

repl_sql="CHANGE MASTER TO
  MASTER_HOST='${SRC_HOST}',
  MASTER_PORT=${SRC_PORT},
  MASTER_USER='${REPL_USER}',
  MASTER_PASSWORD='${REPL_PASS}',
  MASTER_LOG_FILE='${SRC_BINLOG_FILE}',
  MASTER_LOG_POS=${SRC_BINLOG_POS}${repl_ssl_sql};"

echo "Applying replication coordinates: ${SRC_BINLOG_FILE}:${SRC_BINLOG_POS}"
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "STOP REPLICA;" >/dev/null 2>&1 || \
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "STOP SLAVE;" >/dev/null 2>&1 || true

MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "$repl_sql"

MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "START REPLICA;" >/dev/null 2>&1 || \
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --batch --skip-column-names -e "START SLAVE;"

echo "Replication started."
