#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# scripts/09_migrate_app_users.sh
#
# Migrate application users (and roles) from MySQL source to MariaDB target.
#
# Behavior summary (1.2.3):
#   - Wired into every user-facing mode's prepare phase via step_map.yaml.
#   - Distinguishes MySQL 8.0 roles from users and recreates roles via
#     CREATE ROLE IF NOT EXISTS on the target before user grants are replayed.
#   - User creation is plugin-aware:
#     * mysql_native_password (with hash) : hash ported via MariaDB-native
#         IDENTIFIED VIA mysql_native_password USING '<hash>' syntax.
#         User retains the original password.
#     * mysql_native_password (no hash)   : default password + PASSWORD EXPIRE.
#     * caching_sha2_password,
#       sha256_password                   : default password + PASSWORD EXPIRE.
#         These hash formats aren't portable across engines; user must
#         change password on first login.
#     * auth_socket, unix_socket,
#       auth_pam, mysql_no_login,
#       ed25519, gssapi, anything else    : SKIPPED. Non-password auth
#         doesn't translate cleanly; user is reported and must be configured
#         manually on target.
#   - GRANT statements that fail (typically because of MySQL 8.0 dynamic
#     privileges MariaDB doesn't recognize) are dropped whole and reported.
#     No attempt is made to strip individual privileges and retry.
#   - End-of-script report is written to stdout AND to
#     ${RUN_DIR:-artifacts}/user_migration_report.txt for durable record.
# =============================================================================

MIGRATE_APP_USERS="${MIGRATE_APP_USERS:-0}"
if [[ "$MIGRATE_APP_USERS" != "1" ]]; then
  echo "==> Skipping application user migration (MIGRATE_APP_USERS!=1)."
  exit 0
fi

echo "==> Migrate application users from source to target"

# Prefer the mariadb client binary, falling back to 'mysql' if it is absent.
# The MySQL-named binary on modern distributions is often a deprecated alias
# for mariadb and prints a banner on every invocation that floods the log
# (Finding L from 1.2.3 testing). Operators who genuinely need the upstream
# mysql client can still set MYSQL_BIN=mysql in their environment.
MYSQL_BIN="${MYSQL_BIN:-$(command -v mariadb >/dev/null 2>&1 && echo mariadb || echo mysql)}"
MARIADB_BIN="${MARIADB_BIN:-mariadb}"

# Report file path. Falls back to artifacts/ when RUN_DIR isn't set —
# same pattern as the binlog_coords fix in 1.2.1-beta.
REPORT_FILE="${USER_MIGRATION_REPORT:-${RUN_DIR:-artifacts}/user_migration_report.txt}"

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

# Whether users given the default password are forced to change it on first
# login. 1 = PASSWORD EXPIRE (historical default), 0 = no expiry (default
# password is usable immediately). Only affects the default-password bucket
# (native-without-hash and sha2 plugins); hash-ported users keep their
# original password and are never expired regardless of this setting.
APP_USER_PWD_EXPIRE="${APP_USER_PWD_EXPIRE:-1}"
if [[ "$APP_USER_PWD_EXPIRE" == "1" ]]; then
  PWD_EXPIRE_CLAUSE=" PASSWORD EXPIRE"
else
  PWD_EXPIRE_CLAUSE=""
fi

# Whether to attempt porting caching_sha2_password hashes to the target so
# those users keep their original password. 1 = attempt (falls back to the
# default password whenever the target or an individual hash isn't eligible),
# 0 = always use the default password, which was the behavior before 1.4.0.
#
# Eligibility is decided by probing the target for an active
# caching_sha2_password plugin, NOT by comparing version numbers. The plugin
# first shipped in Community 11.4.9 / 11.8.4 and is present in every later
# series, so a probe is correct on 12.x, 13.x, and beyond with no change here.
PORT_SHA2_PASSWORDS="${PORT_SHA2_PASSWORDS:-1}"

# Whether the tool may load the plugin itself when it isn't already active.
# Default 0: probe only, and tell the operator the one command to run. Loading
# a server plugin is a change to the target beyond user creation, so it is
# opt-in rather than something the migration does on its own.
PORT_SHA2_INSTALL_PLUGIN="${PORT_SHA2_INSTALL_PLUGIN:-0}"

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

# Ensure report directory exists. Best-effort; if the path doesn't resolve,
# the script continues and reports to stdout only.
mkdir -p "$(dirname "$REPORT_FILE")" 2>/dev/null || true

sql_escape() {
  local s="$1"
  s="${s//\'/\'\'}"
  printf "%s" "$s"
}

# -----------------------------------------------------------------------------
# caching_sha2_password hash handling.
#
# The hash is 70 bytes of which roughly 20 are a random salt, so it routinely
# contains bytes that are not printable and cannot be read back as text. Two
# consequences shape the code below:
#
#   - The hash is read from the source with HEX() and never as raw text. A
#     --batch read escapes field data (that is what keeps tab-separated output
#     parseable), and for this hash the escaping is not reversible by the SQL
#     parser: MySQL has already escaped some bytes when building the literal,
#     the client escapes the resulting backslash again, and only one level gets
#     undone. Measured on 100 accounts, 55 carried a byte that triggers this.
#
#   - The literal is built here, from the hex, with exactly one level of
#     escaping under our control. Relying on client flags to cancel out two
#     layers works but is fragile, and it breaks outright if the target session
#     has NO_BACKSLASH_ESCAPES set, so the CREATE/ALTER below clears sql_mode
#     for its own session.
#
# MariaDB rejects hex literals and function expressions in the IDENTIFIED
# clause, so a string literal is the only supported route today. MDEV-38524
# tracks accepting 0x... there, which would remove the escaping question and
# the sql_mode dependency entirely.
# -----------------------------------------------------------------------------

# Decide whether a hex-encoded hash can be ported. Prints a short reason and
# returns non-zero when it cannot, so the caller can record why it fell back.
sha2_hash_portable() {
  python3 - "$1" <<'PY'
import sys
try:
    b = bytes.fromhex(sys.argv[1])
except ValueError:
    print("hash is not valid hex"); sys.exit(1)
if len(b) != 70:
    print("unexpected hash length %d (expected 70)" % len(b)); sys.exit(1)
if not b.startswith(b'$A$'):
    print("unrecognized hash prefix"); sys.exit(1)
if any(x >= 0x80 for x in b):
    # Not observed in 200 sampled MySQL 8.4 accounts, but a high byte is not
    # valid utf8mb4 and the behavior of a string literal carrying one is not
    # something to guess at. Fall back rather than create a broken account.
    print("hash contains a byte >= 0x80"); sys.exit(1)
sys.exit(0)
PY
}

# Turn a hex-encoded hash into the inner text of a single-quoted SQL literal.
# Only the bytes that must be escaped are escaped; everything else is emitted
# raw. NUL, LF and CR are escaped so the statement stays a single line and can
# survive a shell variable, which cannot hold a NUL at all.
sha2_literal_from_hex() {
  python3 - "$1" <<'PY'
import sys
b = bytes.fromhex(sys.argv[1])
out = bytearray()
for c in b:
    if   c == 0x00: out += b'\\0'
    elif c == 0x0A: out += b'\\n'
    elif c == 0x0D: out += b'\\r'
    elif c == 0x27: out += b"\\'"
    elif c == 0x5C: out += b'\\\\'
    else: out.append(c)
sys.stdout.buffer.write(out)
PY
}

# Filter client noise that adds 30+ lines per phase to the log without
# carrying actionable information:
#   - "Deprecated program name" banner when mysql is a mariadb alias (Finding L)
#   - "ssl-verify-server-cert is disabled" warning emitted whenever
#     MYSQL_PWD env var is used without TLS (Finding M)
# Stderr from real errors (ERROR 1064 etc.) still passes through.
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
      if MYSQL_PWD="" "$MYSQL_BIN" --protocol=SOCKET -u"$SRC_ADMIN_USER" ${args[@]+"${args[@]}"} \
        --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2); then
        return 0
      fi
    fi
  done

  MYSQL_PWD="$SRC_ADMIN_PASS" "$MYSQL_BIN" --protocol=TCP -h"$SRC_HOST" -P"$SRC_PORT" -u"$SRC_ADMIN_USER" \
    --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2)
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
    TGT_PASS_Q="$(printf '%q' "$TGT_ADMIN_PASS")"
    local tcp_out
    if tcp_out="$(printf '%s\n' "$sql" | ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" \
      "MYSQL_PWD=$TGT_PASS_Q /usr/bin/mariadb --protocol=TCP -h'${TGT_HOST}' -P'${TGT_PORT}' -u'${TGT_ADMIN_USER}' --batch --skip-column-names" 2>&1)"; then
      [[ -n "$tcp_out" ]] && printf '%s\n' "$tcp_out" | mask_client_noise
      return 0
    fi
    printf '%s\n' "$tcp_out" | mask_client_noise >&2
    if [[ "$ALLOW_ROOT_USERS" == "1" && "$TGT_ADMIN_USER" == "root" ]]; then
      ensure_target_socket
      if [[ -n "$TARGET_SOCKET" ]]; then
        printf '%s\n' "$sql" | ssh ${TGT_ADMIN_SSH_OPTS} "${TGT_ADMIN_SSH_USER}@${TGT_SSH_HOST}" \
          "sudo /usr/bin/mariadb --protocol=SOCKET -u'${TGT_ADMIN_USER}' --socket='${TARGET_SOCKET}' --batch --skip-column-names" 2> >(mask_client_noise >&2)
        return 0
      fi
    fi
    return 1
  else
    MYSQL_PWD="$TGT_ADMIN_PASS" "$MARIADB_BIN" --protocol=TCP -h"$TGT_HOST" -P"$TGT_PORT" -u"$TGT_ADMIN_USER" \
      --batch --skip-column-names -e "$sql" 2> >(mask_client_noise >&2)
  fi
}

# -----------------------------------------------------------------------------
# Report accumulators. Each is a newline-separated list filled as we go and
# rendered into the final summary block.
# -----------------------------------------------------------------------------
roles_created=""
users_preserved=""
users_default_password=""
users_skipped=""
users_failed=""
grants_dropped=""
grants_total=0
grants_succeeded=0

# Set by probe_target_sha2_plugin below.
TGT_SHA2_CAPABLE=0
TGT_SHA2_NOTE=""

# -----------------------------------------------------------------------------
# Target capability probe for caching_sha2_password.
#
# Deliberately a probe and not a version comparison. The plugin appeared in
# Community 11.4.9 and 11.8.4 and is present in every later series, so asking
# the target whether the plugin is active is both simpler and correct for
# versions that do not exist yet.
# -----------------------------------------------------------------------------
sha2_plugin_active() {
  local n
  n="$(run_target_sql "SELECT COUNT(*) FROM information_schema.PLUGINS
         WHERE PLUGIN_NAME = 'caching_sha2_password'
           AND PLUGIN_STATUS = 'ACTIVE';" 2>/dev/null | tr -d '[:space:]')"
  [[ "$n" == "1" ]]
}

probe_target_sha2_plugin() {
  if [[ "$PORT_SHA2_PASSWORDS" != "1" ]]; then
    TGT_SHA2_NOTE="disabled by configuration (PORT_SHA2_PASSWORDS=0)"
    return
  fi

  if sha2_plugin_active; then
    TGT_SHA2_CAPABLE=1
    TGT_SHA2_NOTE="plugin active on target"
    return
  fi

  if [[ "$PORT_SHA2_INSTALL_PLUGIN" == "1" ]]; then
    if run_target_sql "INSTALL SONAME 'auth_mysql_sha2';" >/dev/null 2>&1 \
       && sha2_plugin_active; then
      TGT_SHA2_CAPABLE=1
      TGT_SHA2_NOTE="plugin loaded on target by this run"
      return
    fi
    TGT_SHA2_NOTE="plugin not available on target and could not be loaded"
    return
  fi

  TGT_SHA2_NOTE="plugin not active on target"
}

echo "Checking target support for caching_sha2_password..."
probe_target_sha2_plugin
if [[ "$TGT_SHA2_CAPABLE" == "1" ]]; then
  echo "  Original passwords can be kept for caching_sha2_password users (${TGT_SHA2_NOTE})."
else
  echo "  caching_sha2_password users will get the default password (${TGT_SHA2_NOTE})."
  if [[ "$PORT_SHA2_PASSWORDS" == "1" && "$PORT_SHA2_INSTALL_PLUGIN" != "1" ]]; then
    echo "  To keep original passwords, load the plugin on the target and re-run:"
    echo "    INSTALL SONAME 'auth_mysql_sha2';"
    echo "  The plugin requires MariaDB 11.4.9 / 11.8.4 or later."
  fi
fi

# -----------------------------------------------------------------------------
# Role discovery and replay.
#
# In MySQL 8.0+, roles live in mysql.user but are distinguished from users by:
#   account_locked='Y' AND password_expired='Y' AND authentication_string=''
# which is the state CREATE ROLE leaves them in.
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

while IFS=$'\t' read -r r_user r_host; do
  [[ -z "$r_user" ]] && continue
  r_user_esc="$(sql_escape "$r_user")"
  if run_target_sql "CREATE ROLE IF NOT EXISTS '${r_user_esc}';" >/dev/null 2>&1; then
    roles_created+="'${r_user}'@'${r_host}'"$'\n'
  else
    users_failed+="ROLE '${r_user}' (CREATE ROLE failed on target)"$'\n'
  fi
done <<< "$role_rows"

# Build a lookup so the user loop can skip role rows.
role_user_keys=""
while IFS=$'\t' read -r r_user r_host; do
  [[ -z "$r_user" ]] && continue
  role_user_keys+="${r_user}|${r_host}"$'\n'
done <<< "$role_rows"

is_role_row() {
  local u="$1" h="$2"
  grep -Fxq "${u}|${h}" <<< "$role_user_keys"
}

# -----------------------------------------------------------------------------
# User migration loop.
# -----------------------------------------------------------------------------
echo "Migrating application users to target (plugin-aware: native users keep their hash; sha2/no-hash users get the default password [expire=${APP_USER_PWD_EXPIRE}]; non-password plugins are skipped)"

# authentication_string is selected twice on purpose. The text column feeds
# the existing mysql_native_password branch, whose hashes are '*' plus 40 hex
# characters and so survive a --batch read unchanged. The HEX() column is what
# the caching_sha2_password branch uses, because that hash contains bytes that
# do not survive being read as text.
user_rows=$(run_source_admin_sql "
  SELECT user, host, plugin, IFNULL(authentication_string,''),
         HEX(IFNULL(authentication_string,''))
    FROM mysql.user
   WHERE user <> ''
     AND user NOT IN ('root','${admin_user_esc}','debian-sys-maint',
                      'mysql.infoschema','mysql.session','mysql.sys','mysqlxsys');
")

while IFS=$'\t' read -r u h p auth_str auth_hex; do
  [[ -z "$u" ]] && continue

  # Skip role rows — they were handled in the role pass above.
  if is_role_row "$u" "$h"; then
    continue
  fi

  u_esc="$(sql_escape "$u")"
  h_esc="$(sql_escape "$h")"
  p="${p:-}"
  auth_str="${auth_str:-}"
  auth_hex="${auth_hex:-}"
  app_pwd_esc="$(sql_escape "$APP_USER_DEFAULT_PASSWORD")"

  # Plugin-aware user creation. Branches:
  #   - mysql_native_password + hash present : port the hash via MariaDB's
  #       native IDENTIFIED VIA syntax. User keeps original password.
  #   - mysql_native_password, no hash       : default password + PASSWORD
  #       EXPIRE so the operator/app is forced to set one on first login.
  #   - caching_sha2_password, sha256_password : same as above. The hash
  #       formats aren't portable across engines, so default + expire.
  #   - anything else (auth_socket, unix_socket, auth_pam, mysql_no_login,
  #     ed25519, gssapi, etc) : skip entirely. Non-password auth doesn't
  #     translate, and we shouldn't silently force these users onto password
  #     auth without operator knowledge.
  case "$p" in
    mysql_native_password)
      if [[ -n "$auth_str" ]]; then
        auth_esc="$(sql_escape "$auth_str")"
        if run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED VIA mysql_native_password USING '${auth_esc}';" >/dev/null 2>&1 \
           && run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED VIA mysql_native_password USING '${auth_esc}';" >/dev/null 2>&1; then
          users_preserved+="'${u}'@'${h}'"$'\n'
        else
          users_failed+="'${u}'@'${h}' (CREATE/ALTER USER failed)"$'\n'
          continue
        fi
      else
        if run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1 \
           && run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1; then
          users_default_password+="'${u}'@'${h}' (source plugin: ${p}, no source hash)"$'\n'
        else
          users_failed+="'${u}'@'${h}' (CREATE/ALTER USER failed)"$'\n'
          continue
        fi
      fi
      ;;
    caching_sha2_password)
      # Try to keep the original password. Every failure path below falls
      # through to the default-password behavior rather than failing the user,
      # so the worst case is what this script did before 1.4.0.
      sha2_ported=0
      sha2_reason=""

      if [[ "$TGT_SHA2_CAPABLE" != "1" ]]; then
        sha2_reason="$TGT_SHA2_NOTE"
      elif [[ -z "$auth_hex" ]]; then
        sha2_reason="no source hash"
      elif ! sha2_reason="$(sha2_hash_portable "$auth_hex")"; then
        : # sha2_reason now holds the gate's explanation
      else
        sha2_reason=""
        auth_lit="$(sha2_literal_from_hex "$auth_hex")"
        # sql_mode is cleared for this session only. With NO_BACKSLASH_ESCAPES
        # in effect the backslash escapes above are taken literally and the
        # statement fails on hash length, or on a syntax error when the salt
        # happens to contain a quote.
        if run_target_sql "SET SESSION sql_mode='';
              CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}'
                IDENTIFIED WITH 'caching_sha2_password' AS '${auth_lit}';" >/dev/null 2>&1 \
           && run_target_sql "SET SESSION sql_mode='';
              ALTER USER '${u_esc}'@'${h_esc}'
                IDENTIFIED WITH 'caching_sha2_password' AS '${auth_lit}';" >/dev/null 2>&1; then
          # Confirm the bytes landed intact rather than trusting the statement's
          # exit status. A wrong-but-accepted hash would create an account
          # nobody can log into, which is worse than the default password.
          landed="$(run_target_sql "SELECT HEX(JSON_UNQUOTE(JSON_EXTRACT(Priv,'\$.authentication_string')))
                      FROM mysql.global_priv
                     WHERE User='${u_esc}' AND Host='${h_esc}';" 2>/dev/null | tr -d '[:space:]')"
          if [[ -n "$landed" && "$landed" == "$auth_hex" ]]; then
            sha2_ported=1
          else
            sha2_reason="hash did not round-trip on target"
          fi
        else
          sha2_reason="CREATE/ALTER USER with ported hash failed"
        fi
      fi

      if [[ "$sha2_ported" == "1" ]]; then
        users_preserved+="'${u}'@'${h}' (source plugin: ${p})"$'\n'
      else
        if run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1 \
           && run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1; then
          users_default_password+="'${u}'@'${h}' (source plugin: ${p}; ${sha2_reason:-not eligible})"$'\n'
        else
          users_failed+="'${u}'@'${h}' (CREATE/ALTER USER failed)"$'\n'
          continue
        fi
      fi
      ;;
    sha256_password)
      # sha256_password is a different hash format from caching_sha2_password
      # and the MariaDB plugin does not cover it, so this stays on the
      # default-password path.
      if run_target_sql "CREATE USER IF NOT EXISTS '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1 \
         && run_target_sql "ALTER USER '${u_esc}'@'${h_esc}' IDENTIFIED BY '${app_pwd_esc}'${PWD_EXPIRE_CLAUSE};" >/dev/null 2>&1; then
        users_default_password+="'${u}'@'${h}' (source plugin: ${p})"$'\n'
      else
        users_failed+="'${u}'@'${h}' (CREATE/ALTER USER failed)"$'\n'
        continue
      fi
      ;;
    *)
      # Non-password plugin (auth_socket, unix_socket, auth_pam,
      # mysql_no_login, ed25519, gssapi, ...) or empty plugin field.
      # We don't create the user on the target — silently downgrading
      # these to password auth changes the security posture in ways the
      # operator should explicitly opt into.
      users_skipped+="'${u}'@'${h}' (source plugin: ${p:-unknown}; manual handling required)"$'\n'
      continue
      ;;
  esac

  # Replay grants. Strategy per 1.2.3 decision: try as-is, drop the whole
  # grant on failure, record what was dropped. No per-privilege retry.
  grants=$(run_source_admin_sql "SHOW GRANTS FOR '${u_esc}'@'${h_esc}';")
  while IFS= read -r g; do
    [[ -z "$g" ]] && continue
    grants_total=$((grants_total + 1))
    if run_target_sql "$g" >/dev/null 2>&1; then
      grants_succeeded=$((grants_succeeded + 1))
    else
      grants_dropped+="'${u}'@'${h}' -- ${g}"$'\n'
    fi
  done <<< "$grants"
done <<< "$user_rows"

# -----------------------------------------------------------------------------
# Summary report — emitted to stdout AND written to REPORT_FILE.
# -----------------------------------------------------------------------------
count_lines() {
  if [[ -z "$1" ]]; then echo 0; else printf '%s' "$1" | grep -c '^'; fi
}

n_roles=$(count_lines "$roles_created")
n_preserved=$(count_lines "$users_preserved")
n_default=$(count_lines "$users_default_password")
n_skipped=$(count_lines "$users_skipped")
n_failed=$(count_lines "$users_failed")
n_grants_dropped=$(count_lines "$grants_dropped")

if [[ "$APP_USER_PWD_EXPIRE" == "1" ]]; then
  default_pwd_label="PASSWORD EXPIRE set"
else
  default_pwd_label="no expiry (usable as-is)"
fi

if [[ "$TGT_SHA2_CAPABLE" == "1" ]]; then
  sha2_capable_label="yes (${TGT_SHA2_NOTE})"
else
  sha2_capable_label="no (${TGT_SHA2_NOTE})"
fi

report=$(cat <<EOF
================================================================================
Application user migration summary
================================================================================
Roles created on target              : ${n_roles}
Users migrated with original password: ${n_preserved}
Users migrated with default password : ${n_default}  <- ${default_pwd_label}
Users skipped (non-password plugin)  : ${n_skipped}  <- manual handling
Users that failed to migrate         : ${n_failed}
Grants attempted                     : ${grants_total}
Grants successfully replayed         : ${grants_succeeded}
Grants dropped (incompatible)        : ${n_grants_dropped}

caching_sha2_password originals kept : ${sha2_capable_label}

EOF
)

if [[ -n "$roles_created" ]]; then
  report+=$'\n--- Roles created ---\n'"$roles_created"
fi
if [[ -n "$users_preserved" ]]; then
  report+=$'\n--- Users with original password preserved ---\n'"$users_preserved"
fi
if [[ -n "$users_default_password" ]]; then
  if [[ "$APP_USER_PWD_EXPIRE" == "1" ]]; then
    report+=$'\n--- Users with password reset to default (PASSWORD EXPIRE set) ---\n'"$users_default_password"
    report+=$'    Default password value: '"$APP_USER_DEFAULT_PASSWORD"$'\n'
    report+=$'    Note: Users will be prompted to set a new password on first login.\n'
  else
    report+=$'\n--- Users with password reset to default (no expiry) ---\n'"$users_default_password"
    report+=$'    Default password value: '"$APP_USER_DEFAULT_PASSWORD"$'\n'
    report+=$'    Note: The default password is usable immediately; users are NOT forced to change it on first login.\n'
  fi
fi
if [[ -n "$users_skipped" ]]; then
  report+=$'\n--- Users SKIPPED (non-password authentication plugin) ---\n'"$users_skipped"
  report+=$'    These users were NOT created on the target. Common reasons:\n'
  report+=$'    auth_socket / unix_socket : peer-authentication, host-specific.\n'
  report+=$'    auth_pam                  : PAM-backed; configure on target separately.\n'
  report+=$'    mysql_no_login            : no-login accounts; not portable.\n'
  report+=$'    ed25519 / gssapi / other  : MariaDB equivalents may exist; configure manually.\n'
fi
if [[ -n "$users_failed" ]]; then
  report+=$'\n--- Users that FAILED to migrate ---\n'"$users_failed"
fi
if [[ -n "$grants_dropped" ]]; then
  report+=$'\n--- Grants dropped (incompatible with MariaDB) ---\n'"$grants_dropped"
fi

report+=$'\n================================================================================\n'

printf '%s' "$report"

if printf '%s' "$report" > "$REPORT_FILE" 2>/dev/null; then
  echo "User migration report written to: $REPORT_FILE"
else
  echo "NOTE: Could not write user migration report to $REPORT_FILE; stdout copy above is the record."
fi

echo "Application user migration completed."
