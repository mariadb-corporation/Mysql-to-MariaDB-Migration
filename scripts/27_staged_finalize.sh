#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# OFFLINE MIGRATION REMINDER
# This step verifies that databases loaded by 26_staged_load.sh are present
# on the target and have row counts in a reasonable ballpark relative to the
# source-side estimates recorded in the manifest. Hard failures here mean a
# DB is missing entirely or has no tables — i.e. the load did not complete.
# Variance in row counts is reported but not gated, because both sides use
# InnoDB sampled approximations (information_schema.tables.table_rows), not
# exact COUNT(*) results. File integrity is verified separately by the
# checksum check in 26_staged_load.sh during load.
##############################################################################

# ----- STAGED_PHASE self-skip -----
STAGED_PHASE="${STAGED_PHASE:-dump_and_load}"
if [[ "$STAGED_PHASE" == "dump_only" ]]; then
  echo "==> Skipping staged finalize (STAGED_PHASE=dump_only)"
  exit 0
fi

echo "==> Staged finalize (post-load sanity counts and manifest verification)"

# ----- Tunables -----
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
STAGED_DUMP_DIR="${STAGED_DUMP_DIR:-}"

# STAGED_FINALIZE_VARIANCE_PCT is the absolute-variance percentage above which
# a per-DB warning is emitted. InnoDB row-count sampling routinely produces
# 5-15% variance; we default to 50% as the "something is probably wrong"
# threshold. Lower it (e.g. =20) for stricter verification.
#
# Backward compatibility: STAGED_FINALIZE_DRIFT_PCT was the original name
# (v1.1.0-beta). Honor it if set, but emit a deprecation notice and prefer
# the new name.
if [[ -n "${STAGED_FINALIZE_DRIFT_PCT:-}" && -z "${STAGED_FINALIZE_VARIANCE_PCT:-}" ]]; then
  echo "Note: STAGED_FINALIZE_DRIFT_PCT is deprecated; use STAGED_FINALIZE_VARIANCE_PCT instead." >&2
  STAGED_FINALIZE_VARIANCE_PCT="$STAGED_FINALIZE_DRIFT_PCT"
fi
STAGED_FINALIZE_VARIANCE_PCT="${STAGED_FINALIZE_VARIANCE_PCT:-50}"

# ----- Target vars -----
TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"

# ----- Validate -----
if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_USER, TGT_PASS." >&2
  exit 1
fi

# ----- Resolve dump dir + manifest -----
if [[ -z "$STAGED_DUMP_DIR" ]]; then
  if [[ -n "${RUN_DIR:-}" ]]; then
    STAGED_DUMP_DIR="${RUN_DIR}/dumps"
  else
    echo "ERROR: STAGED_DUMP_DIR not set and RUN_DIR not set." >&2
    exit 1
  fi
fi

manifest="${STAGED_DUMP_DIR}/manifest.txt"
if [[ ! -r "$manifest" ]]; then
  echo "ERROR: Manifest not found or not readable: $manifest" >&2
  exit 1
fi

hdr="$(head -1 "$manifest" 2>/dev/null || true)"
if [[ "$hdr" != "# staged-migration-manifest v1" ]]; then
  echo "ERROR: Manifest header missing or unrecognized in: $manifest" >&2
  echo "       Expected first line: '# staged-migration-manifest v1'" >&2
  echo "       Got: '$hdr'" >&2
  exit 1
fi

# ----- Connection args -----
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" --connect-timeout=5 --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

# ----- Helpers -----
sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

run_target_sql() {
  # Runs a query against the target and emits the (header-stripped) output.
  # Uses string-literal queries only — identifiers are not interpolated, so
  # no backtick-escaping headaches across the SSH boundary.
  local q="$1"
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local pwq
    pwq="$(printf '%q' "$TGT_PASS")"
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$pwq ${MARIADB_BIN} -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --batch --skip-column-names -e \"$q\""
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -u"$TGT_USER" -e "$q"
  fi
}

target_db_exists() {
  local db="$1"
  local db_esc; db_esc="$(sql_escape "$db")"
  local q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
  local out
  out="$(run_target_sql "$q" 2>/dev/null | head -1 | tr -dc '0-9')"
  [[ "${out:-0}" -gt 0 ]]
}

target_db_table_count() {
  local db="$1"
  local db_esc; db_esc="$(sql_escape "$db")"
  local q="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='${db_esc}' AND table_type='BASE TABLE';"
  run_target_sql "$q" 2>/dev/null | head -1 | tr -dc '0-9' || echo 0
}

target_db_approx_rows() {
  local db="$1"
  local db_esc; db_esc="$(sql_escape "$db")"
  local q="SELECT COALESCE(SUM(table_rows),0) FROM information_schema.tables WHERE table_schema='${db_esc}' AND table_type='BASE TABLE';"
  run_target_sql "$q" 2>/dev/null | head -1 | tr -dc '0-9' || echo 0
}

# Format a signed variance percentage with one decimal place.
# Args: signed_diff, manifest_rows
# Emits e.g. "+0.6%", "-1.2%", "0.0%"
format_variance_pct() {
  local d="$1" m="$2"
  awk -v d="$d" -v m="$m" 'BEGIN{
    if (m+0 == 0) { print "0.0%"; exit }
    v = (d * 100.0) / m
    if (v > 0) printf "+%.1f%%", v
    else       printf "%.1f%%", v
  }'
}

# Returns "1" if absolute variance percentage exceeds the threshold,
# otherwise "0". Uses floating-point comparison to catch e.g. 50.7 > 50.
variance_exceeds_threshold() {
  local abs_d="$1" m="$2" t="$3"
  awk -v a="$abs_d" -v m="$m" -v t="$t" 'BEGIN{
    if (m+0 == 0) { print 0; exit }
    v = (a * 100.0) / m
    if (v > t) print 1
    else       print 0
  }'
}

# ----- Connectivity check -----
echo "Checking target connectivity..."
if ! run_target_sql "SELECT 1;" >/dev/null 2>&1; then
  echo "ERROR: target connectivity failed: ${TGT_USER}@${TGT_HOST}:${TGT_PORT}" >&2
  exit 4
fi

echo "Manifest           : $manifest"
echo "Target             : $TGT_HOST:$TGT_PORT"
echo "Variance threshold : ${STAGED_FINALIZE_VARIANCE_PCT}% (warn-only)"

# ----- Read manifest into MANIFEST_ROWS -----
declare -A MANIFEST_ROWS
DB_LIST=()
while IFS=$'\t' read -r dbname _filename _sha256 _size_bytes approx_rows; do
  [[ -z "$dbname" || "${dbname:0:1}" == "#" ]] && continue
  DB_LIST+=("$dbname")
  MANIFEST_ROWS[$dbname]="$approx_rows"
done < "$manifest"

if [[ "${#DB_LIST[@]}" -eq 0 ]]; then
  echo "ERROR: manifest contains no databases" >&2
  exit 1
fi

# ----- Per-DB verification -----
missing_dbs=()
empty_dbs=()
warnings=()

echo ""
echo "Note: Row counts shown below are InnoDB sampled estimates from"
echo "      information_schema.tables, not exact COUNT(*) results. Small"
echo "      variance between source and target estimates is expected and"
echo "      does NOT indicate data loss. The checksum check performed during"
echo "      load (see 26_staged_load.sh) is the authoritative file-integrity"
echo "      verification."
echo ""
echo "Per-DB verification:"
printf "  %-24s %-9s %-7s %-15s %-15s %-8s\n" "Database" "Exists" "Tables" "Source rows" "Target rows" "Variance"
printf "  %-24s %-9s %-7s %-15s %-15s %-8s\n" "------------------------" "---------" "-------" "---------------" "---------------" "--------"

for db in "${DB_LIST[@]}"; do
  manifest_rows="${MANIFEST_ROWS[$db]:-0}"

  if ! target_db_exists "$db"; then
    missing_dbs+=("$db")
    printf "  %-24s %-9s %-7s %-15s %-15s %-8s\n" \
      "$db" "MISSING" "-" "~$manifest_rows" "-" "-"
    continue
  fi

  table_count="$(target_db_table_count "$db")"
  table_count="${table_count:-0}"
  target_rows="$(target_db_approx_rows "$db")"
  target_rows="${target_rows:-0}"

  # Hard fail: DB exists but has zero tables AND manifest had data.
  # (A truly empty source DB is theoretically valid; tables=0 with manifest
  # rows>0 is not.)
  if [[ "$table_count" -eq 0 && "$manifest_rows" -gt 0 ]]; then
    empty_dbs+=("$db")
    printf "  %-24s %-9s %-7s %-15s %-15s %-8s\n" \
      "$db" "OK" "0" "~$manifest_rows" "~$target_rows" "EMPTY-LOAD"
    continue
  fi

  # Variance % against manifest (signed, one decimal place).
  if [[ "$manifest_rows" -gt 0 ]]; then
    diff=$(( target_rows - manifest_rows ))
    abs_diff=${diff#-}
    variance_display="$(format_variance_pct "$diff" "$manifest_rows")"
    warn_trigger="$(variance_exceeds_threshold "$abs_diff" "$manifest_rows" "$STAGED_FINALIZE_VARIANCE_PCT")"
  else
    variance_display="0.0%"
    warn_trigger=0
  fi

  variance_marker=""
  if [[ "$manifest_rows" -gt 0 && "$target_rows" -eq 0 ]]; then
    variance_marker=" ← target empty"
    warnings+=("$db: source ~${manifest_rows} rows, target reports 0")
  elif [[ "$warn_trigger" -eq 1 ]]; then
    variance_marker=" ← >${STAGED_FINALIZE_VARIANCE_PCT}%"
    warnings+=("$db: variance ${variance_display} (source=~${manifest_rows}, target=~${target_rows})")
  fi

  printf "  %-24s %-9s %-7s %-15s %-15s %-8s%s\n" \
    "$db" "OK" "$table_count" "~$manifest_rows" "~$target_rows" "$variance_display" "$variance_marker"
done

echo ""

# ----- Hard failures -----
fail=0
if [[ "${#missing_dbs[@]}" -gt 0 ]]; then
  echo "ERROR: Manifest databases missing on target: ${missing_dbs[*]}" >&2
  echo "       The load did not complete for these databases." >&2
  fail=1
fi
if [[ "${#empty_dbs[@]}" -gt 0 ]]; then
  echo "ERROR: Databases on target have no tables but manifest had data: ${empty_dbs[*]}" >&2
  echo "       The load created the schema but did not load tables." >&2
  fail=1
fi
if [[ $fail -eq 1 ]]; then
  exit 5
fi

# ----- Soft warnings (non-fatal) -----
if [[ "${#warnings[@]}" -gt 0 ]]; then
  echo "WARNINGS (non-fatal — informational only):"
  for w in "${warnings[@]}"; do
    echo "  - $w"
  done
  echo ""
  echo "  Notes on variance:"
  echo "  - Both source and target row counts use information_schema, which"
  echo "    provides SAMPLED ESTIMATES for InnoDB tables. Variance up to ~30%"
  echo "    can be normal — both numbers being shown are approximations of"
  echo "    the same underlying data."
  echo "  - For authoritative row-count verification of a specific table, run:"
  echo "      SELECT COUNT(*) FROM <db>.<table>;"
  echo "    against the tables you care about."
  echo "  - File-level integrity was verified at load time via per-database"
  echo "    checksum comparison against the manifest."
fi

echo ""
echo "==> Staged finalize complete."
echo "    Databases verified: ${#DB_LIST[@]}"
[[ "${#warnings[@]}" -gt 0 ]] && echo "    Warnings           : ${#warnings[@]} (see above)"
echo "    Target             : $TGT_HOST:$TGT_PORT"
