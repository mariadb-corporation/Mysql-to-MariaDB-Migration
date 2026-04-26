#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: seed target from source snapshot"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"

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

ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"
BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-artifacts/binlog_coords.env}"

if [[ -z "$SRC_HOST" || ( -z "$SRC_USER" && -z "$SRC_ADMIN_USER" ) || ( -z "$SRC_PASS" && -z "$SRC_ADMIN_PASS" ) || ( -z "$SRC_DB" && -z "$SRC_DBS" ) ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_USER/SRC_ADMIN_USER, SRC_PASS/SRC_ADMIN_PASS, and SRC_DB or SRC_DBS."
  exit 1
fi
if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" || -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_USER, TGT_PASS, TGT_ADMIN_USER, TGT_ADMIN_PASS."
  exit 1
fi

if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  if command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; using mysqldump."
    MARIADB_DUMP_BIN="mysqldump"
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found."
    exit 2
  fi
fi

if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi

mkdir -p "$(dirname "$BINLOG_COORD_FILE")"
DUMP_FILE="$(dirname "$BINLOG_COORD_FILE")/binlog_seed_$(date +%Y%m%d_%H%M%S).sql"

# Connection arg arrays — same rationale as 00_preflight_binlog.sh.
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi
src_admin_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" --batch --skip-column-names )

echo "Preparing target database(s)..."
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  if [[ "$ALLOW_TARGET_DB_OVERWRITE" == "1" ]]; then
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
      -e "DROP DATABASE IF EXISTS \`${db}\`; CREATE DATABASE \`${db}\`;"
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
      -e "CREATE DATABASE IF NOT EXISTS \`${db}\`;"
  fi
done

SRC_DUMP_USER="${SRC_ADMIN_USER:-$SRC_USER}"
SRC_DUMP_PASS="${SRC_ADMIN_PASS:-$SRC_PASS}"
TGT_RESTORE_USER="${TGT_ADMIN_USER:-$TGT_USER}"
TGT_RESTORE_PASS="${TGT_ADMIN_PASS:-$TGT_PASS}"

# If preflight didn't set DUMP_DATA_OPT, decide it here. The right flag
# depends on the *dump binary*, not the source server version:
#   mariadb-dump (any) → --master-data=2 (works; mariadb-dump doesn't accept --source-data)
#   mysqldump >= 8.4   → --source-data=2 (--master-data was removed in 8.4)
#   mysqldump  < 8.4   → --master-data=2
# Note: even if 00_preflight_binlog.sh exported DUMP_DATA_OPT based on the
# source server version, that's wrong when the local dump binary is
# mariadb-dump talking to a MySQL 8.4 source — mariadb-dump still uses
# --master-data. So always re-derive from the dump binary here.
dump_basename="$(basename "$MARIADB_DUMP_BIN")"
DUMP_DATA_OPT="--master-data=2"
if [[ "$dump_basename" == "mysqldump" ]]; then
  # Detect mysqldump's own version. Output looks like:
  #   mysqldump  Ver 8.4.0 for Linux on x86_64 (MySQL Community Server - GPL)
  dump_ver_line="$("$MARIADB_DUMP_BIN" --version 2>/dev/null || true)"
  dump_ver_num="$(printf "%s" "$dump_ver_line" | sed -nE 's/.*Ver +([0-9]+\.[0-9]+).*/\1/p' | head -1)"
  if [[ -z "$dump_ver_num" ]]; then
    # Fallback: some builds print "Distrib 10.x" or similar; pick first X.Y
    dump_ver_num="$(printf "%s" "$dump_ver_line" | grep -oE '[0-9]+\.[0-9]+' | head -1)"
  fi
  d_major="${dump_ver_num%%.*}"
  d_minor="${dump_ver_num#*.}"; d_minor="${d_minor%%.*}"
  if [[ "${d_major:-0}" -gt 8 ]] || { [[ "${d_major:-0}" -eq 8 ]] && [[ "${d_minor:-0}" -ge 4 ]]; }; then
    DUMP_DATA_OPT="--source-data=2"
  fi
fi
echo "Dump tool: $dump_basename (using $DUMP_DATA_OPT)"

echo "Creating source snapshot with binlog coordinates ($DUMP_DATA_OPT)..."
DUMP_ARGS=(
  --single-transaction
  "$DUMP_DATA_OPT"
  --routines --triggers --events
  --hex-blob
  --skip-lock-tables
  --databases
)
if [[ "$MARIADB_DUMP_BIN" == "mysqldump" ]]; then
  DUMP_ARGS+=(--set-gtid-purged=OFF)
else
  DUMP_ARGS+=(--gtid=0)
fi
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -n "$db" ]] && DUMP_ARGS+=("$db")
done

# mariadb-dump itself doesn't support --ssl-verify-server-cert as a flag
# in older versions, but recent builds do; we don't pass --protocol=TCP
# for the same TLS-strictness reason as the client.
MYSQL_PWD="$SRC_DUMP_PASS" "$MARIADB_DUMP_BIN" \
  -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_DUMP_USER" \
  "${DUMP_ARGS[@]}" > "$DUMP_FILE"

# mysqldump emits one of:
#   CHANGE MASTER TO MASTER_LOG_FILE='...', MASTER_LOG_POS=...   (< 8.4)
#   CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='...', SOURCE_LOG_POS=...  (>= 8.4)
# Detect either shape.
coord_line="$(grep -m1 -E "(MASTER_LOG_FILE|SOURCE_LOG_FILE)='[^']+', *(MASTER_LOG_POS|SOURCE_LOG_POS)=[0-9]+" "$DUMP_FILE" || true)"
if [[ -z "$coord_line" ]]; then
  echo "ERROR: Unable to extract binlog coordinates from dump file."
  echo "       Looked for MASTER_LOG_FILE/POS (mysqldump < 8.4) and SOURCE_LOG_FILE/POS (>= 8.4)."
  exit 3
fi

src_file="$(printf "%s" "$coord_line" | sed -E "s/.*(MASTER_LOG_FILE|SOURCE_LOG_FILE)='([^']+)'.*/\2/")"
src_pos="$(printf "%s" "$coord_line"  | sed -E "s/.*(MASTER_LOG_POS|SOURCE_LOG_POS)=([0-9]+).*/\2/")"

cat > "$BINLOG_COORD_FILE" <<COORDS
SRC_BINLOG_FILE=${src_file}
SRC_BINLOG_POS=${src_pos}
COORDS

echo "Restoring snapshot to target..."
restore_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_RESTORE_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  restore_args+=( --ssl-verify-server-cert=OFF )
fi
MYSQL_PWD="$TGT_RESTORE_PASS" "$MARIADB_BIN" "${restore_args[@]}" < "$DUMP_FILE"

echo "Seed completed."
echo "Coordinates file: $BINLOG_COORD_FILE"
echo "Dump file: $DUMP_FILE"
