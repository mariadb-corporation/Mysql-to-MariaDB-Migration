#!/usr/bin/env bash
set -euo pipefail

echo "==> One-step migration (mariadb-dump | mariadb)"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"
PV_BIN="${PV_BIN:-pv}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"
SRC_SSL_MODE="${SRC_SSL_MODE:-}"
STRIP_DEFINERS="${STRIP_DEFINERS:-1}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"
ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"

if [[ -z "$SRC_HOST" || -z "$SRC_USER" || -z "$SRC_PASS" || ( -z "$SRC_DB" && -z "$SRC_DBS" ) ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_USER, SRC_PASS, and SRC_DB or SRC_DBS."
  exit 1
fi

if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_USER, TGT_PASS (TGT_PORT optional)."
  exit 1
fi

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

# Connection args. Drop --protocol=TCP (newer mariadb clients reject self-signed
# certs when it's set), add --ssl-verify-server-cert=OFF for mariadb client.
src_admin_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" --batch --skip-column-names )
tgt_admin_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_admin_args+=( --ssl-verify-server-cert=OFF )
fi

target_db_exists() {
  local db="$1"
  local db_esc
  db_esc="$(sql_escape "$db")"
  local q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
  local out=""
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local tgt_pass_q
    tgt_pass_q="$(printf '%q' "$TGT_PASS")"
    out="$(ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$tgt_pass_q ${MARIADB_BIN} -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --batch --skip-column-names -e \"$q\"")"
  else
    out="$(MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${tgt_admin_args[@]}" -e "$q")"
  fi
  [[ "${out:-0}" -gt 0 ]]
}

# Probe source MySQL version (informational only — one-step does not use
# --master-data / --source-data, so SHOW BINARY LOG STATUS is never issued
# and mariadb-dump works against any MySQL 5.7 / 8.0 / 8.4+ source).
src_version_full="$(MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1)"

# Pick dump binary. One-step does NOT use --master-data / --source-data, so
# the SHOW BINARY LOG STATUS syntax difference between MySQL 8.4 and earlier
# never gets exercised. mariadb-dump produces a valid schema+data dump from
# any MySQL 5.7 / 8.0 / 8.4+ source. Honor a user-set MARIADB_DUMP_BIN if it
# is on PATH; otherwise default to mariadb-dump and fall back to mysqldump
# only when mariadb-dump is unavailable.
chosen_dump=""
if command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  chosen_dump="$MARIADB_DUMP_BIN"
elif command -v mariadb-dump >/dev/null 2>&1; then
  chosen_dump="mariadb-dump"
elif command -v mysqldump >/dev/null 2>&1; then
  echo "mariadb-dump not found; using mysqldump."
  chosen_dump="mysqldump"
else
  echo "ERROR: neither mariadb-dump nor mysqldump found." >&2
  exit 2
fi
MARIADB_DUMP_BIN="$chosen_dump"
echo "Source MySQL: ${src_version_full:-unknown}  Dump tool: $MARIADB_DUMP_BIN"

# Identify the dump tool family (real upstream mysqldump vs MariaDB-supplied).
# All flag selection below branches on this, not on the raw MARIADB_DUMP_BIN
# string (which may be an absolute path).
dump_basename="$(basename "$MARIADB_DUMP_BIN")"
dump_is_mysql=0
if [[ "$dump_basename" == "mysqldump" ]] && \
   ! "$MARIADB_DUMP_BIN" --version 2>/dev/null | grep -iq 'mariadb'; then
  dump_is_mysql=1
fi

SRC_AUTH=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" )
TGT_AUTH=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" )

if [[ -n "$SRC_DBS" ]]; then
  echo "Source: $SRC_HOST:$SRC_PORT  DBs: $SRC_DBS"
else
  echo "Source: $SRC_HOST:$SRC_PORT  DB: $SRC_DB"
fi
echo "Target: $TGT_HOST:$TGT_PORT"

PIPE_CMD=()
if command -v "$PV_BIN" >/dev/null 2>&1; then
  # -f -i 10: force output and emit a progress line every 10 seconds even
  # when stderr is not a TTY. The orchestrator (runner.py) pipes both
  # stdout and stderr to run.log, which would otherwise suppress pv's
  # interactive progress bar. tail -f run.log to watch live.
  PIPE_CMD=( "$PV_BIN" -pet -f -i 10 )
fi

SRC_SSL_ARGS=()
if [[ -n "$SRC_SSL_MODE" ]]; then
  if [[ "$dump_is_mysql" -eq 1 ]]; then
    SRC_SSL_ARGS=( --ssl-mode="$SRC_SSL_MODE" )
  else
    if [[ "$SRC_SSL_MODE" == "DISABLED" ]]; then
      SRC_SSL_ARGS=()
    else
      SRC_SSL_ARGS=( --ssl )
    fi
  fi
fi

PROBE_PID=""
if [[ "${#PIPE_CMD[@]}" -eq 0 ]]; then
  echo "Note: pv not found; emitting heartbeat every 60s instead."
  # Background heartbeat. Prints elapsed-time markers so the operator (and
  # tail -f viewers) can see the script is alive during long dump/load
  # pipelines. We can't report bytes here because one_step has no on-disk
  # file to stat — data goes mariadb-dump → mariadb client over a pipe.
  (
    start=$(date +%s)
    while true; do
      sleep 60
      elapsed=$(( $(date +%s) - start ))
      printf "still running... %ds elapsed\n" "$elapsed"
    done
  ) &
  PROBE_PID=$!
  # shellcheck disable=SC2064  # intentional early expansion of $PROBE_PID
  trap "[[ -n \"$PROBE_PID\" ]] && kill $PROBE_PID 2>/dev/null; wait $PROBE_PID 2>/dev/null; trap - EXIT" EXIT
fi

set -o pipefail
DUMP_ARGS=(
  --routines --triggers --events
  --no-tablespaces --hex-blob --single-transaction
)
FILTER_CMD=()
if [[ "$STRIP_DEFINERS" == "1" ]]; then
  if [[ "$dump_is_mysql" -eq 1 ]]; then
    FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
  else
    if "$MARIADB_DUMP_BIN" --help 2>/dev/null | grep -q -- '--skip-definer'; then
      DUMP_ARGS+=(--skip-definer)
    else
      FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
    fi
  fi
fi
if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
  DUMP_ARGS+=(--databases "${DB_LIST[@]}")
else
  DB_LIST=("$SRC_DB")
  DUMP_ARGS+=(--databases "$SRC_DB")
fi
if [[ "$dump_is_mysql" -eq 1 ]]; then
  DUMP_ARGS+=(--set-gtid-purged=OFF)
else
  DUMP_ARGS+=(--gtid=0)
fi

if [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
  existing=()
  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    if target_db_exists "$db"; then
      existing+=("$db")
    fi
  done
  if [[ "${#existing[@]}" -gt 0 ]]; then
    echo "ERROR: Target DB already exists: ${existing[*]}"
    echo "Set ALLOW_TARGET_DB_OVERWRITE=1 only if you explicitly want to overwrite existing DBs."
    exit 8
  fi
fi

MYSQL_PWD="$SRC_PASS" "$MARIADB_DUMP_BIN" "${SRC_AUTH[@]}" "${SRC_SSL_ARGS[@]}" "${DUMP_ARGS[@]}" \
  | if [[ "${#FILTER_CMD[@]}" -gt 0 ]]; then "${FILTER_CMD[@]}"; else cat; fi \
  | if [[ "${#PIPE_CMD[@]}" -gt 0 ]]; then "${PIPE_CMD[@]}"; else cat; fi \
  | if [[ -n "$TGT_SSH_HOST" ]]; then
      TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
      ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
        "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_AUTH[*]}"
    else
      MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${TGT_AUTH[@]}"
    fi
set +o pipefail

echo "One-step migration completed."
