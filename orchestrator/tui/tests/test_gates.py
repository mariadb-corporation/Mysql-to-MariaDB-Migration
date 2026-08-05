"""Tests for orchestrator.tui.gates: to_check_rows.

report_data is the loaded JSON shape written by orchestrator/report.py's
Report._data (see report.py: set_gates / set_warnings):

    report_data["gates"] = [{"name": str, "status": "PASS"|"FAIL", "details": dict}, ...]
    report_data["warnings"] = [{"name": str, "severity": str, "details": dict}, ...]

Gate names and warning shapes below are taken from the real checks
emitted by orchestrator/checks.py (not invented) so the fixtures exercise
realistic data.
"""

from __future__ import annotations

from orchestrator.tui.gates import to_check_rows
from orchestrator.tui.models import CheckKind, CheckLevel


def _gate(name, status, details=None):
    return {"name": name, "status": status, "details": details or {}}


def _warning(name, severity, details=None):
    return {"name": name, "severity": severity, "details": details or {}}


def test_gate_fail_status_never_renders_as_pass_and_vice_versa():
    report_data = {
        "gates": [
            _gate(
                "mysql_version_supported",
                "FAIL",
                {"version": "5.6", "allowed": ["5.7", "8.0", "8.4"]},
            ),
            _gate("innodb_file_per_table_is_1", "PASS", {"value": "1"}),
        ],
        "warnings": [],
    }

    rows = to_check_rows(report_data)
    by_name = {row.name: row for row in rows}

    assert by_name["mysql_version_supported"].level == CheckLevel.FAIL
    assert by_name["mysql_version_supported"].level != CheckLevel.PASS
    assert by_name["innodb_file_per_table_is_1"].level == CheckLevel.PASS
    assert by_name["innodb_file_per_table_is_1"].level != CheckLevel.FAIL


def test_unknown_severity_maps_to_medium_not_pass_and_carries_raw_value():
    report_data = {
        "gates": [],
        "warnings": [
            _warning("some_future_check", "WEIRD", {"value": "x"}),
        ],
    }

    rows = to_check_rows(report_data)

    assert len(rows) == 1
    row = rows[0]
    assert row.level == CheckLevel.MEDIUM
    assert row.level != CheckLevel.PASS
    assert row.details.get("_raw_severity") == "WEIRD"


def test_full_ordering_fail_high_medium_low_pass_stable_within_level():
    report_data = {
        "gates": [
            _gate(
                "mysql_version_supported",
                "PASS",
                {"version": "8.0.34", "allowed": ["5.7", "8.0", "8.4"]},
            ),
            _gate(
                "source_databases_exist",
                "FAIL",
                {"requested": ["appdb"], "missing": ["appdb"], "auth_source": "admin"},
            ),
        ],
        "warnings": [
            _warning(
                "mysql_sha_or_caching_auth_users",
                "HIGH",
                {"count": 3, "rows_sample": ["u1", "u2", "u3"]},
            ),
            _warning(
                "functional_indexes_present",
                "HIGH",
                {
                    "count": 1,
                    "rows_sample": ["idx1"],
                    "note": "Expression-based indexes may use MySQL-only syntax unsupported by MariaDB.",
                },
            ),
            _warning(
                "sql_mode_review_recommended",
                "MEDIUM",
                {"value": "STRICT_TRANS_TABLES"},
            ),
            _warning(
                "active_plugins_review_recommended",
                "LOW",
                {"count": 2, "rows_sample": ["p1", "p2"]},
            ),
        ],
    }

    rows = to_check_rows(report_data)
    levels = [row.level for row in rows]

    assert levels == [
        CheckLevel.FAIL,
        CheckLevel.HIGH,
        CheckLevel.HIGH,
        CheckLevel.MEDIUM,
        CheckLevel.LOW,
        CheckLevel.PASS,
    ]

    high_rows = [row for row in rows if row.level == CheckLevel.HIGH]
    assert [row.name for row in high_rows] == [
        "mysql_sha_or_caching_auth_users",
        "functional_indexes_present",
    ]


def test_summary_derivation_count_value_and_neither():
    report_data = {
        "gates": [],
        "warnings": [
            _warning(
                "mysql_sha_or_caching_auth_users",
                "HIGH",
                {"count": 5, "rows_sample": []},
            ),
            _warning(
                "sql_mode_review_recommended",
                "MEDIUM",
                {"value": "STRICT_TRANS_TABLES"},
            ),
            _warning(
                "innodb_fast_shutdown_not_0",
                "MEDIUM",
                {"required_before_shutdown": 0},
            ),
        ],
    }

    rows = to_check_rows(report_data)
    by_name = {row.name: row for row in rows}

    assert by_name["mysql_sha_or_caching_auth_users"].summary == "5 affected"
    assert by_name["sql_mode_review_recommended"].summary == "value: STRICT_TRANS_TABLES"
    assert by_name["innodb_fast_shutdown_not_0"].summary == ""


def test_expandable_reflects_details_presence():
    report_data = {
        "gates": [
            _gate("mysql_version_supported", "PASS", {}),
        ],
        "warnings": [
            _warning(
                "server_collation_0900",
                "HIGH",
                {
                    "value": "utf8mb4_0900_ai_ci",
                    "note": "MySQL 8 default collation utf8mb4_0900_ai_ci is not available in MariaDB.",
                },
            ),
        ],
    }

    rows = to_check_rows(report_data)
    by_name = {row.name: row for row in rows}

    assert by_name["mysql_version_supported"].expandable is False
    assert by_name["server_collation_0900"].expandable is True


def test_realistic_fixture_covers_real_gates_and_warnings():
    report_data = {
        "gates": [
            _gate(
                "mysql_version_supported",
                "PASS",
                {"version": "8.0.34", "allowed": ["5.7", "8.0", "8.4"]},
            ),
            _gate("innodb_file_per_table_is_1", "PASS", {"value": "1"}),
            _gate(
                "source_databases_exist",
                "PASS",
                {"requested": ["appdb"], "missing": [], "auth_source": "admin"},
            ),
            _gate(
                "binlog_source_compatibility",
                "PASS",
                {"mode": "binlog", "selected_schemas": ["appdb"], "failures": {}},
            ),
        ],
        "warnings": [
            _warning(
                "mysql_sha_or_caching_auth_users",
                "HIGH",
                {"count": 2, "rows_sample": ["u1", "u2"]},
            ),
            _warning(
                "functional_indexes_present",
                "HIGH",
                {
                    "count": 1,
                    "rows_sample": ["idx1"],
                    "note": "Expression-based indexes may use MySQL-only syntax unsupported by MariaDB.",
                },
            ),
            _warning(
                "json_columns_present",
                "MEDIUM",
                {"count": 4, "rows_sample": ["c1", "c2", "c3", "c4"]},
            ),
            _warning(
                "active_plugins_review_recommended",
                "LOW",
                {"count": 3, "rows_sample": ["p1", "p2", "p3"]},
            ),
            _warning(
                "sql_mode_review_recommended",
                "MEDIUM",
                {"value": "STRICT_TRANS_TABLES"},
            ),
        ],
    }

    rows = to_check_rows(report_data)

    assert len(rows) == 4 + 5
    kinds = {row.kind for row in rows}
    assert kinds == {CheckKind.GATE, CheckKind.WARNING}

    names = {row.name for row in rows}
    for expected_gate in (
        "mysql_version_supported",
        "innodb_file_per_table_is_1",
        "source_databases_exist",
        "binlog_source_compatibility",
    ):
        assert expected_gate in names


def test_malformed_gate_status_renders_fail_with_raw_status_diagnostic():
    report_data = {
        "gates": [_gate("mysql_version_supported", None, {})],
        "warnings": [],
    }

    rows = to_check_rows(report_data)

    assert len(rows) == 1
    assert rows[0].level == CheckLevel.FAIL
    assert rows[0].details["_raw_status"] is None
    assert rows[0].expandable is True


def test_missing_gates_and_warnings_keys_do_not_crash():
    assert to_check_rows({}) == ()
    assert to_check_rows({"gates": None, "warnings": None}) == ()
