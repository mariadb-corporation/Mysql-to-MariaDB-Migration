#!/usr/bin/env bash
set -euo pipefail

##############################################################################
# OFFLINE MIGRATION REMINDER
# This is an OFFLINE dump. Writes to the source database DURING the dump are
# not captured and may be lost on the target. For a consistent cutover:
#   1. Stop application traffic to the source BEFORE starting this run.
#   2. Confirm no writers remain (check active sessions, replication, jobs).
#   3. Resume traffic on the TARGET only after the load completes successfully.
# If downtime is not acceptable, use 'binlog' mode instead of 'staged'.
##############################################################################

# ----- STAGED_PHASE self-skip -----
STAGED_PHASE="${STAGED_PHASE:-dump_and_load}"
if [[ "$STAGED_PHASE" == "load_only" ]]; then
  echo "==> Skipping staged dump (STAGED_PHASE=load_only)"
  exit 0
fi

cat <<'BANNER'
==============================================================================
  STAGED DUMP — OFFLINE MIGRATION
  Source writes during this dump WILL NOT be captured. Stop application
  traffic to the source before proceeding for a consistent cutover.
  (Use 'binlog' mode if you need online replication.)
==============================================================================
BANNER

echo "==> Staged dump (mariadb-dump | pv | gzip > file, per database)"

# ----- Tunables -----
MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
# Prefer the MariaDB client; fall back to mysql. Explicit MYSQL_BIN wins.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"
PV_BIN="${PV_BIN:-pv}"
GZIP_BIN="${GZIP_BIN:-gzip}"
STAGED_DUMP_DIR="${STAGED_DUMP_DIR:-}"
STAGED_COMPRESS="${STAGED_COMPRESS:-1}"
STAGED_PV="${STAGED_PV:-1}"

# Parallel dump default. Per the MariaDB 11.6 blog on logical-dump enhancements,
# --parallel=4 gives ~4x speedup on warm-cache OLTP workloads. We can't use the
# server-side --parallel/--dir flags directly because they require:
#   (a) the dump path to exist on the SOURCE host's filesystem, and
#   (b) FILE privilege granted to the migration user.
# Neither holds for a remote-source migration. Instead we parallelize at the
# bash level: N concurrent `mariadb-dump --databases <one_db>` jobs throttled
# by a job pool. Same wall-clock benefit, works against any MySQL/MariaDB
# source, no FILE privilege needed.
STAGED_PARALLEL="${STAGED_PARALLEL:-4}"

# ----- Source vars -----
SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"
SRC_SSL_MODE="${SRC_SSL_MODE:-}"
STRIP_DEFINERS="${STRIP_DEFINERS:-1}"

# ----- Validate -----
if [[ -z "$SRC_HOST" || -z "$SRC_USER" || -z "$SRC_PASS" || ( -z "$SRC_DB" && -z "$SRC_DBS" ) ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_USER, SRC_PASS, and SRC_DB or SRC_DBS." >&2
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
mkdir -p "$STAGED_DUMP_DIR"
LOG_DIR="${STAGED_DUMP_DIR}/.logs"
mkdir -p "$LOG_DIR"

# ----- DB list -----
if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi
CLEAN_DB_LIST=()
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -n "$db" ]] && CLEAN_DB_LIST+=("$db")
done
DB_LIST=("${CLEAN_DB_LIST[@]}")
db_count=${#DB_LIST[@]}
if [[ "$db_count" -eq 0 ]]; then
  echo "ERROR: empty database list." >&2
  exit 1
fi

# ----- Pick dump binary -----
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

dump_basename="$(basename "$MARIADB_DUMP_BIN")"
dump_is_mysql=0
if [[ "$dump_basename" == "mysqldump" ]] && \
   ! "$MARIADB_DUMP_BIN" --version 2>/dev/null | grep -iq 'mariadb'; then
  dump_is_mysql=1
fi

# ----- Source version -----
src_admin_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" --batch --skip-column-names )
src_version_full="$(MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1 || true)"

# ----- Effective parallelism + pv decision -----
effective_parallel="$STAGED_PARALLEL"
[[ "$effective_parallel" -lt 1 ]] && effective_parallel=1
[[ "$effective_parallel" -gt "$db_count" ]] && effective_parallel="$db_count"

PV_OK=0
if [[ "$STAGED_PV" == "1" ]]; then
  if command -v "$PV_BIN" >/dev/null 2>&1; then
    # pv runs even when parallel>1: each pv instance is tagged with its DB
    # name (-N) and uses line-buffered interval output (-f -i 10), so the
    # parallel pv lines interleave cleanly in the log instead of producing
    # the garbled progress-bar output that motivated the old gate. The
    # interleaved view (tag + bytes/percent per DB every 10s) is actually
    # more useful than the sequential bar for multi-DB dumps.
    PV_OK=1
  else
    echo "Note: pv not found; running without progress meter."
  fi
fi

echo "Source MySQL : ${src_version_full:-unknown}"
echo "Dump tool    : $MARIADB_DUMP_BIN"
echo "Compress     : $([[ "$STAGED_COMPRESS" == "1" ]] && echo "gzip" || echo "none (raw .sql)")"
if [[ "$PV_OK" -eq 1 ]]; then
  progress_mode="pv"
elif [[ "$STAGED_PV" == "1" ]]; then
  progress_mode="probe (60s, pv not installed)"
else
  progress_mode="off"
fi
echo "Progress     : $progress_mode"
echo "Parallel     : $effective_parallel of $db_count DB(s)"
echo "Dump dir     : $STAGED_DUMP_DIR"
echo "Databases    : ${DB_LIST[*]}"

# ----- SSL args -----
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

# ----- Common dump args -----
DUMP_ARGS=(
  --max-allowed-packet=1G
  --routines --triggers --events
  --no-tablespaces --hex-blob --single-transaction
)
if [[ "$dump_is_mysql" -eq 1 ]]; then
  DUMP_ARGS+=(--set-gtid-purged=OFF)
else
  DUMP_ARGS+=(--gtid=0)
fi

# ----- Definer stripping -----
DEFINER_FILTER_CMD=()
if [[ "$STRIP_DEFINERS" == "1" ]]; then
  if [[ "$dump_is_mysql" -eq 1 ]]; then
    DEFINER_FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
  else
    if "$MARIADB_DUMP_BIN" --help 2>/dev/null | grep -q -- '--skip-definer'; then
      DUMP_ARGS+=(--skip-definer)
    else
      DEFINER_FILTER_CMD=( sed -E 's/\/\*!50017 DEFINER=`[^`]+`@`[^`]+`\*\/ ?//g; s/DEFINER=`[^`]+`@`[^`]+`//g' )
    fi
  fi
fi

SRC_AUTH=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" )

# ----- Helpers -----
sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

db_size_bytes() {
  local db="$1"
  local db_esc; db_esc="$(sql_escape "$db")"
  local q="SELECT COALESCE(SUM(data_length + index_length),0) FROM information_schema.tables WHERE table_schema='${db_esc}';"
  MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" -e "$q" 2>/dev/null \
    | head -1 | tr -dc '0-9' || true
}

db_row_estimate() {
  local db="$1"
  local db_esc; db_esc="$(sql_escape "$db")"
  local q="SELECT COALESCE(SUM(table_rows),0) FROM information_schema.tables WHERE table_schema='${db_esc}';"
  MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" -e "$q" 2>/dev/null \
    | head -1 | tr -dc '0-9' || true
}

# Format a byte count as a human-readable string with one decimal place.
fmt_size() {
  local b="${1:-0}"
  if   [[ "$b" -ge 1073741824 ]]; then awk -v n="$b" 'BEGIN{printf "%.1f GiB", n/1073741824}'
  elif [[ "$b" -ge 1048576 ]];    then awk -v n="$b" 'BEGIN{printf "%.1f MiB", n/1048576}'
  elif [[ "$b" -ge 1024 ]];       then awk -v n="$b" 'BEGIN{printf "%.1f KiB", n/1024}'
  else printf "%d B" "$b"
  fi
}

# Background fallback progress reporter for when pv is unavailable.
# Polls the in-progress dump file's on-disk size every $1 seconds and emits
# a tagged line:  <tag>: writing... <size> after <Ns> (rate) [pct%]
#
# Args: interval_sec, file_path, tag, [total_bytes_for_pct]
# The caller is responsible for backgrounding this and killing it when the
# dump finishes (see dump_one_db's trap).
file_size_probe() {
  local interval="$1" path="$2" tag="$3" total="${4:-0}"
  local start now elapsed cur rate_kbs pct extra
  start=$(date +%s)
  while true; do
    sleep "$interval"
    now=$(date +%s)
    elapsed=$((now - start))
    [[ -f "$path" ]] || continue
    cur="$(stat -c%s "$path" 2>/dev/null || stat -f%z "$path" 2>/dev/null || echo 0)"
    cur="${cur:-0}"
    [[ "$cur" -eq 0 ]] && continue
    if [[ "$elapsed" -gt 0 ]]; then
      rate_kbs="$(awk -v c="$cur" -v e="$elapsed" 'BEGIN{printf "%.0f", (c/1024)/e}')"
    else
      rate_kbs=0
    fi
    extra=""
    if [[ "$total" -gt 0 ]]; then
      pct="$(awk -v c="$cur" -v t="$total" 'BEGIN{printf "%.0f", (c*100)/t}')"
      # On-disk dump may exceed source size pre-compression; cap display at 99%
      # so the user doesn't see "120%" and worry. The "complete" line will
      # confirm true completion.
      [[ "$pct" -gt 99 ]] && pct=99
      extra=" [${pct}%]"
    fi
    echo "${tag}: writing... $(fmt_size "$cur") after ${elapsed}s (${rate_kbs} KB/s)${extra}"
  done
}

# Dump one database. Designed to run either inline (sequential) or backgrounded
# (parallel). Inherits parent shell's arrays/vars. Writes a per-DB manifest
# fragment; the parent assembles the final manifest after all workers complete.
dump_one_db() {
  local db="$1"
  local approx_bytes approx_rows out_file size_bytes sha256 db_start db_elapsed size_h
  local manifest_part="${LOG_DIR}/${db}.manifest"
  local PV_CMD=()
  local probe_pid=""

  db_start=$(date +%s)
  approx_bytes="$(db_size_bytes "$db")"; approx_bytes="${approx_bytes:-0}"
  approx_rows="$(db_row_estimate "$db")"; approx_rows="${approx_rows:-0}"

  if [[ "$STAGED_COMPRESS" == "1" ]]; then
    out_file="${STAGED_DUMP_DIR}/${db}.sql.gz"
  else
    out_file="${STAGED_DUMP_DIR}/${db}.sql"
  fi

  if [[ "$PV_OK" -eq 1 ]]; then
    # -f -i 10: force output and emit a status line every 10 seconds even
    # when stderr is not a TTY. The orchestrator (runner.py) pipes both
    # stdout and stderr, which would otherwise suppress pv's interactive
    # progress bar. This trades the live bar for periodic lines in run.log.
    if [[ "${approx_bytes:-0}" -gt 0 ]]; then
      PV_CMD=( "$PV_BIN" -pet -f -i 10 -N "$db" -s "$approx_bytes" )
    else
      PV_CMD=( "$PV_BIN" -pet -f -i 10 -N "$db" )
    fi
  else
    # No pv: launch a background size-probe that prints a status line every
    # 60s by stat'ing the output file. The pipeline writes to $out_file, so
    # the probe just watches that path. Killed via trap when dump_one_db
    # returns (success or failure).
    file_size_probe 60 "$out_file" "$db" "$approx_bytes" &
    probe_pid=$!
    # shellcheck disable=SC2064  # intentional early expansion of $probe_pid
    trap "kill $probe_pid 2>/dev/null; wait $probe_pid 2>/dev/null; trap - RETURN" RETURN
  fi

  set -o pipefail
  if ! MYSQL_PWD="$SRC_PASS" "$MARIADB_DUMP_BIN" "${SRC_AUTH[@]}" "${SRC_SSL_ARGS[@]}" "${DUMP_ARGS[@]}" --databases "$db" \
        | if [[ "${#DEFINER_FILTER_CMD[@]}" -gt 0 ]]; then "${DEFINER_FILTER_CMD[@]}"; else cat; fi \
        | if [[ "${#PV_CMD[@]}" -gt 0 ]]; then "${PV_CMD[@]}"; else cat; fi \
        | if [[ "$STAGED_COMPRESS" == "1" ]]; then "$GZIP_BIN"; else cat; fi \
        > "$out_file"; then
    rm -f "$out_file"
    set +o pipefail
    echo "ERROR: dump failed for database '$db' (partial file removed)" >&2
    return 4
  fi
  set +o pipefail

  size_bytes="$(stat -c%s "$out_file" 2>/dev/null || stat -f%z "$out_file" 2>/dev/null || echo 0)"
  sha256="$(sha256sum "$out_file" 2>/dev/null | awk '{print $1}')"
  if [[ -z "$sha256" ]]; then
    sha256="$(shasum -a 256 "$out_file" 2>/dev/null | awk '{print $1}')"
  fi

  printf "%s\t%s\t%s\t%s\t%s\n" \
    "$db" "$(basename "$out_file")" "${sha256:-unknown}" "$size_bytes" "$approx_rows" \
    > "$manifest_part"

  db_elapsed=$(( $(date +%s) - db_start ))
  size_h="$(numfmt --to=iec --suffix=B "$size_bytes" 2>/dev/null || echo "${size_bytes}B")"
  echo "    [done] $db: ${size_h}, ${approx_rows} approx rows, ${db_elapsed}s"
  return 0
}

# ----- Run dumps (sequential or parallel) -----
total_start=$(date +%s)
declare -A JOB_LOG JOB_RC_FILE
failed_dbs=()

if [[ "$effective_parallel" -le 1 ]]; then
  # Sequential — clean output, full pv visibility.
  for db in "${DB_LIST[@]}"; do
    echo ""
    echo "==> Dumping: $db"
    if ! dump_one_db "$db"; then
      failed_dbs+=("$db")
      break  # fail-fast on sequential
    fi
  done
else
  # Parallel — throttle with `wait -n`, capture exit codes via .rc files
  # since `wait -n` discards them.
  echo ""
  echo "==> Dumping in parallel ($effective_parallel workers)..."
  running=0
  for db in "${DB_LIST[@]}"; do
    while [[ $running -ge $effective_parallel ]]; do
      wait -n || true
      running=$((running - 1))
    done
    log="${LOG_DIR}/${db}.log"
    rcfile="${LOG_DIR}/${db}.rc"
    JOB_LOG[$db]="$log"
    JOB_RC_FILE[$db]="$rcfile"
    (
      set +e
      dump_one_db "$db" >"$log" 2>&1
      echo $? >"$rcfile"
    ) &
    running=$((running + 1))
  done
  wait

  # Collect results in DB order so output is deterministic.
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
      # Surface the "[done]" line from the worker log so the user sees it.
      grep -E '^    \[done\]' "$log" || true
    fi
  done
fi

# ----- Assemble manifest from per-DB fragments -----
manifest="${STAGED_DUMP_DIR}/manifest.txt"
{
  echo "# staged-migration-manifest v1"
  echo "# generated_at: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  echo "# source_host: $SRC_HOST"
  echo "# source_port: $SRC_PORT"
  echo "# source_version: ${src_version_full:-unknown}"
  echo "# dump_tool: $(basename "$MARIADB_DUMP_BIN")"
  echo "# compressed: $STAGED_COMPRESS"
  echo "# parallel_workers: $effective_parallel"
  printf "# database\tfilename\tsha256\tsize_bytes\tapprox_rows\n"
  for db in "${DB_LIST[@]}"; do
    [[ -f "${LOG_DIR}/${db}.manifest" ]] && cat "${LOG_DIR}/${db}.manifest"
  done
} > "$manifest"

# ----- Failure handling -----
if [[ "${#failed_dbs[@]}" -gt 0 ]]; then
  echo ""
  echo "ERROR: dump failed for: ${failed_dbs[*]}" >&2
  echo "       Manifest reflects only successful databases."
  echo "       Per-DB logs in: $LOG_DIR"
  exit 4
fi

# ----- Summary -----
total_elapsed=$(( $(date +%s) - total_start ))
total_size=0
while IFS=$'\t' read -r dbname _filename _sha256 size_bytes _approx_rows; do
  [[ -z "$dbname" || "${dbname:0:1}" == "#" ]] && continue
  total_size=$(( total_size + size_bytes ))
done < "$manifest"
total_h="$(numfmt --to=iec --suffix=B "$total_size" 2>/dev/null || echo "${total_size}B")"

echo ""
echo "==> Staged dump complete."
echo "    Databases : $db_count"
echo "    Total size: $total_h"
echo "    Elapsed   : ${total_elapsed}s"
echo "    Workers   : $effective_parallel"
echo "    Manifest  : $manifest"
echo "    Dump dir  : $STAGED_DUMP_DIR"
