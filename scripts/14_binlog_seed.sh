#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: seed target from source snapshot"

MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
# Prefer the MariaDB client; fall back to mysql. Explicit MYSQL_BIN wins.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_USER:-}"
SRC_PASS="${SRC_PASS:-}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_USER:-}"
TGT_PASS="${TGT_PASS:-}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"
#BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-artifacts/binlog_coords.env}"
BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-${RUN_DIR:-artifacts}/binlog_coords.env}"

if [[ -z "$SRC_HOST" || ( -z "$SRC_USER" && -z "$SRC_ADMIN_USER" ) || ( -z "$SRC_PASS" && -z "$SRC_ADMIN_PASS" ) || ( -z "$SRC_DB" && -z "$SRC_DBS" ) ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_USER/SRC_ADMIN_USER, SRC_PASS/SRC_ADMIN_PASS, and SRC_DB or SRC_DBS."
  exit 1
fi
if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" || -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_USER, TGT_PASS, TGT_ADMIN_USER, TGT_ADMIN_PASS."
  exit 1
fi

# Probe source MySQL version (mariadb-dump is not compatible with MySQL 8.4+).
src_admin_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" --batch --skip-column-names )
src_version_full="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_admin_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1)"
src_version_num="$(printf "%s" "$src_version_full" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')"
src_major="${src_version_num%%.*}"
src_minor="${src_version_num#*.}"; src_minor="${src_minor%%.*}"
src_is_84_plus=0
if [[ "${src_major:-0}" -gt 8 ]] || { [[ "${src_major:-0}" -eq 8 ]] && [[ "${src_minor:-0}" -ge 4 ]]; }; then
  src_is_84_plus=1
fi

# Helper: detect a candidate dump binary's major.minor version.
detect_dump_version() {
  local bin="$1" ver_line ver_num
  ver_line="$("$bin" --version 2>/dev/null || true)"
  ver_num="$(printf "%s" "$ver_line" | sed -nE 's/.*Ver +([0-9]+\.[0-9]+).*/\1/p' | head -1)"
  [[ -z "$ver_num" ]] && ver_num="$(printf "%s" "$ver_line" | grep -oE '[0-9]+\.[0-9]+' | head -1)"
  printf "%s" "$ver_num"
}

# Pick dump binary: source >= 8.4 needs upstream mysqldump 8.4+, else mariadb-dump.
chosen_dump=""
if [[ "$src_is_84_plus" -eq 1 ]]; then
  # Need a real upstream mysqldump 8.4+; reject MariaDB-supplied wrappers.
  candidates=()
  if [[ -n "${MARIADB_DUMP_BIN:-}" && "$MARIADB_DUMP_BIN" != "mariadb-dump" && "$MARIADB_DUMP_BIN" != "mysqldump" ]]; then
    candidates+=("$MARIADB_DUMP_BIN")
  fi
  candidates+=("mysqldump")

  for cand in "${candidates[@]}"; do
    if ! command -v "$cand" >/dev/null 2>&1; then
      continue
    fi
    cand_ver_line="$("$cand" --version 2>/dev/null || true)"
    if printf "%s" "$cand_ver_line" | grep -iq 'mariadb'; then
      continue
    fi
    cand_ver="$(detect_dump_version "$cand")"
    cand_major="${cand_ver%%.*}"
    cand_minor="${cand_ver#*.}"; cand_minor="${cand_minor%%.*}"
    if [[ "${cand_major:-0}" -gt 8 ]] || { [[ "${cand_major:-0}" -eq 8 ]] && [[ "${cand_minor:-0}" -ge 4 ]]; }; then
      chosen_dump="$cand"
      break
    fi
  done

  if [[ -z "$chosen_dump" ]]; then
    echo "ERROR: Source is MySQL 8.4+, needs MySQL mysqldump 8.4+. Download client from https://dev.mysql.com/downloads/mysql/, extract, then: export MARIADB_DUMP_BIN=/path/to/mysqldump" >&2
    exit 2
  fi
else
  # For a < 8.4 source: prefer mariadb-dump (always compatible). A user-set
  # MARIADB_DUMP_BIN pointing at mysqldump 8.4+ will break (mysqldump 8.4 issues
  # SHOW BINARY LOG STATUS, which < 8.4 servers don't understand) — warn and
  # fall back to mariadb-dump.
  candidate="$MARIADB_DUMP_BIN"
  candidate_ok=0
  if command -v "$candidate" >/dev/null 2>&1; then
    cand_basename="$(basename "$candidate")"
    if [[ "$cand_basename" == "mariadb-dump" ]]; then
      candidate_ok=1
    elif [[ "$cand_basename" == "mysqldump" ]]; then
      cand_ver_line="$("$candidate" --version 2>/dev/null || true)"
      if printf "%s" "$cand_ver_line" | grep -iq 'mariadb'; then
        # MariaDB-supplied mysqldump wrapper — fine for < 8.4 sources.
        candidate_ok=1
      else
        # Real upstream mysqldump. Compatible only if its version <= source.
        cand_ver="$(detect_dump_version "$candidate")"
        cand_major="${cand_ver%%.*}"
        cand_minor="${cand_ver#*.}"; cand_minor="${cand_minor%%.*}"
        if [[ "${cand_major:-0}" -lt 8 ]] || \
           { [[ "${cand_major:-0}" -eq 8 ]] && [[ "${cand_minor:-0}" -lt 4 ]]; }; then
          candidate_ok=1
        else
          echo "WARNING: \$MARIADB_DUMP_BIN ($candidate) is mysqldump $cand_ver, which" >&2
          echo "         issues SHOW BINARY LOG STATUS — incompatible with source ${src_version_full:-(< 8.4)}." >&2
          echo "         Falling back to mariadb-dump. Unset MARIADB_DUMP_BIN to silence this warning." >&2
        fi
      fi
    fi
  fi
  if [[ "$candidate_ok" -eq 1 ]]; then
    chosen_dump="$candidate"
  elif command -v mariadb-dump >/dev/null 2>&1; then
    chosen_dump="mariadb-dump"
  elif command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; using mysqldump."
    chosen_dump="mysqldump"
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found."
    exit 2
  fi
fi
MARIADB_DUMP_BIN="$chosen_dump"
echo "Source MySQL: ${src_version_full:-unknown}  Dump tool: $MARIADB_DUMP_BIN"

if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
else
  DB_LIST=("$SRC_DB")
fi

mkdir -p "$(dirname "$BINLOG_COORD_FILE")"
DUMP_FILE="$(dirname "$BINLOG_COORD_FILE")/binlog_seed_$(date +%Y%m%d_%H%M%S).sql"

echo "NOTE: Snapshot dump file will be written to: $DUMP_FILE"
echo "      Ensure this filesystem has free space >= total size of the database(s)"
echo "      being migrated (uncompressed SQL). The file is kept after the run for"
echo "      debugging; delete old binlog_seed_*.sql files to reclaim space."

# Connection arg arrays — same rationale as 00_preflight_binlog.sh.
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

echo "Preparing target database(s)..."
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  if [[ "$ALLOW_TARGET_DB_OVERWRITE" == "1" ]]; then
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
      -e "DROP DATABASE IF EXISTS \`${db}\`; CREATE DATABASE \`${db}\`;"
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
      -e "CREATE DATABASE IF NOT EXISTS \`${db}\`;"
  fi
done

SRC_DUMP_USER="${SRC_ADMIN_USER:-$SRC_USER}"
SRC_DUMP_PASS="${SRC_ADMIN_PASS:-$SRC_PASS}"
TGT_RESTORE_USER="${TGT_ADMIN_USER:-$TGT_USER}"
TGT_RESTORE_PASS="${TGT_ADMIN_PASS:-$TGT_PASS}"

# Identify the dump tool family (real mysqldump vs MariaDB-supplied tool).
# Used for both --master-data/--source-data and the GTID flag selection.
dump_basename="$(basename "$MARIADB_DUMP_BIN")"
dump_is_mysql=0
if [[ "$dump_basename" == "mysqldump" ]] && \
   ! "$MARIADB_DUMP_BIN" --version 2>/dev/null | grep -iq 'mariadb'; then
  dump_is_mysql=1
fi

# Binlog-coords flag:
#   real mysqldump 8.4+ → --source-data=2
#   anything else        → --master-data=2 (mariadb-dump or older mysqldump)
DUMP_DATA_OPT="--master-data=2"
if [[ "$dump_is_mysql" -eq 1 ]] && [[ "$src_is_84_plus" -eq 1 ]]; then
  DUMP_DATA_OPT="--source-data=2"
fi

echo "Creating source snapshot with binlog coordinates ($DUMP_DATA_OPT)..."
DUMP_ARGS=(
  --max-allowed-packet=1G
  --single-transaction
  "$DUMP_DATA_OPT"
  --routines --triggers --events
  --hex-blob
  --skip-lock-tables
  --databases
)
# GTID flag — different syntax between real mysqldump and mariadb-dump.
if [[ "$dump_is_mysql" -eq 1 ]]; then
  DUMP_ARGS+=(--set-gtid-purged=OFF)
else
  DUMP_ARGS+=(--gtid=0)
fi
for db in "${DB_LIST[@]}"; do
  db="${db// /}"
  [[ -n "$db" ]] && DUMP_ARGS+=("$db")
done

# mariadb-dump itself doesn't support --ssl-verify-server-cert as a flag
# in older versions, but recent builds do; we don't pass --protocol=TCP
# for the same TLS-strictness reason as the client.
MYSQL_PWD="$SRC_DUMP_PASS" "$MARIADB_DUMP_BIN" \
  -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_DUMP_USER" \
  "${DUMP_ARGS[@]}" > "$DUMP_FILE"

# mysqldump emits one of:
#   CHANGE MASTER TO MASTER_LOG_FILE='...', MASTER_LOG_POS=...   (< 8.4)
#   CHANGE REPLICATION SOURCE TO SOURCE_LOG_FILE='...', SOURCE_LOG_POS=...  (>= 8.4)
# Detect either shape.
coord_line="$(grep -m1 -E "(MASTER_LOG_FILE|SOURCE_LOG_FILE)='[^']+', *(MASTER_LOG_POS|SOURCE_LOG_POS)=[0-9]+" "$DUMP_FILE" || true)"
if [[ -z "$coord_line" ]]; then
  echo "ERROR: Unable to extract binlog coordinates from dump file."
  echo "       Looked for MASTER_LOG_FILE/POS (mysqldump < 8.4) and SOURCE_LOG_FILE/POS (>= 8.4)."
  exit 3
fi

src_file="$(printf "%s" "$coord_line" | sed -E "s/.*(MASTER_LOG_FILE|SOURCE_LOG_FILE)='([^']+)'.*/\2/")"
src_pos="$(printf "%s" "$coord_line"  | sed -E "s/.*(MASTER_LOG_POS|SOURCE_LOG_POS)=([0-9]+).*/\2/")"

cat > "$BINLOG_COORD_FILE" <<COORDS
SRC_BINLOG_FILE=${src_file}
SRC_BINLOG_POS=${src_pos}
COORDS

echo "Restoring snapshot to target..."
restore_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_RESTORE_USER" --batch --skip-column-names --max-allowed-packet=1G )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  restore_args+=( --ssl-verify-server-cert=OFF )
fi
MYSQL_PWD="$TGT_RESTORE_PASS" "$MARIADB_BIN" "${restore_args[@]}" < "$DUMP_FILE"

echo "Seed completed."
echo "Coordinates file: $BINLOG_COORD_FILE"
echo "Dump file: $DUMP_FILE"
