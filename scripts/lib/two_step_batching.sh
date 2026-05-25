#!/usr/bin/env bash
# scripts/lib/two_step_batching.sh
#
# Batch planner and manifest format for the two_step 'resumable' variant.
#
# This file is intended to be sourced by 12_two_step_sqldata_resumable.sh
# (commit 3), or runnable directly as a CLI for inspecting a plan without
# touching sqldata:
#
#     ./scripts/lib/two_step_batching.sh plan <db> [db2] ...
#     ./scripts/lib/two_step_batching.sh sig  <db> [db2] ...
#     ./scripts/lib/two_step_batching.sh sizes <db>
#
# Exposed functions:
#
#   ts_query_table_sizes <db>
#       Emit "<size_bytes>\t<table_name>" rows sorted size-descending.
#
#   ts_plan_batches_for_db <db>
#       Emit manifest rows for one DB, with batch IDs local to the DB
#       (sequential from 1). Caller may renumber to a global sequence.
#
#   ts_plan_signature <db1> [db2] ...
#       Emit a stable sha256 hex digest of the plan inputs. Used at
#       resume time to detect "different inputs, refuse to resume."
#       Does NOT include sizes (those drift naturally between runs).
#
#   ts_emit_manifest_header
#       Emit the header lines (each prefixed with '#') to stdout.
#
#   ts_emit_full_manifest <db1> [db2] ...
#       Emit the complete manifest (header + body) to stdout. Body rows
#       use global batch IDs across DBs (DB N's batches precede DB N+1's,
#       in the order DBs were provided).
#
# Manifest format (TSV, one row per batch):
#
#     <batch_id>\t<database>\t<tables>\t<size_bytes>\t<status>\t<started_at>\t<completed_at>
#
#   batch_id      Zero-padded 4-digit sequential ID (0001, 0002, ...).
#                 Globally unique within a run.
#   database      Source database name. Batches are DB-scoped — never mixed.
#   tables        Comma-separated unqualified table names, size-descending.
#                 Directly consumable as sqldata's '-t=' argument after
#                 prepending the DB qualifier.
#   size_bytes    Sum of DATA_LENGTH + INDEX_LENGTH for the batch's tables
#                 at plan time. Informational; the manifest does not rely
#                 on size matching at resume time.
#   status        not_started | in_progress | complete | failed
#   started_at    ISO-8601 timestamp. Empty when status=not_started.
#   completed_at  ISO-8601 timestamp. Empty until status=complete.
#
# Planning algorithm:
#   Per-DB greedy bin-packing. Tables sorted size-descending are placed
#   into the currently-smallest bin that has < TWO_STEP_BATCH_SIZE tables.
#   Bins per DB = ceil(table_count / TWO_STEP_BATCH_SIZE).
#
#   DBs run in the order given by the caller (typically the order in
#   SRC_DBS). DB N+1's batches do not start until DB N is complete.
#
# Environment:
#   SRC_HOST, SRC_PORT, SRC_USER, SRC_PASS    Source connection (required)
#   SRC_SSL_MODE                              Optional, default DISABLED
#   TWO_STEP_BATCH_SIZE                       Default: 5
#   MARIADB_BIN                               Default: mariadb

set -euo pipefail

TS_BATCH_SIZE="${TWO_STEP_BATCH_SIZE:-5}"
TS_MARIADB_BIN="${MARIADB_BIN:-mariadb}"
TS_ALGO_VERSION="1"   # bump if planning algorithm changes incompatibly

_ts_log()  { printf '%s\n' "$*" >&2; }
_ts_die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

_ts_now()  { date '+%Y-%m-%dT%H:%M:%S%z'; }

# ts_query_table_sizes <db>
ts_query_table_sizes() {
  local db="$1"
  [[ -z "$db" ]]               && _ts_die "ts_query_table_sizes: missing db arg"
  [[ -z "${SRC_HOST:-}" ]]     && _ts_die "ts_query_table_sizes: SRC_HOST not set"
  [[ -z "${SRC_USER:-}" ]]     && _ts_die "ts_query_table_sizes: SRC_USER not set"
  [[ -z "${SRC_PASS:-}" ]]     && _ts_die "ts_query_table_sizes: SRC_PASS not set"

  local port="${SRC_PORT:-3306}"
  local ssl_args=()
  case "${SRC_SSL_MODE:-DISABLED}" in
    DISABLED|"")                 ssl_args+=( --ssl-verify-server-cert=0 ) ;;
    REQUIRED)                    ssl_args+=( --ssl ) ;;
    VERIFY_CA|VERIFY_IDENTITY)   ssl_args+=( --ssl --ssl-verify-server-cert=1 ) ;;
  esac

  MYSQL_PWD="$SRC_PASS" "$TS_MARIADB_BIN" \
    -h"$SRC_HOST" -P"$port" -u"$SRC_USER" "${ssl_args[@]}" \
    --batch --skip-column-names \
    -e "SELECT COALESCE(DATA_LENGTH,0) + COALESCE(INDEX_LENGTH,0) AS total_bytes,
               TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = '${db//\'/\'\'}'
          AND TABLE_TYPE   = 'BASE TABLE'
        ORDER BY total_bytes DESC, TABLE_NAME ASC;"
}

# ts_plan_batches_for_db <db>
# Emits manifest rows with batch IDs local to this DB (1..N, unpadded).
# The caller is expected to renumber to a global, zero-padded sequence
# (ts_emit_full_manifest does this).
ts_plan_batches_for_db() {
  local db="$1"
  [[ -z "$db" ]] && _ts_die "ts_plan_batches_for_db: missing db arg"

  local sizes
  sizes="$(ts_query_table_sizes "$db")"
  if [[ -z "$sizes" ]]; then
    _ts_log "  [${db}] no base tables found, skipping"
    return 0
  fi

  local table_count
  table_count="$(printf '%s\n' "$sizes" | wc -l | tr -d ' ')"
  local num_bins=$(( (table_count + TS_BATCH_SIZE - 1) / TS_BATCH_SIZE ))

  printf '%s\n' "$sizes" | awk -v db="$db" \
    -v num_bins="$num_bins" \
    -v batch_size="$TS_BATCH_SIZE" \
    -F'\t' '
    {
      size = $1 + 0
      tab  = $2
      # Place into the open bin with the smallest current total.
      best = 0
      best_total = 0
      for (i = 1; i <= num_bins; i++) {
        if (count[i] < batch_size) {
          if (best == 0 || total[i] < best_total) {
            best       = i
            best_total = total[i]
          }
        }
      }
      total[best]  += size
      count[best]++
      # Tables within a bin remain size-descending (input is sorted desc,
      # and once we pick a bin we always append).
      tables[best] = (tables[best] == "" ? tab : tables[best] "," tab)
    }
    END {
      for (i = 1; i <= num_bins; i++) {
        printf("%d\t%s\t%s\t%d\tnot_started\t\t\n", i, db, tables[i], total[i])
      }
    }'
}

# ts_plan_signature <db1> [db2] ...
ts_plan_signature() {
  local dbs=( "$@" )
  [[ "${#dbs[@]}" -eq 0 ]] && _ts_die "ts_plan_signature: no DBs provided"

  {
    printf 'variant=resumable\n'
    printf 'batch_size=%d\n' "$TS_BATCH_SIZE"
    printf 'algo_version=%s\n' "$TS_ALGO_VERSION"
    printf 'db_order=%s\n' "$(IFS=,; echo "${dbs[*]}")"
    local db
    for db in "${dbs[@]}"; do
      # Table identity, sorted alphabetically — signature is stable across
      # runs even as sizes drift. Bin composition is volatile; identity is not.
      ts_query_table_sizes "$db" \
        | awk -F'\t' -v db="$db" '{print db "." $2}' \
        | LC_ALL=C sort
    done
  } | sha256sum | awk '{print $1}'
}

# ts_emit_manifest_header
ts_emit_manifest_header() {
  printf '# two_step resumable manifest\n'
  printf '# Generated: %s\n' "$(_ts_now)"
  printf '# Batch size: %d\n' "$TS_BATCH_SIZE"
  printf '# Algorithm version: %s\n' "$TS_ALGO_VERSION"
  if [[ -n "${TS_SIGNATURE:-}" ]]; then
    printf '# Plan signature: %s\n' "$TS_SIGNATURE"
  fi
  printf '# Fields: batch_id\tdatabase\ttables\tsize_bytes\tstatus\tstarted_at\tcompleted_at\n'
}

# ts_emit_full_manifest <db1> [db2] ...
ts_emit_full_manifest() {
  local dbs=( "$@" )
  [[ "${#dbs[@]}" -eq 0 ]] && _ts_die "ts_emit_full_manifest: no DBs provided"

  TS_SIGNATURE="$(ts_plan_signature "${dbs[@]}")"
  ts_emit_manifest_header

  local global_id=1
  local db local_id rest
  for db in "${dbs[@]}"; do
    while IFS=$'\t' read -r local_id rest; do
      [[ -z "$local_id" ]] && continue
      printf '%04d\t%s\n' "$global_id" "$rest"
      global_id=$(( global_id + 1 ))
    done < <(ts_plan_batches_for_db "$db")
  done
}

# CLI dispatch (only when invoked directly, not when sourced).
if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  cmd="${1:-}"
  shift || true
  case "$cmd" in
    plan)
      [[ "$#" -lt 1 ]] && _ts_die "usage: $0 plan <db> [db2] ..."
      ts_emit_full_manifest "$@"
      ;;
    sig|signature)
      [[ "$#" -lt 1 ]] && _ts_die "usage: $0 sig <db> [db2] ..."
      ts_plan_signature "$@"
      ;;
    sizes)
      [[ "$#" -lt 1 ]] && _ts_die "usage: $0 sizes <db>"
      ts_query_table_sizes "$1"
      ;;
    ""|-h|--help|help)
      cat <<EOF
Usage: $0 <command> [args]

Commands:
  plan  <db> [db2 ...]   Emit full manifest (header + rows) to stdout.
  sig   <db> [db2 ...]   Emit just the plan signature (sha256 hex).
  sizes <db>             Emit table sizes for one DB ("<bytes>\\t<table>").

Environment:
  SRC_HOST, SRC_PORT, SRC_USER, SRC_PASS   Source connection (required)
  SRC_SSL_MODE                             DISABLED|REQUIRED|VERIFY_CA|VERIFY_IDENTITY
  TWO_STEP_BATCH_SIZE                      Default: 5
  MARIADB_BIN                              Default: mariadb
EOF
      ;;
    *)
      _ts_die "unknown command '$cmd'. Try '$0 help'."
      ;;
  esac
fi
