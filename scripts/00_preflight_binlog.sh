#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (binlog)"

# Prefer the MariaDB client; fall back to mysql. Explicit MYSQL_BIN wins.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"
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

# Connection args (no --protocol=TCP; --ssl-verify-server-cert=OFF for mariadb client).
src_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" --connect-timeout=10 --batch --skip-column-names )
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" --connect-timeout=10 --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

echo "Checking source admin connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -e "SELECT 1;" >/dev/null

echo "Checking target admin connectivity..."
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -e "SELECT 1;" >/dev/null

# Detect source MySQL version.
src_version_full="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -e "SELECT VERSION();" | head -1)"
src_version_num="$(printf "%s" "$src_version_full" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')"
src_major="${src_version_num%%.*}"
src_minor="${src_version_num#*.}"
src_minor="${src_minor%%.*}"

# Pick binlog-status SQL: SHOW MASTER STATUS for < 8.4, SHOW BINARY LOG STATUS for >= 8.4.
SHOW_BINLOG_STATUS_SQL="SHOW MASTER STATUS;"
if [[ "$src_major" -gt 8 ]] || { [[ "$src_major" -eq 8 ]] && [[ "$src_minor" -ge 4 ]]; }; then
  SHOW_BINLOG_STATUS_SQL="SHOW BINARY LOG STATUS;"
fi
echo "Source MySQL: $src_version_full (using: $SHOW_BINLOG_STATUS_SQL)"
export SHOW_BINLOG_STATUS_SQL

# Version gate: replication-based migration requires an 8.0+ source.
# MySQL < 8.0 (5.7, 5.6, ...) cannot be a reliable replication source for
# MariaDB (binlog/GTID divergence), so block here — categorically, before any
# schema-specific check (JSON, binlog_format) — rather than failing later in
# binlog setup. Numeric major-version comparison, not a 5.7 string match, so
# any pre-8.0 flavor is caught.
if [[ "$src_major" -lt 8 ]]; then
  echo "ERROR: Replication mode is not supported from MySQL ${src_version_num} sources."
  echo "Replication-based migration requires a MySQL 8.0+ source."
  echo "For a MySQL ${src_version_num} source, use one of the offline migration modes:"
  echo "  - Serial Streaming Copy"
  echo "  - Parallel Restartable Streaming Copy"
  echo "  - Offline Copy"
  exit 11   
fi
export SHOW_BINLOG_STATUS_SQL

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
  out="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -e "$q")"
  if [[ "${out:-0}" -eq 0 ]]; then
    missing_src+=("$db")
  fi
done
if [[ "${#missing_src[@]}" -gt 0 ]]; then
  echo "ERROR: Source DB does not exist: ${missing_src[*]}"
  exit 7
fi

# JSON columns are not supported for replication-based migration. The canonical
# detection query lives in sql/checks/json_columns.sql (also used by the
# assessment precheck); we reuse it here and filter to the selected schemas.
echo "Checking source schemas for JSON columns..."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JSON_COLUMNS_SQL="${REPO_ROOT}/sql/checks/json_columns.sql"
if [[ ! -r "$JSON_COLUMNS_SQL" ]]; then
  echo "ERROR: cannot read $JSON_COLUMNS_SQL"
  exit 10
fi

selected_schemas=""
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  selected_schemas+="${db}"$'\n'
done

json_hits="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
    < "$JSON_COLUMNS_SQL" \
  | awk -v sel="$selected_schemas" '
      BEGIN {
        FS = "\t"
        n = split(sel, a, "\n")
        for (i = 1; i <= n; i++) if (a[i] != "") s[a[i]] = 1
      }
      $1 in s { printf "  %s.%s.%s\n", $1, $2, $3 }
    ')"

if [[ -n "$json_hits" ]]; then
  echo ""
  echo "ERROR: Replication mode is not compatible with JSON columns in the source schema."
  echo ""
  echo "Detected JSON columns:"
  echo "$json_hits"
  echo ""
  echo "JSON column types are not supported for online replication-based migration."
  echo "Please use one of the offline migration modes:"
  echo ""
  echo "  - Serial Streaming Copy"
  echo "  - Parallel Streaming Copy"
  echo "  - Offline Copy"
  echo ""
  exit 10
fi

echo "Checking source binary logging..."
log_bin_val="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
  -e "SHOW VARIABLES LIKE 'log_bin';" | awk 'NR==1 {print $2}')"
if [[ "$log_bin_val" != "ON" && "$log_bin_val" != "1" ]]; then
  echo "ERROR: Source binary log is not enabled (log_bin=$log_bin_val)."
  exit 5
fi

echo "Checking source binlog format..."
required_fmt="ROW"
current_fmt="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
  -e "SHOW VARIABLES LIKE 'binlog_format';" | awk 'NR==1 {print toupper($2)}')"
if [[ "$current_fmt" != "$required_fmt" ]]; then
  echo ""
  echo "ERROR: Source binlog_format is '$current_fmt'. Replication mode requires 'ROW'."
  echo ""
  echo "To remediate, set the following in the source MySQL configuration"
  echo "(e.g. /etc/my.cnf or /etc/mysql/my.cnf) and restart the source server:"
  echo ""
  echo "  [mysqld]"
  echo "  binlog_format = ROW"
  echo ""
  exit 9
fi

echo "Checking source master/binary-log status visibility..."
if ! MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
  -e "$SHOW_BINLOG_STATUS_SQL" | head -n1 | grep -q .; then
  echo "ERROR: $SHOW_BINLOG_STATUS_SQL returned no rows."
  echo "Ensure source is primary and admin user has REPLICATION CLIENT (or BINLOG_ADMIN on 8.4+) privilege."
  exit 6
fi

if [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
  existing=()
  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    db_esc="$(sql_escape "$db")"
    q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
    out="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -e "$q")"
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
