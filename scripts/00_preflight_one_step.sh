#!/usr/bin/env bash
set -euo pipefail

echo "==> Preflight checks (one_step)"

# Source-side client queries use the canonical 'mariadb' client by default
# (avoids the "Deprecated program name" banner when 'mysql' is a MariaDB alias).
# Operators needing the upstream mysql client can still set MYSQL_BIN=mysql.
# Note: this is only for the connectivity/version/db-existence probes below;
# the 8.4 mysqldump detection block is independent and unchanged.
MYSQL_BIN="${MYSQL_BIN:-mariadb}"
MARIADB_DUMP_BIN="${MARIADB_DUMP_BIN:-mariadb-dump}"
PV_BIN="${PV_BIN:-pv}"

# Explicit SSL flag for the mariadb client family on source probes. Passing it
# (rather than letting the client auto-disable verification on a passwordless
# login and warn) keeps preflight output clean. Empty for a non-mariadb client.
SRC_SSL_ARGS=()
if [[ "$MYSQL_BIN" == *mariadb* ]]; then
  SRC_SSL_ARGS=( --ssl-verify-server-cert=OFF )
fi

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="${SRC_ADMIN_USER:-${SRC_USER:-}}"
SRC_PASS="${SRC_ADMIN_PASS:-${SRC_PASS:-}}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"
SRC_DB="${SRC_DB:-}"
SRC_DBS="${SRC_DBS:-}"

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

AUTO_FIX="${PREFLIGHT_AUTO_FIX:-0}"
AUTO_FIX_TARGET="${PREFLIGHT_AUTO_FIX_TARGET:-$AUTO_FIX}"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

missing=()
for v in SRC_HOST SRC_USER SRC_PASS SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_USER TGT_PASS TGT_ADMIN_USER TGT_ADMIN_PASS; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ -z "$SRC_DB" && -z "$SRC_DBS" ]]; then
  missing+=("SRC_DB_or_SRC_DBS")
fi
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi

if ! command -v "$MYSQL_BIN" >/dev/null 2>&1; then
  echo "ERROR: mysql client not found (MYSQL_BIN=$MYSQL_BIN)."
  exit 2
fi

if ! command -v "$MARIADB_DUMP_BIN" >/dev/null 2>&1; then
  if command -v mysqldump >/dev/null 2>&1; then
    echo "mariadb-dump not found; mysqldump is available (OK)."
  else
    echo "ERROR: neither mariadb-dump nor mysqldump found on source host."
    exit 3
  fi
fi

echo "Checking source connectivity..."
MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  "${SRC_SSL_ARGS[@]}" --connect-timeout=5 --batch --skip-column-names \
  -e "SELECT 1;" >/dev/null

# Detect source version and warn early if 8.4+ but no upstream mysqldump 8.4+
# is available. The dump step in 10_one_step_migration.sh hard-errors with the
# same guidance, but surfacing it here saves the user from progressing past
# preflight only to fail mid-migration.
src_version_full="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  "${SRC_SSL_ARGS[@]}" --connect-timeout=5 --batch --skip-column-names \
  -e "SELECT VERSION();" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+' | head -1)"
echo "Source MySQL: ${src_version_full:-unknown}"
src_version_num="$(printf "%s" "$src_version_full" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')"
src_major="${src_version_num%%.*}"
src_minor="${src_version_num#*.}"; src_minor="${src_minor%%.*}"
if [[ "${src_major:-0}" -gt 8 ]] || { [[ "${src_major:-0}" -eq 8 ]] && [[ "${src_minor:-0}" -ge 4 ]]; }; then
  # Source is 8.4+. Walk MARIADB_DUMP_BIN (if set) and a plain mysqldump on
  # PATH; accept the first one that is upstream MySQL (not MariaDB) and >= 8.4.
  upstream_ok=0
  for cand in "${MARIADB_DUMP_BIN}" "mysqldump"; do
    [[ "$cand" == "mariadb-dump" ]] && continue
    if command -v "$cand" >/dev/null 2>&1; then
      ver_line="$("$cand" --version 2>/dev/null || true)"
      if ! printf "%s" "$ver_line" | grep -iq 'mariadb'; then
        ver="$(printf "%s" "$ver_line" | sed -nE 's/.*Ver +([0-9]+\.[0-9]+).*/\1/p' | head -1)"
        v_major="${ver%%.*}"
        v_minor="${ver#*.}"; v_minor="${v_minor%%.*}"
        if [[ "${v_major:-0}" -gt 8 ]] || { [[ "${v_major:-0}" -eq 8 ]] && [[ "${v_minor:-0}" -ge 4 ]]; }; then
          upstream_ok=1; break
        fi
      fi
    fi
  done
  if [[ "$upstream_ok" -ne 1 ]]; then
    echo "WARNING: Source is MySQL 8.4+, but no upstream mysqldump 8.4+ is available."
    echo "         one_step migration will fail at the dump step."
    echo "         Install MySQL 8.4 client tools first:"
    echo "             https://dev.mysql.com/downloads/mysql/"
    echo "         Then: export MARIADB_DUMP_BIN=/path/to/mysqldump"
  fi
fi

echo "Checking source migration user readiness..."
if ! MYSQL_PWD="$SRC_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_USER" \
  "${SRC_SSL_ARGS[@]}" --connect-timeout=5 --batch --skip-column-names -e "SELECT 1;" >/dev/null 2>&1; then
  echo "WARN: Source migration user login failed for ${SRC_USER}@${SRC_HOST}:${SRC_PORT}."
  echo "one_step will continue and attempt to create migration users in the next step."
fi

echo "Checking source database(s) exist..."
sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}
source_db_exists() {
  local db="$1"
  local db_esc
  db_esc="$(sql_escape "$db")"
  local q="SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='${db_esc}';"
  local out
  out="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    "${SRC_SSL_ARGS[@]}" --batch --skip-column-names -e "$q")"
  [[ "${out:-0}" -gt 0 ]]
}
if [[ -n "$SRC_DBS" ]]; then
  IFS=',' read -r -a SRC_DB_LIST <<< "$SRC_DBS"
else
  SRC_DB_LIST=("$SRC_DB")
fi
missing_src=()
for db in "${SRC_DB_LIST[@]}"; do
  db="${db// /}"
  [[ -z "$db" ]] && continue
  if ! source_db_exists "$db"; then
    missing_src+=("$db")
  fi
done
if [[ "${#missing_src[@]}" -gt 0 ]]; then
  echo "ERROR: Source DB does not exist: ${missing_src[*]}"
  exit 5
fi

if [[ -n "$TGT_SSH_HOST" ]]; then
  echo "Checking target SSH connectivity..."
  if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" "true" >/dev/null 2>&1; then
    echo "ERROR: SSH to target failed."
    exit 4
  fi

  echo "Checking target mariadb client..."
  if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" "command -v mariadb >/dev/null 2>&1"; then
    if [[ "$AUTO_FIX_TARGET" == "1" ]]; then
      echo "Attempting to install mariadb client deps on target..."
      ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" "sudo dnf -y install mariadb-client libxcrypt-compat >/dev/null 2>&1 || true"
    else
      echo "WARN: mariadb client not found on target."
    fi
  fi

  echo "Checking target TCP connectivity..."
  if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
    "MYSQL_PWD='${TGT_ADMIN_PASS}' mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --connect-timeout=5 -e 'SELECT 1;' >/dev/null 2>&1"; then
    echo "WARN: target TCP connect failed. Will attempt socket path during user creation/validate."
  fi

  echo "Checking target migration user readiness..."
  if ! ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
    "MYSQL_PWD='${TGT_PASS}' mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_USER}' --connect-timeout=5 -e 'SELECT 1;' >/dev/null 2>&1"; then
    echo "WARN: Target migration user login failed for ${TGT_USER}@${TGT_HOST}:${TGT_PORT}."
    echo "one_step will continue and attempt to create migration users in the next step."
  fi
fi
# ---------------------------------------------------------------------------
# max_allowed_packet headroom
#
# A single row larger than the target's max_allowed_packet aborts the restore
# mid-stream with ERROR 2006 (Server has gone away). The dump cannot split it:
# one row is one INSERT, so there is no seam to break at.
#
# The source can only emit rows its own limit allows, so when the target is at
# least twice the source there is no reachable overflow and the row scan is
# skipped. The 2x margin covers --hex-blob (doubles binary columns) and
# escaping expansion in text columns, neither of which is visible in the raw
# byte total.
#
# Typical case this catches: MySQL 8.x defaults to 64M, MariaDB to 16M.
# MySQL 5.7 defaults to 4M and clears the headroom check outright.
# ---------------------------------------------------------------------------
echo "Checking max_allowed_packet headroom..."

tgt_query() {
  local q="$1"
  if [[ -n "$TGT_SSH_HOST" ]]; then
    local tgt_pass_q
    tgt_pass_q="$(printf '%q' "$TGT_ADMIN_PASS")"
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$tgt_pass_q mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e \"$q\""
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" mariadb -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "$q"
  fi
}

src_packet="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
  "${SRC_SSL_ARGS[@]}" --connect-timeout=5 --batch --skip-column-names \
  -e "SELECT @@max_allowed_packet;" 2>/dev/null | head -1 || true)"
tgt_packet="$(tgt_query "SELECT @@max_allowed_packet;" 2>/dev/null | head -1 || true)"

if [[ ! "$src_packet" =~ ^[0-9]+$ || ! "$tgt_packet" =~ ^[0-9]+$ ]]; then
  # Target connectivity is advisory at this stage (see TCP check above), so a
  # missing value warns rather than blocks.
  echo "WARN: could not read max_allowed_packet (source='${src_packet:-}' target='${tgt_packet:-}'); skipping row-size check."
else
  echo "max_allowed_packet: source=${src_packet} target=${tgt_packet}"
  if [[ "$src_packet" -le $(( tgt_packet / 2 )) ]]; then
    echo "Target has sufficient headroom; row-size scan not required."
  else
    # Scan only tables that could hold an oversized row. Everything else is
    # bounded well below the limit by its column types.
    if [[ -n "$SRC_DBS" ]]; then
      dbs_csv="${SRC_DBS// /}"
    else
      dbs_csv="$SRC_DB"
    fi
    echo "Scanning candidate tables in: ${dbs_csv}"
    limit=$(( tgt_packet / 2 ))
    big_rows="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
      "${SRC_SSL_ARGS[@]}" --batch --skip-column-names \
      --init-command="SET @selected_dbs='$(sql_escape "$dbs_csv")', @row_limit=${limit}" \
      < "${REPO_ROOT}/sql/checks/large_row_bytes.sql" 2>/dev/null || true)"

    if [[ -n "$big_rows" ]]; then
      echo "ERROR: rows exceed safe size for target max_allowed_packet (${tgt_packet} bytes)."
       while IFS=$'\t' read -r s t b; do
        echo "         ${s}.${t}: ${b} bytes"
      done <<< "$big_rows"

      echo "Raise max_allowed_packet on the target before migrating."
      echo "  Runtime (new connections only):"
      echo "    SET GLOBAL max_allowed_packet=${src_packet};"
      echo "  Persistent, under [mysqld], requires restart:"
      echo "    max_allowed_packet=${src_packet}"
      exit 7
    fi
    echo "No oversized rows found."
  fi
fi

if [[ "$ALLOW_TARGET_DB_OVERWRITE" != "1" ]]; then
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
        "MYSQL_PWD=$tgt_pass_q mariadb -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e \"$q\"")"
    else
      out="$(MYSQL_PWD="$TGT_ADMIN_PASS" mariadb -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
        --batch --skip-column-names -e "$q")"
    fi
    [[ "${out:-0}" -gt 0 ]]
  }

  existing=()
  if [[ -n "$SRC_DBS" ]]; then
    IFS=',' read -r -a DB_LIST <<< "$SRC_DBS"
  else
    DB_LIST=("$SRC_DB")
  fi
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

if command -v "$PV_BIN" >/dev/null 2>&1; then
  echo "pv found."
else
  echo "pv not found (optional)."
fi

echo "Preflight complete."
