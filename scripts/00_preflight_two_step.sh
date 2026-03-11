#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (two_step)"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"
ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"

SQLINESDATA_BIN="${SQLINESDATA_BIN:-}"

missing=()
for v in SRC_HOST SRC_USER SRC_PASS SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_USER TGT_PASS TGT_ADMIN_USER TGT_ADMIN_PASS; do
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

if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  if command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; mysqldump is available (OK)."
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found on source host."
    exit 3
  fi
fi

echo "Checking source connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --connect-timeout=5 --batch --skip-column-names \
  -e "SELECT 1;" >/dev/null

echo "Checking source migration user connectivity..."
if ! MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" \
  --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null 2>&1; then
  echo "ERROR: Source migration user login failed: ${SRC_USER}@${SRC_HOST}:${SRC_PORT}"
  echo "two_step mode does not auto-create migration users."
  echo "Ensure source admin credentials are correct and have required privileges."
  exit 7
fi

echo "Checking target migration user connectivity..."
if [[ -n "$TGT_SSH_HOST" ]]; then
  if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
    "MYSQL_PWD='${TGT_PASS}' mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --connect-timeout=5 -e 'SELECT 1;' >/dev/null 2>&1"; then
    echo "ERROR: Target migration user login failed: ${TGT_USER}@${TGT_HOST}:${TGT_PORT}"
    echo "two_step mode does not auto-create migration users."
    echo "Ensure target admin credentials are correct and have required privileges."
    exit 8
  fi
else
  if ! MYSQL_PWD="$TGT_PASS" mariadb -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" \
    --connect-timeout=5 -e "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: Target migration user login failed: ${TGT_USER}@${TGT_HOST}:${TGT_PORT}"
    echo "two_step mode does not auto-create migration users."
    echo "Ensure target admin credentials are correct and have required privileges."
    exit 8
  fi
fi

if [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
  sql_escape() {
    local s="$1"
    s="${s//\'/\'\'}"
    printf "%s" "$s"
  }
  target_db_exists() {
    local db="$1"
    local db_esc
    db_esc="$(sql_escape "$db")"
    local q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
    local out=""
    if [[ -n "$TGT_SSH_HOST" ]]; then
      local tgt_pass_q
      tgt_pass_q="$(printf '%q' "$TGT_ADMIN_PASS")"
      out="$(ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
        "MYSQL_PWD=$tgt_pass_q mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e \"$q\"")"
    else
      out="$(MYSQL_PWD="$TGT_ADMIN_PASS" mariadb -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
        --batch --skip-column-names -e "$q")"
    fi
    [[ "${out:-0}" -gt 0 ]]
  }

  existing=()
  if [[ -n "$SRC_DBS" ]]; then
    IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
  else
    DB_LIST=("$SRC_DB")
  fi
  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    if target_db_exists "$db"; then
      existing+=("$db")
    fi
  done
  if [[ "${#existing[@]}" -gt 0 ]]; then
    echo "ERROR: Target DB already exists: ${existing[*]}"
    echo "Set ALLOW_TARGET_DB_OVERWRITE=1 only if overwrite is intended."
    exit 6
  fi
fi

if [[ -n "$SQLINESDATA_BIN" ]]; then
  if [[ ! -x "$SQLINESDATA_BIN" ]]; then
    echo "ERROR: SQLINESDATA_BIN not executable: $SQLINESDATA_BIN"
    exit 4
  fi
else
  if command -v sqldata >/dev/null 2>&1; then
    echo "sqldata found in PATH (OK)."
  else
    echo "ERROR: SQLINESDATA_BIN not set and sqldata not found in PATH."
    exit 5
  fi
fi

echo "Preflight complete."
