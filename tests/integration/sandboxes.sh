#!/usr/bin/env bash
# Deploy, start, stop, or redeploy MySQL and MariaDB sandbox instances.
#
#   ./sandboxes.sh            deploy if missing, start if stopped
#   ./sandboxes.sh stop       stop all instances
#   ./sandboxes.sh status     show version per port
#   ./sandboxes.sh delete     stop and remove all sandbox directories
#   ./sandboxes.sh redeploy   delete and freshly deploy all instances
#
# Override defaults via env vars if needed (e.g. ARCH=x86 SERVERS=/opt/bins ./sandboxes.sh):

set -u

ARCH="${ARCH:-arm}"                                        # arm | x86
PASSWORD="${PASSWORD:-abc}"
SERVERS="${SERVERS:-$HOME/servers}"
SANDBOX_DIR="${SANDBOX_DIR:-$HOME/.mariadb-shell/sandboxes}"
export MARIADB_SANDBOX_NO_SYNC=1

# port:vendor:series_or_version:server_id
# 5.7 has no aarch64 build; 3327 only deploys on an x86 host (ARCH=x86 SERVERS=...).
INSTANCES=(
  "3327:mysql:5.7:57"
  "3328:mysql:8.0:80"
  "3329:mysql:8.4:84"
  "3311:mariadb:11.8.9:118"
  "3312:mariadb:12.3.3:123"
  "3313:mariadb:13.1.1:131"
)

pkg_path() {
  local vendor="$1" ver="$2"
  if [ "$vendor" = "mariadb" ]; then
    echo "$SERVERS/mariadb-${ver}-linux-glibc2.34-${ARCH}-64bit-sandbox"
  else
    local mysql_arch="aarch64"
    [ "$ARCH" = "x86" ] && mysql_arch="x86_64"
    # Matches any patch release in the series (e.g. mysql-8.4.4-linux-glibc2.28-aarch64)
    local match
    match=$(compgen -G "$SERVERS/mysql-${ver}.*-linux-*-${mysql_arch}" | sort -V | tail -n 1)
    echo "${match:-$SERVERS/mysql-${ver}-linux-glibc2.28-${mysql_arch}}"
  fi
}

is_running() {
  local pidfile="$SANDBOX_DIR/$1/$1.pid"
  [ -f "$pidfile" ] || return 1
  kill -0 "$(cat "$pidfile")" 2>/dev/null
}

deploy_instance() {
  local port="$1" vendor="$2" ver="$3" server_id="$4" path="$5"
  local opts_py=""

  if [ "$vendor" = "mysql" ]; then
    local keyring_dir="$SANDBOX_DIR/keyrings/$port"
    mkdir -p "$keyring_dir"

    if [ "$ver" = "5.7" ] || [ "$ver" = "8.0" ]; then
      opts_py="'early-plugin-load=keyring_file.so', 'keyring_file_data=$keyring_dir/keyring',"
    else
      # MySQL 8.4 uses component_keyring_file manifests instead of keyring_file.so
      printf '{"components": "file://component_keyring_file"}\n' > "$path/bin/mysqld.my"
      printf '{"path": "%s/component_keyring_file", "read_only": false}\n' "$keyring_dir" \
        > "$path/lib/plugin/component_keyring_file.cnf"
      opts_py="'mysql-native-password=ON',"
    fi

    opts_py="$opts_py 'log-bin=mysql-bin', 'binlog-format=ROW', 'gtid-mode=ON', 'enforce-gtid-consistency=ON', 'innodb-flush-log-at-trx-commit=0', 'sync-binlog=0'"
  else
    opts_py="'log-bin=mariadb-bin', 'binlog-format=ROW', 'innodb-flush-log-at-trx-commit=0', 'sync-binlog=0'"
  fi

  mariadb-shell --py -e "
sandbox.deploy($port, {
    'password': '$PASSWORD',
    'sandboxDir': '$SANDBOX_DIR',
    'mariadbdPath': '$path',
    'serverId': $server_id,
    'ssl': False,
    'mariadbdOptions': [$opts_py],
})
"
}

do_up() {
  for i in "${INSTANCES[@]}"; do
    IFS=: read -r port vendor ver server_id <<< "$i"
    path=$(pkg_path "$vendor" "$ver")

    if [ ! -d "$path" ]; then
      echo "SKIP   $port ($vendor $ver) — server package not found at $path"
      continue
    fi

    if [ -d "$SANDBOX_DIR/$port" ]; then
      if is_running "$port"; then
        echo "UP     $port ($vendor $ver) — already running"
      else
        echo "START  $port ($vendor $ver)"
        mariadb-shell -- sandbox start "$port" --sandbox-dir="$SANDBOX_DIR"
      fi
    else
      echo "DEPLOY $port ($vendor $ver)"
      deploy_instance "$port" "$vendor" "$ver" "$server_id" "$path"
    fi
  done
}

do_stop() {
  for i in "${INSTANCES[@]}"; do
    IFS=: read -r port vendor ver _ <<< "$i"
    if is_running "$port"; then
      echo "STOP   $port ($vendor $ver)"
      mariadb-shell -- sandbox stop "$port" --sandbox-dir="$SANDBOX_DIR"
    else
      echo "DOWN   $port ($vendor $ver) — not running"
    fi
  done
}

do_delete() {
  do_stop
  for i in "${INSTANCES[@]}"; do
    IFS=: read -r port vendor ver _ <<< "$i"
    if [ -d "$SANDBOX_DIR/$port" ]; then
      echo "DELETE $port ($vendor $ver)"
      mariadb-shell -- sandbox delete "$port" --sandbox-dir="$SANDBOX_DIR"
    fi
  done
  rm -rf "$SANDBOX_DIR/keyrings"
}

do_status() {
  for i in "${INSTANCES[@]}"; do
    IFS=: read -r port vendor ver _ <<< "$i"
    local ver_out
    printf '%s\t%-7s\t' "$port" "$vendor"
    ver_out=$(mariadb-shell "root:$PASSWORD@127.0.0.1:$port" --sql \
      -e "SELECT VERSION()" 2>/dev/null | tail -n 1)
    printf '%s\n' "${ver_out:-not reachable}"
  done
}

case "${1:-up}" in
  up)       do_up ;;
  stop)     do_stop ;;
  delete)   do_delete ;;
  redeploy) do_delete && do_up ;;
  status)   do_status ;;
  *)        echo "usage: $0 [up|stop|status|delete|redeploy]"; exit 1 ;;
esac
