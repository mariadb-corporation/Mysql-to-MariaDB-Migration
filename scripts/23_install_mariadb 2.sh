#!/usr/bin/env bash
set -euo pipefail

echo "==> Install MariaDB on target host"

TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:--o StrictHostKeyChecking=no}"

REPLACE_TARGET_OS="${REPLACE_TARGET_OS:-ubuntu}"
REPLACE_MARIADB_VERSION="${REPLACE_MARIADB_VERSION:-11.8}"

if [[ -z "$TGT_SSH_HOST" ]]; then
  echo "ERROR: TGT_SSH_HOST is required."
  exit 1
fi
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh client not found."
  exit 2
fi

os_key="$(printf "%s" "$REPLACE_TARGET_OS" | tr '[:upper:]' '[:lower:]' | xargs)"
version_key="$(printf "%s" "$REPLACE_MARIADB_VERSION" | xargs)"
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
    sudo apt-get update
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

echo "MariaDB install step completed."
