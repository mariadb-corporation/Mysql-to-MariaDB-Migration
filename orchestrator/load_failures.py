"""Load-time failure detection and reporting.

The checks in sql/checks/ mostly report behaviour differences: the object
loads, but behaves differently on MariaDB. The constructs handled here are a
different class — the load itself stops with a server error partway through,
leaving a half-migrated target.

This module turns the raw precheck rows into one ordered list of findings
plus the operator-facing report the assess phase writes and the launcher
shows before it offers to continue.

No version gating is applied. Support for several of these moves between
MariaDB releases and can be backported on request, so every finding is
reported and the operator decides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# A finding either fails the load outright, or needs an answer this tool could
# not get on its own (a probe was skipped, errored, or the target was not
# reachable to compare against).
STATUS_WILL_FAIL = "WILL_FAIL"
STATUS_UNVERIFIED = "UNVERIFIED"

# Class is what the operator is being asked to do about a finding. Severity
# (HIGH/MEDIUM/LOW) stays on the existing scale every other check uses, so
# nothing downstream has to be remapped; class is the new axis.
#
#   LOAD_FAIL  the load stops here. Drives the "proceed anyway?" prompt.
#   SIGN_OFF   loads, but behaves differently afterwards. Shown and
#              acknowledged, does not gate.
#   INFO       resolved on the target; recorded so the report is complete.
CLASS_LOAD_FAIL = "LOAD_FAIL"
CLASS_SIGN_OFF = "SIGN_OFF"
CLASS_INFO = "INFO"


# Operator-facing text per feature id. The ids are the contract between
# sql/checks/load_fail_objects.sql, this table, and report.json consumers;
# changing one means changing all three.
FEATURES: Dict[str, Dict[str, str]] = {
    "lateral_in_object": {
        "title": "LATERAL in stored code",
        "error": "1064",
        "remedy": "Rewrite the LATERAL join before migrating, or exclude the object "
                  "and recreate it on the target by hand.",
    },
    "json_arrow_operator": {
        "title": "JSON -> / ->> operators in stored code",
        "error": "1064",
        "remedy": "Replace -> with JSON_EXTRACT(...) and ->> with "
                  "JSON_UNQUOTE(JSON_EXTRACT(...)), which both servers accept.",
        # Reported regardless of target version, per the no-version-gating
        # rule; the annotation keeps a 13.1 target from reading as a false
        # alarm. See MDEV-13594.
        "fixed_in": {"version": "13.1", "mdev": "MDEV-13594", "source": "tested"},
    },
    "mysql_only_operator": {
        "title": "MySQL-only operator in stored code",
        "error": "1064",
        "remedy": "Rewrite the expression. MEMBER OF has no MariaDB equivalent; "
                  "JSON_CONTAINS is the usual substitute. See MDEV-38591.",
    },
    "mysql_only_function": {
        "title": "MySQL-only function in stored code",
        "error": "1305",
        "remedy": "The function does not exist on MariaDB. Rewrite the object, or "
                  "define a compatibility function on the target before loading.",
    },
    "lang_0900_collation": {
        "title": "Language-specific _0900_ collation",
        "error": "1273",
        "remedy": "Re-collate to a collation MariaDB carries (a uca1400 variant, or "
                  "utf8mb4_unicode_ci) before dumping. Sort order will change.",
    },
    "gb18030_charset": {
        "title": "gb18030 character set",
        "error": "1115",
        "remedy": "Convert the affected objects to utf8mb4 on the source before "
                  "migrating; MariaDB has no gb18030.",
    },
    "table_encryption": {
        "title": "Table ENCRYPTION='Y'",
        "error": "1911",
        "remedy": "Drop the table-level ENCRYPTION attribute before dumping and use "
                  "MariaDB's own encryption on the target.",
    },
    "not_enforced_check": {
        "title": "CHECK ... NOT ENFORCED with rows that violate it",
        "error": "4025",
        "remedy": "Fix or delete the violating rows, or drop the constraint before "
                  "migrating — MariaDB has no unenforced CHECK.",
    },
    "unknown_collation": {
        "title": "Collation the target does not have",
        "error": "1273",
        "remedy": "Re-collate to a collation the target carries before dumping, or "
                  "upgrade the target. Sort order will change.",
    },
    "unknown_charset": {
        "title": "Character set the target does not have",
        "error": "1115",
        "remedy": "Convert the affected objects on the source before migrating.",
    },
    "schema_default_encryption": {
        "title": "Tables encrypted by a database DEFAULT ENCRYPTION='Y'",
        "error": "-",
        "remedy": "Nothing errors: MariaDB drops the clause and these tables "
                  "arrive unencrypted, as does anything created in the database "
                  "afterwards. Enable encryption explicitly on the target; for "
                  "inplace, decrypt on the source first.",
    },

    # --- SIGN_OFF: these load, then behave differently ---
    "invisible_columns": {
        "title": "Invisible columns",
        "class": CLASS_SIGN_OFF,
        "severity": "HIGH",
        "change": "Become visible. SELECT * returns them and INSERT without a "
                  "column list needs a value for them.",
    },
    "invisible_indexes": {
        "title": "Invisible indexes",
        "class": CLASS_SIGN_OFF,
        "severity": "MEDIUM",
        "change": "Become visible; the optimizer will use them, which can change "
                  "plans that were tuned with them hidden.",
    },
    "srid_columns": {
        "title": "SRID on spatial columns",
        "class": CLASS_SIGN_OFF,
        "severity": "MEDIUM",
        "change": "The column's SRID restriction is dropped; values in other "
                  "reference systems become insertable.",
    },
    "not_enforced_check_clean": {
        "title": "CHECK ... NOT ENFORCED with no violating rows",
        "class": CLASS_SIGN_OFF,
        "severity": "HIGH",
        "change": "Enforcement switches on. The constraint loads fine today, but "
                  "writes that the source accepted will be rejected afterwards.",
    },
    "general_tablespace": {
        "title": "Table in an InnoDB general tablespace",
        "class": CLASS_SIGN_OFF,
        "severity": "MEDIUM",
        "change": "The dump omits CREATE TABLESPACE but keeps the TABLESPACE "
                  "clause, which MariaDB accepts and stores file-per-table. Any "
                  "storage placement that relied on the tablespace is lost.",
    },
    "autoextend_size": {
        "title": "AUTOEXTEND_SIZE",
        "class": CLASS_SIGN_OFF,
        "severity": "LOW",
        "change": "Dropped. Tablespace extension reverts to the MariaDB default.",
    },

    # --- INFO: resolved on this target, recorded for completeness ---
    "aliased_0900_collation": {
        "title": "_0900_ collation available as an alias on the target",
        "class": CLASS_INFO,
        "severity": "LOW",
        "change": "Maps to a utf8mb4_uca1400_nopad_* collation. Loads and sorts; "
                  "UCA 9.0.0 vs 14.0.0 weights can still differ.",
    },
}

# Everything not marked otherwise is a load failure at HIGH, which is what the
# original six were.
DEFAULT_CLASS = CLASS_LOAD_FAIL
DEFAULT_SEVERITY = "HIGH"


def feature_class(feature: str) -> str:
    return FEATURES.get(feature, {}).get("class", DEFAULT_CLASS)


def feature_severity(feature: str) -> str:
    return FEATURES.get(feature, {}).get("severity", DEFAULT_SEVERITY)

@dataclass
class LoadFailure:
    feature: str
    error: str
    obj_schema: str
    object_type: str
    object_name: str
    detail: str
    status: str = STATUS_WILL_FAIL
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        meta = FEATURES.get(self.feature, {})
        d = {
            "feature": self.feature,
            "class": feature_class(self.feature),
            "severity": feature_severity(self.feature),
            "error": self.error,
            "schema": self.obj_schema,
            "object_type": self.object_type,
            "object_name": self.object_name,
            "detail": self.detail,
            "status": self.status,
        }
        if self.note:
            d["note"] = self.note
        if meta.get("fixed_in"):
            d["fixed_in"] = meta["fixed_in"]
        return d


@dataclass
class LoadFailureSet:
    findings: List[LoadFailure] = field(default_factory=list)
    probe_skipped: bool = False
    # False when the target could not be queried (staged dump host), which
    # makes collation and charset findings approximate rather than certain.
    target_verified: bool = True
    target_version: str = ""

    @property
    def will_fail(self) -> List[LoadFailure]:
        return [f for f in self.findings if f.status == STATUS_WILL_FAIL]

    @property
    def unverified(self) -> List[LoadFailure]:
        return [f for f in self.findings if f.status == STATUS_UNVERIFIED]

    def of_class(self, cls: str) -> List[LoadFailure]:
        return [f for f in self.findings if feature_class(f.feature) == cls]

    @property
    def load_fail_findings(self) -> List[LoadFailure]:
        return self.of_class(CLASS_LOAD_FAIL)

    @property
    def sign_off_findings(self) -> List[LoadFailure]:
        return self.of_class(CLASS_SIGN_OFF)

    @property
    def info_findings(self) -> List[LoadFailure]:
        return self.of_class(CLASS_INFO)

    @property
    def count(self) -> int:
        """Load failures only. This is what the gate prompt is about."""
        return len(self.load_fail_findings)

    @property
    def sign_off_count(self) -> int:
        return len(self.sign_off_findings)

    def as_dict(self) -> Dict[str, Any]:
        by_feature: Dict[str, int] = {}
        for f in self.findings:
            by_feature[f.feature] = by_feature.get(f.feature, 0) + 1
        return {
            "count": self.count,
            "sign_off_count": self.sign_off_count,
            "info_count": len(self.info_findings),
            "will_fail": len(self.will_fail),
            "unverified": len(self.unverified),
            "target_verified": self.target_verified,
            "by_feature": by_feature,
            "findings": [f.as_dict() for f in self.findings],
        }


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


# Where a sign-off TSV's columns sit. Each entry is
# (schema_idx, object_type, name_builder, detail_idx).
SIGN_OFF_LAYOUT: Dict[str, Dict[str, Any]] = {
    "invisible_columns": {"type": "COLUMN", "name": (1, 2), "detail": None},
    "invisible_indexes": {"type": "INDEX", "name": (1, 2), "detail": None},
    "srid_columns": {"type": "COLUMN", "name": (1, 2), "detail": 3},
    "general_tablespace": {"type": "TABLE", "name": (1,), "detail": 2},
    "autoextend_size": {"type": "TABLE", "name": (1,), "detail": 2},
    # row is (schema, encrypted_table_count) -- the finding is the database's
    # default, so the count lands in detail rather than an object name.
    "schema_default_encryption": {"type": "SCHEMA", "name": (), "detail": 1},
}


def _collect_sign_off(rows_by_feature: Dict[str, Sequence[Sequence[str]]]) -> List[LoadFailure]:
    """Turn each sign-off TSV into findings using its declared layout."""
    out: List[LoadFailure] = []
    for feature, rows in (rows_by_feature or {}).items():
        layout = SIGN_OFF_LAYOUT.get(feature)
        if not layout or not rows:
            continue
        for row in rows:
            if not row or not row[0].strip():
                continue
            try:
                name = ".".join(row[i].strip() for i in layout["name"])
                detail_idx = layout["detail"]
                detail = row[detail_idx].strip() if detail_idx is not None and len(row) > detail_idx else ""
            except IndexError:
                continue
            out.append(
                LoadFailure(
                    feature=feature,
                    error=FEATURES.get(feature, {}).get("error", "-"),
                    obj_schema=row[0].strip(),
                    object_type=layout["type"],
                    object_name=name,
                    detail=detail,
                )
            )
    return out


def _collect_collations(
    rows: Sequence[Sequence[str]],
    target_catalog: Optional[Dict[str, Any]],
) -> List[LoadFailure]:
    """Compare the source's collations and charsets against the target's.

    Anything the source uses and the target does not have is a load failure:
    1273 for a collation, 1115 for a character set. A _0900_ collation the
    target DOES have is recorded as INFO -- it resolves to a uca1400 alias and
    loads, but the UCA weight generation differs, which is worth seeing.

    With no target catalog the comparison cannot be made, so _0900_ names are
    reported unverified rather than silently passed.
    """
    out: List[LoadFailure] = []
    have_target = bool(target_catalog)
    tgt_coll = (target_catalog or {}).get("collations", set())
    tgt_chars = (target_catalog or {}).get("charsets", set())

    for row in rows:
        if len(row) < 5:
            continue
        schema, obj_type, obj_name, kind, value = (c.strip() for c in row[:5])
        if not value:
            continue
        lowered = value.lower()
        is_collation = kind.upper() == "COLLATION"
        present = lowered in (tgt_coll if is_collation else tgt_chars)

        if not have_target:
            if "_0900_" in lowered or lowered == "gb18030":
                out.append(LoadFailure(
                    feature="unknown_collation" if is_collation else "unknown_charset",
                    error="1273" if is_collation else "1115",
                    obj_schema=schema, object_type=obj_type, object_name=obj_name,
                    detail=value, status=STATUS_UNVERIFIED,
                    note="target not reachable; not confirmed",
                ))
            continue

        if present:
            if is_collation and "_0900_" in lowered:
                out.append(LoadFailure(
                    feature="aliased_0900_collation", error="-",
                    obj_schema=schema, object_type=obj_type,
                    object_name=obj_name, detail=value,
                ))
            continue

        out.append(LoadFailure(
            feature="unknown_collation" if is_collation else "unknown_charset",
            error="1273" if is_collation else "1115",
            obj_schema=schema, object_type=obj_type,
            object_name=obj_name, detail=value,
        ))
    return out


def collect(
    object_rows: Sequence[Sequence[str]],
    not_enforced_rows: Sequence[Sequence[str]],
    count_violations: Optional[Callable[[str, str, str], Optional[int]]] = None,
    log: Optional[Callable[[str], None]] = None,
    source_collations: Optional[Sequence[Sequence[str]]] = None,
    target_catalog: Optional[Dict[str, Any]] = None,
    sign_off_rows: Optional[Dict[str, Sequence[Sequence[str]]]] = None,
) -> LoadFailureSet:
    """Build the finding list from the two precheck TSVs.

    object_rows come from load_fail_objects.sql and are failures as they
    stand. not_enforced_rows are candidates: count_violations decides, by
    running one COUNT per constraint against the source, whether the table
    actually holds rows the predicate rejects.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    result = LoadFailureSet()
    result.target_verified = bool(target_catalog)
    result.target_version = (target_catalog or {}).get("version", "")

    result.findings.extend(_collect_collations(source_collations or [], target_catalog))
    result.findings.extend(_collect_sign_off(sign_off_rows or {}))

    for row in object_rows:
        if len(row) < 6:
            continue
        result.findings.append(
            LoadFailure(
                feature=row[1].strip(),
                error=row[2].strip(),
                obj_schema=row[0].strip(),
                object_type=row[3].strip(),
                object_name=row[4].strip(),
                detail=row[5].strip(),
            )
        )

    if not not_enforced_rows:
        return result

    # The probe is a full scan per constraint. Skippable, and capped so a
    # source with hundreds of unenforced constraints cannot turn a read-only
    # assessment into a long job.
    if _truthy(os.environ.get("MIGRATOR_SKIP_CHECK_VIOLATION_PROBE")) or count_violations is None:
        result.probe_skipped = True
        for row in not_enforced_rows:
            if len(row) < 4:
                continue
            result.findings.append(
                LoadFailure(
                    feature="not_enforced_check",
                    error=FEATURES["not_enforced_check"]["error"],
                    obj_schema=row[0].strip(),
                    object_type="CONSTRAINT",
                    object_name=f"{row[1].strip()}.{row[2].strip()}",
                    detail=row[3].strip(),
                    status=STATUS_UNVERIFIED,
                    note="violating-row probe skipped",
                )
            )
        return result

    try:
        max_probe = int(os.environ.get("MIGRATOR_MAX_CHECK_PROBE", "50"))
    except ValueError:
        max_probe = 50

    if len(not_enforced_rows) > max_probe:
        _log(
            f"NOTE: {len(not_enforced_rows)} unenforced CHECK constraints found; "
            f"probing the first {max_probe} for violating rows "
            "(raise MIGRATOR_MAX_CHECK_PROBE to cover the rest)."
        )

    for idx, row in enumerate(not_enforced_rows):
        if len(row) < 4:
            continue
        schema, table, name, clause = (c.strip() for c in row[:4])
        object_name = f"{table}.{name}"

        if idx >= max_probe:
            result.findings.append(
                LoadFailure(
                    feature="not_enforced_check",
                    error=FEATURES["not_enforced_check"]["error"],
                    obj_schema=schema,
                    object_type="CONSTRAINT",
                    object_name=object_name,
                    detail=clause,
                    status=STATUS_UNVERIFIED,
                    note="not probed (MIGRATOR_MAX_CHECK_PROBE reached)",
                )
            )
            continue

        violations = count_violations(schema, table, clause)
        if violations is None:
            result.findings.append(
                LoadFailure(
                    feature="not_enforced_check",
                    error=FEATURES["not_enforced_check"]["error"],
                    obj_schema=schema,
                    object_type="CONSTRAINT",
                    object_name=object_name,
                    detail=clause,
                    status=STATUS_UNVERIFIED,
                    note="violating-row count could not be evaluated",
                )
            )
        elif violations > 0:
            result.findings.append(
                LoadFailure(
                    feature="not_enforced_check",
                    error=FEATURES["not_enforced_check"]["error"],
                    obj_schema=schema,
                    object_type="CONSTRAINT",
                    object_name=object_name,
                    detail=clause,
                    note=f"{violations} violating row(s)",
                )
            )
        else:
            # Loads fine, then enforcement switches on: writes the source
            # accepted will be rejected afterwards. Sign-off, not silence.
            result.findings.append(
                LoadFailure(
                    feature="not_enforced_check_clean",
                    error="-",
                    obj_schema=schema,
                    object_type="CONSTRAINT",
                    object_name=object_name,
                    detail=clause,
                    note="no violating rows today",
                )
            )

    return result


def _group(findings: Sequence[LoadFailure]) -> Dict[str, List[LoadFailure]]:
    grouped: Dict[str, List[LoadFailure]] = {}
    for f in findings:
        grouped.setdefault(f.feature, []).append(f)
    return grouped


def _render_group(lines: List[str], items: Sequence[LoadFailure], feature: str, sample: int) -> None:
    meta = FEATURES.get(feature, {})
    title = meta.get("title", feature)
    error = meta.get("error", items[0].error if items else "")
    header = f"{title}  -- {len(items)} object(s)"
    if error and error != "-":
        header = f"{title}  (error {error})  -- {len(items)} object(s)"
    lines.append("-" * 72)
    lines.append(header)
    fixed_in = meta.get("fixed_in")
    if fixed_in:
        ver = fixed_in.get("version") if isinstance(fixed_in, dict) else fixed_in
        ref = f" ({fixed_in.get('mdev')})" if isinstance(fixed_in, dict) and fixed_in.get("mdev") else ""
        lines.append(f"  Not a failure on MariaDB {ver} or later{ref}; still listed "
                     "because support is version-dependent.")
    lines.append("-" * 72)
    for item in items[:sample]:
        where = f"{item.obj_schema}.{item.object_name}" if item.object_name else item.obj_schema
        lines.append(f"  {item.object_type:<12} {where}")
        trailer = item.detail
        if item.note:
            trailer = f"{trailer}  [{item.note}]" if trailer else f"[{item.note}]"
        if trailer:
            lines.append(f"               {trailer}")
    if len(items) > sample:
        lines.append(f"  ... and {len(items) - sample} more (full list in report.json)")
    text = meta.get("remedy") or meta.get("change")
    if text:
        label = "Remedy" if meta.get("remedy") else "What changes"
        lines.append("")
        lines.append(f"  {label}: {text}")
    lines.append("")


def render_report(
    result: LoadFailureSet,
    databases: Sequence[str],
    sample: int = 20,
    source_host: str = "",
    generated_at: str = "",
) -> str:
    """The text written to load_failure_report.txt and shown by the launcher.

    Two sections. Load failures stop the migration and drive the prompt;
    sign-off items load and then behave differently, so they are shown and
    acknowledged but never block. Informational findings are listed last so
    the report accounts for everything that was looked at.
    """
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append("MIGRATION FINDINGS")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"Databases in scope: {', '.join(databases) if databases else '(all)'}")
    if source_host:
        lines.append(f"Source: {source_host}")
    if result.target_version:
        lines.append(f"Target: MariaDB {result.target_version}")
    if generated_at:
        # Staged load happens on another host, days later; without this an old
        # report reads as current.
        lines.append(f"Assessed: {generated_at}")
    if not result.target_verified:
        lines.append("")
        lines.append("NOTE: the target could not be queried, so collation and character")
        lines.append("      set findings below are based on the source alone and have")
        lines.append("      not been confirmed against the server that will receive it.")
    lines.append("")

    if not result.findings:
        lines.append("No findings.")
        lines.append("")
        return "\n".join(lines)

    load_fail = _group(result.load_fail_findings)
    sign_off = _group(result.sign_off_findings)
    info = _group(result.info_findings)

    if load_fail:
        lines.append("#" * 72)
        lines.append(f"# LOAD FAILURES -- {result.count} finding(s)")
        lines.append("#" * 72)
        lines.append("")
        lines.append(
            "The dump restores until it reaches one of these, then the client "
            "reports the error and the load stops, leaving the target partly "
            "migrated. Address them on the source first."
        )
        lines.append("")
        for feature, items in load_fail.items():
            _render_group(lines, items, feature, sample)

    if sign_off:
        lines.append("#" * 72)
        lines.append(f"# SIGN-OFF -- {result.sign_off_count} finding(s)")
        lines.append("#" * 72)
        lines.append("")
        lines.append(
            "These load without error and then behave differently on MariaDB. "
            "Nothing stops; the point is that someone sees them and accepts the "
            "change before it reaches production."
        )
        lines.append("")
        for feature, items in sign_off.items():
            _render_group(lines, items, feature, sample)

    if info:
        lines.append("#" * 72)
        lines.append(f"# INFORMATIONAL -- {len(result.info_findings)} finding(s)")
        lines.append("#" * 72)
        lines.append("")
        for feature, items in info.items():
            meta = FEATURES.get(feature, {})
            lines.append("-" * 72)
            lines.append(f"{meta.get('title', feature)}  -- {len(items)} object(s)")
            lines.append("-" * 72)
            # One line per distinct value rather than per object: a server
            # whose default collation is utf8mb4_0900_ai_ci puts it on every
            # table and most columns, and a thousand identical lines bury the
            # sections that need reading.
            by_value: Dict[str, int] = {}
            for item in items:
                by_value[item.detail] = by_value.get(item.detail, 0) + 1
            for value, count in sorted(by_value.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {value:<32} {count} object(s)")
            change = meta.get("change")
            if change:
                lines.append("")
                lines.append(f"  What changes: {change}")
            lines.append("")
        lines.append("  Per-object detail is in report.json.")
        lines.append("")

    lines.append("=" * 72)
    lines.append(
        "Support for some of these moves between MariaDB releases and can be "
        "backported on request. Findings are not gated on target version."
    )
    lines.append("=" * 72)
    lines.append("")
    return "\n".join(lines)


def render_summary(result: LoadFailureSet) -> List[str]:
    """Short console block printed by assess, ahead of the full report."""
    if not result.findings:
        return []
    lines: List[str] = [""]
    if result.count:
        lines.append(f"LOAD FAILURES: {result.count} finding(s) that will stop the load on MariaDB.")
        for feature, items in _group(result.load_fail_findings).items():
            meta = FEATURES.get(feature, {})
            fx = meta.get("fixed_in")
            ver = fx.get("version") if isinstance(fx, dict) else fx
            suffix = f" [fixed in {ver}]" if ver else ""
            lines.append(f"  - {meta.get('title', feature)} (error {meta.get('error', items[0].error)}): {len(items)}{suffix}")
        lines.append("")
    if result.sign_off_count:
        lines.append(f"SIGN-OFF: {result.sign_off_count} finding(s) that load, then behave differently.")
        for feature, items in _group(result.sign_off_findings).items():
            meta = FEATURES.get(feature, {})
            lines.append(f"  - {meta.get('title', feature)}: {len(items)}")
        lines.append("")
    return lines
