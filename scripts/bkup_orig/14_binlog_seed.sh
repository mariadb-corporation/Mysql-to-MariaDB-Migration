#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: seed target from source snapshot"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

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

echo "Preparing target database(s)..."
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  if [[ "$ALLOW_TARGET_DB_OVERWRITE" == "1" ]]; then
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "DROP DATABASE IF EXISTS \`${db}\`; CREATE DATABASE \`${db}\`;"
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "CREATE DATABASE IF NOT EXISTS \`${db}\`;"
  fi
done

SRC_DUMP_USER="${SRC_ADMIN_USER:-$SRC_USER}"
SRC_DUMP_PASS="${SRC_ADMIN_PASS:-$SRC_PASS}"
TGT_RESTORE_USER="${TGT_ADMIN_USER:-$TGT_USER}"
TGT_RESTORE_PASS="${TGT_ADMIN_PASS:-$TGT_PASS}"

echo "Creating source snapshot with binlog coordinates..."
DUMP_ARGS=(
  --single-transaction
  --master-data=2
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

MYSQL_PWD="$SRC_DUMP_PASS" "$MARIADB_DUMP_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_DUMP_USER" \
  "${DUMP_ARGS[@]}" > "$DUMP_FILE"

coord_line="$(grep -m1 -E "MASTER_LOG_FILE='[^']+', MASTER_LOG_POS=[0-9]+" "$DUMP_FILE" || true)"
if [[ -z "$coord_line" ]]; then
  echo "ERROR: Unable to extract binlog coordinates from dump file."
  exit 3
fi

src_file="$(printf "%s" "$coord_line" | sed -E "s/.*MASTER_LOG_FILE='([^']+)'.*/\1/")"
src_pos="$(printf "%s" "$coord_line" | sed -E "s/.*MASTER_LOG_POS=([0-9]+).*/\1/")"

cat > "$BINLOG_COORD_FILE" <<COORDS
SRC_BINLOG_FILE=${src_file}
SRC_BINLOG_POS=${src_pos}
COORDS

echo "Restoring snapshot to target..."
MYSQL_PWD="$TGT_RESTORE_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_RESTORE_USER" < "$DUMP_FILE"

echo "Seed completed."
echo "Coordinates file: $BINLOG_COORD_FILE"
echo "Dump file: $DUMP_FILE"
