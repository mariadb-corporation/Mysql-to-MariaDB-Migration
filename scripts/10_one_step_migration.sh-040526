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

# Probe source MySQL version (mariadb-dump is not compatible with MySQL 8.4+).
src_version_full="$(MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1)"
src_version_num="$(printf "%s" "$src_version_full" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')"
src_major="${src_version_num%%.*}"
src_minor="${src_version_num#*.}"; src_minor="${src_minor%%.*}"
src_is_84_plus=0
if [[ "${src_major:-0}" -gt 8 ]] || { [[ "${src_major:-0}" -eq 8 ]] && [[ "${src_minor:-0}" -ge 4 ]]; }; then
  src_is_84_plus=1
fi

detect_dump_version() {
  local bin="$1" ver_line ver_num
  ver_line="$("$bin" --version 2>/dev/null || true)"
  ver_num="$(printf "%s" "$ver_line" | sed -nE 's/.*Ver +([0-9]+\.[0-9]+).*/\1/p' | head -1)"
  [[ -z "$ver_num" ]] && ver_num="$(printf "%s" "$ver_line" | grep -oE '[0-9]+\.[0-9]+' | head -1)"
  printf "%s" "$ver_num"
}

# Pick dump binary: source >= 8.4 needs upstream mysqldump 8.4+, else mariadb-dump.
chosen_dump=""
if [[ "$src_is_84_plus" -eq 1 ]]; then
  candidates=()
  if [[ -n "${MARIADB_DUMP_BIN:-}" && "$MARIADB_DUMP_BIN" != "mariadb-dump" && "$MARIADB_DUMP_BIN" != "mysqldump" ]]; then
    candidates+=("$MARIADB_DUMP_BIN")
  fi
  candidates+=("mysqldump")

  for cand in "${candidates[@]}"; do
    if ! command -v "$cand" >/dev/null 2>&1; then
      continue
    fi
    cand_ver_line="$("$cand" --version 2>/dev/null || true)"
    if printf "%s" "$cand_ver_line" | grep -iq 'mariadb'; then
      continue
    fi
    cand_ver="$(detect_dump_version "$cand")"
    cand_major="${cand_ver%%.*}"
    cand_minor="${cand_ver#*.}"; cand_minor="${cand_minor%%.*}"
    if [[ "${cand_major:-0}" -gt 8 ]] || { [[ "${cand_major:-0}" -eq 8 ]] && [[ "${cand_minor:-0}" -ge 4 ]]; }; then
      chosen_dump="$cand"
      break
    fi
  done

  if [[ -z "$chosen_dump" ]]; then
    echo "ERROR: Source is MySQL 8.4+, needs MySQL mysqldump 8.4+. Download client from https://dev.mysql.com/downloads/mysql/, extract, then: export MARIADB_DUMP_BIN=/path/to/mysqldump" >&2
    exit 2
  fi
else
  # For a < 8.4 source: prefer mariadb-dump (always compatible). A user-set
  # MARIADB_DUMP_BIN pointing at mysqldump 8.4+ will break (mysqldump 8.4 issues
  # SHOW BINARY LOG STATUS, which < 8.4 servers don't understand) — warn and
  # fall back to mariadb-dump.
  candidate="$MARIADB_DUMP_BIN"
  candidate_ok=0
  if command -v "$candidate" >/dev/null 2>&1; then
    cand_basename="$(basename "$candidate")"
    if [[ "$cand_basename" == "mariadb-dump" ]]; then
      candidate_ok=1
    elif [[ "$cand_basename" == "mysqldump" ]]; then
      cand_ver_line="$("$candidate" --version 2>/dev/null || true)"
      if printf "%s" "$cand_ver_line" | grep -iq 'mariadb'; then
        candidate_ok=1
      else
        cand_ver="$(detect_dump_version "$candidate")"
        cand_major="${cand_ver%%.*}"
        cand_minor="${cand_ver#*.}"; cand_minor="${cand_minor%%.*}"
        if [[ "${cand_major:-0}" -lt 8 ]] ||            { [[ "${cand_major:-0}" -eq 8 ]] && [[ "${cand_minor:-0}" -lt 4 ]]; }; then
          candidate_ok=1
        else
          echo "WARNING: \$MARIADB_DUMP_BIN ($candidate) is mysqldump $cand_ver, which" >&2
          echo "         issues SHOW BINARY LOG STATUS — incompatible with source ${src_version_full:-(< 8.4)}." >&2
          echo "         Falling back to mariadb-dump. Unset MARIADB_DUMP_BIN to silence this warning." >&2
        fi
      fi
    fi
  fi
  if [[ "$candidate_ok" -eq 1 ]]; then
    chosen_dump="$candidate"
  elif command -v mariadb-dump >/dev/null 2>&1; then
    chosen_dump="mariadb-dump"
  elif command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; using mysqldump."
    chosen_dump="mysqldump"
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found."
    exit 2
  fi
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
  PIPE_CMD=( "$PV_BIN" -pet )
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

if [[ "${#PIPE_CMD[@]}" -eq 0 ]]; then
  echo "pv not found; running without progress meter."
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
