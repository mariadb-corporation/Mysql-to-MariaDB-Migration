"""SummaryScreen(Screen[None]) per design doc §4.9.

Ports mariadb-migrator:2420-2621's four post-run guidance variants:
``print_success_summary``'s three branches (staged/dump_only, staged/load_only,
and the default full-migration path) plus ``print_failure_summary``.

This screen has no access to ``ConfigDraft``/env (only ``report_data`` and
``run_dir``), so any host/port/user values used in connect-command snippets
fall back to the same placeholders the bash itself uses when a var is unset
(e.g. ``${TGT_HOST:-<target>}``, ``${TGT_ADMIN_USER:-<user>}`` at
mariadb-migrator:2586) rather than guessing at values this screen cannot see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Markdown, Static

from orchestrator.tui.modes import MODE_CATALOG
from orchestrator.tui.screens.log_view import LogScreen

# report.py's StepStatus enum only ever writes DONE/FAILED/SKIPPED into the
# top-level "steps" array (add_step()) -- unrelated to the TUI's own richer
# StepUiStatus used while a run is in progress.
_STEP_STATUS_ICON = {
    "DONE": "✓",
    "FAILED": "✗",
    "SKIPPED": "⊘",
}


def _mode_label(mode: str) -> str:
    for info in MODE_CATALOG:
        if info.key == mode:
            return info.label
    return mode


def _connect_cmd(desc: Mapping[str, Any], *, host_placeholder: str, user_placeholder: str = "<user>") -> str:
    host = desc.get("host") or host_placeholder
    port = desc.get("port") or "3306"
    user = desc.get("user") or user_placeholder
    return f"mariadb -h {host} -P {port} -u {user} -p --ssl-verify-server-cert=OFF"


def _verdict_text(success: bool, staged_phase: str | None, plan_only: bool = False) -> str:
    if not success:
        return "MIGRATION FAILED"
    if plan_only:
        return "PLAN COMPLETE"
    if staged_phase == "dump_only":
        return "DUMP COMPLETE"
    if staged_phase == "load_only":
        return "LOAD COMPLETE"
    return "MIGRATION SUCCESSFUL"


def _dump_only_body(run_dir: Path) -> str:
    # mariadb-migrator:2442-2472
    dump_dir = run_dir / "dumps"
    manifest = dump_dir / "manifest.txt"
    return f"""\
The target was **NOT** touched. Source dump files are on disk and ready
for load on this host or another.

- **Dump dir**: `{dump_dir}`
- **Manifest**: `{manifest}`

Next steps -- load these dumps into a target:

1. (Optional) Move the dump dir to the target host:

       scp -r {dump_dir} <target-host>:/path/to/dumps/

2. Run the loader, pointing `STAGED_DUMP_DIR` at the directory:

       STAGED_PHASE=load_only \\
       STAGED_DUMP_DIR={dump_dir} \\
       ./mariadb-migrator

   The 'staged' mode + 'load_only' phase will skip source connection
   and read the database list from manifest.txt.
"""


def _plan_only_body(mode: str, plan_dir: Path, assess_dir: Path | None) -> str:
    assess_report = (assess_dir / "report.json") if assess_dir is not None else Path("<assess dir>/report.json")
    plan_report = plan_dir / "report.json"
    return f"""\
No data was moved -- this run only assessed the source and target and
produced a migration plan.

- **Assessment report**: `{assess_report}`
- **Plan report**: `{plan_report}`

Next steps -- proceed with the full migration:

1. Re-launch the TUI and choose "2) Assess + Run" to continue through the
   full migration, with confirm steps between phases.

2. Or invoke the CLI directly:

       python3 -m orchestrator.migrationctl run \\
           --config config/migration.yaml \\
           --mode {mode} \\
           --out <new run dir>
"""


def _load_only_body(report_data: Mapping[str, Any], target: Mapping[str, Any]) -> str:
    # mariadb-migrator:2477-2509. STAGED_DUMP_DIR is a wizard env var, not
    # part of report.json's documented schema -- best-effort read from
    # report_data, otherwise fall back to "?" exactly as the bash itself
    # does for an unset STAGED_DUMP_DIR (`${STAGED_DUMP_DIR:-?}`).
    source = report_data.get("source") or {}
    dump_dir = source.get("staged_dump_dir") or source.get("path") or "?"
    manifest = f"{dump_dir}/manifest.txt"
    connect = _connect_cmd(target, host_placeholder="<target>")
    return f"""\
Next steps -- validate the loaded data on the target:

1. Connect to the target MariaDB:

       {connect}

2. Confirm the loaded databases are present:

       SHOW DATABASES;

3. Compare row counts against the manifest:

       cat {manifest}
"""


def _full_migration_body(target: Mapping[str, Any]) -> str:
    # mariadb-migrator:2511-2577. The row-count parity and spot-check
    # queries below are copied verbatim from :2554-2561.
    connect = _connect_cmd(target, host_placeholder="<target>")
    return f"""\
Next steps -- validate the migrated data on the target:

1. Connect to the target MariaDB:

       {connect}

2. Confirm the database is present and inspect its tables:

       SHOW DATABASES;

3. Row-count parity check (run on source and target, compare):

       SELECT table_schema, table_name, table_rows
       FROM information_schema.tables
       WHERE table_schema NOT IN ('mysql','information_schema','performance_schema','sys');

4. Spot-check a few rows from each key table:

       SELECT * FROM <db>.<table> LIMIT 10;
"""


def _failure_body(mode: str, run_dir: Path, report_data: Mapping[str, Any]) -> str:
    # mariadb-migrator:2580-2621
    run_log = run_dir / "run.log"
    source = report_data.get("source") or {}
    target = report_data.get("target") or {}
    src_connect = _connect_cmd(source, host_placeholder="<source>")
    tgt_connect = _connect_cmd(target, host_placeholder="<target>")
    if mode == "two_step":
        resume_note = (
            "**two_step has no mid-run resume.** mariadb-mtk auto-retries "
            "transient errors (network blips, brief target restarts) within "
            "a run; an interrupted or failed run is restarted by dropping "
            "the target database(s) and re-running from a clean target."
        )
    elif mode in ("binlog", "replace_slave"):
        resume_note = (
            f"`{_mode_label(mode)}` resumes from its checkpoint "
            "(re-run with the same config)."
        )
    else:
        resume_note = (
            "An interrupted or failed run is restarted by dropping the "
            "target database(s) and re-running from a clean target."
        )
    return f"""\
What to do next:

1. Inspect the run log for the failing step:

       less {run_log}

2. Re-check source/target connectivity independently:

       {src_connect}
       {tgt_connect}

3. {resume_note}

4. If a pre-existing database on the target caused the failure, drop it
   on the target and re-run.
"""


def nextsteps_markdown(
    *,
    success: bool,
    mode: str,
    staged_phase: str | None,
    run_dir: Path,
    report_data: Mapping[str, Any],
    plan_only: bool = False,
    assess_dir: Path | None = None,
) -> str:
    """Selects exactly one of the five bodies per design doc §4.9 (plus the
    plan-only case, a design gap not covered by §4.9 itself -- see the
    Phase 1 review that identified it)."""
    if not success:
        return _failure_body(mode, run_dir, report_data)
    if plan_only:
        return _plan_only_body(mode, run_dir, assess_dir)
    target = report_data.get("target") or {}
    if staged_phase == "dump_only":
        return _dump_only_body(run_dir)
    if staged_phase == "load_only":
        return _load_only_body(report_data, target)
    return _full_migration_body(target)


class SummaryScreen(Screen[None]):
    BINDINGS = [
        Binding("o", "open_run_dir", "open run dir", show=True),
        Binding("l", "full_log", "full log", show=True),
        Binding("q", "quit", "quit", show=True),
    ]

    def __init__(
        self,
        *,
        success: bool,
        mode: str,
        staged_phase: str | None,
        run_dir: Path,
        report_data: dict,
        assess_dir: Path | None = None,
        plan_only: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.success = success
        self.mode = mode
        self.staged_phase = staged_phase
        self.run_dir = run_dir
        self.report_data = report_data
        self.assess_dir = assess_dir
        self.plan_only = plan_only

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static(_verdict_text(self.success, self.staged_phase, self.plan_only), id="verdict")
            body = nextsteps_markdown(
                success=self.success,
                mode=self.mode,
                staged_phase=self.staged_phase,
                run_dir=self.run_dir,
                report_data=self.report_data,
                plan_only=self.plan_only,
                assess_dir=self.assess_dir,
            )
            yield Markdown(body, id="nextsteps")
            table = DataTable(id="steps", cursor_type="row")
            table.add_columns("id", "name", "status")
            yield table
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#steps", DataTable)
        for step in self.report_data.get("steps") or []:
            status = step.get("status", "")
            icon = _STEP_STATUS_ICON.get(status, "?")
            table.add_row(step.get("id", ""), step.get("name", ""), f"{icon} {status}")

    def action_open_run_dir(self) -> None:
        # Nice-to-have per design doc §4.9 -- no shell-out, just surface the
        # path so the operator can navigate there themselves.
        self.notify(f"Run directory: {self.run_dir}")

    def action_full_log(self) -> None:
        filename = "plan.log" if self.plan_only else "run.log"
        self.app.push_screen(LogScreen(self.run_dir, log_filename=filename))

    def action_quit(self) -> None:
        self.dismiss(None)
