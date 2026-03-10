#!/usr/bin/env bash
set -euo pipefail

echo "==> Replace MySQL slave engine with MariaDB on target host"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"

REPLACE_STOP_MYSQL_CMD="${REPLACE_STOP_MYSQL_CMD:-sudo systemctl stop mysql || sudo systemctl stop mysqld}"
REPLACE_UNINSTALL_MYSQL_CMD="${REPLACE_UNINSTALL_MYSQL_CMD:-}"
REPLACE_START_MARIADB_CMD="${REPLACE_START_MARIADB_CMD:-sudo systemctl start mariadb || sudo systemctl start mysql}"
REPLACE_CONFIGURE_BIND_ADDRESS="${REPLACE_CONFIGURE_BIND_ADDRESS:-1}"
REPLACE_MARIADB_BIND_ADDRESS="${REPLACE_MARIADB_BIND_ADDRESS:-0.0.0.0}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"
REPLACE_AUTO_GRANT_TARGET_ADMIN="${REPLACE_AUTO_GRANT_TARGET_ADMIN:-1}"
REPLACE_TARGET_ADMIN_HOST_PATTERN="${REPLACE_TARGET_ADMIN_HOST_PATTERN:-%}"

if [[ -z "$TGT_SSH_HOST" ]]; then
  echo "ERROR: TGT_SSH_HOST is required."
  exit 1
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 2
fi

echo "Stopping MySQL service on target host..."
ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "$REPLACE_STOP_MYSQL_CMD"

if [[ -n "$REPLACE_UNINSTALL_MYSQL_CMD" ]]; then
  echo "Uninstalling MySQL packages on target host..."
  ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "$REPLACE_UNINSTALL_MYSQL_CMD"
else
  echo "No MySQL uninstall command configured; skipping package removal."
fi

if [[ "$REPLACE_CONFIGURE_BIND_ADDRESS" == "1" ]]; then
  echo "Configuring MariaDB bind-address on target host..."
  bind_q="$(printf '%q' "$REPLACE_MARIADB_BIND_ADDRESS")"
  ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "
set -e
bind_addr=$bind_q
cfg=''
for c in /etc/mysql/mariadb.conf.d/50-server.cnf /etc/my.cnf /etc/my.cnf.d/server.cnf; do
  if [ -f \"\$c\" ]; then cfg=\"\$c\"; break; fi
done
if [ -n \"\$cfg\" ]; then
  if sudo grep -Eq '^[[:space:]]*bind-address[[:space:]]*=' \"\$cfg\"; then
    sudo sed -i -E \"s|^[[:space:]]*bind-address[[:space:]]*=.*|bind-address = \$bind_addr|\" \"\$cfg\"
  else
    if sudo grep -Eq '^[[:space:]]*\\[mysqld\\][[:space:]]*$' \"\$cfg\"; then
      sudo sed -i \"/^[[:space:]]*\\[mysqld\\][[:space:]]*$/a bind-address = \$bind_addr\" \"\$cfg\"
    else
      echo '[mysqld]' | sudo tee -a \"\$cfg\" >/dev/null
      echo \"bind-address = \$bind_addr\" | sudo tee -a \"\$cfg\" >/dev/null
    fi
  fi
else
  echo \"WARN: Could not find MariaDB config file to set bind-address.\"
fi
"
else
  echo "Skipping bind-address configuration (REPLACE_CONFIGURE_BIND_ADDRESS=$REPLACE_CONFIGURE_BIND_ADDRESS)."
fi

echo "Starting MariaDB on target host..."
ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "$REPLACE_START_MARIADB_CMD"

if [[ "$REPLACE_AUTO_GRANT_TARGET_ADMIN" == "1" ]]; then
  if [[ -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
    echo "ERROR: TGT_ADMIN_USER and TGT_ADMIN_PASS are required for auto-grant."
    exit 5
  fi
  echo "Ensuring target admin user can connect remotely..."
  tgt_user_esc="${TGT_ADMIN_USER//\'/\'\'}"
  tgt_pass_esc="${TGT_ADMIN_PASS//\'/\'\'}"
  tgt_host_esc="${REPLACE_TARGET_ADMIN_HOST_PATTERN//\'/\'\'}"
  sql="CREATE USER IF NOT EXISTS '${tgt_user_esc}'@'${tgt_host_esc}' IDENTIFIED BY '${tgt_pass_esc}';
ALTER USER '${tgt_user_esc}'@'${tgt_host_esc}' IDENTIFIED BY '${tgt_pass_esc}';
GRANT ALL PRIVILEGES ON *.* TO '${tgt_user_esc}'@'${tgt_host_esc}' WITH GRANT OPTION;
FLUSH PRIVILEGES;"
  sql_q="$(printf '%q' "$sql")"
  if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
    "sudo mariadb --batch --skip-column-names -e $sql_q"; then
    echo "ERROR: Failed to apply remote admin grants on target MariaDB."
    exit 6
  fi
fi

echo "Verifying MariaDB TCP listener on target host..."
if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
  "ss -lnt | awk '{print \$4}' | grep -Eq '(^|:)${TGT_PORT}\$'"; then
  echo "ERROR: MariaDB is not listening on target port ${TGT_PORT}."
  exit 7
fi

echo "Checking MariaDB remote listener scope on target host..."
if ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
  "ss -lnt | awk '{print \$4}' | grep -Eq '^127\\.0\\.0\\.1:${TGT_PORT}\$'"; then
  echo "ERROR: MariaDB is listening only on 127.0.0.1:${TGT_PORT}."
  echo "Verify bind-address configuration and restart MariaDB."
  exit 8
fi

echo "Verifying target admin TCP login from target host..."
tgt_pass_q="$(printf '%q' "$TGT_ADMIN_PASS")"
if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
  "MYSQL_PWD=$tgt_pass_q mariadb --protocol=TCP -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e 'SELECT 1;' >/dev/null"; then
  echo "ERROR: Target admin TCP login failed after MariaDB start."
  echo "Check target grants for ${TGT_ADMIN_USER}@${REPLACE_TARGET_ADMIN_HOST_PATTERN}."
  exit 9
fi

echo "Engine switch step completed."
