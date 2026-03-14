from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .report import Gate, GateStatus, WarningItem, Report


@dataclass
class AssessmentResult:
    source: Dict[str, Any]
    target: Dict[str, Any]
    gates: List[Gate]
    warnings: List[WarningItem]
    inventory: Dict[str, Any]


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
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", "mysql")))
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

    env = {
        "MYSQL_BIN": str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", "mysql"))),
        "HOST": str(env_cfg.get("SRC_HOST", client.get("host", "127.0.0.1"))),
        "PORT": str(env_cfg.get("SRC_PORT", client.get("port", 3306))),
        "USER": user,
        "OUTDIR": str(precheck_out),
        "CHECKS_DIR": str(repo_root / "sql" / "checks"),
    }
    # Pass through env vars first.
    for k, v in env_cfg.items():
        env[str(k)] = str(v)

    # Then force the effective credentials (and password) used by precheck.
    env["MYSQL_BIN"] = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", "mysql")))
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
    mysql_bin = str(env_cfg.get("MYSQL_BIN", client.get("mysql_bin", "mysql")))
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


def run_assessment_checks(cfg: Dict[str, Any], report: Report, repo_root: Path, outdir: Path) -> AssessmentResult:
    gates: List[Gate] = []
    warnings: List[WarningItem] = []
    inventory: Dict[str, Any] = {}

    pre = _run_precheck(repo_root, cfg, outdir, report.log)

    # Load TSVs
    mysql_version = _read_tsv(pre / "mysql_version.tsv")          # expected: 1 row: version, comment?
    innodb = _read_tsv(pre / "innodb_settings.tsv")               # expected: 1 row: file_per_table, fast_shutdown
    auth = _read_tsv(pre / "auth_plugins.tsv")
    json_cols = _read_tsv(pre / "json_columns.tsv")
    enc = _read_tsv(pre / "compression_encryption.tsv")
    engines = _read_tsv(pre / "engines_summary.tsv")
    sizes = _read_tsv(pre / "schema_sizes.tsv")

    schema_charsets = _read_tsv(pre / "schema_charsets.tsv")
    tcoll = _read_tsv(pre / "mysql8_collations.tsv")
    ccoll = _read_tsv(pre / "mysql8_column_collations.tsv")
    sql_mode = _read_tsv(pre / "sql_mode.tsv")
    definers = _read_tsv(pre / "definers_inventory.tsv")
    partitions = _read_tsv(pre / "partitioned_tables.tsv")
    plugins = _read_tsv(pre / "active_plugins.tsv")

    # Previously unwired checks
    func_indexes = _read_tsv(pre / "functional_indexes.tsv")
    func_defaults = _read_tsv(pre / "functional_defaults.tsv")
    invisible_cols = _read_tsv(pre / "invisible_columns.tsv")
    check_cons = _read_tsv(pre / "check_constraints.tsv")
    partial_rev = _read_tsv(pre / "partial_revokes.tsv")
    gis_srid = _read_tsv(pre / "gis_srid_usage.tsv")
    res_groups = _read_tsv(pre / "resource_groups.tsv")
    xplugin = _read_tsv(pre / "xplugin_status.tsv")
    fk_names = _read_tsv(pre / "fk_name_lengths.tsv")
    trigger_ord = _read_tsv(pre / "trigger_order.tsv")

    # New checks
    mysql_roles = _read_tsv(pre / "mysql_roles.tsv")
    gen_cols = _read_tsv(pre / "generated_columns.tsv")
    srv_defaults = _read_tsv(pre / "server_defaults.tsv")
    view_routine = _read_tsv(pre / "view_routine_bodies.tsv")

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
    if innodb and len(innodb[0]) >= 2:
        innodb_file_per_table = innodb[0][0].strip()
        innodb_fast_shutdown = innodb[0][1].strip()

    gates.append(
        Gate(
            "innodb_file_per_table_is_1",
            GateStatus.PASS if innodb_file_per_table == "1" else GateStatus.FAIL,
            {"value": innodb_file_per_table},
        )
    )
    gates.append(_source_db_gate(cfg))

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

    return AssessmentResult(source=source, target=target, gates=gates, warnings=warnings, inventory=inventory)
