#!/usr/bin/env bash
set -euo pipefail

echo "==> Two-step migration: schema only (no data)"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MYSQL_BIN="${MYSQL_BIN:-mysql}"

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

# Output directory for schema_post.sql files. These are produced here in this
# script (POST pass) and consumed by 13_two_step_finalize.sh after the data
# load completes. Resolution mirrors 12_two_step_sqldata.sh's SQLINES_OUT_DIR
# logic so all per-run artifacts cluster under the same run directory:
#   1. SCHEMA_POST_DIR if explicitly set by the caller
#   2. $RUN_DIR/schema_post if RUN_DIR is exported by the orchestrator
#   3. .migration_last_run pointer (current orchestrator behavior, since
#      RUN_DIR is a bash local in mariadb-migrator and is not exported)
#   4. artifacts/schema_post fallback (legacy flat layout)
if [[ -z "${SCHEMA_POST_DIR:-}" ]]; then
  if [[ -n "${RUN_DIR:-}" ]]; then
    SCHEMA_POST_DIR="${RUN_DIR}/schema_post"
  elif [[ -f .migration_last_run ]]; then
    _run_dir_from_file="$(tr -d '\n\r' < .migration_last_run 2>/dev/null || true)"
    if [[ -n "$_run_dir_from_file" && -d "$_run_dir_from_file" ]]; then
      SCHEMA_POST_DIR="${_run_dir_from_file}/schema_post"
    else
      SCHEMA_POST_DIR="artifacts/schema_post"
    fi
    unset _run_dir_from_file
  else
    SCHEMA_POST_DIR="artifacts/schema_post"
  fi
fi
mkdir -p "$SCHEMA_POST_DIR"

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

# Probe source MySQL version (informational only — used in the startup banner
# below). Two-step schema dumps do NOT use --master-data / --source-data, so
# SHOW BINARY LOG STATUS / SHOW MASTER STATUS is never issued. That means any
# dump tool (mariadb-dump, or mysqldump of any version) works against any
# MySQL 5.7 / 8.0 / 8.4+ source for this schema-only path. No version-gating
# is required here — only the binlog-seed script (14_binlog_seed.sh) needs
# upstream mysqldump 8.4+ for 8.4 sources, because it passes --master-data.
src_version_full="$(MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1)"

# Pick dump binary. Honor a user-set MARIADB_DUMP_BIN if it resolves; otherwise
# fall back to mariadb-dump (default), then upstream mysqldump as last resort.
# Mirrors the selection logic in 10_one_step_migration.sh — same constraints,
# same fix.
if command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  chosen_dump="$MARIADB_DUMP_BIN"
elif command -v mariadb-dump >/dev/null 2>&1; then
  chosen_dump="mariadb-dump"
elif command -v mysqldump >/dev/null 2>&1; then
  echo "mariadb-dump not found; using mysqldump."
  chosen_dump="mysqldump"
else
  echo "ERROR: neither mariadb-dump nor mysqldump found."
  exit 2
fi
MARIADB_DUMP_BIN="$chosen_dump"
echo "Source MySQL: ${src_version_full:-unknown}  Dump tool: $MARIADB_DUMP_BIN"

# Identify the dump tool family (real upstream mysqldump vs MariaDB-supplied).
dump_basename="$(basename "$MARIADB_DUMP_BIN")"
dump_is_mysql=0
if [[ "$dump_basename" == "mysqldump" ]] && \
   ! "$MARIADB_DUMP_BIN" --version 2>/dev/null | grep -iq 'mariadb'; then
  dump_is_mysql=1
fi

SRC_AUTH=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" )
TGT_AUTH=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --max-allowed-packet=1G )

if [[ -n "$SRC_DBS" ]]; then
  echo "Source: $SRC_HOST:$SRC_PORT  DBs: $SRC_DBS"
else
  echo "Source: $SRC_HOST:$SRC_PORT  DB: $SRC_DB"
fi
echo "Target: $TGT_HOST:$TGT_PORT"
echo "Schema PRE will be applied to target now."
echo "Schema POST (triggers/routines/events) will be staged at: $SCHEMA_POST_DIR"
echo "  → 13_two_step_finalize.sh applies it after the data load."

set -o pipefail
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

# Pre-data DDL: tables, indexes, foreign keys. NO triggers/routines/events.
# These are deferred to 13_two_step_finalize.sh so they don't fire during
# the data load. Without this split, triggers on the target re-insert rows
# that sqldata then tries to load directly, producing duplicate-key churn
# (sakila.film_text is the canonical example). FK_CHECKS=0 in step 12
# handles cross-table parallel load contention; deferring triggers fixes
# the orthogonal trigger-side double-load problem.
PRE_ARGS=(
  --no-data
  --skip-triggers
  --no-tablespaces
  --skip-lock-tables
)

# Post-data DDL: triggers, routines, events ONLY.
#   --no-create-info: skip CREATE TABLE (tables already exist from PRE).
#   --no-create-db:   skip CREATE DATABASE (DBs already exist from PRE).
#                     USE statements are still emitted so triggers/routines/
#                     events attach to the correct schema.
#   --add-drop-trigger: emit DROP TRIGGER IF EXISTS before each CREATE TRIGGER.
#                       Makes step 13 idempotent for triggers. Routines and
#                       events have no equivalent flag — if step 13 fails
#                       partway through routines/events, manual cleanup of
#                       partially-created objects is required before retry.
POST_ARGS=(
  --no-data
  --no-create-info
  --no-create-db
  --routines --triggers --events
  --no-tablespaces
  --skip-lock-tables
)
if "$MARIADB_DUMP_BIN" --help 2>/dev/null | grep -q -- '--add-drop-trigger'; then
  POST_ARGS+=( --add-drop-trigger )
fi

FILTER_CMD=()
if [[ "$STRIP_DEFINERS" == "1" ]]; then
  if [[ "$dump_is_mysql" -eq 1 ]]; then
    FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
  else
    if "$MARIADB_DUMP_BIN" --help 2>/dev/null | grep -q -- '--skip-definer'; then
      PRE_ARGS+=(--skip-definer)
      POST_ARGS+=(--skip-definer)
    else
      FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
    fi
  fi
fi
if [[ "$dump_is_mysql" -eq 1 ]]; then
  PRE_ARGS+=(--set-gtid-purged=OFF)
  POST_ARGS+=(--set-gtid-purged=OFF)
else
  PRE_ARGS+=(--gtid=0)
  POST_ARGS+=(--gtid=0)
fi

if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
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

for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue

  # ---- PRE pass: dump tables/indexes/FKs and apply directly to target ----
  echo "==> [${db}] dumping pre-data DDL and applying to target"
  PRE_DUMP_ARGS=("${PRE_ARGS[@]}" --databases "$db")
  MYSQL_PWD="$SRC_PASS" "$MARIADB_DUMP_BIN" "${SRC_AUTH[@]}" "${SRC_SSL_ARGS[@]}" "${PRE_DUMP_ARGS[@]}" \
    | if [[ "${#FILTER_CMD[@]}" -gt 0 ]]; then "${FILTER_CMD[@]}"; else cat; fi \
    | if [[ -n "$TGT_SSH_HOST" ]]; then
        TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
        ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
          "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_AUTH[*]}"
      else
        MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${TGT_AUTH[@]}"
      fi

  # ---- POST pass: dump triggers/routines/events to file for step 13 ----
  echo "==> [${db}] dumping post-data DDL (triggers/routines/events) to file"
  post_file="${SCHEMA_POST_DIR}/${db}_schema_post.sql"
  POST_DUMP_ARGS=("${POST_ARGS[@]}" --databases "$db")
  MYSQL_PWD="$SRC_PASS" "$MARIADB_DUMP_BIN" "${SRC_AUTH[@]}" "${SRC_SSL_ARGS[@]}" "${POST_DUMP_ARGS[@]}" \
    | if [[ "${#FILTER_CMD[@]}" -gt 0 ]]; then "${FILTER_CMD[@]}"; else cat; fi \
    > "$post_file"
  if [[ -s "$post_file" ]]; then
    post_size="$(wc -c < "$post_file" | tr -d ' ')"
    echo "    Wrote ${post_file} (${post_size} bytes)"
  else
    echo "    Wrote ${post_file} (empty — no triggers/routines/events in ${db})"
  fi
done
set +o pipefail

echo "Schema-only migration completed."
echo "  PRE applied to target. POST staged at: ${SCHEMA_POST_DIR}"
