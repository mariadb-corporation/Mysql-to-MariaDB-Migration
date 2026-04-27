#!/usr/bin/env bash
set -euo pipefail

echo "==> Two-step migration: parallel data transfer (SQLines Data)"

SQLINESDATA_BIN="${SQLINESDATA_BIN:-}"
SQLINES_OUT_DIR="${SQLINES_OUT_DIR:-artifacts/sqldata}"
SRC_DBS="${SRC_DBS:-}"
SRC_DB="${SRC_DB:-}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"

if [[ -z "$SQLINESDATA_BIN" ]]; then
  if command -v sqldata >/dev/null 2>&1; then
    SQLINESDATA_BIN="sqldata"
  elif command -v sqlinesdata >/dev/null 2>&1; then
    SQLINESDATA_BIN="sqlinesdata"
  fi
fi

if [[ -z "$SQLINESDATA_BIN" ]] || ! command -v "$SQLINESDATA_BIN" >/dev/null 2>&1; then
  echo "ERROR: sqldata binary not found (SQLINESDATA_BIN=$SQLINESDATA_BIN)."
  exit 1
fi

missing=()
for v in SRC_HOST SRC_USER SRC_PASS TGT_HOST TGT_USER TGT_PASS; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ -z "$SRC_DB" && -z "$SRC_DBS" ]]; then
  missing+=("SRC_DB_or_SRC_DBS")
fi
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars for sqldata transfer: ${missing[*]}"
  exit 1
fi

if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi

mkdir -p "$SQLINES_OUT_DIR"

for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  db_out_dir="$SQLINES_OUT_DIR/$db"
  mkdir -p "$db_out_dir"
  "$SQLINESDATA_BIN" \
    "-sd=mysql,${SRC_USER}/${SRC_PASS}@${SRC_HOST}:${SRC_PORT}/${db}" \
    "-td=mariadb,${TGT_USER}/${TGT_PASS}@${TGT_HOST}:${TGT_PORT}/${db}" \
    "-smap=${db}:${db}" \
    "-out=$db_out_dir" \
    "-log=$db_out_dir/sqldata.log" \
    -ss=6 \
    "-t=${db}.*" \
    -constraints=no \
    -indexes=no \
    -triggers=no \
    -views=no \
    -procedures=no
done

echo "SQLines Data transfer completed."
