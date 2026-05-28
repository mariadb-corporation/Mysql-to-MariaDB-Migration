#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# scripts/01_assess_app_users.sh
#
# ASSESS-PHASE counterpart to scripts/09_migrate_app_users.sh.
#
# This script DOES NOT WRITE TO THE TARGET. It performs the same discovery
# (roles, users, plugins, hashes, grants) and emits the same shape of report,
# but with "would create" / "would skip" / "would drop" framing instead of
# past-tense outcomes. The output is written to
# ${ASSESS_DIR:-${RUN_DIR:-artifacts}}/user_assessment_report.txt so the
# operator can review what the run-phase migration is expected to do before
# committing to a run.
#
# Behavior summary (1.2.3):
#   - Gated by MIGRATE_APP_USERS=1 (same as the run-phase script). If the
#     operator did not opt in to user migration, this assess step is a no-op.
#   - Reads from source only. Does NOT connect to the target.
#   - Uses the same plugin-aware classification logic as the run-phase script:
#     mysql_native_password (with hash)  -> would preserve via IDENTIFIED VIA
#     mysql_native_password (no hash)    -> would set default password + EXPIRE
#     caching_sha2_password, sha256      -> would set default password + EXPIRE
#     anything else                      -> would skip
#   - Grants are listed but NOT classified as "would drop" — that requires
#     replaying against the target to discover incompatibilities, which is
#     write-adjacent. The report shows grant counts and full text so the
#     operator can eyeball for 8.0-only privileges before the run.
# =============================================================================

MIGRATE_APP_USERS="${MIGRATE_APP_USERS:-0}"
if [[ "$MIGRATE_APP_USERS" != "1" ]]; then
  echo "==> Skipping application user assessment (MIGRATE_APP_USERS!=1)."
  exit 0
fi

echo "==> Assess application users on source"

MYSQL_BIN="${MYSQL_BIN:-mariadb}"

# Report file path. Prefer ASSESS_DIR if the assess phase set it; otherwise
# fall back to RUN_DIR or artifacts/. Same fallback chain as the run script.
REPORT_FILE="${USER_ASSESSMENT_REPORT:-${ASSESS_DIR:-${RUN_DIR:-artifacts}}/user_assessment_report.txt}"

trim_ws() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf "%s" "$s"
}

SRC_HOST="$(trim_ws "${SRC_HOST:-}")"
SRC_PORT="${SRC_PORT:-3306}"
SRC_ADMIN_USER="$(trim_ws "${SRC_ADMIN_USER:-}")"
SRC_ADMIN_PASS="$(trim_ws "${SRC_ADMIN_PASS:-}")"
ALLOW_ROOT_USERS="${ALLOW_ROOT_USERS:-0}"
APP_USER_DEFAULT_PASSWORD="$(trim_ws "${APP_USER_DEFAULT_PASSWORD:-Str0ngChangeMe!2026}")"

if [[ -z "$SRC_HOST" || -z "$SRC_ADMIN_USER" || -z "$SRC_ADMIN_PASS" ]]; then
  echo "ERROR: Missing source envs. Set SRC_HOST, SRC_ADMIN_USER, SRC_ADMIN_PASS."
  exit 1
fi

if [[ "${ALLOW_ROOT_USERS}" != "1" && "$SRC_ADMIN_USER" == "root" ]]; then
  echo "ERROR: SRC admin user must not be root. Set ALLOW_ROOT_USERS=1 to override."
  exit 1
fi

mkdir -p "$(dirname "$REPORT_FILE")" 2>/dev/null || true

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

mask_client_noise() {
  grep -v -e 'Deprecated program name' \
          -e 'ssl-verify-server-cert is disabled' \
          -e 'WARNING: option' || true
}

run_source_admin_sql() {
  local sql="$1"
  if [[ "$SRC_HOST" != "localhost" && "$SRC_HOST" != "127.0.0.1" ]]; then
    MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
      --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2)
    return 0
  fi

  local args=()
  local socket_paths=()
  socket_paths+=(/var/lib/mysql/mysql.sock /run/mysqld/mysqld.sock /tmp/mysql.sock)
  for s in "${socket_paths[@]}"; do
    if [[ -S "$s" ]]; then
      args=(--socket="$s")
      if MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=SOCKET -u"$SRC_ADMIN_USER" ${args[@]+"${args[@]}"} \
        --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2); then
        return 0
      fi
    fi
  done
  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2)
}

# -----------------------------------------------------------------------------
# Report accumulators.
# -----------------------------------------------------------------------------
roles_found=""
users_would_preserve=""
users_would_default=""
users_would_skip=""
grants_total=0

# -----------------------------------------------------------------------------
# Role discovery. Same heuristic as the run script: account_locked='Y' AND
# password_expired='Y' AND authentication_string=''.
# -----------------------------------------------------------------------------
echo "Discovering roles on source..."
admin_user_esc="$(sql_escape "$SRC_ADMIN_USER")"
role_rows=$(run_source_admin_sql "
  SELECT user, host
    FROM mysql.user
   WHERE account_locked = 'Y'
     AND password_expired = 'Y'
     AND authentication_string = ''
     AND user <> ''
     AND user NOT IN ('root','${admin_user_esc}','debian-sys-maint',
                      'mysql.infoschema','mysql.session','mysql.sys','mysqlxsys');
")

role_user_keys=""
while IFS=$'\t' read -r r_user r_host; do
  [[ -z "$r_user" ]] && continue
  roles_found+="'${r_user}'@'${r_host}'"$'\n'
  role_user_keys+="${r_user}|${r_host}"$'\n'
done <<< "$role_rows"

is_role_row() {
  local u="$1" h="$2"
  grep -Fxq "${u}|${h}" <<< "$role_user_keys"
}

# -----------------------------------------------------------------------------
# User classification (no writes).
# -----------------------------------------------------------------------------
echo "Classifying users by plugin..."

user_rows=$(run_source_admin_sql "
  SELECT user, host, plugin, IFNULL(authentication_string,'')
    FROM mysql.user
   WHERE user <> ''
     AND user NOT IN ('root','${admin_user_esc}','debian-sys-maint',
                      'mysql.infoschema','mysql.session','mysql.sys','mysqlxsys');
")

while IFS=$'\t' read -r u h p auth_str; do
  [[ -z "$u" ]] && continue
  if is_role_row "$u" "$h"; then
    continue
  fi
  p="${p:-}"
  auth_str="${auth_str:-}"

  case "$p" in
    mysql_native_password)
      if [[ -n "$auth_str" ]]; then
        users_would_preserve+="'${u}'@'${h}'"$'\n'
      else
        users_would_default+="'${u}'@'${h}' (source plugin: ${p}, no source hash)"$'\n'
      fi
      ;;
    caching_sha2_password|sha256_password)
      users_would_default+="'${u}'@'${h}' (source plugin: ${p})"$'\n'
      ;;
    *)
      users_would_skip+="'${u}'@'${h}' (source plugin: ${p:-unknown}; manual handling required)"$'\n'
      ;;
  esac

  # Count grants. We don't replay them — the assess phase only inspects.
  u_esc="$(sql_escape "$u")"
  h_esc="$(sql_escape "$h")"
  grant_count=$(run_source_admin_sql "SHOW GRANTS FOR '${u_esc}'@'${h_esc}';" | grep -c '^GRANT' || true)
  grants_total=$((grants_total + grant_count))
done <<< "$user_rows"

# -----------------------------------------------------------------------------
# Summary report.
# -----------------------------------------------------------------------------
count_lines() {
  if [[ -z "$1" ]]; then echo 0; else printf '%s' "$1" | grep -c '^'; fi
}

n_roles=$(count_lines "$roles_found")
n_preserve=$(count_lines "$users_would_preserve")
n_default=$(count_lines "$users_would_default")
n_skip=$(count_lines "$users_would_skip")

report=$(cat <<EOF
================================================================================
Application user assessment (no writes performed)
================================================================================
Roles found on source                : ${n_roles}    (would CREATE ROLE on target)
Users with portable native password  : ${n_preserve} (would preserve via IDENTIFIED VIA)
Users requiring default password     : ${n_default}  (would set default + PASSWORD EXPIRE)
Users that would be SKIPPED          : ${n_skip}     (non-password auth plugin)
Grants found across all users        : ${grants_total} (replay outcome unknown until run)

This is a discovery report. Actual creation, alteration, and grant replay
happens during the run phase via scripts/09_migrate_app_users.sh. Grants are
not classified as compatible/incompatible here because that requires
replaying against the target.

EOF
)

if [[ -n "$roles_found" ]]; then
  report+=$'\n--- Roles found (would be created on target) ---\n'"$roles_found"
fi
if [[ -n "$users_would_preserve" ]]; then
  report+=$'\n--- Users that would migrate with original password ---\n'"$users_would_preserve"
fi
if [[ -n "$users_would_default" ]]; then
  report+=$'\n--- Users that would migrate with default password (PASSWORD EXPIRE) ---\n'"$users_would_default"
  report+=$'    Default password value: '"$APP_USER_DEFAULT_PASSWORD"$'\n'
fi
if [[ -n "$users_would_skip" ]]; then
  report+=$'\n--- Users that would be SKIPPED (non-password plugins) ---\n'"$users_would_skip"
  report+=$'    These users will NOT be created on the target by the run phase.\n'
  report+=$'    Configure auth_socket / unix_socket / auth_pam / mysql_no_login\n'
  report+=$'    manually on target if needed.\n'
fi

report+=$'\n================================================================================\n'

printf '%s' "$report"

if printf '%s' "$report" > "$REPORT_FILE" 2>/dev/null; then
  echo "User assessment report written to: $REPORT_FILE"
else
  echo "NOTE: Could not write user assessment report to $REPORT_FILE; stdout copy above is the record."
fi

echo "Application user assessment completed."
