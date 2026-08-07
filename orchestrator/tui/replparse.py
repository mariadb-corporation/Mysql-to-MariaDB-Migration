"""Parser for ``SHOW REPLICA STATUS\\G`` / ``SHOW SLAVE STATUS\\G`` output.

TDD unit 1 (design doc tui-phase0-design.md Sec 4.8, line ~359). This must
agree field-for-field with ``scripts/16_binlog_verify.sh`` (its awk parse
at :38-52 and the ``Seconds_Behind_Master``/``NULL`` guard at :106), since
both the TUI and that shell script read the same server output and must
reach the same running/lag verdict.

One deliberate divergence from the shell script, noted here for anyone
diffing behavior: ``16_binlog_verify.sh`` splits each line with
``awk -F': '``, which splits on *every* occurrence of ``": "`` in the
line, not just the first. For a ``Last_SQL_Error`` value that itself
contains ``": "`` (confirmed empirically against
``tests/fixtures/replica/fixture_replica_sql_error_dup_key.txt`` --
its real errno-1062 text contains ``"Error_code: 1062"``), the shell
script's ``lsql=$2`` silently truncates the message at that inner
``": "``. That truncation is a side effect of the shell script's field
splitter, not part of the naming/NULL-handling contract this module is
required to match "exactly" -- the design doc describes the layout as
"leading-whitespace-then-``Field: value``", i.e. one field name then
one value, so this module splits each line on the *first* ``": "``/``:``
only and preserves the full error text unmodified. The TUI needs the
complete error message for its health screen, so reproducing the
shell script's incidental truncation would be strictly worse here.
"""

from __future__ import annotations

from orchestrator.tui.models import ReplicaStatus

_REPLICA_IO_FIELDS = ("Replica_IO_Running", "Slave_IO_Running")
_REPLICA_SQL_FIELDS = ("Replica_SQL_Running", "Slave_SQL_Running")
_SECONDS_BEHIND_FIELDS = ("Seconds_Behind_Source", "Seconds_Behind_Master")


def _split_field_line(line: str) -> tuple[str, str] | None:
    """Split one ``\\G``-format line into ``(field_name, value)``.

    Returns ``None`` if the line has no ``Field_Name:`` prefix at all
    (e.g. the ``*** 1. row ***`` header, or a connection-error string
    with no matching field). Leading whitespace before the field name
    is stripped (the script's ``gsub(/^[[:space:]]+/, "", $1)`` at :40).
    A single leading space after the colon is stripped too, matching
    the ``": "`` separator the real output uses; the rest of the value
    is preserved verbatim.
    """
    if ":" not in line:
        return None
    name, _, value = line.partition(":")
    name = name.strip()
    if not name:
        return None
    if value.startswith(" "):
        value = value[1:]
    return name, value


def parse_replica_status(text: str) -> ReplicaStatus:
    """Parse ``SHOW REPLICA STATUS\\G`` / ``SHOW SLAVE STATUS\\G`` text.

    Accepts both MariaDB/MySQL 8.4+ ``Replica_*`` naming and the legacy
    ``Slave_*`` naming (script comment at :36-37). Empty input (no rows
    -- what a non-replica server returns) and connection-error strings
    that never contain any of the expected fields both fall through to
    an all-``None``/``""`` result with ``raw_naming="unknown"``, same
    as the shell script's "no field matched" branch (:48, :58).
    """
    io_value: str | None = None
    sql_value: str | None = None
    seconds_value: str | None = None
    io_error_lines: list[str] = []
    sql_error_lines: list[str] = []
    saw_replica_naming = False
    saw_slave_naming = False
    current_multiline_field: str | None = None

    for raw_line in text.splitlines():
        split = _split_field_line(raw_line)
        if split is None:
            if current_multiline_field == "Last_IO_Error":
                io_error_lines.append(raw_line)
            elif current_multiline_field == "Last_SQL_Error":
                sql_error_lines.append(raw_line)
            continue
        name, value = split
        current_multiline_field = None

        if name in _REPLICA_IO_FIELDS:
            io_value = value
            if name == "Replica_IO_Running":
                saw_replica_naming = True
            else:
                saw_slave_naming = True
        elif name in _REPLICA_SQL_FIELDS:
            sql_value = value
            if name == "Replica_SQL_Running":
                saw_replica_naming = True
            else:
                saw_slave_naming = True
        elif name in _SECONDS_BEHIND_FIELDS:
            seconds_value = value
            if name == "Seconds_Behind_Source":
                saw_replica_naming = True
            else:
                saw_slave_naming = True
        elif name == "Last_IO_Error":
            io_error_lines = [value]
            current_multiline_field = "Last_IO_Error"
        elif name == "Last_SQL_Error":
            sql_error_lines = [value]
            current_multiline_field = "Last_SQL_Error"

    last_io_error = "\n".join(io_error_lines)
    last_sql_error = "\n".join(sql_error_lines)

    io_running = _parse_running(io_value)
    sql_running = _parse_running(sql_value)
    seconds_behind = _parse_seconds_behind(seconds_value)

    if saw_replica_naming:
        raw_naming = "replica"
    elif saw_slave_naming:
        raw_naming = "slave"
    else:
        raw_naming = "unknown"

    return ReplicaStatus(
        io_running=io_running,
        sql_running=sql_running,
        seconds_behind=seconds_behind,
        last_io_error=last_io_error,
        last_sql_error=last_sql_error,
        raw_naming=raw_naming,
    )


def _parse_running(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "Yes"


def _parse_seconds_behind(value: str | None) -> int | None:
    if value is None:
        return None
    if value == "NULL":
        return None
    try:
        return int(value)
    except ValueError:
        return None
