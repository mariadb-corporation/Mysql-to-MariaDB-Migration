#!/usr/bin/env bash
set -euo pipefail

if [[ "${MIGRATION_USERS_DISABLED:-1}" == "1" ]]; then
  echo "==> Skipping migration user creation (admin-only mode)."
  exit 0
fi

echo "==> Create migration user on source and target"

MYSQL_BIN="${MYSQL_BIN:-mysql}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

trim_ws() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf "%s" "$s"
}

SRC_HOST="$(trim_ws "${SRC_HOST:-}")"
SRC_PORT="${SRC_PORT:-3306}"
SRC_USER="$(trim_ws "${SRC_USER:-}")"
SRC_PASS="$(trim_ws "${SRC_PASS:-}")"
SRC_DB="$(trim_ws "${SRC_DB:-}")"
SRC_DBS="$(trim_ws "${SRC_DBS:-}")"
SRC_ADMIN_USER="$(trim_ws "${SRC_ADMIN_USER:-}")"
SRC_ADMIN_PASS="$(trim_ws "${SRC_ADMIN_PASS:-}")"

TGT_HOST="$(trim_ws "${TGT_HOST:-}")"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="$(trim_ws "${TGT_USER:-}")"
TGT_PASS="$(trim_ws "${TGT_PASS:-}")"
TGT_ADMIN_USER="$(trim_ws "${TGT_ADMIN_USER:-}")"
TGT_ADMIN_PASS="$(trim_ws "${TGT_ADMIN_PASS:-}")"
TGT_SSH_HOST="$(trim_ws "${TGT_SSH_HOST:-}")"
TGT_ADMIN_SSH_USER="${TGT_ADMIN_SSH_USER:-${TGT_SSH_USER:-root}}"
TGT_ADMIN_SSH_OPTS="${TGT_ADMIN_SSH_OPTS:-${TGT_SSH_OPTS:-}}"
ALLOW_ROOT_USERS="${ALLOW_ROOT_USERS:-0}"
MIGRATE_APP_USERS="$(trim_ws "${MIGRATE_APP_USERS:-1}")"
APP_USER_DEFAULT_PASSWORD="$(trim_ws "${APP_USER_DEFAULT_PASSWORD:-Str0ngChangeMe!2026}")"

if [[ -z "$SRC_HOST" || -z "$SRC_USER" || -z "$SRC_PASS" || ( -z "$SRC_DB" && -z "$SRC_DBS" ) ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_USER, SRC_PASS, and SRC_DB or SRC_DBS."
  exit 1
fi

if [[ -z "$TGT_HOST" || -z "$TGT_USER" || -z "$TGT_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_USER, TGT_PASS."
  exit 1
fi

if [[ "${ALLOW_ROOT_USERS}" != "1" ]]; then
  if [[ "$SRC_USER" == "root" || "$TGT_USER" == "root" || "$SRC_ADMIN_USER" == "root" || "$TGT_ADMIN_USER" == "root" ]]; then
    echo "ERROR: SRC/TGT admin and migration users must not be root. Set ALLOW_ROOT_USERS=1 to override."
    exit 1
  fi
fi

if [[ -z "$SRC_ADMIN_USER" || -z "$SRC_ADMIN_PASS" || -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
  echo "ERROR: Missing admin credentials. Set SRC_ADMIN_USER/PASS and TGT_ADMIN_USER/PASS."
  exit 1
fi

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

run_source_admin_sql() {
  local sql="$1"
  # For remote source hosts, use TCP directly.
  if [[ "$SRC_HOST" != "localhost" && "$SRC_HOST" != "127.0.0.1" ]]; then
    MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
      --batch --skip-column-names -e "$sql"
    return 0
  fi

  local args=()
  local socket_paths=()
  socket_paths+=(/var/lib/mysql/mysql.sock /run/mysqld/mysqld.sock /tmp/mysql.sock)

  for s in "${socket_paths[@]}"; do
    if [[ -S "$s" ]]; then
      args=(--socket="$s")
      if MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=SOCKET -u"$SRC_ADMIN_USER" ${args[@]+"${args[@]}"} \
        --batch --skip-column-names -e "$sql"; then
        return 0
      fi
      if MYSQL_PWD="" "$MYSQL_BIN" --protocol=SOCKET -u"$SRC_ADMIN_USER" ${args[@]+"${args[@]}"} \
        --batch --skip-column-names -e "$sql"; then
        return 0
      fi
    fi
  done

  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "$sql"
}

TARGET_SOCKET=""
ensure_target_socket() {
  if [[ -n "$TARGET_SOCKET" ]]; then
    return
  fi
  if [[ -z "${TGT_SSH_HOST:-}" ]]; then
    return
  fi
  TGT_SOCKET_EXPR='
sock="";
for c in /var/lib/mysql/mysql.sock /run/mysqld/mysqld.sock /tmp/mysql.sock; do
  if [ -S "$c" ]; then sock="$c"; break; fi;
done;
echo "$sock";
'
  ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" "sudo systemctl start mariadb >/dev/null 2>&1 || true"
  TARGET_SOCKET=$(ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" "$TGT_SOCKET_EXPR")
  if [[ -z "$TARGET_SOCKET" ]]; then
    echo "ERROR: Could not detect MariaDB socket on target."
    exit 2
  fi
}

run_target_sql() {
  local sql="$1"
  if [[ -n "${TGT_SSH_HOST:-}" ]]; then
    # Prefer TCP+password for explicit admin users; this works for non-root service admins.
    TGT_PASS_Q="$(printf '%q' "$TGT_ADMIN_PASS")"
    local tcp_out
    if tcp_out="$(printf '%s\n' "$sql" | ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q /usr/bin/mariadb --protocol=TCP -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names" 2>&1)"; then
      [[ -n "$tcp_out" ]] && printf '%s\n' "$tcp_out"
      return 0
    fi
    echo "$tcp_out" >&2
    # Optional fallback to local socket only for root override mode.
    if [[ "$ALLOW_ROOT_USERS" == "1" && "$TGT_ADMIN_USER" == "root" ]]; then
      ensure_target_socket
      if [[ -n "$TARGET_SOCKET" ]]; then
        printf '%s\n' "$sql" | ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" \
          "sudo /usr/bin/mariadb --protocol=SOCKET -u'${TGT_ADMIN_USER}' --socket='${TARGET_SOCKET}' --batch --skip-column-names"
        return 0
      fi
    fi
    return 1
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "$sql"
  fi
}

SRC_USER_ESC="$(sql_escape "$SRC_USER")"
SRC_PASS_ESC="$(sql_escape "$SRC_PASS")"
SRC_DB_ESC="$(sql_escape "$SRC_DB")"
TGT_USER_ESC="$(sql_escape "$TGT_USER")"
TGT_PASS_ESC="$(sql_escape "$TGT_PASS")"
SRC_ADMIN_USER_ESC="$(sql_escape "$SRC_ADMIN_USER")"
TGT_ADMIN_USER_ESC="$(sql_escape "$TGT_ADMIN_USER")"

build_db_grants() {
  local dbs="$1"
  local user_esc="$2"
  local grants=""
  if [[ -n "$dbs" ]]; then
    IFS=',' read -r -a _db_list <<< "$dbs"
    for _db in "${_db_list[@]}"; do
      _db="${_db// /}"
      if [[ -n "$_db" ]]; then
        _db_esc="$(sql_escape "$_db")"
        grants+="GRANT SHOW VIEW, TRIGGER, EVENT ON \`${_db_esc}\`.* TO '${user_esc}'@'%';"$'\n'
      fi
    done
  elif [[ -n "$SRC_DB_ESC" ]]; then
    grants+="GRANT SHOW VIEW, TRIGGER, EVENT ON \`${SRC_DB_ESC}\`.* TO '${user_esc}'@'%';"$'\n'
  fi
  printf "%s" "$grants"
}

build_target_privs() {
  local dbs="$1"
  local user_esc="$2"
  local grants=""
  if [[ -n "$dbs" ]]; then
    IFS=',' read -r -a _db_list <<< "$dbs"
    for _db in "${_db_list[@]}"; do
      _db="${_db// /}"
      if [[ -n "$_db" ]]; then
        _db_esc="$(sql_escape "$_db")"
        grants+="GRANT ALL PRIVILEGES ON \`${_db_esc}\`.* TO '${user_esc}'@'%';"$'\n'
      fi
    done
  elif [[ -n "$SRC_DB_ESC" ]]; then
    grants+="GRANT ALL PRIVILEGES ON \`${SRC_DB_ESC}\`.* TO '${user_esc}'@'%';"$'\n'
  fi
  printf "%s" "$grants"
}

echo "Creating migration user on source: $SRC_HOST:$SRC_PORT"
SRC_DB_GRANTS="$(build_db_grants "$SRC_DBS" "$SRC_USER_ESC")"
run_source_admin_sql "CREATE USER IF NOT EXISTS '${SRC_USER_ESC}'@'%' IDENTIFIED BY '${SRC_PASS_ESC}';
GRANT SELECT ON *.* TO '${SRC_USER_ESC}'@'%';
${SRC_DB_GRANTS}FLUSH PRIVILEGES;"

echo "Creating migration user on target: $TGT_HOST:$TGT_PORT"
TGT_DB_GRANTS="$(build_target_privs "$SRC_DBS" "$TGT_USER_ESC")"
SQL_TGT="CREATE USER IF NOT EXISTS '${TGT_USER_ESC}'@'%' IDENTIFIED BY '${TGT_PASS_ESC}';
${TGT_DB_GRANTS}GRANT SELECT ON mysql.user TO '${TGT_USER_ESC}'@'%';
FLUSH PRIVILEGES;"
run_target_sql "$SQL_TGT"

if [[ "$MIGRATE_APP_USERS" == "1" ]]; then
  echo "Migrating application users to target (default password)"
  app_pwd_esc="$(sql_escape "$APP_USER_DEFAULT_PASSWORD")"
  user_rows=$(run_source_admin_sql "SELECT user, host, plugin, IFNULL(authentication_string,'') FROM mysql.user WHERE user <> '' AND user NOT IN ('root','${SRC_USER}','mysql.infoschema','mysql.session','mysql.sys');")
  while IFS=$'\t' read -r u h p auth_str; do
    [[ -z "$u" ]] && continue
    u_esc="$(sql_escape "$u")"
    h_esc="$(sql_escape "$h")"
    p="${p:-}"
    auth_str="${auth_str:-}"

    if [[ "$p" == "mysql_native_password" && -n "$auth_str" ]]; then
      auth_esc="$(sql_escape "$auth_str")"
      run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED BY PASSWORD '${auth_esc}';"
      run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED BY PASSWORD '${auth_esc}';"
    else
      run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}';"
      run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}';"
    fi

    grants=$(run_source_admin_sql "SHOW GRANTS FOR '${u_esc}'@'${h_esc}';")
    while IFS= read -r g; do
      [[ -z "$g" ]] && continue
      run_target_sql "$g"
    done <<< "$grants"
  done <<< "$user_rows"
fi

echo "Migration user setup completed."
