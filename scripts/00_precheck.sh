#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# --- Gate: client option files must not alter output format --------------
# Runs before anything else: no connectivity or credentials required.
. "$ROOT/scripts/lib/gate_client_output.sh"

_gate_fail=0
while read -r _bin; do
  gate_client_defaults_clean "$_bin" || _gate_fail=1
done < <(printf '%s\n' "${MYSQL_BIN:-mysql}" \
                       mariadb \
                       "${MARIADB_DUMP_BIN:-mariadb-dump}" | sort -u)
if (( _gate_fail )); then
  echo "==> Precheck aborted: client output configuration is unsafe." >&2
  exit 1
fi
unset _gate_fail _bin
# -------------------------------------------------------------------------

# Prefer the MariaDB client; fall back to mysql. Explicit MYSQL_BIN wins.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"
# Use admin creds if provided; fall back to SRC_USER/SRC_PASS.
HOST="${SRC_HOST:-${HOST:-127.0.0.1}}"
PORT="${SRC_PORT:-${PORT:-3306}}"
USER="${SRC_ADMIN_USER:-${SRC_USER:-${USER:-root}}}"

# Prefer MYSQL_PWD from orchestrator config env.
PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-${PASS:-}}}"
if [[ -n "$PASS" && -z "${MYSQL_PWD:-}" ]]; then
  export MYSQL_PWD="$PASS"
fi

OUTDIR="${OUTDIR:-$ROOT/artifacts/precheck}"
mkdir -p "$OUTDIR"

# Operator-selected databases (comma-separated), for display scoping.
SEL_DBS="${SRC_DBS:-${SRC_DB:-}}"

# First TSV column is a schema name. Keep in sync with the
# _filter_by_schema() call sites in orchestrator/checks.py.
SCHEMA_SCOPED=(
  json_columns schema_charsets mysql8_collations mysql8_column_collations
  partitioned_tables functional_indexes functional_defaults invisible_columns
  check_constraints gis_srid_usage fk_name_lengths trigger_order
  generated_columns
)

CHECKS_DIR="${CHECKS_DIR:-$ROOT/sql/checks}"
COMBINED_OUT="$OUTDIR/precheck.out"
COMBINED_ERR="$OUTDIR/precheck.err"

MYSQL_AUTH=( -h"$HOST" -P"$PORT" -u"$USER" )
MYSQL_OPTS=( --batch --skip-column-names --raw )

echo "== Precheck runner ==" | tee "$COMBINED_OUT"
echo "Host: $HOST  Port: $PORT  User: $USER" | tee -a "$COMBINED_OUT"
echo "Checks dir: $CHECKS_DIR" | tee -a "$COMBINED_OUT"
echo "Outdir: $OUTDIR" | tee -a "$COMBINED_OUT"
echo "" | tee -a "$COMBINED_OUT"
echo "Databases: ${SEL_DBS:-<all - no selection in environment>}" | tee -a "$COMBINED_OUT"

: > "$COMBINED_ERR"

# Config checker reminder (mariadb-migrate-config-file)
INTERACTIVE="${INTERACTIVE:-}"
if [[ -z "$INTERACTIVE" ]]; then
  if [[ -t 0 ]]; then
    INTERACTIVE=1
  else
    INTERACTIVE=0
  fi
fi

CFG_BIN="${MARIADB_MIGRATE_CONFIG_FILE_BIN:-mariadb-migrate-config-file}"
CFG_ARGS="${MARIADB_MIGRATE_CONFIG_FILE_ARGS:---print}"
if command -v "$CFG_BIN" >/dev/null 2>&1; then
  echo "Config checker available: $CFG_BIN" | tee -a "$COMBINED_OUT"
  if [[ "$INTERACTIVE" == "1" ]]; then
    read -r -p "Run config checker now? [y/N] " _ans
    if [[ "$_ans" =~ ^[Yy]$ ]]; then
      "$CFG_BIN" $CFG_ARGS | tee -a "$COMBINED_OUT" || true
    else
      echo "Skipped config checker." | tee -a "$COMBINED_OUT"
    fi
  else
    echo "NOTE: Run '$CFG_BIN $CFG_ARGS' to check my.cnf compatibility." | tee -a "$COMBINED_OUT"
  fi
else
  echo "NOTE: mariadb-migrate-config-file not found; skipping config check." | tee -a "$COMBINED_OUT"
fi

# IMPORTANT: run each sql/checks/*.sql file individually so each output maps 1:1 to a TSV.
SQL_FILES=(
  "$CHECKS_DIR/mysql_version.sql"
  "$CHECKS_DIR/innodb_settings.sql"
  "$CHECKS_DIR/auth_plugins.sql"
  "$CHECKS_DIR/json_columns.sql"
  "$CHECKS_DIR/compression_encryption.sql"
  "$CHECKS_DIR/engines_summary.sql"
  "$CHECKS_DIR/schema_sizes.sql"
  "$CHECKS_DIR/schema_charsets.sql"
  "$CHECKS_DIR/mysql8_collations.sql"
  "$CHECKS_DIR/mysql8_column_collations.sql"
  "$CHECKS_DIR/sql_mode.sql"
  "$CHECKS_DIR/definers_inventory.sql"
  "$CHECKS_DIR/partitioned_tables.sql"
  "$CHECKS_DIR/active_plugins.sql"
  "$CHECKS_DIR/functional_indexes.sql"
  "$CHECKS_DIR/functional_defaults.sql"
  "$CHECKS_DIR/invisible_columns.sql"
  "$CHECKS_DIR/check_constraints.sql"
  "$CHECKS_DIR/partial_revokes.sql"
  "$CHECKS_DIR/gis_srid_usage.sql"
  "$CHECKS_DIR/resource_groups.sql"
  "$CHECKS_DIR/xplugin_status.sql"
  "$CHECKS_DIR/fk_name_lengths.sql"
  "$CHECKS_DIR/trigger_order.sql"
  "$CHECKS_DIR/mysql_roles.sql"
  "$CHECKS_DIR/generated_columns.sql"
  "$CHECKS_DIR/server_defaults.sql"
  "$CHECKS_DIR/view_routine_bodies.sql"
)

_view_tmp="$(mktemp)"
trap 'rm -f "$_view_tmp"' EXIT

# Write the display view of a TSV to $_view_tmp. Schema-scoped checks are
# filtered to the selected databases; everything else passes through. An
# empty selection means "unknown scope" and is unfiltered, matching
# _filter_by_schema() in orchestrator/checks.py.
_display_view() {
  local file="$1" base="$2" s
  if [[ -n "$SEL_DBS" ]]; then
    for s in "${SCHEMA_SCOPED[@]}"; do
      if [[ "$s" == "$base" ]]; then
        awk -F'\t' -v dbs="$SEL_DBS" '
          BEGIN { n = split(dbs, a, ",")
                  for (i = 1; i <= n; i++) { gsub(/^[ \t]+|[ \t]+$/, "", a[i]); sel[a[i]] = 1 } }
          $1 in sel
        ' "$file" > "$_view_tmp"
        return
      fi
    done
  fi
  cat "$file" > "$_view_tmp"
}

for f in "${SQL_FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: Missing SQL file: $f" | tee -a "$COMBINED_OUT"
    exit 2
  fi

  base="$(basename "$f" .sql)"
  out="$OUTDIR/${base}.tsv"
  echo "---- $base ----" | tee -a "$COMBINED_OUT"

  if "$MYSQL_BIN" "${MYSQL_AUTH[@]}" "${MYSQL_OPTS[@]}" < "$f" > "$out" 2>>"$COMBINED_ERR"; then

    if [[ -s "$out" ]]; then
      _display_view "$out" "$base"
      if [[ -s "$_view_tmp" ]]; then
        head -n 50 "$_view_tmp" | tee -a "$COMBINED_OUT"
        if [[ $(wc -l < "$_view_tmp") -gt 50 ]]; then
          echo "... (truncated; full output in $out)" | tee -a "$COMBINED_OUT"
        fi
      else
        echo "(no rows in selected databases)" | tee -a "$COMBINED_OUT"
      fi
    else
      echo "(no rows)" | tee -a "$COMBINED_OUT"
    fi
  else
    echo "ERROR: mysql failed for $base (see $COMBINED_ERR)" | tee -a "$COMBINED_OUT"
    exit 3
  fi

  echo "" | tee -a "$COMBINED_OUT"
done

# Hard gate quick check (optional, but useful)
ift_file="$OUTDIR/innodb_settings.tsv"
if [[ -s "$ift_file" ]]; then
  IFT="$(head -n 1 "$ift_file" | awk -F'\t' '{print $1}')"
  if [[ "$IFT" != "1" ]]; then
    echo "GATE FAIL: innodb_file_per_table=$IFT (must be 1)" | tee -a "$COMBINED_OUT"
    exit 4
  fi
fi

echo "Precheck complete." | tee -a "$COMBINED_OUT"
echo "TSV outputs: $OUTDIR/*.tsv" | tee -a "$COMBINED_OUT"
