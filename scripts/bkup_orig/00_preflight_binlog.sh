#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (binlog)"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_USER:-}"
SRC_PASS="${SRC_PASS:-}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_USER:-}"
TGT_PASS="${TGT_PASS:-}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

REPL_USER="${REPL_USER:-}"
REPL_PASS="${REPL_PASS:-}"
ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"

missing=()
for v in SRC_HOST SRC_USER SRC_PASS SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_USER TGT_PASS TGT_ADMIN_USER TGT_ADMIN_PASS REPL_USER REPL_PASS; do
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

if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: mysql client not found (MYSQL_BIN=$MYSQL_BIN)."
  exit 2
fi
if ! command -v "$MARIADB_BIN" >/dev/null 2>&1; then
  echo "ERROR: mariadb client not found (MARIADB_BIN=$MARIADB_BIN)."
  exit 3
fi
if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  if command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; mysqldump is available (OK)."
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found on source host."
    exit 4
  fi
fi

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

echo "Checking source admin connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null

echo "Checking target admin connectivity..."
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
  --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null

echo "Checking source binary logging..."
log_bin_val="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW VARIABLES LIKE 'log_bin';" | awk 'NR==1 {print $2}')"
if [[ "$log_bin_val" != "ON" && "$log_bin_val" != "1" ]]; then
  echo "ERROR: Source binary log is not enabled (log_bin=$log_bin_val)."
  exit 5
fi

echo "Checking source binlog format..."
required_fmt="MIXED"
current_fmt="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW VARIABLES LIKE 'binlog_format';" | awk 'NR==1 {print toupper($2)}')"
if [[ "$current_fmt" != "$required_fmt" ]]; then
  echo "Current source binlog_format is $current_fmt. Setting it to $required_fmt..."
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "SET GLOBAL binlog_format='${required_fmt}';"
  current_fmt="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "SHOW VARIABLES LIKE 'binlog_format';" | awk 'NR==1 {print toupper($2)}')"
fi
if [[ "$current_fmt" != "$required_fmt" ]]; then
  echo "ERROR: source binlog_format is $current_fmt, expected $required_fmt."
  exit 9
fi

echo "Checking source master status visibility..."
if ! MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SHOW MASTER STATUS;" | head -n1 | grep -q .; then
  echo "ERROR: SHOW MASTER STATUS returned no rows."
  echo "Ensure source is primary and admin user has REPLICATION CLIENT privilege."
  exit 6
fi

echo "Checking source database(s) exist..."
if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi
missing_src=()
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  db_esc="$(sql_escape "$db")"
  q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
  out="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "$q")"
  if [[ "${out:-0}" -eq 0 ]]; then
    missing_src+=("$db")
  fi
done
if [[ "${#missing_src[@]}" -gt 0 ]]; then
  echo "ERROR: Source DB does not exist: ${missing_src[*]}"
  exit 7
fi

if [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
  existing=()
  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    db_esc="$(sql_escape "$db")"
    q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
    out="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "$q")"
    if [[ "${out:-0}" -gt 0 ]]; then
      existing+=("$db")
    fi
  done
  if [[ "${#existing[@]}" -gt 0 ]]; then
    echo "ERROR: Target DB already exists: ${existing[*]}"
    echo "Set ALLOW_TARGET_DB_OVERWRITE=1 only if overwrite is intended."
    exit 8
  fi
fi

echo "Preflight complete."
