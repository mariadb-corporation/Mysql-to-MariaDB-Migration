#!/usr/bin/env bash
# Gate: reject client option files that alter output format.
#
# Options like [client] verbose or vertical change the framing of client
# output. Scripts here capture single values from the client (server
# variables, gate comparisons, version probes); altered framing silently
# corrupts those captures, and the failure surfaces much later at an
# unrelated step. This gate stops the run at the point the cause is legible.
#
# The check is local to the tooling host and needs no server connection:
# --print-defaults asks each client to report its own resolved options.
# That covers all option-file layering (/etc/my.cnf, ~/.my.cnf, MYSQL_HOME,
# !includedir) without parsing any of it.

# Options that alter output format or destination. Deliberately excludes
# 'tee' (duplicates output, does not corrupt stdout).
_MIGRATOR_UNSAFE_CLIENT_OPTS='verbose|vertical|auto-vertical-output|xml|html|column-type-info|show-warnings|pager'

gate_client_defaults_clean() {
  local bin="$1"
  local out hits

  # Absent binaries are not this gate's concern — scripts that need a
  # given client check for it themselves. Nothing to inspect, so pass.
  if ! command -v "$bin" >/dev/null 2>&1; then
    return 0
  fi

  if ! out="$( "$bin" --print-defaults 2>/dev/null )"; then
    printf 'GATE client_defaults_clean: %s --print-defaults failed\n' "$bin" >&2
    return 1
  fi

  # --print-defaults echoes the resolved argv. Match option tokens only, so
  # credentials present in the same output are never selected or printed.
  hits="$( tr ' ' '\n' <<<"$out" \
           | grep -E "^--(${_MIGRATOR_UNSAFE_CLIENT_OPTS})(=|$)" \
           | sort -u || true )"

  if [[ -z "$hits" ]]; then
    return 0
  fi

    if [[ -z "${_MIGRATOR_GATE_EXPLAINED:-}" ]]; then
    printf 'GATE client_defaults_clean: FAILED\n' >&2
    printf '  These output-altering options are active in your client config:\n' >&2
    sed 's/^/    /' <<<"$hits" >&2
    printf '  They corrupt the values this program reads back from the servers.\n' >&2
    printf '  Remove them from the [client], [mariadb], [mysql] or [mariadb-dump]\n' >&2
    printf '  sections of your option files, then re-run.\n' >&2
    printf '  Affected client: %s\n' "$bin" >&2
    _MIGRATOR_GATE_EXPLAINED=1
  else
    printf '  Affected client: %s\n' "$bin" >&2
  fi
  return 1
}
