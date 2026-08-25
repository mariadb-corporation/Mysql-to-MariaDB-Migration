#!/usr/bin/env bash
set -euo pipefail

echo "==> Two-step migration: finalize schema (triggers, routines, events)"

# This step consumes the per-DB schema_post.sql files produced by
# 11_two_step_schema.sh and applies them to the target after the data load
# in 12_two_step_sqldata.sh has completed. The deferral is what prevents
# trigger-driven row duplication during the data load 

MARIADB_BIN="${MARIADB_BIN:-mariadb}"

SRC_DBS="${SRC_DBS:-}"
SRC_DB="${SRC_DB:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"

# Locate the schema_post directory produced by 11_two_step_schema.sh.
# Resolution mirrors steps 11 and 12 for consistency:
#   1. SCHEMA_POST_DIR if explicitly set by the caller
#   2. $RUN_DIR/schema_post if RUN_DIR is exported by the orchestrator
#   3. .migration_last_run pointer (current orchestrator behavior)
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

if [[ ! -d "$SCHEMA_POST_DIR" ]]; then
  echo "ERROR: schema_post directory not found: $SCHEMA_POST_DIR"
  echo "       Did 11_two_step_schema.sh run successfully in this run?"
  exit 1
fi

# Env validation.
missing=()
for v in TGT_HOST TGT_USER TGT_PASS; do
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

TGT_AUTH=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --max-allowed-packet=1G )
tgt_client_args=()
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_client_args+=( --ssl-verify-server-cert=OFF )
fi

if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi

echo "Target: $TGT_HOST:$TGT_PORT"
echo "Schema POST source dir: $SCHEMA_POST_DIR"

applied=0
skipped_empty=0
skipped_missing=0

for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  post_file="${SCHEMA_POST_DIR}/${db}_schema_post.sql"

  if [[ ! -f "$post_file" ]]; then
    echo "WARNING: [${db}] schema_post file missing: $post_file"
    echo "         Step 11 should have produced this. Skipping."
    skipped_missing=$((skipped_missing + 1))
    continue
  fi

  # An empty or whitespace-only file means the source DB had no triggers,
  # routines, or events. Skip silently rather than invoking the client for
  # nothing — common case for greenfield schemas.
  if [[ ! -s "$post_file" ]] || ! grep -qE '\S' "$post_file"; then
    echo "==> [${db}] no post-data objects to apply (empty file)"
    skipped_empty=$((skipped_empty + 1))
    continue
  fi

  echo "==> [${db}] applying triggers/routines/events"
  if [[ -n "$TGT_SSH_HOST" ]]; then
    TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
    # Stream the local file through ssh stdin to mariadb on the target host.
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_AUTH[*]} ${tgt_client_args[*]}" \
      < "$post_file"
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${TGT_AUTH[@]}" "${tgt_client_args[@]}" \
      < "$post_file"
  fi
  echo "    Applied: $post_file"
  applied=$((applied + 1))
done

echo
echo "Finalize completed."
echo "  Applied:        ${applied} DB(s)"
echo "  Skipped (empty): ${skipped_empty} DB(s)"
echo "  Skipped (missing file): ${skipped_missing} DB(s)"
