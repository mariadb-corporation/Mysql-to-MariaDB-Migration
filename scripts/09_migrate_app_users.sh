#!/usr/bin/env bash
set -euo pipefail

MIGRATE_APP_USERS="${MIGRATE_APP_USERS:-0}"
if [[ "$MIGRATE_APP_USERS" != "1" ]]; then
  echo "==> Skipping application user migration (MIGRATE_APP_USERS!=1)."
  exit 0
fi

echo "==> Migrate application users from source to target"

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
SRC_DB="$(trim_ws "${SRC_DB:-}")"
SRC_DBS="$(trim_ws "${SRC_DBS:-}")"
SRC_ADMIN_USER="$(trim_ws "${SRC_ADMIN_USER:-}")"
SRC_ADMIN_PASS="$(trim_ws "${SRC_ADMIN_PASS:-}")"

TGT_HOST="$(trim_ws "${TGT_HOST:-}")"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="$(trim_ws "${TGT_ADMIN_USER:-}")"
TGT_ADMIN_PASS="$(trim_ws "${TGT_ADMIN_PASS:-}")"
TGT_SSH_HOST="$(trim_ws "${TGT_SSH_HOST:-}")"
TGT_ADMIN_SSH_USER="${TGT_ADMIN_SSH_USER:-${TGT_SSH_USER:-root}}"
TGT_ADMIN_SSH_OPTS="${TGT_ADMIN_SSH_OPTS:-${TGT_SSH_OPTS:-}}"
ALLOW_ROOT_USERS="${ALLOW_ROOT_USERS:-0}"
APP_USER_DEFAULT_PASSWORD="$(trim_ws "${APP_USER_DEFAULT_PASSWORD:-Str0ngChangeMe!2026}")"

if [[ -z "$SRC_HOST" || -z "$SRC_ADMIN_USER" || -z "$SRC_ADMIN_PASS" ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_ADMIN_USER, SRC_ADMIN_PASS."
  exit 1
fi

if [[ -z "$TGT_HOST" || -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
  echo "ERROR: Missing target envs. Set TGT_HOST, TGT_ADMIN_USER, TGT_ADMIN_PASS."
  exit 1
fi

if [[ "${ALLOW_ROOT_USERS}" != "1" ]]; then
  if [[ "$SRC_ADMIN_USER" == "root" || "$TGT_ADMIN_USER" == "root" ]]; then
    echo "ERROR: SRC/TGT admin users must not be root. Set ALLOW_ROOT_USERS=1 to override."
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

echo "Migrating application users to target (default password for non-mysql_native_password)"
app_pwd_esc="$(sql_escape "$APP_USER_DEFAULT_PASSWORD")"
admin_user_esc="$(sql_escape "$SRC_ADMIN_USER")"
user_rows=$(run_source_admin_sql "SELECT user, host, plugin, IFNULL(authentication_string,'') FROM mysql.user WHERE user <> '' AND user NOT IN ('root','${admin_user_esc}','debian-sys-maint','mysql.infoschema','mysql.session','mysql.sys','mysqlxsys');")
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
    if ! run_target_sql "$g"; then
      echo "WARN: Skipping incompatible grant for '${u}'@'${h}'."
    fi
  done <<< "$grants"
done <<< "$user_rows"

echo "Application user migration completed."
