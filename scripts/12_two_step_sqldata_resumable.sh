#!/usr/bin/env bash
# scripts/12_two_step_sqldata_resumable.sh
#
# Two-step data transfer in the 'resumable' variant.
#
# This is one of two variants of the two_step data load:
#
#   12_two_step_sqldata.sh           (single_pass) — one sqldata invocation
#                                                   per database, no resume.
#   12_two_step_sqldata_resumable.sh (this file)   — tables loaded in
#                                                   per-DB batches with a
#                                                   manifest written at each
#                                                   batch boundary. A failed
#                                                   load resumes from the
#                                                   first non-complete batch.
#
# Both are dispatched via scripts/12_two_step_dispatch.sh based on
# TWO_STEP_VARIANT (set by mariadb-migrator at prompt time).
#
# Requirements (enforced by 00_preflight_two_step.sh in resumable mode):
#   - Every migrated table must have a PRIMARY KEY. Resume relies on
#     PK-based deduplication via sqldata's IGNORE behavior; without a
#     PK, a failed-batch retry would produce duplicate rows.
#
# Sqldata configuration expectations:
#   - sqldata.cfg should set 'mariadb_load_data_duplicates=IGNORE' (or the
#     equivalent setting in your sqldata version) so retried batches
#     dedupe rather than fail. If this is not set and a batch retry
#     encounters previously-loaded rows, the retry fails with duplicate-
#     key errors. The PK preflight is necessary but not sufficient: the
#     cfg setting is the other half of the contract.
#
# Manifest:
#   Lives at "$SQLINES_OUT_DIR/manifest.txt". Format and planner are in
#   scripts/lib/two_step_batching.sh. Status transitions per batch:
#     not_started → in_progress → complete
#                              \→ failed
#
# Resume detection:
#   On startup, if manifest.txt exists, the recorded plan signature is
#   compared against a freshly-computed signature. Mismatch (DBs or tables
#   changed) refuses to resume — operator must re-run the launcher with
#   FORCE_NEW_RUN=1, which creates a new run directory and a fresh manifest.

set -euo pipefail
echo "==> Two-step migration: parallel data transfer (SQLines Data, resumable)"

SQLINESDATA_BIN="${SQLINESDATA_BIN:-}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

# Output directory resolution — identical to single_pass for consistency.
# See 12_two_step_sqldata.sh for the rationale on the fallback chain.
if [[ -z "${SQLINES_OUT_DIR:-}" ]]; then
  if [[ -n "${RUN_DIR:-}" ]]; then
    SQLINES_OUT_DIR="${RUN_DIR}/sqldata"
  elif [[ -f .migration_last_run ]]; then
    _run_dir_from_file="$(tr -d '\n\r' < .migration_last_run 2>/dev/null || true)"
    if [[ -n "$_run_dir_from_file" && -d "$_run_dir_from_file" ]]; then
      SQLINES_OUT_DIR="${_run_dir_from_file}/sqldata"
    else
      SQLINES_OUT_DIR="artifacts/sqldata"
    fi
    unset _run_dir_from_file
  else
    SQLINES_OUT_DIR="artifacts/sqldata"
  fi
fi

# FK enforcement during load (same rationale as single_pass; see that file
# for the full comment). For resumable, the trap runs at script exit just
# like single_pass — so FK_CHECKS is restored to 1 between failed-run and
# resumed-run, then disabled again at the resumed-run's start. That's the
# correct steady state: enforcement on when no load is in flight.
TWO_STEP_KEEP_FK_CHECKS="${TWO_STEP_KEEP_FK_CHECKS:-0}"

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
# Strip whitespace from each DB name (matches single_pass loop behavior).
for i in "${!DB_LIST[@]}"; do
  DB_LIST[$i]="${DB_LIST[$i]// /}"
done
# Drop empties (e.g. trailing comma in SRC_DBS).
_clean_list=()
for d in "${DB_LIST[@]}"; do
  [[ -n "$d" ]] && _clean_list+=("$d")
done
DB_LIST=("${_clean_list[@]}")
unset _clean_list

mkdir -p "$SQLINES_OUT_DIR"
echo "==> sqldata output: $SQLINES_OUT_DIR"
echo "    Per-batch sqldata logs: $SQLINES_OUT_DIR/<db>/batch_NNNN/sqldata.log"
echo "    The exact log path is also printed in each batch banner below."

# Source the planner library. Required for ts_plan_signature (resume
# validation) and ts_emit_full_manifest (fresh planning).
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"
if [[ ! -f "$LIB_DIR/two_step_batching.sh" ]]; then
  echo "ERROR: Required library not found: $LIB_DIR/two_step_batching.sh" >&2
  exit 1
fi
# shellcheck source=lib/two_step_batching.sh
. "$LIB_DIR/two_step_batching.sh"

# ---- FK toggle setup (mirrors single_pass) -----------------------------------

tgt_admin_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_admin_args+=( --ssl-verify-server-cert=OFF )
fi
target_sql() {
  local sql="$1"
  MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${tgt_admin_args[@]}" -e "$sql"
}

fk_was_set=0
restore_fk_checks() {
  if [[ "$fk_was_set" -eq 1 ]]; then
    echo "==> Restoring target FOREIGN_KEY_CHECKS=1"
    if ! target_sql "SET GLOBAL FOREIGN_KEY_CHECKS=1;" 2>/dev/null; then
      echo "WARNING: Failed to restore FOREIGN_KEY_CHECKS=1 on target." >&2
      echo "         Run manually:" >&2
      echo "           ${MARIADB_BIN} -h ${TGT_HOST} -P ${TGT_PORT} -u ${TGT_USER} -p \\" >&2
      echo "             -e \"SET GLOBAL FOREIGN_KEY_CHECKS=1;\"" >&2
    fi
  fi
}
trap restore_fk_checks EXIT

if [[ "$TWO_STEP_KEEP_FK_CHECKS" != "1" ]]; then
  echo "==> Setting target FOREIGN_KEY_CHECKS=0 for the data load"
  if target_sql "SET GLOBAL FOREIGN_KEY_CHECKS=0;"; then
    echo "    Disabled at GLOBAL scope; will be restored on exit"
    fk_was_set=1
  else
    echo "    GLOBAL scope unavailable (managed target without SUPER) — using session scope."
    echo "    Per-worker FK_CHECKS=0 is set by sqldata.cfg; no cleanup needed."
  fi
fi

SQLDATA_TOPT="${SQLDATA_TOPT:-none}"

# ---- Manifest helpers --------------------------------------------------------

MANIFEST_FILE="${SQLINES_OUT_DIR}/manifest.txt"

_now() { date '+%Y-%m-%dT%H:%M:%S%z'; }

read_recorded_signature() {
  grep -m1 '^# Plan signature: ' "$MANIFEST_FILE" 2>/dev/null \
    | awk '{print $NF}'
}

# update_batch_row <batch_id> <new_status> [<new_started_at>] [<new_completed_at>]
#
# Atomically rewrites the manifest with one batch's fields updated. Pass ""
# for started_at or completed_at to leave the existing value in place. Atomic
# via tmp file + mv (rename(2) on the same filesystem is atomic on POSIX).
update_batch_row() {
  local batch_id="$1" new_status="$2" new_started="${3:-}" new_completed="${4:-}"
  local tmp="${MANIFEST_FILE}.tmp.$$"
  awk -F'\t' -v OFS='\t' \
      -v target="$batch_id" \
      -v new_status="$new_status" \
      -v new_started="$new_started" \
      -v new_completed="$new_completed" '
    /^#/ { print; next }
    NF == 0 { print; next }
    $1 == target {
      $5 = new_status
      if (new_started != "")   $6 = new_started
      if (new_completed != "") $7 = new_completed
    }
    { print }
  ' "$MANIFEST_FILE" > "$tmp" && mv "$tmp" "$MANIFEST_FILE"
}

# ts_target_table_check <db> <expected_csv>
#
# Verify every table in <expected_csv> exists as a BASE TABLE on the target
# in the named DB. Used on the resume path to detect the case where the
# operator dropped the target DB between runs: without this check, sqldata
# would fail per-table with the misleading "Table 'X' doesn't exist" message
# and the manifest would mark the batch failed for the wrong reason.
#
# Returns 0 if all expected tables are present, 1 with a guidance message
# otherwise. Caller is expected to exit on non-zero.
ts_target_table_check() {
  local db="$1"
  local expected_csv="$2"

  local target_tables
  target_tables="$(target_sql \
    "SELECT TABLE_NAME FROM information_schema.TABLES
       WHERE TABLE_SCHEMA = '${db//\'/\'\'}' AND TABLE_TYPE = 'BASE TABLE';" \
    2>/dev/null || true)"

  local -a expected_arr
  IFS=',' read -r -a expected_arr <<< "$expected_csv"

  local -a missing=()
  local t
  for t in "${expected_arr[@]}"; do
    if ! grep -qFx "$t" <<< "$target_tables"; then
      missing+=("${db}.${t}")
    fi
  done

  if [[ "${#missing[@]}" -gt 0 ]]; then
    echo "ERROR: Target schema is incomplete; cannot resume." >&2
    echo "       The following tables are in the manifest but missing on the target:" >&2
    local m
    for m in "${missing[@]}"; do
      echo "         - $m" >&2
    done
    echo "" >&2
    echo "       This usually means the target database was dropped (or the schema" >&2
    echo "       was never applied) between the previous run and now. The resumable" >&2
    echo "       variant cannot recreate schema — it expects step 11's schema from" >&2
    echo "       the previous run to still be present." >&2
    echo "" >&2
    echo "       To recover, re-run mariadb-migrator with FORCE_NEW_RUN=1." >&2
    echo "       This creates a new run that re-applies the schema before loading." >&2
    return 1
  fi
  return 0
}

# ---- Plan or resume decision -------------------------------------------------

if [[ -f "$MANIFEST_FILE" ]]; then
  echo "==> Existing manifest found: $MANIFEST_FILE"
  recorded_sig="$(read_recorded_signature)"
  current_sig="$(ts_plan_signature "${DB_LIST[@]}")"
  if [[ -z "$recorded_sig" ]]; then
    echo "ERROR: Manifest is missing a plan signature header."
    echo "       File may be truncated or hand-edited. Re-run with FORCE_NEW_RUN=1"
    echo "       to discard and re-plan."
    exit 1
  fi
  if [[ "$recorded_sig" != "$current_sig" ]]; then
    echo "ERROR: Plan signature mismatch."
    echo "       Manifest: $recorded_sig"
    echo "       Current : $current_sig"
    echo ""
    echo "       The set of source databases/tables has changed since the"
    echo "       existing manifest was written. Resuming would produce an"
    echo "       inconsistent migration."
    echo ""
    echo "       To start fresh, re-run mariadb-migrator with FORCE_NEW_RUN=1."
    echo "       This creates a new run directory and a new manifest."
    exit 1
  fi
  echo "    Plan signature matches; resuming"

  # Resume safety net: verify target schema still has all the tables the
  # manifest expects. Aggregate expected tables per DB, then check each.
  echo "==> Verifying target schema matches manifest..."
  declare -A _expected_per_db=()
  while IFS=$'\t' read -r _bid _db _tables _size _status _started _completed; do
    [[ "$_bid" =~ ^# ]] && continue
    [[ -z "$_bid" ]] && continue
    if [[ -z "${_expected_per_db[$_db]:-}" ]]; then
      _expected_per_db[$_db]="$_tables"
    else
      _expected_per_db[$_db]="${_expected_per_db[$_db]},$_tables"
    fi
  done < "$MANIFEST_FILE"
  for _db in "${!_expected_per_db[@]}"; do
    if ! ts_target_table_check "$_db" "${_expected_per_db[$_db]}"; then
      exit 1
    fi
  done
  echo "    Target schema OK"
  unset _expected_per_db _bid _db _tables _size _status _started _completed
else
  echo "==> No existing manifest; planning fresh"
  ts_emit_full_manifest "${DB_LIST[@]}" > "$MANIFEST_FILE"
  echo "    Wrote $MANIFEST_FILE"
fi

# ---- Manifest summary --------------------------------------------------------

total_batches=0
complete_batches=0
while IFS=$'\t' read -r bid _db _tables _size status _started _completed; do
  [[ "$bid" =~ ^# ]] && continue
  [[ -z "$bid" ]] && continue
  total_batches=$((total_batches + 1))
  [[ "$status" == "complete" ]] && complete_batches=$((complete_batches + 1))
done < "$MANIFEST_FILE"

echo "==> Manifest summary: ${complete_batches}/${total_batches} batches complete"

remaining=$((total_batches - complete_batches))
if [[ "$remaining" -eq 0 ]]; then
  echo "==> All batches already complete; nothing to do"
  exit 0
fi

echo "==> Loading ${remaining} remaining batches"

# ---- Batch execution loop ----------------------------------------------------

# Read the manifest into memory before iterating so that the in-loop
# update_batch_row rewrites don't perturb our cursor (the FD-based loop
# would tolerate that on Linux, but reading up front is more obvious).
mapfile -t MANIFEST_LINES < "$MANIFEST_FILE"

for line in "${MANIFEST_LINES[@]}"; do
  [[ "$line" =~ ^# ]] && continue
  [[ -z "$line" ]] && continue

  IFS=$'\t' read -r bid db tables size status started completed <<< "$line"

  if [[ "$status" == "complete" ]]; then
    continue
  fi

  if [[ "$status" == "in_progress" || "$status" == "failed" ]]; then
    echo "==> Batch ${bid} (${db}) was '${status}' in previous run; retrying"
  fi

  # Compute output paths up front so we can surface the per-batch log
  # path in the banner before invoking sqldata. The operator can then tail
  # this file in another terminal while the batch runs.
  db_out_dir="$SQLINES_OUT_DIR/$db"
  batch_out_dir="$db_out_dir/batch_${bid}"
  batch_log="$batch_out_dir/sqldata.log"
  mkdir -p "$batch_out_dir"

  echo "==> Batch ${bid} (${db}): ${tables}"
  echo "    Log: ${batch_log}"
  update_batch_row "$bid" "in_progress" "$(_now)" ""

  # Build qualified table list for sqldata's -t flag. Manifest stores
  # unqualified table names; sqldata wants "db.table1,db.table2,...".
  qualified=""
  IFS=',' read -r -a tbl_arr <<< "$tables"
  for t in "${tbl_arr[@]}"; do
    if [[ -z "$qualified" ]]; then
      qualified="${db}.${t}"
    else
      qualified="${qualified},${db}.${t}"
    fi
  done

  # Each batch gets its own -out subdirectory so sqldata's per-invocation
  # artifacts (ddl/, failed-tables list, etc.) don't clobber prior batches'.
  # (Paths were computed above so the banner could surface them.)

  # Run sqldata for this batch. The argument shape is identical to single_pass
  # except for -t (explicit table list, not the "db.*" wildcard) and the
  # per-batch -out / -log paths.
#    -ss=6 \
  set +e
  "$SQLINESDATA_BIN" \
    "-sd=mysql,${SRC_USER}/${SRC_PASS}@${SRC_HOST}:${SRC_PORT}/${db}" \
    "-td=mariadb,${TGT_USER}/${TGT_PASS}@${TGT_HOST}:${TGT_PORT}/${db}" \
    "-smap=${db}:${db}" \
    "-out=$batch_out_dir" \
    "-log=$batch_log" \
    "-t=${qualified}" \
    "-topt=${SQLDATA_TOPT}" \
    -constraints=no \
    -indexes=no \
    -triggers=no \
    -views=no \
    -procedures=no
  rc=$?
  set -e

  if [[ "$rc" -eq 0 ]]; then
    update_batch_row "$bid" "complete" "" "$(_now)"
    echo "==> Batch ${bid} complete"
  else
    update_batch_row "$bid" "failed" "" ""
    echo "" >&2
    echo "ERROR: Batch ${bid} failed (sqldata exit ${rc})." >&2
    echo "       Log: ${batch_log}" >&2
    echo "       Manifest preserved with batch ${bid} marked 'failed'." >&2
    echo "       Re-run mariadb-migrator (same inputs) to resume from this batch." >&2
    exit "$rc"
  fi
done

echo "SQLines Data resumable transfer completed."
