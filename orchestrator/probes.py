"""Ask the target what it supports, instead of inferring it from a version.

Only some constructs are worth probing. What decides is whether the construct
reaches the target's parser at all, and that is a property of the dump, not of
either server:

  wrapped    mariadb-dump copies MySQL attributes verbatim inside version
             comments -- /*!80023 INVISIBLE */, /*!80016 NOT ENFORCED */,
             /*!50100 TABLESPACE */, /*!80003 SRID */. MariaDB skips comments
             numbered 50700-99999 (MDEV-5009), so the attribute is discarded
             before the parser sees it. Target support is irrelevant: the
             construct is dropped on every version. Probing these would be
             actively misleading -- AUTOEXTEND_SIZE parses on 12.3+, yet the
             dump hides it there too. See MDEV-41696.

  bare       routine and view bodies, collations, character sets. Nothing
             wraps these, so the target decides, and it genuinely varies:
             ST_IsValid resolves on 12.3 and 13.1 but not on 11.8, and ->>
             parses on 13.1 but not on 11.8 or 12.3. These are what this
             module probes.

  executed   ENCRYPTION='Y' arrives bare and parses on every MariaDB version
             tested, then fails at execution with 1911. Parsing proves
             nothing here, so it is classified from tested evidence and not
             probed.

Nothing is created on the target. Every probe is a SELECT; a failure is the
answer, not an error. Probing is best-effort -- when the target cannot be
reached the caller falls back to the static table and marks findings
unverified.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

# (capability id, probe SQL). The id is what load_fail_objects.sql emits as
# its label, so a finding can be matched to its probe result directly.
#
# Every probe must return a SCALAR. A function that returns a geometry or a
# BINARY value emits raw bytes on a server that HAS it, which is not valid
# UTF-8 -- so the probe crashes on exactly the targets where it succeeds.
# Hence the IS NOT NULL wrappers.
#
# The JSON arrow operators need a column identifier on the left; an expression
# is a syntax error on MySQL too, so the value comes from a derived table.
# CAST(... AS JSON) is deliberately absent: it parses differently on MariaDB
# and would fail the probe for the wrong reason.
PROBES: List[Tuple[str, str]] = [
    ("->> operator", "SELECT t.doc ->> '$.a' FROM (SELECT '{\"a\":1}' AS doc) t"),
    ("-> operator", "SELECT t.doc -> '$.a' FROM (SELECT '{\"a\":1}' AS doc) t"),
    ("LATERAL", "SELECT d.n FROM (SELECT 1 AS id) b, LATERAL (SELECT 1 AS n) d"),
    ("MEMBER OF", "SELECT 1 MEMBER OF ('[1,2,3]')"),
    ("UUID_TO_BIN",
     "SELECT UUID_TO_BIN('6ccd780c-baba-1026-9564-5b8c656024db') IS NOT NULL"),
    ("BIN_TO_UUID", "SELECT BIN_TO_UUID(UNHEX(REPEAT('00',16))) IS NOT NULL"),
    ("IS_UUID", "SELECT IS_UUID('6ccd780c-baba-1026-9564-5b8c656024db')"),
    ("REGEXP_LIKE", "SELECT REGEXP_LIKE('abc','b')"),
    ("ANY_VALUE", "SELECT ANY_VALUE(1)"),
    # GROUPING only makes sense under WITH ROLLUP; called bare it returns 1210
    # on a server that has it, which would read as absent.
    ("GROUPING", "SELECT GROUPING(c) FROM (SELECT 1 AS c) t GROUP BY c WITH ROLLUP"),
    ("JSON_STORAGE_SIZE", "SELECT JSON_STORAGE_SIZE('{\"a\":1}')"),
    ("JSON_STORAGE_FREE", "SELECT JSON_STORAGE_FREE('{\"a\":1}')"),
    ("JSON_SCHEMA_VALIDATION_REPORT",
     "SELECT JSON_SCHEMA_VALIDATION_REPORT('{\"type\":\"object\"}','{\"a\":1}')"),
    ("PS_THREAD_ID", "SELECT PS_THREAD_ID(CONNECTION_ID())"),
    ("VALIDATE_PASSWORD_STRENGTH", "SELECT VALIDATE_PASSWORD_STRENGTH('abc')"),
    ("GTID_SUBSET", "SELECT GTID_SUBSET('','')"),
    ("GTID_SUBTRACT", "SELECT GTID_SUBTRACT('','')"),
    ("WAIT_FOR_EXECUTED_GTID_SET", "SELECT WAIT_FOR_EXECUTED_GTID_SET('', 0)"),
    ("SOURCE_POS_WAIT", "SELECT SOURCE_POS_WAIT('', 0, 0)"),
    ("ST_Collect", "SELECT ST_Collect(ST_GeomFromText('POINT(1 1)')) IS NOT NULL"),
    ("ST_IsValid", "SELECT ST_IsValid(ST_GeomFromText('POINT(1 1)'))"),
    ("ST_Simplify",
     "SELECT ST_Simplify(ST_GeomFromText('LINESTRING(0 0,1 1,2 2)'), 0.5) IS NOT NULL"),
    ("ST_Validate", "SELECT ST_Validate(ST_GeomFromText('POINT(1 1)')) IS NOT NULL"),
    ("ST_GeoHash", "SELECT ST_GeoHash(1, 1, 10)"),
    ("ST_Latitude", "SELECT ST_Latitude(ST_GeomFromText('POINT(1 1)', 4326))"),
    ("ST_PointAtDistance",
     "SELECT ST_PointAtDistance(ST_GeomFromText('LINESTRING(0 0,1 1)'), 0.5) IS NOT NULL"),
]

# 1064 (syntax) and 1305 (no such function) mean the construct is absent.
ABSENT_CODES = {"1064", "1305"}

# Reaching one of these means the name resolved and the server understood the
# statement -- it refused for a reason unrelated to support. 1210 is bad
# arguments, 3062 is "GTID_MODE is OFF". Both are evidence of presence.
PRESENT_DESPITE_ERROR_CODES = {"1210", "3062"}


def _error_code(stderr: str) -> str:
    """Pull the numeric code out of 'ERROR 1305 (42000) at line 1: ...'."""
    for token in (stderr or "").split():
        if token.isdigit() and len(token) == 4:
            return token
    return ""


def run_probes(
    mariadb_bin: str,
    host: str,
    port: str,
    user: str,
    password: str,
    log=None,
) -> Optional[Dict[str, Any]]:
    """Run every probe against the target and report what it supports.

    Returns {capability_id: {"supported": bool, "evidence": str}}, or None if
    the target could not be reached at all -- which the caller must treat as
    "unverified", never as "everything is supported".
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in env:
        del env["MYSQL_PWD"]

    base = [mariadb_bin, f"-h{host}", f"-P{port}", f"-u{user}",
            "--batch", "--skip-column-names", "--ssl-verify-server-cert=OFF"]

    # One connectivity check first, so an unreachable target is reported once
    # rather than as 26 individual failures that look like 26 missing features.
    try:
        check = subprocess.run(base + ["-e", "SELECT 1"],
                               capture_output=True, text=True, errors="replace",
                               env=env, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _log("NOTE: target not reachable for capability probes; findings will be "
             "classified from the static table and marked unverified.")
        return None
    if check.returncode != 0:
        _log("NOTE: target not reachable for capability probes; findings will be "
             "classified from the static table and marked unverified.")
        return None

    results: Dict[str, Any] = {}
    for capability, sql in PROBES:
        try:
            proc = subprocess.run(base + ["-e", sql],
                                  capture_output=True, text=True, errors="replace",
                               env=env, timeout=30)
        except subprocess.TimeoutExpired:
            results[capability] = {"supported": None, "evidence": "probe timed out"}
            continue

        if proc.returncode == 0:
            results[capability] = {"supported": True, "evidence": "query succeeded"}
            continue

        code = _error_code(proc.stderr)
        if code in ABSENT_CODES:
            results[capability] = {"supported": False, "evidence": f"error {code}"}
        elif code in PRESENT_DESPITE_ERROR_CODES:
            results[capability] = {"supported": True, "evidence": f"error {code} (resolved, refused)"}
        else:
            # An unexpected error means the probe is wrong, not the server.
            # Saying so beats recording a guess.
            results[capability] = {
                "supported": None,
                "evidence": f"inconclusive: error {code or '?'}",
            }
            _log(f"NOTE: capability probe for {capability} was inconclusive "
                 f"(error {code or '?'}); classified from the static table.")

    supported = sum(1 for r in results.values() if r["supported"] is True)
    _log(f"NOTE: probed {len(results)} target capabilities; {supported} supported.")
    return results
