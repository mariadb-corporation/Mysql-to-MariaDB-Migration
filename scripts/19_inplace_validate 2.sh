#!/usr/bin/env bash
set -euo pipefail

echo "==> In-place upgrade: validate"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"

if [[ -z "$SRC_HOST" || -z "$SRC_ADMIN_USER" || -z "$SRC_ADMIN_PASS" ]]; then
  echo "ERROR: Missing env vars: SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS"
  exit 1
fi

echo "Checking server connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SELECT 1;" >/dev/null

echo "Checking server version..."
version="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  --batch --skip-column-names -e "SELECT VERSION();" | head -n1)"
echo "Version: $version"
if [[ "$version" != *"MariaDB"* ]]; then
  echo "ERROR: Version does not look like MariaDB after in-place upgrade."
  exit 2
fi

echo "In-place validation passed."
