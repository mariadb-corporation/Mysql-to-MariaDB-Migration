#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (inplace)"

MYSQL_BIN="${MYSQL_BIN:-mysql}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
INPLACE_BACKUP_DIR="${INPLACE_BACKUP_DIR:-artifacts/inplace_backup}"

missing=()
for v in SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi

if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: mysql client not found (MYSQL_BIN=$MYSQL_BIN)."
  exit 2
fi

echo "Checking source admin connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null

echo "Checking source MySQL version..."
src_version="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SELECT VERSION();" | head -n1)"
if [[ -z "$src_version" ]]; then
  echo "ERROR: Could not read source version."
  exit 3
fi

echo "Source version: $src_version"
major_minor="$(printf "%s" "$src_version" | sed -E 's/^([0-9]+)\.([0-9]+).*/\1.\2/')"
major="$(printf "%s" "$major_minor" | cut -d. -f1)"
minor="$(printf "%s" "$major_minor" | cut -d. -f2)"
if [[ "$major" -gt 8 || ( "$major" -eq 8 && "$minor" -ge 0 ) ]]; then
  echo "ERROR: In-place mode is not supported for MySQL 8.0+ in this tool."
  echo "Use one_step, two_step, or binlog migration path."
  exit 4
fi

echo "Checking backup directory..."
mkdir -p "$INPLACE_BACKUP_DIR"
if [[ ! -w "$INPLACE_BACKUP_DIR" ]]; then
  echo "ERROR: Backup directory is not writable: $INPLACE_BACKUP_DIR"
  exit 5
fi

echo "Preflight complete."
