#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# scripts/28_analyze_target.sh
#
# Post-load ANALYZE TABLE on the TARGET.
#
# After a dump/restore the target's optimizer statistics are empty or stale,
# which can produce poor query plans until something refreshes them. This phase
# runs ANALYZE TABLE on every base table in the migrated database(s) so the
# MariaDB optimizer has accurate cardinality/index statistics immediately.
#
# Wiring (step_map.yaml): runs AFTER the load step in one_step, two_step, and
# staged. Not wired into binlog (replication keeps mutating tables, so a
# post-snapshot analyze is meaningless) or inplace/replace_slave (no fresh load).
#
# Self-skip (house pattern — gate inside the script, same as 09 / 26):
#   - ANALYZE_TARGET != 1      -> operator answered "no" to the prompt.
#   - STAGED_PHASE == dump_only -> no load happened on this host.
#
# Failure semantics:
#   - Cannot connect to target, or tool missing  -> FATAL (exit non-zero).
#   - Individual table reports an error           -> recorded in the report,
#       NON-fatal. A stats refresh failing on one table must not mark an
#       otherwise-successful data migration as failed.
#
# Report: written to stdout AND ${RUN_DIR:-artifacts}/analyze_target_report.txt,
# mirroring 09_migrate_app_users.sh.
#
# Client convention (item 2): canonical 'mariadb' binary + explicit
# --ssl-verify-server-cert=0. The binary name avoids the "Deprecated program
# name" banner; the explicit flag makes non-verification a stated operator
# choice so the client does not auto-disable-and-warn on a MYSQL_PWD login.
# Result: no client noise to mask in the first place.
# =============================================================================

# ----- Self-skip gates -----
ANALYZE_TARGET="${ANALYZE_TARGET:-1}"
if [[ "$ANALYZE_TARGET" != "1" ]]; then
  echo "==> Skipping post-load ANALYZE TABLE (ANALYZE_TARGET!=1)."
  exit 0
fi

STAGED_PHASE="${STAGED_PHASE:-dump_and_load}"
if [[ "$STAGED_PHASE" == "dump_only" ]]; then
  echo "==> Skipping post-load ANALYZE TABLE (STAGED_PHASE=dump_only; no load on this host)."
  exit 0
fi

echo "==> Post-load ANALYZE TABLE on target (refresh optimizer statistics)"

# ----- Binaries -----
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

# ----- Target connection -----
trim_ws() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf "%s" "$s"
}

TGT_HOST="$(trim_ws "${TGT_HOST:-}")"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="$(trim_ws "${TGT_ADMIN_USER:-${TGT_USER:-}}")"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_SSH_HOST="$(trim_ws "${TGT_SSH_HOST:-}")"
TGT_SSH_USER="${TGT_ADMIN_SSH_USER:-${TGT_SSH_USER:-root}}"
TGT_SSH_OPTS="${TGT_ADMIN_SSH_OPTS:-${TGT_SSH_OPTS:-}}"

STAGED_DUMP_DIR="${STAGED_DUMP_DIR:-}"
SRC_DB="$(trim_ws "${SRC_DB:-}")"
SRC_DBS="$(trim_ws "${SRC_DBS:-}")"

REPORT_FILE="${ANALYZE_TARGET_REPORT:-${RUN_DIR:-artifacts}/analyze_target_report.txt}"

if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_ADMIN_USER, TGT_ADMIN_PASS." >&2
  exit 1
fi

mkdir -p "$(dirname "$REPORT_FILE")" 2>/dev/null || true

# Explicit SSL flag only for the mariadb client family.
TGT_SSL_ARGS=()
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  TGT_SSL_ARGS=( --ssl-verify-server-cert=0 )
fi

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

# Run SQL on the target and print result rows (tab-separated, no headers).
# Mirrors the connection handling in 09_migrate_app_users.sh (TCP, or via SSH
# when the target is reached through a bastion/admin host).
run_tgt() {
  local sql="$1"
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local pass_q
    pass_q="$(printf '%q' "$TGT_PASS")"
    printf '%s\n' "$sql" | ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$pass_q ${MARIADB_BIN} ${TGT_SSL_ARGS[*]} -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --batch --skip-column-names"
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${TGT_SSL_ARGS[@]}" \
      -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" --batch --skip-column-names -e "$sql"
  fi
}

# ----- Resolve the database list -----
# Priority: explicit SRC_DBS / SRC_DB (one_step, two_step, staged dump_and_load),
# else the staged manifest (staged load_only, where source vars may be absent).
DB_LIST=()
if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a _raw <<< "$SRC_DBS"
  for d in "${_raw[@]}"; do
    d="${d// /}"
    [[ -n "$d" ]] && DB_LIST+=("$d")
  done
elif [[ -n "$SRC_DB" ]]; then
  DB_LIST=("$SRC_DB")
else
  if [[ -z "$STAGED_DUMP_DIR" && -n "${RUN_DIR:-}" ]]; then
    STAGED_DUMP_DIR="${RUN_DIR}/dumps"
  fi
  manifest="${STAGED_DUMP_DIR}/manifest.txt"
  if [[ -r "$manifest" ]]; then
    while IFS=$'\t' read -r dbname _rest; do
      [[ -z "$dbname" || "${dbname:0:1}" == "#" ]] && continue
      DB_LIST+=("$dbname")
    done < "$manifest"
  fi
fi

if [[ "${#DB_LIST[@]}" -eq 0 ]]; then
  echo "ERROR: No databases to analyze. Set SRC_DB/SRC_DBS, or provide a manifest in STAGED_DUMP_DIR." >&2
  exit 1
fi

# ----- Connectivity probe (the fatal-failure gate) -----
# If we cannot even run SELECT 1, the credentials/host are wrong; that is a real
# failure and should stop the run. Per-table errors below are treated softly.
if ! run_tgt "SELECT 1;" >/dev/null 2>&1; then
  echo "ERROR: cannot connect to target ${TGT_HOST}:${TGT_PORT} as '${TGT_USER}'." >&2
  echo "       ANALYZE skipped; fix target connectivity and re-run this phase." >&2
  exit 2
fi

echo "Target    : ${TGT_HOST}:${TGT_PORT}"
echo "Databases : ${DB_LIST[*]}"
echo "Client    : ${MARIADB_BIN} (ANALYZE TABLE, base tables only)"
echo ""

# ----- Analyze each base table -----
# Base tables only: views and the system schemas are excluded by the
# information_schema filter, so ANALYZE is never issued against a view.
ok_count=0
note_count=0
err_count=0
tbl_total=0
err_lines=""
results=""
total_start=$(date +%s)

for db in "${DB_LIST[@]}"; do
  db_esc="$(sql_escape "$db")"
  echo "==> Analyzing database: $db"
  db_start=$(date +%s)

  tables="$(run_tgt "
    SELECT table_name
      FROM information_schema.tables
     WHERE table_schema='${db_esc}'
       AND table_type='BASE TABLE';" 2>/dev/null || true)"

  if [[ -z "$tables" ]]; then
    echo "    (no base tables found in '$db'; nothing to analyze)"
    results+=$'\n=== '"$db"$' ===\n(no base tables)\n'
    continue
  fi

  # Build one multi-statement batch so the whole database is analyzed over a
  # single connection rather than one connection per table.
  analyze_sql=""
  db_tbl_count=0
  while IFS= read -r tbl; do
    [[ -z "$tbl" ]] && continue
    analyze_sql+="ANALYZE TABLE \`${db}\`.\`${tbl}\`;"$'\n'
    db_tbl_count=$((db_tbl_count + 1))
  done <<< "$tables"

  out="$(run_tgt "$analyze_sql" 2>&1 || true)"
  results+=$'\n=== '"$db"$' ('"$db_tbl_count"$' tables) ===\n'"$out"$'\n'

  # ANALYZE TABLE result rows (batch, no headers) are tab-separated:
  #   <db.table>\t<op=analyze>\t<msg_type>\t<msg_text>
  # msg_type 'status' + msg_text 'OK' is success; 'note' is informational
  # (e.g. "Table is already up to date"); 'Error'/'error' is a real problem.
  while IFS=$'\t' read -r t_name _ t_msgtype t_msgtext; do
    [[ -z "$t_name" ]] && continue
    tbl_total=$((tbl_total + 1))
    case "$t_msgtype" in
      status)
        if [[ "$t_msgtext" == "OK" ]]; then
          ok_count=$((ok_count + 1))
        else
          note_count=$((note_count + 1))
        fi
        ;;
      note|Note)
        note_count=$((note_count + 1))
        ;;
      Error|error)
        err_count=$((err_count + 1))
        err_lines+="${t_name}: ${t_msgtext}"$'\n'
        ;;
      *)
        # Unrecognized line (blank op, continuation, etc.) — count nothing.
        tbl_total=$((tbl_total - 1))
        ;;
    esac
  done <<< "$out"

  db_elapsed=$(( $(date +%s) - db_start ))
  echo "    [done] $db: $db_tbl_count table(s) in ${db_elapsed}s"
done

total_elapsed=$(( $(date +%s) - total_start ))

# ----- Summary report (stdout + REPORT_FILE) -----
overall="PASS"
[[ "$err_count" -gt 0 ]] && overall="PASS (with table errors — see below; data load was not affected)"

report=$(cat <<EOF
================================================================================
Post-load ANALYZE TABLE summary
================================================================================
Target               : ${TGT_HOST}:${TGT_PORT}
Databases analyzed   : ${#DB_LIST[@]}  (${DB_LIST[*]})
Tables analyzed      : ${tbl_total}
  status OK          : ${ok_count}
  notes              : ${note_count}
  errors             : ${err_count}
Elapsed              : ${total_elapsed}s
Overall              : ${overall}
EOF
)

if [[ -n "$err_lines" ]]; then
  report+=$'\n--- Tables that reported an ANALYZE error ---\n'"$err_lines"
  report+=$'    These are statistics-refresh errors only; the migrated data is\n'
  report+=$'    already loaded. Investigate individually if query plans are poor.\n'
fi

report+=$'\n--- Raw ANALYZE TABLE output ---\n'"$results"
report+=$'\n================================================================================\n'

printf '%s' "$report"

if printf '%s' "$report" > "$REPORT_FILE" 2>/dev/null; then
  echo "ANALYZE report written to: $REPORT_FILE"
else
  echo "NOTE: could not write ANALYZE report to $REPORT_FILE; stdout copy above is the record."
fi

echo "Post-load ANALYZE TABLE completed."
