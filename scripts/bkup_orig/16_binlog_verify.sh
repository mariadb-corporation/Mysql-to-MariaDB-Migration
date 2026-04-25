#!/usr/bin/env bash
set -euo pipefail

echo "==> Binlog migration: verify replication"

MARIADB_BIN="${MARIADB_BIN:-mariadb}"

TGT_HOST="${TGT_HOST:-}"
TGT_PORT="${TGT_PORT:-3306}"
TGT_ADMIN_USER="${TGT_ADMIN_USER:-}"
TGT_ADMIN_PASS="${TGT_ADMIN_PASS:-}"
BINLOG_MAX_LAG_SECS="${BINLOG_MAX_LAG_SECS:-30}"
BINLOG_VERIFY_TIMEOUT_SECS="${BINLOG_VERIFY_TIMEOUT_SECS:-90}"
BINLOG_VERIFY_POLL_SECS="${BINLOG_VERIFY_POLL_SECS:-3}"

if [[ -z "$TGT_HOST" || -z "$TGT_ADMIN_USER" || -z "$TGT_ADMIN_PASS" ]]; then
  echo "ERROR: Missing target admin envs for verify step."
  exit 1
fi

status_out=""
status_line=""
deadline=$(( $(date +%s) + BINLOG_VERIFY_TIMEOUT_SECS ))
last_io_error=""
last_sql_error=""
while :; do
  status_line=""
  for q in "SHOW REPLICA STATUS\\G" "SHOW SLAVE STATUS\\G"; do
    status_out="$(MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" -e "$q" 2>&1 || true)"
    status_line="$(printf "%s\n" "$status_out" | awk -F': ' '
      {
        gsub(/^[[:space:]]+/, "", $1)
      }
      $1 ~ /^(Replica_IO_Running|Slave_IO_Running)$/ {io=$2}
      $1 ~ /^(Replica_SQL_Running|Slave_SQL_Running)$/ {sql=$2}
      $1 == "Seconds_Behind_Master" {lag=$2}
      $1 == "Last_IO_Error" {lio=$2}
      $1 == "Last_SQL_Error" {lsql=$2}
      END {
        if (io != "" || sql != "" || lag != "" || lio != "" || lsql != "") {
          print io "\t" sql "\t" lag "\t" lio "\t" lsql
        }
      }
    ')"
    if [[ -n "$status_line" ]]; then
      break
    fi
  done

  if [[ -z "$status_line" ]]; then
    break
  fi

  io_state="$(printf "%s" "$status_line" | awk -F'\t' '{print $1}')"
  sql_state="$(printf "%s" "$status_line" | awk -F'\t' '{print $2}')"
  lag_secs="$(printf "%s" "$status_line" | awk -F'\t' '{print $3}')"
  last_io_error="$(printf "%s" "$status_line" | awk -F'\t' '{print $4}')"
  last_sql_error="$(printf "%s" "$status_line" | awk -F'\t' '{print $5}')"

  if [[ "${io_state:-No}" == "Yes" && "${sql_state:-No}" == "Yes" ]]; then
    break
  fi
  if (( $(date +%s) >= deadline )); then
    break
  fi
  sleep "$BINLOG_VERIFY_POLL_SECS"
done

if [[ -z "$status_line" ]]; then
  echo "ERROR: Could not read replication status from target."
  if [[ -n "$status_out" ]]; then
    echo "$status_out"
  fi
  exit 2
fi

io_state="$(printf "%s" "$status_line" | awk -F'\t' '{print $1}')"
sql_state="$(printf "%s" "$status_line" | awk -F'\t' '{print $2}')"
lag_secs="$(printf "%s" "$status_line" | awk -F'\t' '{print $3}')"
last_io_error="$(printf "%s" "$status_line" | awk -F'\t' '{print $4}')"
last_sql_error="$(printf "%s" "$status_line" | awk -F'\t' '{print $5}')"

echo "IO running: ${io_state:-unknown}"
echo "SQL running: ${sql_state:-unknown}"
echo "Seconds behind master: ${lag_secs:-unknown}"

if [[ "${io_state:-No}" != "Yes" || "${sql_state:-No}" != "Yes" ]]; then
  echo "ERROR: Replication threads are not healthy."
  if [[ -n "$last_io_error" ]]; then
    echo "Last_IO_Error: $last_io_error"
  fi
  if [[ -n "$last_sql_error" ]]; then
    echo "Last_SQL_Error: $last_sql_error"
  fi
  exit 3
fi

if [[ -n "$lag_secs" && "$lag_secs" != "NULL" ]]; then
  if [[ "$lag_secs" =~ ^[0-9]+$ ]] && (( lag_secs > BINLOG_MAX_LAG_SECS )); then
    echo "ERROR: Replication lag too high (${lag_secs}s > ${BINLOG_MAX_LAG_SECS}s)."
    exit 4
  fi
fi

echo "Replication verify passed."
