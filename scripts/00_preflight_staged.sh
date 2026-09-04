#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (staged)"

# ----- Phase + tunables -----
STAGED_PHASE="${STAGED_PHASE:-dump_and_load}"
STAGED_DUMP_DIR="${STAGED_DUMP_DIR:-}"
STAGED_COMPRESS="${STAGED_COMPRESS:-1}"
STAGED_PV="${STAGED_PV:-1}"
STAGED_DISK_HEADROOM_FACTOR="${STAGED_DISK_HEADROOM_FACTOR:-2}"

case "$STAGED_PHASE" in
  dump_and_load|dump_only|load_only) ;;
  *)
    echo "ERROR: STAGED_PHASE must be one of: dump_and_load, dump_only, load_only"
    echo "       Got: '$STAGED_PHASE'"
    exit 2
    ;;
esac

echo "Phase: $STAGED_PHASE"

# Phase predicates
needs_source()           { [[ "$STAGED_PHASE" == "dump_and_load" || "$STAGED_PHASE" == "dump_only" ]]; }
needs_target()           { [[ "$STAGED_PHASE" == "dump_and_load" || "$STAGED_PHASE" == "load_only" ]]; }
needs_dump_dir_writable(){ [[ "$STAGED_PHASE" == "dump_and_load" || "$STAGED_PHASE" == "dump_only" ]]; }
needs_dump_dir_readable(){ [[ "$STAGED_PHASE" == "load_only" ]]; }

# ----- Binary names (overridable) -----
# Prefer the MariaDB client; fall back to mysql. Explicit MYSQL_BIN wins.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"
MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
PV_BIN="${PV_BIN:-pv}"
GZIP_BIN="${GZIP_BIN:-gzip}"

# ----- Source vars (only consulted if needs_source) -----
SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

# ----- Target vars (only consulted if needs_target) -----
TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"
ALLOW_TARGET_DB_OVERWRITE="${ALLOW_TARGET_DB_OVERWRITE:-0}"

# ----- Connection arg arrays (lazy: only used when each side is reachable) -----
src_args=( -h"$SRC_HOST" -P"$SRC_PORT" --connect-timeout=5 --batch --skip-column-names )
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" --connect-timeout=5 --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

# ----- Phase-conditional env validation -----
missing=()
if needs_source; then
  for v in SRC_HOST SRC_USER SRC_PASS SRC_ADMIN_USER SRC_ADMIN_PASS; do
    [[ -z "${!v:-}" ]] && missing+=("$v")
  done
  if [[ -z "$SRC_DB" && -z "$SRC_DBS" ]]; then
    missing+=("SRC_DB_or_SRC_DBS")
  fi
fi
if needs_target; then
  for v in TGT_HOST TGT_USER TGT_PASS TGT_ADMIN_USER TGT_ADMIN_PASS; do
    [[ -z "${!v:-}" ]] && missing+=("$v")
  done
fi
if [[ "$STAGED_PHASE" == "load_only" ]]; then
  [[ -z "$STAGED_DUMP_DIR" ]] && missing+=("STAGED_DUMP_DIR")
fi
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi

# ----- Binary checks (phase-conditional) -----
if needs_source; then
  if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
    echo "ERROR: mysql client not found (MYSQL_BIN=$MYSQL_BIN)."
    exit 5
  fi
  if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
    if command -v mysqldump >/dev/null 2>&1; then
      echo "mariadb-dump not found; mysqldump is available (OK)."
    else
      echo "ERROR: neither mariadb-dump nor mysqldump found on this host."
      echo "       The dump phase needs one of them."
      exit 3
    fi
  fi
fi

if needs_target; then
  if ! command -v "$MARIADB_BIN" >/dev/null 2>&1; then
    echo "ERROR: mariadb client not found (MARIADB_BIN=$MARIADB_BIN)."
    exit 4
  fi
fi

if [[ "$STAGED_PV" == "1" ]]; then
  if ! command -v "$PV_BIN" >/dev/null 2>&1; then
    echo "WARNING: pv not found; progress meter will be disabled."
    echo "         Install 'pv' (e.g. apt/yum install pv) or set STAGED_PV=0 to silence."
  fi
fi

if [[ "$STAGED_COMPRESS" == "1" ]]; then
  if ! command -v "$GZIP_BIN" >/dev/null 2>&1; then
    echo "ERROR: gzip not found (GZIP_BIN=$GZIP_BIN). Install gzip or set STAGED_COMPRESS=0."
    exit 3
  fi
fi

# ----- Source connectivity (dump phases only) -----
if needs_source; then
  echo "Checking source connectivity..."
  if ! MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -u"$SRC_ADMIN_USER" \
      -e "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: Source admin login failed: ${SRC_ADMIN_USER}@${SRC_HOST}:${SRC_PORT}"
    exit 7
  fi

  echo "Checking source migration user connectivity..."
  if ! MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" "${src_args[@]}" -u"$SRC_USER" \
      -e "SELECT 1;" >/dev/null 2>&1; then
    echo "ERROR: Source migration user login failed: ${SRC_USER}@${SRC_HOST}:${SRC_PORT}"
    echo "staged mode does not auto-create migration users."
    echo "Ensure source admin credentials are correct and have required privileges."
    exit 7
  fi

  src_version_full="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -u"$SRC_ADMIN_USER" \
    -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1 || true)"
  echo "Source MySQL: ${src_version_full:-unknown}"
  # Note: staged uses mariadb-dump WITHOUT --master-data, so SHOW BINARY LOG STATUS
  # is never issued. No 8.4-source dump-binary check is needed here (unlike binlog).
fi

# ----- Target connectivity (load phases only) -----
if needs_target; then
  echo "Checking target migration user connectivity..."
  if [[ -n "$TGT_SSH_HOST" ]]; then
    if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD='${TGT_PASS}' ${MARIADB_BIN} -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --connect-timeout=5 -e 'SELECT 1;' >/dev/null 2>&1"; then
      echo "ERROR: Target migration user login failed via SSH: ${TGT_USER}@${TGT_HOST}:${TGT_PORT}"
      exit 8
    fi
  else
    if ! MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -u"$TGT_USER" \
        -e "SELECT 1;" >/dev/null 2>&1; then
      echo "ERROR: Target migration user login failed: ${TGT_USER}@${TGT_HOST}:${TGT_PORT}"
      echo "staged mode does not auto-create migration users."
      exit 8
    fi
  fi
fi

# ----- Helpers used below -----
sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

target_db_exists() {
  local db="$1"
  local db_esc
  db_esc="$(sql_escape "$db")"
  local q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
  local out=""
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local tgt_pass_q
    tgt_pass_q="$(printf '%q' "$TGT_ADMIN_PASS")"
    out="$(ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$tgt_pass_q ${MARIADB_BIN} -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e \"$q\"")"
  else
    out="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -u"$TGT_ADMIN_USER" \
      -e "$q")"
  fi
  [[ "${out:-0}" -gt 0 ]]
}

source_db_size_bytes() {
  # Sum data + index length across the requested DBs. Returns 0 on any failure
  # (caller treats that as "size unknown, skip headroom check").
  local in_clause=""
  local db
  local first=1
  for db in "${SIZE_DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    local db_esc
    db_esc="$(sql_escape "$db")"
    if [[ $first -eq 1 ]]; then
      in_clause="'${db_esc}'"
      first=0
    else
      in_clause="${in_clause},'${db_esc}'"
    fi
  done
  [[ -z "$in_clause" ]] && { printf "0"; return; }
  local q="SELECT COALESCE(SUM(data_length + index_length),0) FROM information_schema.tables WHERE table_schema IN (${in_clause});"
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -u"$SRC_ADMIN_USER" -e "$q" 2>/dev/null \
    | head -1 | tr -dc '0-9' || true
}

# ----- Dump dir checks (phase-conditional) -----
if needs_dump_dir_writable; then
  # Dumps live under the per-run directory by default (same model as sqldata's
  # working files under RUN_DIR). The fallback is an explicit STAGED_DUMP_DIR;
  # we deliberately do NOT invent a generic 'artifacts/staged_dumps' path,
  # because that would orphan dumps outside any run dir and drift away from the
  # run_*/state.json + run_*/report.json convention.
  if [[ -z "$STAGED_DUMP_DIR" ]]; then
    if [[ -n "${RUN_DIR:-}" ]]; then
      STAGED_DUMP_DIR="${RUN_DIR}/dumps"
      echo "STAGED_DUMP_DIR not set; defaulting to: $STAGED_DUMP_DIR"
    else
      echo "ERROR: STAGED_DUMP_DIR is not set and RUN_DIR is not set."
      echo "       Normally the orchestrator sets RUN_DIR (e.g. artifacts/run_staged_<ts>)."
      echo "       If invoking this script directly outside the orchestrator,"
      echo "       export STAGED_DUMP_DIR=/absolute/path/to/dumps before running."
      exit 9
    fi
  fi
  if ! mkdir -p "$STAGED_DUMP_DIR" 2>/dev/null; then
    echo "ERROR: Cannot create dump directory: $STAGED_DUMP_DIR"
    exit 9
  fi
  if [[ ! -w "$STAGED_DUMP_DIR" ]]; then
    echo "ERROR: Dump directory is not writable: $STAGED_DUMP_DIR"
    exit 9
  fi
  echo "Dump directory (writable): $STAGED_DUMP_DIR"

  # Headroom warning. Build a list of DBs to size, then compare to free space.
  if [[ -n "$SRC_DBS" ]]; then
    IFS=',' read -r -a SIZE_DB_LIST <<< "$SRC_DBS"
  else
    SIZE_DB_LIST=("$SRC_DB")
  fi
  src_bytes="$(source_db_size_bytes || true)"
  src_bytes="${src_bytes:-0}"
  if [[ "${src_bytes:-0}" -gt 0 ]]; then
    free_kb="$(df -Pk "$STAGED_DUMP_DIR" | awk 'NR==2{print $4}')"
    free_bytes=$(( free_kb * 1024 ))
    needed_bytes=$(( src_bytes * STAGED_DISK_HEADROOM_FACTOR ))
    src_h="$(numfmt --to=iec --suffix=B "$src_bytes" 2>/dev/null || echo "${src_bytes}B")"
    free_h="$(numfmt --to=iec --suffix=B "$free_bytes" 2>/dev/null || echo "${free_bytes}B")"
    needed_h="$(numfmt --to=iec --suffix=B "$needed_bytes" 2>/dev/null || echo "${needed_bytes}B")"
    echo "Source data size: ~${src_h}  Dump dir free: ${free_h}"
    if [[ "$free_bytes" -lt "$needed_bytes" ]]; then
      echo "WARNING: Free space at $STAGED_DUMP_DIR (${free_h}) is below the recommended"
      echo "         ${STAGED_DISK_HEADROOM_FACTOR}x source data size (${needed_h}). Compressed dumps may still"
      echo "         fit, but uncompressed runs (STAGED_COMPRESS=0) will likely fail."
      echo "         Free more space or set STAGED_DUMP_DIR to a larger volume."
    fi
  else
    echo "Source data size: unknown (could not query information_schema; skipping headroom check)."
  fi
fi

if needs_dump_dir_readable; then
  if [[ ! -d "$STAGED_DUMP_DIR" ]]; then
    echo "ERROR: STAGED_DUMP_DIR does not exist or is not a directory: $STAGED_DUMP_DIR"
    exit 9
  fi
  if [[ ! -r "$STAGED_DUMP_DIR" ]]; then
    echo "ERROR: STAGED_DUMP_DIR is not readable: $STAGED_DUMP_DIR"
    exit 9
  fi

  manifest="$STAGED_DUMP_DIR/manifest.txt"
  if [[ ! -f "$manifest" ]]; then
    echo "ERROR: Manifest not found: $manifest"
    echo "       Was this directory produced by a 'dump_only' or 'dump_and_load' run?"
    exit 9
  fi
  if [[ ! -r "$manifest" ]]; then
    echo "ERROR: Manifest is not readable: $manifest"
    exit 9
  fi

  # Header sanity: first line should advertise our manifest version.
  hdr="$(head -1 "$manifest" 2>/dev/null || true)"
  if [[ "$hdr" != "# staged-migration-manifest v1" ]]; then
    echo "ERROR: Manifest header missing or unrecognized in: $manifest"
    echo "       Expected first line: '# staged-migration-manifest v1'"
    echo "       Got: '$hdr'"
    exit 9
  fi

  # Walk data lines, build DB list, verify each referenced dump file exists.
  MANIFEST_DBS=()
  missing_files=()
  while IFS=$'\t' read -r dbname filename rest; do
    [[ -z "$dbname" ]] && continue
    [[ "${dbname:0:1}" == "#" ]] && continue
    MANIFEST_DBS+=("$dbname")
    if [[ ! -f "${STAGED_DUMP_DIR}/${filename}" ]]; then
      missing_files+=("${filename}")
    fi
  done < "$manifest"

  if [[ "${#MANIFEST_DBS[@]}" -eq 0 ]]; then
    echo "ERROR: Manifest contains no databases: $manifest"
    exit 9
  fi
  if [[ "${#missing_files[@]}" -gt 0 ]]; then
    echo "ERROR: Manifest references missing dump file(s): ${missing_files[*]}"
    exit 9
  fi
  echo "Dump directory (readable): $STAGED_DUMP_DIR"
  echo "Manifest: ${#MANIFEST_DBS[@]} database(s) — ${MANIFEST_DBS[*]}"
fi

# ----- Target DB pre-existence (load phases only) -----
if needs_target && [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
  if [[ "$STAGED_PHASE" == "load_only" ]]; then
    DB_LIST=("${MANIFEST_DBS[@]}")
  else
    if [[ -n "$SRC_DBS" ]]; then
      IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
    else
      DB_LIST=("$SRC_DB")
    fi
  fi

  existing=()
  for db in "${DB_LIST[@]}"; do
    db="${db// /}"
    [[ -z "$db" ]] && continue
    if target_db_exists "$db"; then
      existing+=("$db")
    fi
  done
  if [[ "${#existing[@]}" -gt 0 ]]; then
    echo "ERROR: Target DB already exists: ${existing[*]}"
    echo "Set ALLOW_TARGET_DB_OVERWRITE=1 only if overwrite is intended."
    exit 6
  fi
fi

echo "Preflight complete."
