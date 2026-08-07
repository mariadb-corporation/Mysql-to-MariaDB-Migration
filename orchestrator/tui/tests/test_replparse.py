"""Tests for orchestrator.tui.replparse.parse_replica_status.

TDD unit 1 (design doc tui-phase0-design.md Sec 4.8, line ~359). One test
per fixture in ``tests/fixtures/replica/`` (see that directory's README.md
for exact provenance -- real capture vs hand-authored -- of each file),
plus one synthetic ``Replica_*``-naming case constructed inline below
because no real MariaDB version defaulting to ``Replica_*`` naming was
available to capture.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.tui.models import ReplicaStatus
from orchestrator.tui.replparse import parse_replica_status

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "replica"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


def test_healthy_10_6_slave_naming() -> None:
    """Real capture: MariaDB 10.6.15, Slave_* naming, both threads running."""
    text = _read_fixture("fixture_replica_healthy_10.6.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=True,
        seconds_behind=0,
        last_io_error="",
        last_sql_error="",
        raw_naming="slave",
    )


def test_healthy_docker_10_11_slave_naming() -> None:
    """Real capture: Docker MariaDB 10.11 master/replica pair, healthy."""
    text = _read_fixture("fixture_replica_healthy_docker_10.11.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=True,
        seconds_behind=0,
        last_io_error="",
        last_sql_error="",
        raw_naming="slave",
    )


def test_null_lag_sql_thread_stopped() -> None:
    """Real capture: STOP SLAVE SQL_THREAD; Seconds_Behind_Master: NULL.

    Confirms the NULL guard (script's `!= "NULL"` at :106) -- the literal
    string "NULL" must parse to seconds_behind=None, not an int or a
    ValueError.
    """
    text = _read_fixture("fixture_replica_null_lag.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=False,
        seconds_behind=None,
        last_io_error="",
        last_sql_error="",
        raw_naming="slave",
    )


def test_sql_error_dup_key_round_trips_full_text() -> None:
    """Real capture: genuine errno 1062 duplicate-key Last_SQL_Error.

    The real error text contains an inner ": " (in "Error_code: 1062"),
    which is exactly the case where the shell script's `awk -F': '`
    parse would truncate (see replparse.py's module docstring for the
    empirical confirmation). This module must NOT truncate here --
    assert the full message round-trips unmodified.
    """
    text = _read_fixture("fixture_replica_sql_error_dup_key.txt")
    status = parse_replica_status(text)
    expected_error = (
        "Could not execute Write_rows_v1 event on table fixturedb.t1; "
        "Duplicate entry '2' for key 'PRIMARY', Error_code: 1062; "
        "handler error HA_ERR_FOUND_DUPP_KEY; the event's master log "
        "mariadb-bin.000002, end_log_pos 1514"
    )
    assert status.last_sql_error == expected_error
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=False,
        seconds_behind=None,
        last_io_error="",
        last_sql_error=expected_error,
        raw_naming="slave",
    )


def test_multiline_sql_error_preserves_both_lines() -> None:
    """Hand-authored: Last_SQL_Error value spans two physical lines.

    Continuation lines (no ``Field_Name:`` prefix) that follow a
    Last_SQL_Error/Last_IO_Error line must be appended to that field's
    value rather than silently dropped -- otherwise a failing multi-line
    SQL statement embedded in the error text would be truncated at the
    first newline, contradicting this module's own docstring commitment
    to preserving the complete error message.
    """
    text = _read_fixture("fixture_replica_sql_error_multiline.txt")
    status = parse_replica_status(text)
    assert "\n" in status.last_sql_error
    assert "Error executing row event" in status.last_sql_error
    assert "BEGIN INSERT INTO fixturedb.t1 VALUES (1); END" in status.last_sql_error


def test_not_a_replica_empty_output() -> None:
    """Real capture: empty file -- server isn't configured as a replica.

    Zero rows is not an error; SHOW REPLICA STATUS legitimately returns
    nothing in this case. Everything should come back None/"" and
    raw_naming="unknown".
    """
    text = _read_fixture("fixture_replica_not_a_replica.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=None,
        sql_running=None,
        seconds_behind=None,
        last_io_error="",
        last_sql_error="",
        raw_naming="unknown",
    )


def test_mysql57_slave_naming_tolerates_unknown_fields() -> None:
    """Hand-authored from documented MySQL 5.7 SHOW SLAVE STATUS format.

    Includes MySQL-only fields absent from MariaDB (Master_UUID,
    Executed_Gtid_Set, Channel_Name, etc.) -- the parser must ignore
    these without erroring, and still resolve Slave_* naming correctly.
    """
    text = _read_fixture("fixture_slave_status_mysql57.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=True,
        seconds_behind=0,
        last_io_error="",
        last_sql_error="",
        raw_naming="slave",
    )


def test_connection_error_string_yields_all_none_unknown() -> None:
    """Hand-authored: a client connection failure, no result set at all.

    Must not crash, and must fall through to the same all-None/"unknown"
    result as the empty-output case since no expected field is present.
    """
    text = _read_fixture("fixture_replica_connection_error.txt")
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=None,
        sql_running=None,
        seconds_behind=None,
        last_io_error="",
        last_sql_error="",
        raw_naming="unknown",
    )


def test_replica_naming_synthetic() -> None:
    """Inline-constructed, NOT fixture-file-backed.

    No real MariaDB version defaulting to Replica_*/Source naming
    (MySQL 8.0.22+/8.4-style aliasing) was available to capture, so this
    case is hand-built here rather than added as a fixture file. A
    future reader should not mistake this for a real capture.
    """
    text = (
        "*************************** 1. row ***************************\n"
        "            Replica_IO_State: Waiting for source to send event\n"
        "                 Source_Host: 10.0.0.20\n"
        "           Replica_IO_Running: Yes\n"
        "          Replica_SQL_Running: Yes\n"
        "                  Last_IO_Errno: 0\n"
        "                  Last_IO_Error: \n"
        "                 Last_SQL_Errno: 0\n"
        "                 Last_SQL_Error: \n"
        "       Seconds_Behind_Source: 2\n"
    )
    status = parse_replica_status(text)
    assert status == ReplicaStatus(
        io_running=True,
        sql_running=True,
        seconds_behind=2,
        last_io_error="",
        last_sql_error="",
        raw_naming="replica",
    )


def test_mixed_replica_and_slave_naming_resolves_to_replica() -> None:
    """Hand-authored, derived from ``fixture_replica_healthy_docker_10.11.txt``.

    Reflects real MariaDB 10.5+ behavior: ``Replica_IO_Running``/
    ``Replica_SQL_Running`` emitted alongside the older
    ``Seconds_Behind_Master`` (not ``Seconds_Behind_Source``). Both
    ``saw_replica_naming`` and ``saw_slave_naming`` become True in this
    case; the ``if saw_replica_naming: ... elif saw_slave_naming: ...``
    order means Replica naming wins. This test pins that as a checked
    decision, not an accident of code order.
    """
    text = _read_fixture("fixture_replica_mixed_naming.txt")
    status = parse_replica_status(text)
    assert status.io_running is True
    assert status.sql_running is True
    assert status.seconds_behind == 0
    assert status.raw_naming == "replica"


def test_connecting_state_maps_to_not_running() -> None:
    """A ``Connecting`` retry state must resolve to False, not None.

    Per the finding, only a genuinely missing field should yield None
    ("unknown"); any other observed string value -- including the
    legitimately common ``Connecting`` retry state -- must resolve to
    False, matching the shell script's boolean (not tri-state) verdict.
    """
    text = (
        "*************************** 1. row ***************************\n"
        "              Slave_IO_Running: Connecting\n"
        "             Slave_SQL_Running: Yes\n"
        "         Seconds_Behind_Master: NULL\n"
    )
    status = parse_replica_status(text)
    assert status.io_running is False
    assert status.sql_running is True


def test_empty_string_input() -> None:
    """Zero-length input (not even a trailing newline) behaves like the
    empty-file fixture."""
    status = parse_replica_status("")
    assert status == ReplicaStatus(
        io_running=None,
        sql_running=None,
        seconds_behind=None,
        last_io_error="",
        last_sql_error="",
        raw_naming="unknown",
    )
