#!/usr/bin/env bash
set -euo pipefail
echo "==> Two-step migration: parallel data transfer (SQLines Data)"

SQLINESDATA_BIN="${SQLINESDATA_BIN:-}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

# Output directory:
#   1. Honor SQLINES_OUT_DIR if explicitly set by the caller.
#   2. Otherwise nest under the per-run dir if the orchestrator exports RUN_DIR.
#   3. Otherwise read the active run dir from .migration_last_run (the
#      orchestrator writes the current run path there before launching the
#      Python runner that invokes this script -- see mariadb-migrator
#      around line 1474). This is the path that works today, since RUN_DIR
#      is currently a bash local in the orchestrator and is not exported.
#   4. Otherwise fall back to the legacy flat artifacts/sqldata path.
# This makes each run's sqldata artifacts (logs, failed-table list, ddl) live
# alongside the rest of the run, and stops new runs from overwriting prior ones.
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

# FK enforcement during load.
#
# sqldata loads tables in parallel, and TRUNCATEs each one before loading. In
# any FK-bearing schema, that ordering races: a child table loaded first leaves
# its parent's TRUNCATE rejected with error 1451 ("Cannot delete or update a
# parent row: a foreign key constraint fails"). The fix is to disable FK
# enforcement on the target for the duration of the load, then restore it.
#
# Default: disabled (opt-out). The trap below restores enforcement on any exit
# path -- success, set -e failure, Ctrl-C, or signal -- as long as the script's
# own process isn't SIGKILL'd.
#
# Override: set TWO_STEP_KEEP_FK_CHECKS=1 to leave enforcement on. Use only
# when you know the load can't violate FK ordering (e.g. single-table imports
# or schemas with no FKs).
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
  if command -v mariadb-mtk >/dev/null 2>&1; then
    SQLINESDATA_BIN="mariadb-mtk"
  elif command -v sqldata >/dev/null 2>&1; then
    SQLINESDATA_BIN="sqldata"
  fi
fi
if [[ -z "$SQLINESDATA_BIN" ]] || ! command -v "$SQLINESDATA_BIN" >/dev/null 2>&1; then
  echo "ERROR: mariadb-mtk binary not found (SQLINESDATA_BIN=$SQLINESDATA_BIN)."
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
echo "==> sqldata output: $SQLINES_OUT_DIR"

# Helper: run a one-shot SQL statement on the target as the admin user.
# Mirrors the connection style used in 11_two_step_schema.sh.
tgt_admin_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_admin_args+=( --ssl-verify-server-cert=OFF )
fi
target_sql() {
  local sql="$1"
  MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${tgt_admin_args[@]}" -e "$sql"
}

# FK toggle state + restore trap. Set fk_was_set=1 only AFTER the disable
# succeeds, so the trap only restores what it disabled (a privilege failure
# at disable time means no restore is needed).
fk_was_set=0
restore_fk_checks() {
  if [[ "$fk_was_set" -eq 1 ]]; then
    echo "==> Restoring target FOREIGN_KEY_CHECKS=1"
    # 'if !' keeps a restore failure from being clobbered by set -e during
    # trap execution; a manual recovery hint is printed instead of swallowing.
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

# Sqldata transfer-option behavior (-topt=none). Step 1 (11_two_step_schema.sh,
# via mariadb-dump --no-data) already created the target schema, so sqldata only
# loads row data and leaves the schema as-is. (Sqldata's own default is
# 'recreate', which DROP/CREATEs tables from MySQL source metadata and would
# discard the step-1 schema, so it is not used. SQLDATA_TOPT can override.)
SQLDATA_TOPT="${SQLDATA_TOPT:-none}"

# Large-table chunking and transient-error retries are native to sqldata and
# driven by sqldata.cfg defaults (see sqldata.cfg-example) — not by this script.
# Large tables are transferred as parallel chunks (large_tables_parallel /
# large_tables_rows); chunking requires an AUTO_INCREMENT column, so tables
# without one transfer as a single stream. The size-based threshold
# (large_tables_mb) is not yet supported for MySQL sources.
#
# sqldata does NOT resume across separate invocations: there is no cross-run
# mid-table continue. Within a single run, transient errors are retried
# automatically (restart_attempts). An interrupted two_step run is restarted by
# dropping the target database(s) and re-running from a clean target (the
# launcher's target pre-existence check enforces the clean-target requirement).
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  db_out_dir="$SQLINES_OUT_DIR/$db"
  mkdir -p "$db_out_dir"
#    -ss=6 \
  "$SQLINESDATA_BIN" \
    "-sd=mysql,${SRC_USER}/${SRC_PASS}@${SRC_HOST}:${SRC_PORT}/${db}" \
    "-td=mariadb,${TGT_USER}/${TGT_PASS}@${TGT_HOST}:${TGT_PORT}/${db}" \
    "-smap=${db}:${db}" \
    "-out=$db_out_dir" \
    "-log=$db_out_dir/sqldata.log" \
    "-t=${db}.*" \
    "-topt=${SQLDATA_TOPT}" \
    -constraints=no \
    -indexes=no \
    -triggers=no \
    -views=no \
    -procedures=no
done

echo "SQLines Data transfer completed."

# The data transfer is the hard gate. Under 'set -e', a failed sqldata load in
# the loop above aborts this script before reaching this point, so getting here
# means the transfer reported success for every selected database. Row-count
# validation below runs only on that success and is a REPORT, not a gate -- it
# never fails the run.
transfer_ok=1

# --- Row-count validation (source vs. target) -------------------------------
# For each database, sqldata's own validate command (-cmd=validate
# -vopt=rowcount) compares source vs. target row counts. The full per-db report
# is appended to BOTH the per-db load log (sqldata/<db>/sqldata.log) and the run
# log (run.log), and a concise verdict line is echoed for live feedback.
# sqldata's own working/log files for the validate pass go to a throwaway temp
# dir, so the only thing written under sqldata/ is the appended report itself.
#
#   MIGRATOR_SKIP_ROWCOUNT_VALIDATE=1  skip validation entirely
#   RUN_LOG=<path>                     override run-log path (default: the run
#                                      dir's run.log, i.e. the parent of the
#                                      sqldata out dir)
RUN_LOG="${RUN_LOG:-$(dirname "$SQLINES_OUT_DIR")/run.log}"

if [[ "${transfer_ok:-0}" != "1" ]]; then
  :   # transfer did not report success: nothing to validate
elif [[ "${MIGRATOR_SKIP_ROWCOUNT_VALIDATE:-0}" == "1" ]]; then
  echo "==> Row-count validation skipped (MIGRATOR_SKIP_ROWCOUNT_VALIDATE=1)"
else
  echo "==> Validating row counts (source vs. target); detail appended to per-db sqldata.log and $RUN_LOG"
  validate_mismatches=0

  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue

    db_out_dir="$SQLINES_OUT_DIR/$db"
    db_log="$db_out_dir/sqldata.log"
    mkdir -p "$db_out_dir"

    vtmp="$(mktemp)"
    vout="$(mktemp -d)"
    rc=0
    # Detail goes to a temp log (parsed, then folded into both logs); console
    # output suppressed to avoid double-logging.
    "$SQLINESDATA_BIN" \
      "-sd=mysql,${SRC_USER}/${SRC_PASS}@${SRC_HOST}:${SRC_PORT}/${db}" \
      "-td=mariadb,${TGT_USER}/${TGT_PASS}@${TGT_HOST}:${TGT_PORT}/${db}" \
      "-smap=${db}:${db}" \
      "-t=${db}.*" \
      "-out=$vout" \
      "-log=$vtmp" \
      -cmd=validate \
      -vopt=rowcount >/dev/null 2>&1 || rc=$?
      # Session count intentionally omitted: sqldata uses the -ss default from
      # sqldata.cfg (mirrors the load loop's commented-out -ss). Add "-ss=<n>"
      # above to override per run.

    # Parse sqldata's summary block rather than trusting rc alone:
    #   Tables: N (N compared, N failed) / Equal tables: N / Different tables: N
    different=$(grep -aE 'Different tables:' "$vtmp" 2>/dev/null | grep -oE '[0-9]+' | tail -n1 || true)
    failed=$(grep -aE 'compared,' "$vtmp" 2>/dev/null | tail -n1 | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' || true)
    different="${different:-0}"
    failed="${failed:-0}"

    if [[ "$rc" -ne 0 || "$different" -gt 0 || "$failed" -gt 0 ]]; then
      result="ROW-COUNT MISMATCH (rc=$rc, different=$different, failed=$failed)"
      validate_mismatches=$((validate_mismatches + 1))
    else
      equal_line=$(grep -aE 'Equal tables:' "$vtmp" 2>/dev/null | tail -n1 | tr -s ' ' | sed 's/^ *//' || true)
      result="row counts OK (${equal_line:-all tables equal})"
    fi

    # Fold the full per-db report + verdict into BOTH the per-db sqldata.log
    # (append, after the load log) and the run log. tee -a appends to the per-db
    # log; its passthrough is appended to run.log.
    {
      echo ""
      echo "===== row-count validation: ${db} ====="
      cat "$vtmp" 2>/dev/null || true
      echo "[${db}] ${result}"
    } | tee -a "$db_log" >> "$RUN_LOG" 2>/dev/null \
      || echo "WARNING: could not append validation report to $db_log and/or $RUN_LOG" >&2

    echo "    [${db}] ${result}"
    rm -rf "$vout"
    rm -f "$vtmp"
  done

  if [[ "$validate_mismatches" -gt 0 ]]; then
    echo "==> Row-count validation reported mismatches in ${validate_mismatches} database(s); see $RUN_LOG (transfer reported success; not gated here)"
  else
    echo "==> Row-count validation: all selected database(s) match"
  fi
fi
