#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# OFFLINE MIGRATION REMINDER
# This load reconstructs source data on the TARGET. While this load is running,
# do NOT direct application traffic at the target — the schema and data are in
# a partial/inconsistent state until the load completes successfully.
# Only point applications at the target after this script finishes cleanly.
##############################################################################

# ----- STAGED_PHASE self-skip -----
STAGED_PHASE="${STAGED_PHASE:-dump_and_load}"
if [[ "$STAGED_PHASE" == "dump_only" ]]; then
  echo "==> Skipping staged load (STAGED_PHASE=dump_only)"
  exit 0
fi

cat <<'BANNER'
==============================================================================
  STAGED LOAD — OFFLINE MIGRATION
  Do NOT direct application traffic to the target until this load completes.
  Schema and data are partial until the script reports success.
==============================================================================
BANNER

echo "==> Staged load (gunzip | pv | mariadb, per database)"

# ----- Tunables -----
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
PV_BIN="${PV_BIN:-pv}"
GZIP_BIN="${GZIP_BIN:-gzip}"
GUNZIP_BIN="${GUNZIP_BIN:-gunzip}"
STAGED_DUMP_DIR="${STAGED_DUMP_DIR:-}"
STAGED_PV="${STAGED_PV:-1}"
STAGED_VERIFY_SHA256="${STAGED_VERIFY_SHA256:-1}"

# Note: load is sequential by design.
#   - All loads target the same target server, so the bottleneck is target
#     write throughput (redo, doublewrite, log_buffer), not connection count.
#   - Sequential gives clear pv progress and predictable error attribution.
#   - The MariaDB 11.6 `mariadb-import --parallel=N` flag is the right tool
#     for parallel restore, but it requires the new --dir format produced by
#     `mariadb-dump --dir` — which we don't use here for the reasons noted in
#     25_staged_dump.sh (server-side write requirement). If you have headroom
#     on the target and want parallel load, set STAGED_LOAD_PARALLEL > 1.
STAGED_LOAD_PARALLEL="${STAGED_LOAD_PARALLEL:-1}"

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

# ----- Resolve dump dir -----
if [[ -z "$STAGED_DUMP_DIR" ]]; then
  if [[ -n "${RUN_DIR:-}" ]]; then
    STAGED_DUMP_DIR="${RUN_DIR}/dumps"
  else
    echo "ERROR: STAGED_DUMP_DIR not set and RUN_DIR not set." >&2
    exit 1
  fi
fi
if [[ ! -d "$STAGED_DUMP_DIR" ]]; then
  echo "ERROR: Dump directory does not exist: $STAGED_DUMP_DIR" >&2
  exit 1
fi

manifest="${STAGED_DUMP_DIR}/manifest.txt"
if [[ ! -r "$manifest" ]]; then
  echo "ERROR: Manifest not found or not readable: $manifest" >&2
  exit 1
fi

# Header sanity
hdr="$(head -1 "$manifest" 2>/dev/null || true)"
if [[ "$hdr" != "# staged-migration-manifest v1" ]]; then
  echo "ERROR: Manifest header missing or unrecognized in: $manifest" >&2
  echo "       Expected first line: '# staged-migration-manifest v1'" >&2
  echo "       Got: '$hdr'" >&2
  exit 1
fi

# Read compressed flag from manifest (don't infer from filename — manifest is
# the source of truth).
compressed_flag="$(awk -F': ' '/^# compressed:/ {print $2; exit}' "$manifest")"
compressed_flag="${compressed_flag:-1}"

# Optional: source version recorded at dump time (informational).
src_version_at_dump="$(awk -F': ' '/^# source_version:/ {print $2; exit}' "$manifest")"

# ----- Connection args -----
TGT_AUTH=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" )
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" --connect-timeout=5 --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

# pv availability
PV_OK=0
if [[ "$STAGED_PV" == "1" ]] && command -v "$PV_BIN" >/dev/null 2>&1; then
  PV_OK=1
fi

# ----- Read manifest into parallel arrays -----
# Note: row counts live in the manifest but the load doesn't use them; the
# finalize step (27_staged_finalize.sh) re-reads the manifest to compare
# against post-load COUNT(*) results.
declare -A MANIFEST_FILE MANIFEST_SHA MANIFEST_SIZE
DB_LIST=()
while IFS=$'\t' read -r dbname filename sha256 size_bytes _approx_rows; do
  [[ -z "$dbname" || "${dbname:0:1}" == "#" ]] && continue
  DB_LIST+=("$dbname")
  MANIFEST_FILE[$dbname]="$filename"
  MANIFEST_SHA[$dbname]="$sha256"
  MANIFEST_SIZE[$dbname]="$size_bytes"
done < "$manifest"

if [[ "${#DB_LIST[@]}" -eq 0 ]]; then
  echo "ERROR: Manifest contains no databases." >&2
  exit 1
fi

# ----- Effective parallelism + pv decision -----
effective_parallel="$STAGED_LOAD_PARALLEL"
[[ "$effective_parallel" -lt 1 ]] && effective_parallel=1
[[ "$effective_parallel" -gt "${#DB_LIST[@]}" ]] && effective_parallel="${#DB_LIST[@]}"

if [[ "$effective_parallel" -gt 1 && "$PV_OK" -eq 1 ]]; then
  echo "Note: pv disabled because STAGED_LOAD_PARALLEL=$effective_parallel (>1)."
  echo "      Set STAGED_LOAD_PARALLEL=1 for per-DB progress meter."
  PV_OK=0
fi

echo "Target           : $TGT_HOST:$TGT_PORT"
echo "Dump dir         : $STAGED_DUMP_DIR"
echo "Manifest         : $manifest"
echo "Source @ dump    : ${src_version_at_dump:-unknown}"
echo "Compressed dumps : $([[ "$compressed_flag" == "1" ]] && echo "yes" || echo "no")"
echo "Verify sha256    : $([[ "$STAGED_VERIFY_SHA256" == "1" ]] && echo "yes" || echo "no (skipped)")"
echo "Progress         : $([[ "$PV_OK" -eq 1 ]] && echo "pv" || echo "off")"
echo "Parallel         : $effective_parallel of ${#DB_LIST[@]} DB(s)"
echo "Databases        : ${DB_LIST[*]}"

LOG_DIR="${STAGED_DUMP_DIR}/.logs"
mkdir -p "$LOG_DIR"

# ----- Helpers -----
verify_sha256() {
  local file="$1" expected="$2"
  if [[ "$expected" == "unknown" || -z "$expected" ]]; then
    echo "    sha256: skipped (manifest entry is 'unknown')"
    return 0
  fi
  local actual
  actual="$(sha256sum "$file" 2>/dev/null | awk '{print $1}')"
  if [[ -z "$actual" ]]; then
    actual="$(shasum -a 256 "$file" 2>/dev/null | awk '{print $1}')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "ERROR: sha256 mismatch" >&2
    echo "  file:     $file" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   ${actual:-<could not compute>}" >&2
    return 1
  fi
  echo "    sha256: OK"
  return 0
}

# Load one database. Reads dump file, optionally gunzips, optionally pv,
# pipes to mariadb on target (directly or via SSH).
load_one_db() {
  local db="$1"
  local filename="${MANIFEST_FILE[$db]}"
  local expected_sha="${MANIFEST_SHA[$db]}"
  local size_bytes="${MANIFEST_SIZE[$db]}"
  local dump_file="${STAGED_DUMP_DIR}/${filename}"
  local PV_CMD=()
  local db_start db_elapsed size_h

  if [[ ! -r "$dump_file" ]]; then
    echo "ERROR: dump file not readable: $dump_file" >&2
    return 4
  fi

  if [[ "$STAGED_VERIFY_SHA256" == "1" ]]; then
    verify_sha256 "$dump_file" "$expected_sha" || return 5
  fi

  db_start=$(date +%s)

  if [[ "$PV_OK" -eq 1 ]]; then
    if [[ "${size_bytes:-0}" -gt 0 ]]; then
      PV_CMD=( "$PV_BIN" -pet -N "$db" -s "$size_bytes" )
    else
      PV_CMD=( "$PV_BIN" -pet -N "$db" )
    fi
  fi

  # Pipeline: cat file | (pv|cat) | (gunzip|cat) | mariadb [via ssh]
  # `cat $dump_file` is intentional — it lets the optional pv/gunzip stages
  # follow the same if/else cat idiom used elsewhere. The cat overhead is
  # negligible relative to the load itself.
  set -o pipefail
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local TGT_PASS_Q
    TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
    # shellcheck disable=SC2002
    if ! cat "$dump_file" \
          | if [[ "${#PV_CMD[@]}" -gt 0 ]]; then "${PV_CMD[@]}"; else cat; fi \
          | if [[ "$compressed_flag" == "1" ]]; then "$GUNZIP_BIN"; else cat; fi \
          | ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
              "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_AUTH[*]}"; then
      set +o pipefail
      echo "ERROR: load failed for database '$db' (via SSH)" >&2
      return 4
    fi
  else
    # shellcheck disable=SC2002
    if ! cat "$dump_file" \
          | if [[ "${#PV_CMD[@]}" -gt 0 ]]; then "${PV_CMD[@]}"; else cat; fi \
          | if [[ "$compressed_flag" == "1" ]]; then "$GUNZIP_BIN"; else cat; fi \
          | MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${TGT_AUTH[@]}"; then
      set +o pipefail
      echo "ERROR: load failed for database '$db'" >&2
      return 4
    fi
  fi
  set +o pipefail

  db_elapsed=$(( $(date +%s) - db_start ))
  size_h="$(numfmt --to=iec --suffix=B "$size_bytes" 2>/dev/null || echo "${size_bytes}B")"
  echo "    [done] $db: loaded ${size_h} in ${db_elapsed}s"
  return 0
}

# ----- Run loads (sequential or parallel) -----
total_start=$(date +%s)
declare -A JOB_LOG JOB_RC_FILE
failed_dbs=()

if [[ "$effective_parallel" -le 1 ]]; then
  for db in "${DB_LIST[@]}"; do
    echo ""
    echo "==> Loading: $db (file: ${MANIFEST_FILE[$db]})"
    if ! load_one_db "$db"; then
      failed_dbs+=("$db")
      break  # fail-fast on sequential
    fi
  done
else
  echo ""
  echo "==> Loading in parallel ($effective_parallel workers)..."
  running=0
  for db in "${DB_LIST[@]}"; do
    while [[ $running -ge $effective_parallel ]]; do
      wait -n || true
      running=$((running - 1))
    done
    log="${LOG_DIR}/${db}.load.log"
    rcfile="${LOG_DIR}/${db}.load.rc"
    JOB_LOG[$db]="$log"
    JOB_RC_FILE[$db]="$rcfile"
    (
      set +e
      load_one_db "$db" >"$log" 2>&1
      echo $? >"$rcfile"
    ) &
    running=$((running + 1))
  done
  wait

  for db in "${DB_LIST[@]}"; do
    rcfile="${JOB_RC_FILE[$db]:-}"
    log="${JOB_LOG[$db]:-}"
    if [[ -z "$rcfile" || ! -f "$rcfile" ]]; then
      failed_dbs+=("$db")
      echo "----- $db output (worker did not write rc file) -----"
      [[ -n "$log" && -f "$log" ]] && cat "$log"
      continue
    fi
    rc="$(cat "$rcfile")"
    if [[ "$rc" -ne 0 ]]; then
      failed_dbs+=("$db")
      echo "----- $db output (FAILED rc=$rc) -----"
      cat "$log"
    else
      grep -E '^    \[done\]' "$log" || true
    fi
  done
fi

# ----- Failure handling -----
if [[ "${#failed_dbs[@]}" -gt 0 ]]; then
  echo ""
  echo "ERROR: load failed for: ${failed_dbs[*]}" >&2
  echo "       Per-DB logs in: $LOG_DIR"
  echo "       Inspect target state and retry; partial schemas may exist."
  exit 4
fi

# ----- Summary -----
total_elapsed=$(( $(date +%s) - total_start ))
echo ""
echo "==> Staged load complete."
echo "    Databases : ${#DB_LIST[@]}"
echo "    Elapsed   : ${total_elapsed}s"
echo "    Workers   : $effective_parallel"
echo "    Target    : $TGT_HOST:$TGT_PORT"
echo ""
echo "Next: run finalize step for post-load sanity counts and manifest verification."
