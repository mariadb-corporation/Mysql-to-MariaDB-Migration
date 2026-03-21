#!/usr/bin/env bash
set -euo pipefail

echo "==> Install MariaDB on target host"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"
TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"

INSTALL_TARGET_MARIADB="${INSTALL_TARGET_MARIADB:-1}"
TARGET_INSTALL_OS="${TARGET_INSTALL_OS:-${REPLACE_TARGET_OS:-ubuntu}}"
TARGET_MARIADB_VERSION="${TARGET_MARIADB_VERSION:-${REPLACE_MARIADB_VERSION:-11.8}}"
INSTALL_CONFIGURE_BIND_ADDRESS="${INSTALL_CONFIGURE_BIND_ADDRESS:-${REPLACE_CONFIGURE_BIND_ADDRESS:-1}}"
INSTALL_MARIADB_BIND_ADDRESS="${INSTALL_MARIADB_BIND_ADDRESS:-${REPLACE_MARIADB_BIND_ADDRESS:-0.0.0.0}}"
INSTALL_AUTO_GRANT_TARGET_ADMIN="${INSTALL_AUTO_GRANT_TARGET_ADMIN:-${REPLACE_AUTO_GRANT_TARGET_ADMIN:-1}}"
INSTALL_TARGET_ADMIN_HOST_PATTERN="${INSTALL_TARGET_ADMIN_HOST_PATTERN:-${REPLACE_TARGET_ADMIN_HOST_PATTERN:-%}}"

if [[ "$INSTALL_TARGET_MARIADB" != "1" ]]; then
  echo "INSTALL_TARGET_MARIADB is not 1. Skipping MariaDB install step."
  exit 0
fi

if [[ -z "$TGT_SSH_HOST" ]]; then
  echo "ERROR: TGT_SSH_HOST is required."
  exit 1
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 2
fi

os_key="$(printf "%s" "$TARGET_INSTALL_OS" | tr '[:upper:]' '[:lower:]' | xargs)"
version_key="$(printf "%s" "$TARGET_MARIADB_VERSION" | xargs)"
if [[ -z "$version_key" ]]; then
  version_key="11.8"
fi

echo "Installing MariaDB ${version_key} for target OS: ${os_key}"

ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
  "REPLACE_TARGET_OS='$os_key' REPLACE_MARIADB_VERSION='$version_key' bash -s" <<'EOS'
set -euo pipefail

os_key="${REPLACE_TARGET_OS}"
version_key="${REPLACE_MARIADB_VERSION}"

case "$os_key" in
  ubuntu|debian)
    # MariaDB preinst may rename /var/lib/mysql -> /var/lib/mysql-8.0.
    # If a previous attempt already left /var/lib/mysql-8.0, archive it first.
    if sudo test -d /var/lib/mysql-8.0; then
      ts="$(date +%Y%m%d_%H%M%S)"
      sudo mv /var/lib/mysql-8.0 "/var/lib/mysql-8.0.backup-${ts}" || true
    fi
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    if ! sudo apt-get update; then
      echo "apt-get update failed. Checking for stale MaxScale repo entries..."
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [[ -f "$f" ]] || continue
        if grep -q 'dlm.mariadb.com/repo/maxscale/latest/apt' "$f"; then
          echo "Disabling stale MaxScale repo in: $f"
          sudo sed -i.bak '/dlm\.mariadb\.com\/repo\/maxscale\/latest\/apt/s/^/# disabled by migration tool: /' "$f"
        fi
      done
      sudo apt-get update
    fi
    # Recover from any previous interrupted dpkg state before install/upgrade.
    sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a || true
    sudo DEBIAN_FRONTEND=noninteractive apt-get -f install -y \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" || true
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      mariadb-server mariadb-client
    ;;
  rocky|rhel)
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo dnf -y makecache
    # Best-effort cleanup if previous package operations were interrupted.
    sudo dnf -y check || true
    sudo dnf -y install MariaDB-server MariaDB-client
    ;;
  centos7)
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo yum -y makecache
    # Best-effort cleanup if previous package operations were interrupted.
    sudo yum -y check || true
    sudo yum -y install MariaDB-server MariaDB-client
    ;;
  sles)
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo zypper --non-interactive refresh
    # Best-effort cleanup if previous package operations were interrupted.
    sudo zypper --non-interactive verify || true
    sudo zypper --non-interactive install MariaDB-server MariaDB-client
    ;;
  *)
    echo "ERROR: Unsupported REPLACE_TARGET_OS: $os_key"
    echo "Supported: ubuntu|debian|rocky|rhel|centos7|sles"
    exit 3
    ;;
esac
EOS

if [[ "$INSTALL_CONFIGURE_BIND_ADDRESS" == "1" ]]; then
  echo "Configuring MariaDB bind-address on target host..."
  bind_q="$(printf '%q' "$INSTALL_MARIADB_BIND_ADDRESS")"
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
fi

echo "Restarting MariaDB service on target host..."
ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
  "sudo systemctl restart mariadb >/dev/null 2>&1 || sudo systemctl restart mysql >/dev/null 2>&1 || sudo systemctl start mariadb >/dev/null 2>&1 || sudo systemctl start mysql >/dev/null 2>&1"

if [[ "$INSTALL_AUTO_GRANT_TARGET_ADMIN" == "1" ]]; then
  if [[ -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
    echo "ERROR: TGT_ADMIN_USER and TGT_ADMIN_PASS are required for admin grant after install."
    exit 4
  fi
  echo "Ensuring target admin user can connect remotely..."
  tgt_user_esc="${TGT_ADMIN_USER//\'/\'\'}"
  tgt_pass_esc="${TGT_ADMIN_PASS//\'/\'\'}"
  tgt_host_esc="${INSTALL_TARGET_ADMIN_HOST_PATTERN//\'/\'\'}"
  sql="CREATE USER IF NOT EXISTS '${tgt_user_esc}'@'${tgt_host_esc}' IDENTIFIED BY '${tgt_pass_esc}';
ALTER USER '${tgt_user_esc}'@'${tgt_host_esc}' IDENTIFIED BY '${tgt_pass_esc}';
GRANT ALL PRIVILEGES ON *.* TO '${tgt_user_esc}'@'${tgt_host_esc}' WITH GRANT OPTION;
FLUSH PRIVILEGES;"
  sql_q="$(printf '%q' "$sql")"
  if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" "sudo mariadb --batch --skip-column-names -e $sql_q"; then
    echo "ERROR: Failed to apply target admin grants after install."
    exit 5
  fi
fi

if [[ -n "$TGT_HOST" ]]; then
  echo "Verifying MariaDB listener and target admin TCP login..."
  if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
    "ss -lnt | awk '{print \$4}' | grep -Eq '(^|:)${TGT_PORT}\$'"; then
    echo "ERROR: MariaDB is not listening on target port ${TGT_PORT}."
    exit 6
  fi
  if ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
    "ss -lnt | awk '{print \$4}' | grep -Eq '^127\\.0\\.0\\.1:${TGT_PORT}\$'"; then
    echo "ERROR: MariaDB is listening only on 127.0.0.1:${TGT_PORT}."
    exit 7
  fi
  if [[ -n "$TGT_ADMIN_USER" && -n "$TGT_ADMIN_PASS" ]]; then
    tgt_pass_q="$(printf '%q' "$TGT_ADMIN_PASS")"
    if ! ssh $TGT_SSH_OPTS "$TGT_SSH_USER@$TGT_SSH_HOST" \
      "MYSQL_PWD=$tgt_pass_q mariadb --protocol=TCP -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names -e 'SELECT 1;' >/dev/null"; then
      echo "ERROR: Target admin TCP login failed after MariaDB install."
      exit 8
    fi
  fi
fi

echo "MariaDB install step completed."
