"""Map report.json's gates/warnings into CheckRow view models.

report_data is the loaded JSON produced by orchestrator/report.py's
Report._data (see Report.set_gates / Report.set_warnings):

    report_data["gates"] = [{"name": str, "status": "PASS"|"FAIL", "details": dict}, ...]
    report_data["warnings"] = [{"name": str, "severity": str, "details": dict}, ...]

No I/O here -- pure mapping of already-loaded data.
"""

from __future__ import annotations

from typing import Any, Mapping

from orchestrator.tui.models import CheckKind, CheckLevel, CheckRow

# Sort key for the final ordering: FAIL, HIGH, MEDIUM, LOW, PASS.
_LEVEL_ORDER = {
    CheckLevel.FAIL: 0,
    CheckLevel.HIGH: 1,
    CheckLevel.MEDIUM: 2,
    CheckLevel.LOW: 3,
    CheckLevel.PASS: 4,
}


def _summary_for(details: Mapping[str, Any]) -> str:
    if "count" in details:
        return f"{details['count']} affected"
    if "value" in details:
        return f"value: {details['value']}"
    return ""


def _gate_row(gate: Mapping[str, Any]) -> CheckRow:
    details = gate.get("details") or {}
    status = gate.get("status")
    if status == "PASS":
        level = CheckLevel.PASS
    else:
        level = CheckLevel.FAIL
        if status != "FAIL":
            # Malformed/truncated report: don't render a silent, unexplained
            # FAIL -- attach the raw value so it's diagnosable, mirroring
            # _warning_row's _raw_severity treatment below.
            details = dict(details)
            details["_raw_status"] = status
    return CheckRow(
        kind=CheckKind.GATE,
        name=gate.get("name", ""),
        level=level,
        summary=_summary_for(details),
        details=details,
        expandable=bool(details),
    )


def _warning_row(warning: Mapping[str, Any]) -> CheckRow:
    details = warning.get("details") or {}
    severity = warning.get("severity")
    try:
        level = CheckLevel(severity)
    except ValueError:
        # Unknown/garbage severity: never silently treat as PASS. Fall back
        # to MEDIUM and attach the raw value so it is inspectable/diagnosable.
        level = CheckLevel.MEDIUM
        details = dict(details)
        details["_raw_severity"] = severity

    return CheckRow(
        kind=CheckKind.WARNING,
        name=warning.get("name", ""),
        level=level,
        summary=_summary_for(details),
        details=details,
        expandable=bool(details),
    )


def to_check_rows(report_data: Mapping[str, Any]) -> tuple[CheckRow, ...]:
    """Map report_data's gates and warnings into a sorted tuple of CheckRow.

    Gates are mapped first (CheckKind.GATE, level PASS/FAIL from status),
    then warnings (CheckKind.WARNING, level = severity, unknown severities
    mapped to MEDIUM). The combined sequence is stable-sorted so the order
    is FAIL, HIGH, MEDIUM, LOW, PASS; ties keep their original input order.
    """
    rows = [_gate_row(gate) for gate in report_data.get("gates") or []]
    rows.extend(_warning_row(warning) for warning in report_data.get("warnings") or [])

    rows.sort(key=lambda row: _LEVEL_ORDER[row.level])
    return tuple(rows)
