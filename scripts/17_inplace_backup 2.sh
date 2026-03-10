#!/usr/bin/env bash
set -euo pipefail

echo "==> In-place upgrade: backup"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
INPLACE_BACKUP_DIR="${INPLACE_BACKUP_DIR:-artifacts/inplace_backup}"

if [[ -z "$SRC_HOST" || -z "$SRC_ADMIN_USER" || -z "$SRC_ADMIN_PASS" ]]; then
  echo "ERROR: Missing env vars: SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS"
  exit 1
fi

if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  if command -v mysqldump >/dev/null 2>&1; then
    MARIADB_DUMP_BIN="mysqldump"
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found."
    exit 2
  fi
fi

mkdir -p "$INPLACE_BACKUP_DIR"
backup_sql="$INPLACE_BACKUP_DIR/full_backup_$(date +%Y%m%d_%H%M%S).sql"
meta_file="$INPLACE_BACKUP_DIR/backup_meta.txt"

echo "Creating logical backup: $backup_sql"
MYSQL_PWD="$SRC_ADMIN_PASS" "$MARIADB_DUMP_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --all-databases --routines --events --triggers --single-transaction > "$backup_sql"

echo "backup_file=$backup_sql" > "$meta_file"
echo "created_at=$(date -u +%FT%TZ)" >> "$meta_file"

echo "Backup completed."
echo "Metadata file: $meta_file"
