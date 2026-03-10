#!/usr/bin/env bash
set -euo pipefail

echo "==> In-place upgrade: install MariaDB"

INPLACE_EXECUTE="${INPLACE_EXECUTE:-0}"
INPLACE_TARGET_OS="${INPLACE_TARGET_OS:-ubuntu}"
INPLACE_MARIADB_VERSION="${INPLACE_MARIADB_VERSION:-11.8}"

if [[ "$INPLACE_EXECUTE" != "1" ]]; then
  echo "INPLACE_EXECUTE is not 1. Dry-run only."
  echo "Planned target OS: ${INPLACE_TARGET_OS}"
  echo "Planned MariaDB version: ${INPLACE_MARIADB_VERSION}"
  exit 0
fi

os_key="$(printf "%s" "$INPLACE_TARGET_OS" | tr '[:upper:]' '[:lower:]' | xargs)"
version_key="$(printf "%s" "$INPLACE_MARIADB_VERSION" | xargs)"
if [[ -z "$version_key" ]]; then
  version_key="11.8"
fi

echo "Installing MariaDB ${version_key} for inplace target OS: ${os_key}"

case "$os_key" in
  ubuntu|debian)
    if sudo test -d /var/lib/mysql-8.0; then
      ts="$(date +%Y%m%d_%H%M%S)"
      sudo mv /var/lib/mysql-8.0 "/var/lib/mysql-8.0.backup-${ts}" || true
    fi
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo apt-get update
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
    sudo dnf -y check || true
    sudo dnf -y install MariaDB-server MariaDB-client
    ;;
  centos7)
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo yum -y makecache
    sudo yum -y check || true
    sudo yum -y install MariaDB-server MariaDB-client
    ;;
  sles)
    curl -LsS https://r.mariadb.com/downloads/mariadb_repo_setup \
      | sudo bash -s -- --mariadb-server-version="mariadb-${version_key}"
    sudo zypper --non-interactive refresh
    sudo zypper --non-interactive verify || true
    sudo zypper --non-interactive install MariaDB-server MariaDB-client
    ;;
  *)
    echo "ERROR: Unsupported INPLACE_TARGET_OS: $os_key"
    echo "Supported: ubuntu|debian|rocky|rhel|centos7|sles"
    exit 3
    ;;
esac

echo "In-place MariaDB install step completed."
