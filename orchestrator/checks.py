from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .report import Gate, GateStatus, WarningItem, Report
from . import load_failures as lf
from . import probes as cap_probes


def _default_mysql_bin() -> str:
    """Prefer the 'mariadb' client, falling back to 'mysql' if absent."""
    return "mariadb" if shutil.which("mariadb") else "mysql"


@dataclass
class AssessmentResult:
    source: Dict[str, Any]
    target: Dict[str, Any]
    gates: List[Gate]
    warnings: List[WarningItem]
    inventory: Dict[str, Any]
    load_failures: Optional[lf.LoadFailureSet] = None
    selected_dbs: List[str] = field(default_factory=list)


def _read_tsv(path: Path) -> List[List[str]]:
    if not path.exists():
        return []
    rows: List[List[str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(line.split("\t"))
    return rows


def _selected_dbs(env_cfg: Dict[str, str]) -> List[str]:
    """Resolve the operator-selected database list (SRC_DBS, else SRC_DB)."""
    src_dbs = str(env_cfg.get("SRC_DBS", "")).strip()
    src_db = str(env_cfg.get("SRC_DB", "")).strip()
    if src_dbs:
        return [x.strip() for x in src_dbs.split(",") if x.strip()]
    if src_db:
        return [src_db]
    return []


def _filter_by_schema(rows: List[List[str]], dbs: List[str]) -> List[List[str]]:
    """Keep only rows whose first field names a selected schema.

    Precheck queries run instance-wide, so schema-scoped checks return objects
    from databases that are not being migrated. Instance-scoped checks
    (partial_revokes, mysql_roles, resource_groups, active_plugins,
    definers_inventory) must NOT be passed through here, nor must checks
    whose first field is an object type rather than a schema name
    (view_routine_bodies, definers_inventory).

    An empty db list means "unknown scope" and is passed through unfiltered.
    """
    if not dbs:
        return rows
    return [r for r in rows if r and r[0] in dbs]


def _effective_env_cfg(cfg: Dict[str, Any]) -> Dict[str, str]:
    env_cfg = {str(k): str(v) for k, v in (cfg.get("env", {}) or {}).items()}
    # Allow interactive wrapper exports to override assessment config.
    override_keys = [
        "SRC_HOST",
        "SRC_PORT",
        "SRC_USER",
        "SRC_PASS",
        "SRC_ADMIN_USER",
        "SRC_ADMIN_PASS",
        "SRC_DB",
        "SRC_DBS",
        "MYSQL_PWD",
        "MYSQL_BIN",
    ]
    for k in override_keys:
        v = os.environ.get(k)
        if v is not None and str(v) != "":
            env_cfg[k] = str(v)
    return env_cfg


def _select_source_credentials(cfg: Dict[str, Any], env_cfg: Dict[str, str]) -> Tuple[Optional[str], Optional[str], str]:
    client = cfg.get("client", {}) or {}
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    host = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    port = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))

    allow_root = str(env_cfg.get("ALLOW_ROOT_USERS", "0")) in ("1", "true", "TRUE", "True")
    # Priority: explicit assess creds -> admin creds.
    candidates: List[Tuple[str, str, str]] = []
    explicit_user = str(env_cfg.get("SRC_ASSESS_USER", "")).strip()
    explicit_pass = str(env_cfg.get("SRC_ASSESS_PASS", "")).strip()
    if explicit_user:
        candidates.append((explicit_user, explicit_pass, "SRC_ASSESS_USER"))

    admin_user = str(env_cfg.get("SRC_ADMIN_USER", "")).strip()
    admin_pass = str(env_cfg.get("SRC_ADMIN_PASS", "")).strip()
    if admin_user:
        candidates.append((admin_user, admin_pass, "SRC_ADMIN_USER"))

    # De-dupe by (user, pass) while preserving order.
    seen = set()
    deduped: List[Tuple[str, str, str]] = []
    for user, pwd, src in candidates:
        if not allow_root and user == "root":
            continue
        key = (user, pwd)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((user, pwd, src))

    last_err = "no_source_credentials_available"
    for user, pwd, src in deduped:
        env = dict(os.environ)
        if pwd:
            env["MYSQL_PWD"] = pwd
        elif "MYSQL_PWD" in env:
            del env["MYSQL_PWD"]
        try:
            p = subprocess.run(
                [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}", "--batch", "--skip-column-names", "-e", "SELECT 1;"],
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            return None, None, f"mysql client not found: {mysql_bin}"
        if p.returncode == 0:
            return user, pwd, src
        err = (p.stderr or p.stdout or "").strip().replace("\n", " ")
        last_err = f"{src} ({user}) failed: {err[:240]}"

    return None, None, last_err


def _run_precheck(repo_root: Path, cfg: Dict[str, Any], outdir: Path, log) -> Path:
    precheck_out = outdir / "precheck"
    precheck_out.mkdir(parents=True, exist_ok=True)

    client = cfg.get("client", {}) or {}
    env_cfg = _effective_env_cfg(cfg)

    user, password, cred_source = _select_source_credentials(cfg, env_cfg)
    if not user:
        raise RuntimeError(f"unable to authenticate to source for assessment: {cred_source}")
    log(f"Assessment source auth selected: {cred_source} ({user})")

    # Seed from the parent environment so PATH (and locale, TMPDIR, etc.)
    # reach the phase script; a bare dict leaves it with no PATH at all.
    env = os.environ.copy()
    env.update({
        "MYSQL_BIN": str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin()))),
        "HOST": str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1"))),
        "PORT": str(env_cfg.get("SRC_PORT", client.get("port", 3306))),
        "USER": user,
        "OUTDIR": str(precheck_out),
        "CHECKS_DIR": str(repo_root / "sql" / "checks"),
    })
    # Pass through env vars first.
    for k, v in env_cfg.items():
        env[str(k)] = str(v)

    # Then force the effective credentials (and password) used by precheck.
    env["MYSQL_BIN"] = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    env["HOST"] = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    env["PORT"] = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))
    env["USER"] = user
    env["SRC_USER"] = user
    if password:
        env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in env:
        del env["MYSQL_PWD"]
    if password:
        env["SRC_PASS"] = password
    elif "SRC_PASS" in env:
        del env["SRC_PASS"]

    script = repo_root / "scripts" / "00_precheck.sh"
    if not script.exists():
        raise RuntimeError(f"Missing precheck script: {script}")

    log(f"RUN precheck -> {script}")
    p = subprocess.run(
        ["bash", str(script)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )

    # Log stdout/stderr (should not contain password)
    if p.stdout:
        for ln in p.stdout.splitlines():
            log(ln)
    if p.stderr:
        for ln in p.stderr.splitlines():
            log(ln)

    if p.returncode != 0:
        raise RuntimeError(f"precheck failed rc={p.returncode} (see {precheck_out}/precheck.err)")

    return precheck_out


def _source_db_gate(cfg: Dict[str, Any]) -> Gate:
    env_cfg = _effective_env_cfg(cfg)
    src_db = str(env_cfg.get("SRC_DB", "")).strip()
    src_dbs = str(env_cfg.get("SRC_DBS", "")).strip()
    dbs: List[str] = []
    if src_dbs:
        dbs = [x.strip() for x in src_dbs.split(",") if x.strip()]
    elif src_db:
        dbs = [src_db]

    if not dbs:
        return Gate(
            "source_databases_exist",
            GateStatus.FAIL,
            {"reason": "SRC_DB_or_SRC_DBS_missing_for_assessment"},
        )

    client = cfg.get("client", {}) or {}
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    host = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    port = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))
    user, password, cred_source = _select_source_credentials(cfg, env_cfg)
    if not user:
        return Gate(
            "source_databases_exist",
            GateStatus.FAIL,
            {"requested": dbs, "missing": dbs, "reason": f"source auth failed: {cred_source}"},
        )

    def sql_escape(s: str) -> str:
        return s.replace("'", "''")

    missing: List[str] = []
    for db in dbs:
        query = (
            "SELECT COUNT(*) FROM information_schema.schemata "
            f"WHERE schema_name='{sql_escape(db)}';"
        )
        env = dict(os.environ)
        if password:
            env["MYSQL_PWD"] = password
        elif "MYSQL_PWD" in env:
            del env["MYSQL_PWD"]
        try:
            p = subprocess.run(
                [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}", "--batch", "--skip-column-names", "-e", query],
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            return Gate(
                "source_databases_exist",
                GateStatus.FAIL,
                {"requested": dbs, "missing": dbs, "reason": f"mysql client not found: {mysql_bin}"},
            )
        if p.returncode != 0 or (p.stdout or "").strip() != "1":
            missing.append(db)

    return Gate(
        "source_databases_exist",
        GateStatus.PASS if not missing else GateStatus.FAIL,
        {"requested": dbs, "missing": missing, "auth_source": cred_source},
    )


def _binlog_source_compatibility_gate(
    cfg: Dict[str, Any],
    mode: Optional[str],
    json_cols: List[List[str]],
) -> Optional[Gate]:
    """
    Mode-aware gate. Returns a Gate only when mode == 'binlog'.

    Composes two source-side compatibility checks into one gate:
      - JSON columns in any selected source schema cannot be replicated
        to a MariaDB target via binlog mode.
      - The source must be running binlog_format=ROW. MIXED and STATEMENT
        are both rejected.

    Either failure trips the gate. When both fail, both are surfaced in
    the gate's details so the operator-facing message can render the
    full picture in one shot.

    The binlog_format value is queried inline via subprocess (mirroring
    _source_db_gate) rather than read from a precheck TSV, because the
    query is a one-liner with no shared-SQL-file complexity to justify.
    """
    if mode != "binlog":
        return None

    env_cfg = _effective_env_cfg(cfg)
    src_db = str(env_cfg.get("SRC_DB", "")).strip()
    src_dbs = str(env_cfg.get("SRC_DBS", "")).strip()
    selected: List[str] = []
    if src_dbs:
        selected = [x.strip() for x in src_dbs.split(",") if x.strip()]
    elif src_db:
        selected = [src_db]
    if not selected:
        # No schemas in scope; cannot evaluate. Don't emit a gate.
        return None

    # Shared source-connection setup, used by both the version gate below and
    # the binlog_format sub-check further down.
    client = cfg.get("client", {}) or {}
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    host = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    port = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))
    user, password, _ = _select_source_credentials(cfg, env_cfg)
    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in env:
        del env["MYSQL_PWD"]

    # Version gate (categorical, evaluated FIRST). Replication-based migration
    # requires a MySQL 8.0+ source; MySQL < 8.0 (5.7, 5.6, ...) cannot be a
    # reliable binlog/GTID replication source for MariaDB. This disqualifies the
    # source regardless of schema, so short-circuit here BEFORE the JSON and
    # binlog_format sub-checks — a pre-8.0 source reports only source_version,
    # not incidental JSON/format failures, and a JSON-free 5.7 source is still
    # blocked rather than slipping through to a downstream binlog failure.
    if user:
        try:
            pv = subprocess.run(
                [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}",
                 "--batch", "--skip-column-names",
                 "-e", "SELECT VERSION();"],
                capture_output=True, text=True, env=env,
            )
            if pv.returncode == 0:
                ver_lines = (pv.stdout or "").strip().splitlines()[:1]
                if ver_lines:
                    ver = ver_lines[0].strip()
                    # VERSION() may return e.g. "5.7.44-log"; take leading major.
                    major_str = ver.split(".", 1)[0]
                    if major_str.isdigit() and int(major_str) < 8:
                        return Gate(
                            "binlog_source_compatibility",
                            GateStatus.FAIL,
                            {
                                "mode": mode,
                                "selected_schemas": selected,
                                "failures": {"source_version": ver},
                            },
                        )
        except FileNotFoundError:
            # mysql client missing; the source_databases_exist gate will
            # have already failed for the same reason. Skip silently.
            pass

    failures: Dict[str, Any] = {}

    # JSON sub-check
    selected_set = set(selected)
    offenders = [
        f"{r[0]}.{r[1]}.{r[2]}"
        for r in json_cols
        if len(r) >= 3 and r[0] in selected_set
    ]
    if offenders:
        failures["json_columns"] = offenders

    # binlog_format sub-check (inline query, no precheck TSV dependency)
    if user:
        try:
            p = subprocess.run(
                [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}",
                 "--batch", "--skip-column-names",
                 "-e", "SHOW VARIABLES LIKE 'binlog_format';"],
                capture_output=True, text=True, env=env,
            )
            if p.returncode == 0:
                # Output is "binlog_format<tab>VALUE"
                first = (p.stdout or "").splitlines()[:1]
                if first and "\t" in first[0]:
                    current_fmt = first[0].split("\t", 1)[1].strip().upper()
                    if current_fmt and current_fmt != "ROW":
                        failures["binlog_format"] = current_fmt
        except FileNotFoundError:
            # mysql client missing; the source_databases_exist gate will
            # have already failed for the same reason. Skip silently.
            pass

    return Gate(
        "binlog_source_compatibility",
        GateStatus.PASS if not failures else GateStatus.FAIL,
        {
            "mode": mode,
            "selected_schemas": selected,
            "failures": failures,
        },
    )


def _source_has_process_priv(cfg: Dict[str, Any]) -> bool:
    """Whether the assessment user can read the INNODB_* views.

    Only used to tell "no general tablespaces" apart from "could not look".
    Errs toward True: a false reassurance here is worse than a missing note,
    so an unreadable grants query is treated as privileged rather than firing
    a warning at every user whose grants cannot be parsed.
    """
    env_cfg = _effective_env_cfg(cfg)
    client = cfg.get("client", {}) or {}
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    host = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    port = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))
    user, password, _ = _select_source_credentials(cfg, env_cfg)
    if not user:
        return True

    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in env:
        del env["MYSQL_PWD"]
    try:
        proc = subprocess.run(
            [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}",
             "--batch", "--skip-column-names", "-e", "SHOW GRANTS;"],
            capture_output=True, text=True, env=env,
        )
    except FileNotFoundError:
        return True
    if proc.returncode != 0:
        return True
    out = (proc.stdout or "").upper()
    return "PROCESS" in out or "ALL PRIVILEGES" in out


def _query_target_catalog(cfg: Dict[str, Any], log) -> Optional[Dict[str, Any]]:
    """Read the target's collations and character sets.

    The launcher exports TGT_HOST/TGT_PORT/TGT_ADMIN_USER/TGT_ADMIN_PASS
    before any phase runs, so assess already has working target credentials --
    no check used them until now. INSTALL_TARGET_MARIADB is dead code (the
    target is always already running), so the only case where this returns
    None is a staged dump host with no target in reach.

    Asking the server is what makes the collation check exact. MariaDB 11.4.5+
    carries most of MySQL's _0900_ collations as aliases for
    utf8mb4_uca1400_nopad_*, but not ja/ru/zh, and not the locales MySQL added
    in 8.0.30. Any hardcoded pattern is wrong for some target.
    """
    env_cfg = _effective_env_cfg(cfg)
    host = str(env_cfg.get("TGT_HOST", os.environ.get("TGT_HOST", "")) or "")
    port = str(env_cfg.get("TGT_PORT", os.environ.get("TGT_PORT", "3306")) or "3306")
    user = str(env_cfg.get("TGT_ADMIN_USER", os.environ.get("TGT_ADMIN_USER", "")) or "")
    password = str(env_cfg.get("TGT_ADMIN_PASS", os.environ.get("TGT_ADMIN_PASS", "")) or "")
    if not host or not user:
        log("NOTE: no target credentials available; collation and charset findings "
            "will not be confirmed against the target.")
        return None

    client = cfg.get("client", {}) or {}
    mariadb_bin = str(env_cfg.get("MARIADB_BIN", client.get("mariadb_bin", "mariadb")))

    env = dict(os.environ)
    if password:
        env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in env:
        del env["MYSQL_PWD"]

    query = (
        "SELECT 'C', COLLATION_NAME FROM information_schema.COLLATIONS "
        "UNION ALL "
        "SELECT 'S', CHARACTER_SET_NAME FROM information_schema.CHARACTER_SETS;"
    )
    try:
        proc = subprocess.run(
            [mariadb_bin, f"-h{host}", f"-P{port}", f"-u{user}",
             "--batch", "--skip-column-names", "--ssl-verify-server-cert=OFF",
             "-e", query],
            capture_output=True, text=True, env=env,
        )
    except FileNotFoundError:
        log(f"WARN: {mariadb_bin} not found; cannot verify collations against the target.")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().replace("\n", " ")
        log(f"WARN: could not read the target catalog: {err[:200]}")
        return None

    collations, charsets = set(), set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[1].strip():
            continue
        (collations if parts[0] == "C" else charsets).add(parts[1].strip().lower())
    if not collations:
        log("WARN: the target reported no collations; treating as unverified.")
        return None

    return {
        "collations": collations,
        "charsets": charsets,
        "version": str(os.environ.get("TGT_VERSION_SHORT", "")
                       or os.environ.get("TGT_VERSION_DETECTED", "")),
    }


def _probe_target_capabilities(cfg: Dict[str, Any], log) -> Optional[Dict[str, Any]]:
    """Ask the target which MySQL-only constructs it actually supports.

    Shares the credentials _query_target_catalog uses. Creates nothing: every
    probe is a SELECT, and a failure is the answer rather than an error. See
    orchestrator/probes.py for why only parser-reaching constructs are probed
    and version-comment-wrapped attributes are not.
    """
    env_cfg = _effective_env_cfg(cfg)
    host = str(env_cfg.get("TGT_HOST", os.environ.get("TGT_HOST", "")) or "")
    port = str(env_cfg.get("TGT_PORT", os.environ.get("TGT_PORT", "3306")) or "3306")
    user = str(env_cfg.get("TGT_ADMIN_USER", os.environ.get("TGT_ADMIN_USER", "")) or "")
    password = str(env_cfg.get("TGT_ADMIN_PASS", os.environ.get("TGT_ADMIN_PASS", "")) or "")
    if not host or not user:
        return None

    client = cfg.get("client", {}) or {}
    mariadb_bin = str(env_cfg.get("MARIADB_BIN", client.get("mariadb_bin", "mariadb")))
    return cap_probes.run_probes(mariadb_bin, host, port, user, password, log=log)


def _make_violation_counter(cfg: Dict[str, Any], log) -> Optional[Any]:
    """Return a callable counting rows that violate a CHECK predicate.

    MariaDB enforces every CHECK it is given, so a constraint the source
    carries as NOT ENFORCED only breaks the load when the table actually
    holds rows the predicate rejects. Only a COUNT against the source can
    tell the two apart.

    A row violates iff the predicate evaluates to FALSE; NULL and TRUE both
    pass, which is what `IS FALSE` expresses exactly. Returns None (rather
    than raising) when the query cannot be evaluated, so the caller can
    report the constraint as unverified instead of failing the assessment.
    """
    env_cfg = _effective_env_cfg(cfg)
    client = cfg.get("client", {}) or {}
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", _default_mysql_bin())))
    host = str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1")))
    port = str(env_cfg.get("SRC_PORT", client.get("port", 3306)))
    user, password, _ = _select_source_credentials(cfg, env_cfg)
    if not user:
        return None

    base_env = dict(os.environ)
    if password:
        base_env["MYSQL_PWD"] = password
    elif "MYSQL_PWD" in base_env:
        del base_env["MYSQL_PWD"]

    def _ident(name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def count_violations(schema: str, table: str, clause: str) -> Optional[int]:
        if not schema or not table or not clause:
            return None
        query = (
            f"SELECT COUNT(*) FROM {_ident(schema)}.{_ident(table)} "
            f"WHERE ({clause}) IS FALSE;"
        )
        try:
            p = subprocess.run(
                [mysql_bin, f"-h{host}", f"-P{port}", f"-u{user}",
                 "--batch", "--skip-column-names", "-e", query],
                capture_output=True, text=True, env=base_env,
            )
        except FileNotFoundError:
            return None
        if p.returncode != 0:
            err = (p.stderr or "").strip().replace("\n", " ")
            log(f"WARN: violating-row probe failed for {schema}.{table}: {err[:200]}")
            return None
        out = (p.stdout or "").strip()
        try:
            return int(out.splitlines()[0])
        except (ValueError, IndexError):
            return None

    return count_violations


def run_assessment_checks(
    cfg: Dict[str, Any],
    report: Report,
    repo_root: Path,
    outdir: Path,
    mode: Optional[str] = None,
) -> AssessmentResult:
    gates: List[Gate] = []
    warnings: List[WarningItem] = []
    inventory: Dict[str, Any] = {}

    pre = _run_precheck(repo_root, cfg, outdir, report.log)
    _sel_dbs = _selected_dbs(_effective_env_cfg(cfg))

    # Load TSVs
    mysql_version = _read_tsv(pre / "mysql_version.tsv")          # expected: 1 row: version, comment?
    innodb = _read_tsv(pre / "innodb_settings.tsv")               # expected: 1 row: file_per_table, fast_shutdown, max_allowed_packet
    auth = _read_tsv(pre / "auth_plugins.tsv")
    json_cols = _filter_by_schema(_read_tsv(pre / "json_columns.tsv"), _sel_dbs)
    enc = _read_tsv(pre / "compression_encryption.tsv")
    engines = _read_tsv(pre / "engines_summary.tsv")
    sizes = _read_tsv(pre / "schema_sizes.tsv")

    schema_charsets = _filter_by_schema(_read_tsv(pre / "schema_charsets.tsv"), _sel_dbs)
    tcoll = _filter_by_schema(_read_tsv(pre / "mysql8_collations.tsv"), _sel_dbs)
    ccoll = _filter_by_schema(_read_tsv(pre / "mysql8_column_collations.tsv"), _sel_dbs)
    sql_mode = _read_tsv(pre / "sql_mode.tsv")
    definers = _read_tsv(pre / "definers_inventory.tsv")
    partitions = _filter_by_schema(_read_tsv(pre / "partitioned_tables.tsv"), _sel_dbs)
    plugins = _read_tsv(pre / "active_plugins.tsv")

    # Previously unwired checks
    func_indexes = _filter_by_schema(_read_tsv(pre / "functional_indexes.tsv"), _sel_dbs)
    func_defaults = _filter_by_schema(_read_tsv(pre / "functional_defaults.tsv"), _sel_dbs)
    invisible_cols = _filter_by_schema(_read_tsv(pre / "invisible_columns.tsv"), _sel_dbs)
    check_cons = _filter_by_schema(_read_tsv(pre / "check_constraints.tsv"), _sel_dbs)
    partial_rev = _read_tsv(pre / "partial_revokes.tsv")
    gis_srid = _filter_by_schema(_read_tsv(pre / "gis_srid_usage.tsv"), _sel_dbs)
    res_groups = _read_tsv(pre / "resource_groups.tsv")
    xplugin = _read_tsv(pre / "xplugin_status.tsv")
    fk_names = _filter_by_schema(_read_tsv(pre / "fk_name_lengths.tsv"), _sel_dbs)
    trigger_ord = _filter_by_schema(_read_tsv(pre / "trigger_order.tsv"), _sel_dbs)

    # New checks
    mysql_roles = _read_tsv(pre / "mysql_roles.tsv")
    gen_cols = _filter_by_schema(_read_tsv(pre / "generated_columns.tsv"), _sel_dbs)
    srv_defaults = _read_tsv(pre / "server_defaults.tsv")
    view_routine = _read_tsv(pre / "view_routine_bodies.tsv")

    # Load-failure and sign-off checks
    load_fail_objs = _filter_by_schema(_read_tsv(pre / "load_fail_objects.tsv"), _sel_dbs)
    not_enforced = _filter_by_schema(_read_tsv(pre / "not_enforced_constraints.tsv"), _sel_dbs)
    src_collations = _filter_by_schema(_read_tsv(pre / "source_collations.tsv"), _sel_dbs)
    invisible_idx = _filter_by_schema(_read_tsv(pre / "invisible_indexes.tsv"), _sel_dbs)
    general_ts = _filter_by_schema(_read_tsv(pre / "general_tablespaces.tsv"), _sel_dbs)
    autoextend = _filter_by_schema(_read_tsv(pre / "autoextend_size.tsv"), _sel_dbs)
    schema_enc = _filter_by_schema(_read_tsv(pre / "schema_default_encryption.tsv"), _sel_dbs)
    invisible_cols_rows = _filter_by_schema(_read_tsv(pre / "invisible_columns.tsv"), _sel_dbs)
    srid_rows = _filter_by_schema(_read_tsv(pre / "gis_srid_usage.tsv"), _sel_dbs)

    # Source/target
    version = mysql_version[0][0].strip() if mysql_version and mysql_version[0] else ""
    env_cfg = _effective_env_cfg(cfg)
    source = {
        "type": "mysql",
        "version": version,
        "host": env_cfg.get("SRC_HOST", (cfg.get("client", {}) or {}).get("host", "")),
        "port": env_cfg.get("SRC_PORT", (cfg.get("client", {}) or {}).get("port", "")),
    }
    target = cfg.get("target", {"type": "mariadb", "version": "LTS"})

    # Gates
    allowed = {"5.7", "8.0", "8.4"}
    major_minor = ".".join(version.split(".")[:2]) if version else ""
    gates.append(
        Gate(
            "mysql_version_supported",
            GateStatus.PASS if major_minor in allowed else GateStatus.FAIL,
            {"version": version, "allowed": sorted(list(allowed))},
        )
    )

    innodb_file_per_table = ""
    innodb_fast_shutdown = ""
    max_allowed_packet = ""
    if innodb and len(innodb[0]) >= 2:
        innodb_file_per_table = innodb[0][0].strip()
        innodb_fast_shutdown = innodb[0][1].strip()
    if innodb and len(innodb[0]) >= 3:
        max_allowed_packet = innodb[0][2].strip()

    gates.append(
        Gate(
            "innodb_file_per_table_is_1",
            GateStatus.PASS if innodb_file_per_table == "1" else GateStatus.FAIL,
            {"value": innodb_file_per_table},
        )
    )
    gates.append(_source_db_gate(cfg))

    binlog_gate = _binlog_source_compatibility_gate(cfg, mode, json_cols)
    if binlog_gate is not None:
        gates.append(binlog_gate)

    # Warnings/Inventory
    if innodb_fast_shutdown and innodb_fast_shutdown != "0":
        warnings.append(
            WarningItem(
                "innodb_fast_shutdown_not_0",
                "MEDIUM",
                {"value": innodb_fast_shutdown, "required_before_shutdown": 0},
            )
        )

    auth_lines = ["\t".join(r) for r in auth if r]
    if auth_lines:
        warnings.append(WarningItem("mysql_sha_or_caching_auth_users", "HIGH", {"count": len(auth_lines), "rows_sample": auth_lines[:200]}))
    inventory["auth_plugin_users"] = {"count": len(auth_lines)}

    json_lines = ["\t".join(r) for r in json_cols if r]
    if json_lines:
        warnings.append(WarningItem("json_columns_present", "MEDIUM", {"count": len(json_lines), "rows_sample": json_lines[:200]}))
    inventory["json_columns"] = {"count": len(json_lines)}

    enc_lines = ["\t".join(r) for r in enc if r]
    inventory["encryption_or_compression"] = {"count": len(enc_lines)}
    if enc_lines:
        warnings.append(WarningItem("encryption_or_compression_detected", "HIGH", {"count": len(enc_lines), "rows_sample": enc_lines[:200]}))

    inventory["engines"] = {"rows": ["\t".join(r) for r in engines[:200]]}
    inventory["schema_sizes_mb"] = {"rows": ["\t".join(r) for r in sizes[:200]]}
    inventory["schema_charsets"] = {"rows": ["\t".join(r) for r in schema_charsets[:200]]}

    tcoll_lines = ["\t".join(r) for r in tcoll if r]
    if tcoll_lines:
        warnings.append(WarningItem("mysql8_table_collations_present", "MEDIUM", {"count": len(tcoll_lines), "rows_sample": tcoll_lines[:200]}))
    inventory["mysql8_table_collations"] = {"count": len(tcoll_lines)}

    ccoll_lines = ["\t".join(r) for r in ccoll if r]
    if ccoll_lines:
        warnings.append(WarningItem("mysql8_column_collations_present", "MEDIUM", {"count": len(ccoll_lines), "rows_sample": ccoll_lines[:200]}))
    inventory["mysql8_column_collations"] = {"count": len(ccoll_lines)}

    sql_mode_val = sql_mode[0][0] if sql_mode and sql_mode[0] else ""
    if sql_mode_val:
        warnings.append(WarningItem("sql_mode_review_recommended", "MEDIUM", {"value": sql_mode_val}))
    inventory["sql_mode"] = {"value": sql_mode_val}

    definers_lines = ["\t".join(r) for r in definers if r]
    if definers_lines:
        warnings.append(WarningItem("definer_objects_present", "MEDIUM", {"count": len(definers_lines), "rows_sample": definers_lines[:200]}))
    inventory["definers"] = {"count": len(definers_lines)}

    partition_lines = ["\t".join(r) for r in partitions if r]
    if partition_lines:
        warnings.append(WarningItem("partitioned_tables_present", "MEDIUM", {"count": len(partition_lines), "rows_sample": partition_lines[:200]}))
    inventory["partitioned_tables"] = {"count": len(partition_lines)}

    plugin_lines = ["\t".join(r) for r in plugins if r]
    if plugin_lines:
        warnings.append(WarningItem("active_plugins_review_recommended", "LOW", {"count": len(plugin_lines), "rows_sample": plugin_lines[:200]}))
    inventory["active_plugins"] = {"rows": plugin_lines[:200]}

    # --- Previously unwired checks ---

    fi_lines = ["\t".join(r) for r in func_indexes if r]
    if fi_lines:
        warnings.append(WarningItem("functional_indexes_present", "HIGH", {"count": len(fi_lines), "rows_sample": fi_lines[:200], "note": "Expression-based indexes may use MySQL-only syntax unsupported by MariaDB."}))
    inventory["functional_indexes"] = {"count": len(fi_lines)}

    fd_lines = ["\t".join(r) for r in func_defaults if r]
    if fd_lines:
        warnings.append(WarningItem("functional_defaults_present", "MEDIUM", {"count": len(fd_lines), "rows_sample": fd_lines[:200], "note": "Expression defaults may need review for MariaDB compatibility."}))
    inventory["functional_defaults"] = {"count": len(fd_lines)}

    inv_lines = ["\t".join(r) for r in invisible_cols if r]
    if inv_lines:
        warnings.append(WarningItem("invisible_columns_present", "LOW", {"count": len(inv_lines), "rows_sample": inv_lines[:200], "note": "MariaDB supports INVISIBLE columns but verify syntax compatibility."}))
    inventory["invisible_columns"] = {"count": len(inv_lines)}

    cc_lines = ["\t".join(r) for r in check_cons if r]
    if cc_lines:
        warnings.append(WarningItem("check_constraints_present", "MEDIUM", {"count": len(cc_lines), "rows_sample": cc_lines[:200], "note": "CHECK constraint naming and enforcement may differ between MySQL 8 and MariaDB."}))
    inventory["check_constraints"] = {"count": len(cc_lines)}

    pr_lines = ["\t".join(r) for r in partial_rev if r]
    if pr_lines:
        warnings.append(WarningItem("partial_revokes_detected", "HIGH", {"count": len(pr_lines), "rows_sample": pr_lines[:200], "note": "MariaDB does not support MySQL 8 partial revokes. Affected user privileges will not migrate correctly."}))
    inventory["partial_revokes"] = {"count": len(pr_lines)}

    gis_lines = ["\t".join(r) for r in gis_srid if r]
    if gis_lines:
        warnings.append(WarningItem("gis_srid_columns_present", "MEDIUM", {"count": len(gis_lines), "rows_sample": gis_lines[:200], "note": "MariaDB handles SRID attributes differently from MySQL 8. Verify spatial data compatibility."}))
    inventory["gis_srid_columns"] = {"count": len(gis_lines)}

    rg_lines = ["\t".join(r) for r in res_groups if r]
    if rg_lines:
        warnings.append(WarningItem("resource_groups_in_use", "MEDIUM", {"count": len(rg_lines), "rows_sample": rg_lines[:200], "note": "MariaDB has no equivalent of MySQL resource groups. Performance tuning may need adjustment."}))
    inventory["resource_groups"] = {"count": len(rg_lines)}

    xp_lines = ["\t".join(r) for r in xplugin if r]
    if xp_lines:
        warnings.append(WarningItem("xplugin_active", "HIGH", {"count": len(xp_lines), "rows_sample": xp_lines[:200], "note": "MariaDB does not support MySQL X Protocol (port 33060). Applications using X DevAPI must be migrated."}))
    inventory["xplugin"] = {"count": len(xp_lines)}

    fk_lines = ["\t".join(r) for r in fk_names if r]
    if fk_lines:
        warnings.append(WarningItem("long_fk_names_detected", "MEDIUM", {"count": len(fk_lines), "rows_sample": fk_lines[:200], "note": "Foreign key names >60 chars may cause errors on MariaDB import."}))
    inventory["long_fk_names"] = {"count": len(fk_lines)}

    to_lines = ["\t".join(r) for r in trigger_ord if r]
    if to_lines:
        warnings.append(WarningItem("multiple_triggers_per_event", "MEDIUM", {"count": len(to_lines), "rows_sample": to_lines[:200], "note": "MySQL FOLLOWS/PRECEDES trigger ordering may not import cleanly into MariaDB."}))
    inventory["multiple_triggers_per_event"] = {"count": len(to_lines)}

    # --- New checks ---

    role_lines = ["\t".join(r) for r in mysql_roles if r]
    if role_lines:
        warnings.append(WarningItem("mysql_roles_in_use", "HIGH", {"count": len(role_lines), "rows_sample": role_lines[:200], "note": "MySQL 8 roles (role_edges) do not transfer to MariaDB automatically. Users may lose role-based privileges."}))
    inventory["mysql_roles"] = {"count": len(role_lines)}

    gc_lines = ["\t".join(r) for r in gen_cols if r]
    if gc_lines:
        warnings.append(WarningItem("generated_columns_present", "HIGH", {"count": len(gc_lines), "rows_sample": gc_lines[:200], "note": "Generated column expressions may use MySQL-only functions (UUID_TO_BIN, etc.) that fail on MariaDB."}))
    inventory["generated_columns"] = {"count": len(gc_lines)}

    if srv_defaults and srv_defaults[0]:
        row = srv_defaults[0]
        sd = {}
        sd_keys = ["character_set_server", "collation_server", "lower_case_table_names",
                   "explicit_defaults_for_timestamp", "event_scheduler", "innodb_default_row_format"]
        for i, key in enumerate(sd_keys):
            sd[key] = row[i].strip() if i < len(row) else ""
        inventory["server_defaults"] = sd
        # Warn on collation mismatch: MySQL 8 defaults to utf8mb4_0900_ai_ci
        if sd.get("collation_server", "").startswith("utf8mb4_0900"):
            warnings.append(WarningItem("server_collation_0900", "HIGH", {"value": sd["collation_server"], "note": "MySQL 8 default collation utf8mb4_0900_ai_ci is not available in MariaDB. Target will use utf8mb4_general_ci or utf8mb4_unicode_ci."}))
        if sd.get("lower_case_table_names", "") not in ("", "0"):
            warnings.append(WarningItem("lower_case_table_names_nonzero", "MEDIUM", {"value": sd["lower_case_table_names"], "note": "Ensure target MariaDB has matching lower_case_table_names to avoid object reference issues."}))
        if sd.get("event_scheduler", "").upper() == "ON":
            warnings.append(WarningItem("event_scheduler_active", "MEDIUM", {"value": sd["event_scheduler"], "note": "Event scheduler is ON on source. Ensure target MariaDB also has event_scheduler=ON or migrated events will not fire."}))
    else:
        inventory["server_defaults"] = {}

    vr_lines = ["\t".join(r) for r in view_routine if r]
    if vr_lines:
        warnings.append(WarningItem("mysql_only_syntax_in_views_routines", "HIGH", {"count": len(vr_lines), "rows_sample": vr_lines[:200], "note": "Views or routines contain MySQL-only syntax (JSON_TABLE, LATERAL, UUID_TO_BIN, etc.) that MariaDB does not support."}))
    inventory["mysql_only_views_routines"] = {"count": len(vr_lines)}

    # --- Load failures ---
    # These stop the load rather than changing behaviour afterwards, so they
    # carry their own severity and their own report. They are deliberately NOT
    # gates: the operator is shown the full list and asked whether to proceed,
    # because several are backportable on request and the call is theirs.
    target_catalog = _query_target_catalog(cfg, report.log)
    capabilities = _probe_target_capabilities(cfg, report.log)
    load_fail_set = lf.collect(
        load_fail_objs,
        not_enforced,
        count_violations=_make_violation_counter(cfg, report.log),
        log=report.log,
        source_collations=src_collations,
        target_catalog=target_catalog,
        capabilities=capabilities,
        sign_off_rows={
            "invisible_columns": invisible_cols_rows,
            "invisible_indexes": invisible_idx,
            "srid_columns": srid_rows,
            "general_tablespace": general_ts,
            "autoextend_size": autoextend,
            "schema_default_encryption": schema_enc,
        },
    )

    # INNODB_TABLESPACES needs PROCESS. Without it the view returns empty
    # rather than erroring, so an unprivileged assess would report "no general
    # tablespaces" on a source that has them. Say so rather than lie.
    if not general_ts and not _source_has_process_priv(cfg):
        warnings.append(WarningItem(
            "tablespace_check_unprivileged", "MEDIUM",
            {"note": "The assessment user lacks PROCESS, so information_schema."
                     "INNODB_TABLESPACES is unreadable and the general-tablespace "
                     "check cannot confirm its result. Grant PROCESS and re-run to "
                     "be sure."}))
    for feature, items in _group_findings(load_fail_set).items():
        meta = lf.FEATURES.get(feature, {})
        cls = lf.feature_class(feature)
        warnings.append(
            WarningItem(
                f"{cls.lower()}_{feature}",
                lf.feature_severity(feature),
                {
                    "class": cls,
                    "fixed_in": meta.get("fixed_in"),
                    "count": len(items),
                    "mysql_error": meta.get("error", items[0].error),
                    "rows_sample": [
                        f"{i.obj_schema}.{i.object_name} ({i.detail})" if i.object_name
                        else f"{i.obj_schema} ({i.detail})"
                        for i in items[:200]
                    ],
                    "note": meta.get("remedy") or meta.get("change", ""),
                },
            )
        )
    inventory["load_failures"] = load_fail_set.as_dict()

    return AssessmentResult(
        source=source,
        target=target,
        gates=gates,
        warnings=warnings,
        inventory=inventory,
        load_failures=load_fail_set,
        selected_dbs=_sel_dbs,
    )


def _group_findings(result: "lf.LoadFailureSet") -> Dict[str, List[Any]]:
    grouped: Dict[str, List[Any]] = {}
    for f in result.findings:
        grouped.setdefault(f.feature, []).append(f)
    return grouped
