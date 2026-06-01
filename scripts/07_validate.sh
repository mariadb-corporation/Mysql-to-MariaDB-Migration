#!/usr/bin/env bash
set -euo pipefail

MARIADB_BIN="${MARIADB_BIN:-mariadb}"
SOCKET="${SOCKET:-/var/lib/mysql/mysql.sock}"

# Explicit SSL flag for the mariadb client family. Passing it (rather than
# letting the client auto-disable verification on a passwordless login and warn)
# keeps the TCP validation output clean. Empty for a non-mariadb client.
TGT_SSL_ARGS=()
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  TGT_SSL_ARGS=( --ssl-verify-server-cert=OFF )
fi
# Single string form for the SSH-wrapped invocations below.
TGT_SSL_STR=""
if [[ "$MARIADB_BIN" == *mariadb* ]]; then
  TGT_SSL_STR="--ssl-verify-server-cert=OFF"
fi
TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_USER="${TGT_ADMIN_USER:-${TGT_USER:-root}}"
TGT_PASS="${TGT_ADMIN_PASS:-${TGT_PASS:-}}"
TGT_SSH_HOST="${TGT_SSH_HOST:-}"
TGT_SSH_USER="${TGT_SSH_USER:-root}"
TGT_SSH_OPTS="${TGT_SSH_OPTS:-}"

if [[ -n "$TGT_HOST" && -n "$TGT_USER" && -n "$TGT_PASS" ]]; then
  echo "MariaDB version (TCP validation):"
  if [[ -n "$TGT_SSH_HOST" ]]; then
    TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_SSL_STR} -h'$TGT_HOST' -P'$TGT_PORT' -u'$TGT_USER' --batch --skip-column-names -e \"SELECT VERSION();\""
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" \
      -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" \
      "${TGT_SSL_ARGS[@]}" \
      --batch --skip-column-names \
      -e "SELECT VERSION();"
  fi
else
  echo "MariaDB version (socket-based validation):"
  sudo "$MARIADB_BIN" \
    --socket="$SOCKET" \
    -u root \
    --batch --skip-column-names \
    -e "SELECT VERSION();"
fi

echo
echo "User authentication plugins:"
if [[ -n "$TGT_HOST" && -n "$TGT_USER" && -n "$TGT_PASS" ]]; then
  if [[ -n "$TGT_SSH_HOST" ]]; then
    TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_SSL_STR} -h'$TGT_HOST' -P'$TGT_PORT' -u'$TGT_USER' --batch --skip-column-names -e \"SELECT user, host, plugin FROM mysql.user ORDER BY user, host;\""
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" \
      -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" \
      "${TGT_SSL_ARGS[@]}" \
      --batch --skip-column-names \
      -e "SELECT user, host, plugin FROM mysql.user ORDER BY user, host;"
  fi
else
  sudo "$MARIADB_BIN" \
    --socket="$SOCKET" \
    -u root \
    --batch --skip-column-names \
    -e "SELECT user, host, plugin FROM mysql.user ORDER BY user, host;"
fi

echo
echo "Storage engines:"
if [[ -n "$TGT_HOST" && -n "$TGT_USER" && -n "$TGT_PASS" ]]; then
  if [[ -n "$TGT_SSH_HOST" ]]; then
    TGT_PASS_Q="$(printf '%q' "$TGT_PASS")"
    ssh ${TGT_SSH_OPTS} "${TGT_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q ${MARIADB_BIN} ${TGT_SSL_STR} -h'$TGT_HOST' -P'$TGT_PORT' -u'$TGT_USER' --batch --skip-column-names -e \"SHOW ENGINES;\""
  else
    MYSQL_PWD="$TGT_PASS" "$MARIADB_BIN" \
      -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_USER" \
      "${TGT_SSL_ARGS[@]}" \
      --batch --skip-column-names \
      -e "SHOW ENGINES;"
  fi
else
  sudo "$MARIADB_BIN" \
    --socket="$SOCKET" \
    -u root \
    --batch --skip-column-names \
    -e "SHOW ENGINES;"
fi

echo
echo "Validation complete (socket-first)."
