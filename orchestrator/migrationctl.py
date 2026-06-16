from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List

import typer
import yaml

from .state import StateStore
from .report import Report, GateStatus, StepStatus
from .runner import run_step
from .checks import run_assessment_checks, AssessmentResult

app = typer.Typer(add_completion=False, help="MySQL -> MariaDB migration orchestrator\n© 2026 MariaDB plc ")

DEFAULT_OUTDIR = "artifacts"
DEFAULT_STATE = "state.json"
DEFAULT_REPORT = "report.json"
DEFAULT_LOG = "run.log"

def _failure_hint_from_meta(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    if not meta:
        return None
    tail = meta.get("output_tail") or []
    if not isinstance(tail, list):
        return None
    for line in reversed(tail):
        s = str(line)
        if "ERROR:" in s or "Got error:" in s:
            return s
    for line in reversed(tail):
        s = str(line).strip()
        if s:
            return s
    return None

def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise typer.BadParameter(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _ensure_outdir(outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]

def _load_step_map(repo_root: Path) -> Dict[str, Any]:
    step_map_path = repo_root / "orchestrator" / "step_map.yaml"
    if not step_map_path.exists():
        raise typer.BadParameter(f"Missing step map: {step_map_path}")
    return _load_yaml(step_map_path)

def _resolve_mode(cfg: Dict[str, Any], cli_mode: Optional[str]) -> str:
    if cli_mode:
        return cli_mode.strip().lower()
    return (cfg.get("mode") or "offline").lower()

def _validate_mode(step_map: Dict[str, Any], mode: str) -> None:
    modes = step_map.get("modes", {}) or {}
    if mode not in modes:
        available = ", ".join(sorted(modes.keys()))
        raise typer.BadParameter(f"Unknown mode/playbook: {mode}. Available: {available}")

def _require_env(env: Dict[str, str], keys: List[str], mode_value: str) -> None:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise typer.BadParameter(
            f"Missing required env vars for mode '{mode_value}': {', '.join(missing)}"
        )

def _prompt_env(env: Dict[str, str], key: str, prompt: str, secret: bool = False) -> None:
    if env.get(key):
        return
    env[key] = typer.prompt(prompt, hide_input=secret, confirmation_prompt=False)

def _prompt_required_env(env: Dict[str, str], mode_value: str, non_interactive: bool) -> None:
    if non_interactive:
        return

    # For staged mode, the phase determines which side's prompts apply:
    #   dump_and_load (default) — both source and target prompts
    #   dump_only               — source prompts only (no target)
    #   load_only               — target prompts only (no source); STAGED_DUMP_DIR required
    staged_phase = env.get("STAGED_PHASE", "dump_and_load") if mode_value == "staged" else ""
    skip_source = (staged_phase == "load_only")
    skip_target = (staged_phase == "dump_only") or (mode_value == "inplace")

    if not skip_source:
        _prompt_env(env, "SRC_HOST", "Source host")
        _prompt_env(env, "SRC_PORT", "Source port")
        if mode_value != "inplace" and not env.get("SRC_DB") and not env.get("SRC_DBS"):
            dbs = typer.prompt("Source database(s) (comma-separated for multiple)")
            if "," in dbs:
                env["SRC_DBS"] = dbs
            else:
                env["SRC_DB"] = dbs
        _prompt_env(env, "SRC_ADMIN_USER", "Source admin user")
        _prompt_env(env, "SRC_ADMIN_PASS", "Source admin password", secret=True)

    if not skip_target:
        _prompt_env(env, "TGT_HOST", "Target host")
        _prompt_env(env, "TGT_PORT", "Target port")
        _prompt_env(env, "TGT_ADMIN_USER", "Target admin user")
        _prompt_env(env, "TGT_ADMIN_PASS", "Target admin password", secret=True)
    if mode_value == "binlog":
        _prompt_env(env, "REPL_USER", "Replication user")
        _prompt_env(env, "REPL_PASS", "Replication password", secret=True)
    if mode_value == "replace_slave":
        _prompt_env(env, "REPL_USER", "Replication user")
        _prompt_env(env, "REPL_PASS", "Replication password", secret=True)
        _prompt_env(env, "TGT_SSH_HOST", "Target SSH host")
        _prompt_env(env, "TGT_SSH_USER", "Target SSH user")
        _prompt_env(env, "REPLACE_TARGET_OS", "Target OS (ubuntu|debian|rocky|rhel|centos7|sles)")
        _prompt_env(env, "REPLACE_MARIADB_VERSION", "MariaDB version (for example 11.8)")
    if mode_value == "inplace":
        _prompt_env(env, "INPLACE_BACKUP_DIR", "In-place backup directory")
        _prompt_env(env, "INPLACE_TARGET_OS", "In-place target OS (ubuntu|debian|rocky|rhel|centos7|sles)")
        _prompt_env(env, "INPLACE_MARIADB_VERSION", "In-place MariaDB version (for example 11.8)")
    if mode_value == "staged" and staged_phase == "load_only":
        _prompt_env(env, "STAGED_DUMP_DIR", "Path to existing dump directory (must contain manifest.txt)")

    # Admin-only flow: align migration creds with admin creds.
    if env.get("SRC_ADMIN_USER"):
        env["SRC_USER"] = env["SRC_ADMIN_USER"]
    if env.get("SRC_ADMIN_PASS"):
        env["SRC_PASS"] = env["SRC_ADMIN_PASS"]
    if env.get("TGT_ADMIN_USER"):
        env["TGT_USER"] = env["TGT_ADMIN_USER"]
    if env.get("TGT_ADMIN_PASS"):
        env["TGT_PASS"] = env["TGT_ADMIN_PASS"]

    # two_step uses installed sqldata by default; no SQLINESDATA_CMD* prompts required.

@app.command()
def assess(
    config: Path = typer.Option(..., "--config", "-c", help="Source DB config YAML (read-only)."),
    out: Path = typer.Option(DEFAULT_OUTDIR, "--out", "-o", help="Output directory for artifacts."),
    mode: Optional[str] = typer.Option(
        None,
        "--mode",
        "--playbook",
        "-m",
        help="Selected migration mode (enables mode-aware gates, e.g. binlog/JSON compatibility).",
    ),
    non_interactive: bool = typer.Option(True, "--non-interactive", help="Never prompt; CI-safe."),
):
    """Run read-only assessment: safety gates + warnings + inventory."""
    repo_root = _repo_root()
    _ensure_outdir(out)

    # Initialize state + report
    state_path = out / DEFAULT_STATE
    report_path = out / DEFAULT_REPORT
    log_path = out / DEFAULT_LOG

    state = StateStore(state_path)
    report = Report(report_path, log_path)

    cfg = _load_yaml(config)
    # Added by Manoj on 08May
    # Phase-aware short-circuit: load_only has no source to assess. The
    if os.environ.get("STAGED_PHASE") == "load_only":
        report.start_run(mode="assessment", config_path=str(config))
        report.log("Skipping assessment: STAGED_PHASE=load_only has no source in scope.")
        report.finish_run(success=True, message="Skipped: load_only phase has no source to assess.")
        typer.echo("ASSESSMENT: SKIPPED (load_only: no source in scope)")
        return

    report.start_run(mode="assessment", config_path=str(config))

    try:
        # Perform assessment checks (read-only)
        result: AssessmentResult = run_assessment_checks(cfg, report, repo_root, out, mode=mode)
    except Exception as exc:
        msg = f"Assessment failed during checks: {exc}"
        report.log(f"ERROR: {msg}")
        report.finish_run(success=False, message=msg)
        typer.echo("ASSESSMENT: FAIL (see artifacts/report.json and run.log)")
        raise typer.Exit(code=2)

    # Persist summary
    report.set_source(result.source)
    report.set_target(result.target)
    report.set_gates(result.gates)
    report.set_warnings(result.warnings)
    report.set_inventory(result.inventory)

    # User assessment (read-only). Runs only when the operator opted into
    # user migration via MIGRATE_APP_USERS=1, and only for user-facing modes.
    # Failures are logged but do NOT fail the assess phase: user migration is
    # optional and the operator can still proceed without it. The companion
    # write-phase script (scripts/09_migrate_app_users.sh) runs during the
    # run phase. Added in 1.2.3.
    if os.environ.get("MIGRATE_APP_USERS") == "1" and mode in (
        "one_step", "two_step", "binlog", "staged",
    ):
        assess_script = repo_root / "scripts" / "01_assess_app_users.sh"
        if assess_script.is_file() and os.access(assess_script, os.X_OK):
            env = os.environ.copy()
            # Surface the assess output dir so the script writes its report
            # alongside report.json/run.log instead of falling back to
            # artifacts/.
            env["ASSESS_DIR"] = str(out)
            try:
                proc = subprocess.run(
                    [str(assess_script)],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in (proc.stdout or "").splitlines():
                    report.log(line)
                for line in (proc.stderr or "").splitlines():
                    report.log(f"STDERR: {line}")
                if proc.returncode != 0:
                    report.log(
                        f"WARN: user assessment script exited with code "
                        f"{proc.returncode}; continuing (assessment is optional)."
                    )
            except Exception as exc:
                report.log(f"WARN: user assessment script failed to invoke: {exc}")
        else:
            report.log(
                "NOTE: scripts/01_assess_app_users.sh not found or not executable; "
                "skipping user assessment."
            )

    # Gate decision
    if any(g.status == GateStatus.FAIL for g in result.gates):
        # Surface a friendly, actionable message on stdout for the binlog
        # source-compatibility gate before the generic FAIL summary. The
        # gate may report one or both sub-failures (JSON columns and/or
        # non-ROW binlog_format); render whichever are present.
        for g in result.gates:
            if g.name == "binlog_source_compatibility" and g.status == GateStatus.FAIL:
                failures = (g.details or {}).get("failures") or {}
                json_cols = failures.get("json_columns") or []
                bad_fmt = failures.get("binlog_format")

                typer.echo("")
                typer.echo("ERROR: Source is not compatible with replication mode.")
                typer.echo("")

                if json_cols:
                    typer.echo("Detected JSON columns:")
                    for col in json_cols:
                        typer.echo(f"  {col}")
                    typer.echo("")
                    typer.echo("JSON column types are not supported for online replication-based migration.")
                    typer.echo("Please use one of the offline migration modes:")
                    typer.echo("")
                    typer.echo("  - Serial Streaming Copy")
                    typer.echo("  - Parallel Streaming Copy")
                    typer.echo("  - Offline Copy")
                    typer.echo("")

                if bad_fmt:
                    typer.echo(f"Source binlog_format is '{bad_fmt}'. Replication mode requires 'ROW'.")
                    typer.echo("")
                    typer.echo("To remediate, set the following in the source MySQL configuration")
                    typer.echo("(e.g. /etc/my.cnf or /etc/mysql/my.cnf) and restart the source server:")
                    typer.echo("")
                    typer.echo("  [mysqld]")
                    typer.echo("  binlog_format = ROW")
                    typer.echo("")
                break
        report.finish_run(success=False, message="Assessment failed: one or more hard gates failed.")
        typer.echo("ASSESSMENT: FAIL (see artifacts/report.json and run.log)")
        raise typer.Exit(code=2)

    report.finish_run(success=True, message="Assessment passed. Ready to plan/run.")
    typer.echo("ASSESSMENT: PASS (see artifacts/report.json and run.log)")


@app.command()
def plan(
    config: Path = typer.Option(..., "--config", "-c", help="Migration config YAML."),
    out: Path = typer.Option(DEFAULT_OUTDIR, "--out", "-o", help="Output directory for artifacts."),
    mode: str = typer.Option(
        ...,
        "--mode",
        "--playbook",
        "-m",
        help="Execution mode/playbook (e.g., offline, local, one_step, two_step, near_zero).",
    ),
):
    """Generate a plan from config + step map (no execution)."""
    repo_root = _repo_root()
    _ensure_outdir(out)

    report = Report(out / DEFAULT_REPORT, out / DEFAULT_LOG)
    report.start_run(mode="plan", config_path=str(config))

    cfg = _load_yaml(config)
    step_map = _load_step_map(repo_root)

    mode_value = _resolve_mode(cfg, mode)
    _validate_mode(step_map, mode_value)
    phases = step_map.get("modes", {}).get(mode_value, [])
    steps: List[Dict[str, Any]] = []
    for ph in phases:
        steps.extend(step_map.get("phases", {}).get(ph, []))

    env = cfg.get("env", {}) or {}
    env = {str(k): str(v) for k, v in env.items()}
    # Allow environment variables to override/extend config envs.
    env = {**env, **os.environ}
    if env.get("SRC_ADMIN_USER"):
        env.setdefault("SRC_USER", env["SRC_ADMIN_USER"])
    if env.get("SRC_ADMIN_PASS"):
        env.setdefault("SRC_PASS", env["SRC_ADMIN_PASS"])
    if env.get("TGT_ADMIN_USER"):
        env.setdefault("TGT_USER", env["TGT_ADMIN_USER"])
    if env.get("TGT_ADMIN_PASS"):
        env.setdefault("TGT_PASS", env["TGT_ADMIN_PASS"])
    _prompt_required_env(env, mode_value, non_interactive=False)
    if mode_value in ("one_step", "two_step", "binlog", "replace_slave"):
        _require_env(
            env,
            ["SRC_HOST", "TGT_HOST"],
            mode_value,
        )
        _require_env(
            env,
            ["SRC_ADMIN_USER", "SRC_ADMIN_PASS", "TGT_ADMIN_USER", "TGT_ADMIN_PASS"],
            mode_value,
        )
        install_target = str(env.get("INSTALL_TARGET_MARIADB", "0")).strip().lower() in ("1", "true", "yes", "y")
        if mode_value in ("one_step", "two_step") and install_target:
            _require_env(env, ["TGT_SSH_HOST"], mode_value)
        if not (env.get("SRC_DB") or env.get("SRC_DBS")):
            raise typer.BadParameter("Missing SRC_DB or SRC_DBS for one_step/two_step/binlog.")
        if mode_value in ("binlog", "replace_slave"):
            _require_env(env, ["REPL_USER", "REPL_PASS"], mode_value)
        if mode_value == "replace_slave":
            _require_env(
                env,
                ["TGT_SSH_HOST", "TGT_SSH_USER", "REPLACE_TARGET_OS", "REPLACE_MARIADB_VERSION"],
                mode_value,
            )
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if (
                env.get("SRC_USER") == "root"
                or env.get("TGT_USER") == "root"
                or env.get("SRC_ADMIN_USER") == "root"
                or env.get("TGT_ADMIN_USER") == "root"
            ):
                raise typer.BadParameter(
                    "SRC/TGT admin and migration users must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )
    if mode_value == "staged":
        staged_phase = env.get("STAGED_PHASE", "dump_and_load")
        if staged_phase not in ("dump_and_load", "dump_only", "load_only"):
            raise typer.BadParameter(
                f"Invalid STAGED_PHASE='{staged_phase}'. "
                "Must be one of: dump_and_load, dump_only, load_only"
            )
        # Make the resolved phase explicit so the plan reflects the value the
        # run phase will actually use (keeps plan/run defaults in lockstep).
        env["STAGED_PHASE"] = staged_phase
        if staged_phase != "load_only":
            _require_env(env, ["SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS"], mode_value)
            if not (env.get("SRC_DB") or env.get("SRC_DBS")):
                raise typer.BadParameter(
                    f"Missing SRC_DB or SRC_DBS for staged ({staged_phase})."
                )
        if staged_phase != "dump_only":
            _require_env(env, ["TGT_HOST", "TGT_ADMIN_USER", "TGT_ADMIN_PASS"], mode_value)
            install_target = str(env.get("INSTALL_TARGET_MARIADB", "0")).strip().lower() in ("1", "true", "yes", "y")
            if install_target:
                _require_env(env, ["TGT_SSH_HOST"], mode_value)
        if staged_phase == "load_only":
            _require_env(env, ["STAGED_DUMP_DIR"], mode_value)
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if (
                env.get("SRC_USER") == "root"
                or env.get("TGT_USER") == "root"
                or env.get("SRC_ADMIN_USER") == "root"
                or env.get("TGT_ADMIN_USER") == "root"
            ):
                raise typer.BadParameter(
                    "SRC/TGT admin and migration users must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )
    if mode_value == "inplace":
        _require_env(
            env,
            ["SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS", "INPLACE_BACKUP_DIR", "INPLACE_TARGET_OS", "INPLACE_MARIADB_VERSION"],
            mode_value,
        )
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if env.get("SRC_ADMIN_USER") == "root":
                raise typer.BadParameter(
                    "SRC admin user must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )

    report.set_plan({"mode": mode_value, "phases": phases, "steps": steps})
    report.finish_run(success=True, message="Plan generated (no execution).")
    typer.echo("PLAN: generated in artifacts/report.json")


@app.command()
def run(
    config: Path = typer.Option(..., "--config", "-c", help="Migration config YAML."),
    out: Path = typer.Option(DEFAULT_OUTDIR, "--out", "-o", help="Output directory for artifacts."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt; CI-safe."),
    mode: str = typer.Option(
        ...,
        "--mode",
        "--playbook",
        "-m",
        help="Execution mode/playbook (e.g., offline, local, one_step, two_step, near_zero).",
    ),
):
    """Execute migration steps (offline mode) with resume-safe state tracking."""
    repo_root = _repo_root()
    _ensure_outdir(out)

    state = StateStore(out / DEFAULT_STATE)
    report = Report(out / DEFAULT_REPORT, out / DEFAULT_LOG)
    report.start_run(mode="run", config_path=str(config))

    cfg = _load_yaml(config)
    step_map = _load_step_map(repo_root)

    mode_value = _resolve_mode(cfg, mode)
    _validate_mode(step_map, mode_value)
    phases = step_map.get("modes", {}).get(mode_value, [])
    steps: List[Dict[str, Any]] = []
    for ph in phases:
        steps.extend(step_map.get("phases", {}).get(ph, []))

    report.set_plan({"mode": mode_value, "phases": phases, "steps": steps})
    
   #executor = cfg.get("executor", {}) or {}

    # Execute steps sequentially
    env = cfg.get("env", {}) or {}
    env = {str(k): str(v) for k, v in env.items()}
    # Allow environment variables to override/extend config envs.
    env = {**env, **os.environ}
    if env.get("SRC_ADMIN_USER"):
        env.setdefault("SRC_USER", env["SRC_ADMIN_USER"])
    if env.get("SRC_ADMIN_PASS"):
        env.setdefault("SRC_PASS", env["SRC_ADMIN_PASS"])
    if env.get("TGT_ADMIN_USER"):
        env.setdefault("TGT_USER", env["TGT_ADMIN_USER"])
    if env.get("TGT_ADMIN_PASS"):
        env.setdefault("TGT_PASS", env["TGT_ADMIN_PASS"])
    _prompt_required_env(env, mode_value, non_interactive)

    # Added 12Jun: direct `migrationctl run` invocations must provide the same
    # env contract as the launcher, which exports RUN_DIR before invoking
    # phase scripts. Scripts like 00_preflight_staged.sh default
    # STAGED_DUMP_DIR to RUN_DIR/dumps; without this, a direct run fails at
    # preflight ("STAGED_DUMP_DIR is not set and RUN_DIR is not set").
    # setdefault preserves a launcher-set RUN_DIR if one is already exported;
    # --out is authoritative only when RUN_DIR is absent.
    env.setdefault("RUN_DIR", str(out.resolve()))
    if env["RUN_DIR"] != str(out.resolve()):
        report.log(
            f"NOTE: RUN_DIR ({env['RUN_DIR']}) differs from --out ({out}); "
            "honoring RUN_DIR from environment (launcher-set)."
        )

    if mode_value in ("one_step", "two_step", "binlog", "replace_slave"):
        _require_env(
            env,
            ["SRC_HOST", "TGT_HOST"],
            mode_value,
        )
        _require_env(
            env,
            ["SRC_ADMIN_USER", "SRC_ADMIN_PASS", "TGT_ADMIN_USER", "TGT_ADMIN_PASS"],
            mode_value,
        )
        install_target = str(env.get("INSTALL_TARGET_MARIADB", "0")).strip().lower() in ("1", "true", "yes", "y")
        if mode_value in ("one_step", "two_step") and install_target:
            _require_env(env, ["TGT_SSH_HOST"], mode_value)
        if not (env.get("SRC_DB") or env.get("SRC_DBS")):
            raise typer.BadParameter("Missing SRC_DB or SRC_DBS for one_step/two_step/binlog.")
        if mode_value in ("binlog", "replace_slave"):
            _require_env(env, ["REPL_USER", "REPL_PASS"], mode_value)
        if mode_value == "replace_slave":
            _require_env(
                env,
                ["TGT_SSH_HOST", "TGT_SSH_USER", "REPLACE_TARGET_OS", "REPLACE_MARIADB_VERSION"],
                mode_value,
            )
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if (
                env.get("SRC_USER") == "root"
                or env.get("TGT_USER") == "root"
                or env.get("SRC_ADMIN_USER") == "root"
                or env.get("TGT_ADMIN_USER") == "root"
            ):
                raise typer.BadParameter(
                    "SRC/TGT admin and migration users must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )
    if mode_value == "staged":
        staged_phase = env.get("STAGED_PHASE", "dump_and_load")
        if staged_phase not in ("dump_and_load", "dump_only", "load_only"):
            raise typer.BadParameter(
                f"Invalid STAGED_PHASE='{staged_phase}'. "
                "Must be one of: dump_and_load, dump_only, load_only"
            )
        # Added 12Jun: pass the resolved phase to the phase scripts explicitly,
        # so the subprocess env doesn't depend on the operator (or launcher)
        # having exported STAGED_PHASE. Keeps migrationctl's validation and
        # 00_preflight_staged.sh's defaulting in lockstep rather than agreeing
        # by coincidence on 'dump_and_load'.
        env["STAGED_PHASE"] = staged_phase
        if staged_phase != "load_only":
            _require_env(env, ["SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS"], mode_value)
            if not (env.get("SRC_DB") or env.get("SRC_DBS")):
                raise typer.BadParameter(
                    f"Missing SRC_DB or SRC_DBS for staged ({staged_phase})."
                )
        if staged_phase != "dump_only":
            _require_env(env, ["TGT_HOST", "TGT_ADMIN_USER", "TGT_ADMIN_PASS"], mode_value)
            install_target = str(env.get("INSTALL_TARGET_MARIADB", "0")).strip().lower() in ("1", "true", "yes", "y")
            if install_target:
                _require_env(env, ["TGT_SSH_HOST"], mode_value)
        if staged_phase == "load_only":
            _require_env(env, ["STAGED_DUMP_DIR"], mode_value)
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if (
                env.get("SRC_USER") == "root"
                or env.get("TGT_USER") == "root"
                or env.get("SRC_ADMIN_USER") == "root"
                or env.get("TGT_ADMIN_USER") == "root"
            ):
                raise typer.BadParameter(
                    "SRC/TGT admin and migration users must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )
    if mode_value == "inplace":
        _require_env(
            env,
            ["SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS", "INPLACE_BACKUP_DIR", "INPLACE_TARGET_OS", "INPLACE_MARIADB_VERSION"],
            mode_value,
        )
        if env.get("ALLOW_ROOT_USERS") not in ("1", "true", "TRUE", "True"):
            if env.get("SRC_ADMIN_USER") == "root":
                raise typer.BadParameter(
                    "SRC admin user must not be root. Set ALLOW_ROOT_USERS=1 to override."
                )

    failures = []
    failure_meta: Optional[Dict[str, Any]] = None
    for s in steps:
        step_id = s["id"]
        name = s.get("name", step_id)
        script = s.get("script")
        args = s.get("args", []) or []

        # Skip completed
        if state.is_done(step_id):
            report.log(f"SKIP {step_id} ({name}) - already DONE")
            report.add_step(step_id, name, StepStatus.SKIPPED, details={"reason": "already_done"})
            continue

        report.log(f"RUN  {step_id} ({name}) -> {script}")
        ok, meta = run_step(repo_root, script, args=args, extra_env=env, log=report.log)
        if ok:
            state.mark_done(step_id, meta=meta)
            report.add_step(step_id, name, StepStatus.DONE, details=meta)
        else:
            state.mark_failed(step_id, meta=meta)
            report.add_step(step_id, name, StepStatus.FAILED, details=meta)
            failure_meta = meta
            failures.append(step_id)
            break  # fail-fast

    if failures:
        report.finish_run(success=False, message=f"Run failed at step: {failures[0]}")
        typer.echo(f"RUN: FAIL at {failures[0]} (see artifacts/run.log)")
        failure_hint = _failure_hint_from_meta(failure_meta)
        if failure_hint:
            typer.echo(f"OUT {failure_hint}")
        raise typer.Exit(code=3)

    report.finish_run(success=True, message="Run completed successfully.")
    typer.echo("RUN: PASS")


@app.command()
def resume(
    out: Path = typer.Option(DEFAULT_OUTDIR, "--out", "-o", help="Output directory containing state.json."),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Migration config YAML. If omitted, uses path stored in report.json if present."),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Never prompt; CI-safe."),
    mode: str = typer.Option(
        ...,
        "--mode",
        "--playbook",
        "-m",
        help="Execution mode/playbook override.",
    ),
):
    """Resume a previously failed run using the state.json checkpoint."""
    report_path = out / DEFAULT_REPORT
    if config is None:
        if report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            cfg_path = data.get("config_path")
            if cfg_path:
                config = Path(cfg_path)
    if config is None:
        raise typer.BadParameter("Config path not provided and not found in report.json")

    # Just call run() (it will skip DONE steps)
    run(config=config, out=out, non_interactive=non_interactive, mode=mode)


def main():
    app()


if __name__ == "__main__":
    main()
