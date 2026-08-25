#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: configure and start replication"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

SRC_HOST="${SRC_HOST:-}"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="${SRC_ADMIN_USER:-}"
SRC_ADMIN_PASS="${SRC_ADMIN_PASS:-}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

REPL_USER="${REPL_USER:-}"
REPL_PASS="${REPL_PASS:-}"
#BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-artifacts/binlog_coords.env}"
BINLOG_COORD_FILE="${BINLOG_COORD_FILE:-${RUN_DIR:-artifacts}/binlog_coords.env}"
SRC_BINLOG_FILE="${SRC_BINLOG_FILE:-}"
SRC_BINLOG_POS="${SRC_BINLOG_POS:-}"
BINLOG_CREATE_REPL_USER="${BINLOG_CREATE_REPL_USER:-1}"
BINLOG_MASTER_SSL="${BINLOG_MASTER_SSL:-0}"
BINLOG_MASTER_SSL_VERIFY_SERVER_CERT="${BINLOG_MASTER_SSL_VERIFY_SERVER_CERT:-0}"
BINLOG_SRC_SSL_CA="${BINLOG_SRC_SSL_CA:-}"
BINLOG_REPL_AUTH="${BINLOG_REPL_AUTH:-auto}"
BINLOG_AUTO_FIX_SERVER_ID="${BINLOG_AUTO_FIX_SERVER_ID:-1}"
TGT_SERVER_ID="${TGT_SERVER_ID:-}"

missing=()
for v in SRC_HOST SRC_ADMIN_USER SRC_ADMIN_PASS TGT_HOST TGT_ADMIN_USER TGT_ADMIN_PASS REPL_USER REPL_PASS; do
  if [[ -z "${!v:-}" ]]; then
    missing+=("$v")
  fi
done
if [[ "${#missing[@]}" -gt 0 ]]; then
  echo "ERROR: Missing env vars: ${missing[*]}"
  exit 1
fi

if [[ -z "$SRC_BINLOG_FILE" || -z "$SRC_BINLOG_POS" ]]; then
  if [[ -f "$BINLOG_COORD_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$BINLOG_COORD_FILE"
  fi
fi

# Build per-side connection arg arrays.
src_args=( -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" --batch --skip-column-names )
tgt_args=( -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" --batch --skip-column-names )
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  tgt_args+=( --ssl-verify-server-cert=OFF )
fi

echo "Checking source/target server_id..."
src_server_id="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
  -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
tgt_server_id="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
  -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
if [[ -z "$src_server_id" || -z "$tgt_server_id" ]]; then
  echo "ERROR: Could not read source/target server_id."
  exit 10
fi

if [[ "$src_server_id" == "$tgt_server_id" ]]; then
  if [[ "$BINLOG_AUTO_FIX_SERVER_ID" != "1" ]]; then
    echo "ERROR: Source and target server_id are equal ($src_server_id). Set distinct IDs before replication."
    exit 11
  fi
  new_tgt_id="$TGT_SERVER_ID"
  if [[ -z "$new_tgt_id" ]]; then
    host_hash="$(printf "%s" "$TGT_HOST" | tr -cd '0-9')"
    if [[ -z "$host_hash" ]]; then host_hash="200"; fi
    new_tgt_id="$(( (10#$host_hash % 2147483000) + 1000 ))"
    if [[ "$new_tgt_id" == "$src_server_id" ]]; then
      new_tgt_id="$((new_tgt_id + 1))"
    fi
  fi
  if ! [[ "$new_tgt_id" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Computed target server_id is invalid: $new_tgt_id"
    exit 12
  fi
  echo "Source/target server_id collision detected ($src_server_id). Setting target server_id=$new_tgt_id"
  MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
    -e "SET GLOBAL server_id=${new_tgt_id};"
  tgt_server_id="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
    -e "SHOW VARIABLES LIKE 'server_id';" | awk 'NR==1{print $2}')"
  if [[ "$tgt_server_id" != "$new_tgt_id" ]]; then
    echo "ERROR: Failed to apply target server_id change."
    exit 13
  fi
  echo "Target server_id updated to $tgt_server_id"
fi
if [[ -z "$SRC_BINLOG_FILE" || -z "$SRC_BINLOG_POS" ]]; then
  echo "ERROR: Missing binlog coordinates. Set SRC_BINLOG_FILE/SRC_BINLOG_POS or generate $BINLOG_COORD_FILE in seed step."
  exit 2
fi

echo "Detecting source version and authentication plugins..."
src_version="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
  -e "SELECT VERSION();" 2>/dev/null | head -1 | tr -d '[:space:]')"
[[ -n "$src_version" ]] || src_version="unknown"

# Probe a single auth plugin. Distinguishes "probe failed" (non-zero return)
# from "plugin not present" (success, empty output) so a connection error is
# never reported as a missing plugin.
probe_plugin() {
  local name="$1" out err rc
  err="$(mktemp)"
  out="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
    -e "SELECT plugin_status FROM information_schema.plugins WHERE plugin_name='${name}';" 2>"$err")" && rc=0 || rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "ERROR: Could not query authentication plugin status on the source ($SRC_HOST:$SRC_PORT)." >&2
    sed 's/^/       /' "$err" >&2
    rm -f "$err"
    return $rc
  fi
  rm -f "$err"
  printf "%s\n" "$out" | head -1 | tr -d '[:space:]'
}

native_status="$(probe_plugin mysql_native_password)" || exit 14
sha2_status="$(probe_plugin caching_sha2_password)"   || exit 14

# Choose the replication auth plugin.
#   native  - works without TLS; default wherever available (MySQL 8.0 and earlier)
#   sha2    - requires TLS on the replication link; no RSA fallback
# Verified working against MySQL 8.0.46 and 8.4.7 with MASTER_SSL=1.
repl_auth_plugin=""
case "$BINLOG_REPL_AUTH" in
  native)
    if [[ "$native_status" == "ACTIVE" ]]; then
      repl_auth_plugin="mysql_native_password"
    else
      echo "ERROR: BINLOG_REPL_AUTH=native but mysql_native_password is not active on the source." >&2
      exit 14
    fi
    ;;
  sha2)
    if [[ "$sha2_status" == "ACTIVE" ]]; then
      repl_auth_plugin="caching_sha2_password"
    else
      echo "ERROR: BINLOG_REPL_AUTH=sha2 but caching_sha2_password is not active on the source." >&2
      exit 14
    fi
    ;;
  auto)
    if [[ "$native_status" == "ACTIVE" ]]; then
      repl_auth_plugin="mysql_native_password"
    elif [[ "$sha2_status" == "ACTIVE" ]]; then
      repl_auth_plugin="caching_sha2_password"
    fi
    ;;
  *)
    echo "ERROR: BINLOG_REPL_AUTH must be auto, native, or sha2 (got '$BINLOG_REPL_AUTH')." >&2
    exit 1
    ;;
esac

if [[ -z "$repl_auth_plugin" ]]; then
  cat >&2 <<EOF
ERROR: No usable authentication plugin for replication on the source
       ($SRC_HOST:$SRC_PORT, version $src_version).

       Neither 'mysql_native_password' nor 'caching_sha2_password' is active.

       Verify with:
           SELECT plugin_name, plugin_status FROM information_schema.plugins
           WHERE plugin_name IN ('mysql_native_password','caching_sha2_password');
EOF
  exit 14
fi

# When the user is pre-created, take the auth path from the existing account so
# the TLS decision below matches how it will actually authenticate.
if [[ "$BINLOG_CREATE_REPL_USER" != "1" ]]; then
  existing_plugin="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
    -e "SELECT plugin FROM mysql.user WHERE user='${REPL_USER//\'/\'\'}' AND host='%';" 2>/dev/null | head -1 | tr -d '[:space:]')"
  if [[ -n "$existing_plugin" ]]; then
    repl_auth_plugin="$existing_plugin"
  fi
fi

echo "Replication auth plugin: $repl_auth_plugin (source $src_version)"

# caching_sha2_password requires a secure transport for full verification and
# has no RSA fallback on this path: without TLS the IO thread fails with 1045
# once the source-side fast-auth cache goes cold.
if [[ "$repl_auth_plugin" == "caching_sha2_password" ]]; then
  src_ssl_cert="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
    -e "SELECT @@global.ssl_cert;" 2>/dev/null | head -1 | tr -d '[:space:]')"
  if [[ -z "$src_ssl_cert" || "$src_ssl_cert" == "NULL" ]]; then
    cat >&2 <<EOF
ERROR: The source ($SRC_HOST:$SRC_PORT, version $src_version) requires
       caching_sha2_password for replication, but has no TLS certificate
       configured. That combination cannot authenticate.

       Either configure TLS on the source, or enable mysql_native_password
       (MySQL 8.0 and earlier only):

           [mysqld]
           mysql_native_password=ON

       Note: mysql_native_password was removed in MySQL 9.0 and setting it
       there is an invalid option.
EOF
    exit 15
  fi

  BINLOG_MASTER_SSL=1
  if [[ -n "$BINLOG_SRC_SSL_CA" ]]; then
    BINLOG_MASTER_SSL_VERIFY_SERVER_CERT=1
  else
    BINLOG_MASTER_SSL_VERIFY_SERVER_CERT=0
    cat >&2 <<EOF
WARNING: The replication link to $SRC_HOST is encrypted, but the source
         certificate will not be verified. Set BINLOG_SRC_SSL_CA to the
         source's CA certificate path to enable verification.
EOF
  fi
fi

echo "Ensuring replication user exists on source..."
if [[ "$BINLOG_CREATE_REPL_USER" == "1" ]]; then
  repl_user_esc="${REPL_USER//\'/\'\'}"
  repl_pass_esc="${REPL_PASS//\'/\'\'}"

  create_user_sql="CREATE USER IF NOT EXISTS '${repl_user_esc}'@'%' IDENTIFIED WITH ${repl_auth_plugin} BY '${repl_pass_esc}';"
  alter_user_sql="ALTER USER '${repl_user_esc}'@'%' IDENTIFIED WITH ${repl_auth_plugin} BY '${repl_pass_esc}';"
  grant_sql="GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '${repl_user_esc}'@'%';"

  # Account DDL executed here lands in the source binlog at a position AFTER
  # the coordinates captured during seed, so the replica would replay the
  # tool's own CREATE/ALTER/GRANT. On a MariaDB target an
  # "ALTER USER ... IDENTIFIED WITH caching_sha2_password AS '<hash>'" fails
  # (ERROR 1396, no server-side sha2 plugin) and stops the SQL thread.
  # Suppress binary logging for these statements where the source account has
  # the privilege to do so.
  nolog=""
  if MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
       -e "SET SESSION sql_log_bin=0;" >/dev/null 2>&1; then
    nolog="SET SESSION sql_log_bin=0; "
  else
    cat >&2 <<EOF
WARNING: '$SRC_ADMIN_USER' cannot set sql_log_bin on the source, so the
         replication account DDL below will be written to the binary log and
         replayed on the target. If the target rejects it the replica SQL
         thread will stop. Grant BINLOG_ADMIN (or SUPER) to avoid this.
EOF
  fi

  run_sql() {
    local sql="$1" out rc
    out="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
      -e "${nolog}$sql" 2>&1)" && rc=0 || rc=$?
    [[ $rc -ne 0 ]] && printf "%s\n" "$out" >&2
    return $rc
  }

  # CREATE is advisory (the account may already exist); ALTER and GRANT are not.
  run_sql "$create_user_sql" || true
  run_sql "$alter_user_sql" || {
    echo "ERROR: Could not set the replication user's password/plugin on the source." >&2
    exit 16
  }
  run_sql "$grant_sql" || {
    echo "ERROR: Could not grant replication privileges on the source." >&2
    exit 16
  }
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" -e "${nolog}FLUSH PRIVILEGES;"

  # Confirm the account landed as intended rather than discovering it 60s later
  # as a generic access-denied in Last_IO_Error.
  actual_plugin="$(MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" "${src_args[@]}" \
    -e "SELECT plugin FROM mysql.user WHERE user='${repl_user_esc}' AND host='%';" 2>/dev/null | head -1 | tr -d '[:space:]')"
  if [[ -z "$actual_plugin" ]]; then
    echo "WARNING: Could not verify the replication user on the source (no read access to mysql.user)." >&2
  elif [[ "$actual_plugin" != "$repl_auth_plugin" ]]; then
    echo "ERROR: Replication user '${REPL_USER}'@'%' uses '$actual_plugin', expected '$repl_auth_plugin'." >&2
    exit 16
  else
    echo "Replication user '${REPL_USER}'@'%' ready ($actual_plugin)."
  fi
fi

repl_ssl_sql=", MASTER_SSL=0, MASTER_SSL_VERIFY_SERVER_CERT=0"
if [[ "$BINLOG_MASTER_SSL" == "1" ]]; then
  if [[ -n "$BINLOG_SRC_SSL_CA" ]]; then
    repl_ssl_sql=", MASTER_SSL=1, MASTER_SSL_CA='${BINLOG_SRC_SSL_CA}', MASTER_SSL_VERIFY_SERVER_CERT=${BINLOG_MASTER_SSL_VERIFY_SERVER_CERT}"
  else
    repl_ssl_sql=", MASTER_SSL=1, MASTER_SSL_VERIFY_SERVER_CERT=${BINLOG_MASTER_SSL_VERIFY_SERVER_CERT}"
  fi
fi

repl_sql="CHANGE MASTER TO
  MASTER_HOST='${SRC_HOST}',
  MASTER_PORT=${SRC_PORT},
  MASTER_USER='${REPL_USER}',
  MASTER_PASSWORD='${REPL_PASS}',
  MASTER_LOG_FILE='${SRC_BINLOG_FILE}',
  MASTER_LOG_POS=${SRC_BINLOG_POS}${repl_ssl_sql};"

echo "Applying replication coordinates: ${SRC_BINLOG_FILE}:${SRC_BINLOG_POS}"
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
  -e "STOP REPLICA;" >/dev/null 2>&1 || \
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
  -e "STOP SLAVE;" >/dev/null 2>&1 || true

MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" -e "$repl_sql"

MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
  -e "START REPLICA;" >/dev/null 2>&1 || \
MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" "${tgt_args[@]}" \
  -e "START SLAVE;"

echo "Replication started."
