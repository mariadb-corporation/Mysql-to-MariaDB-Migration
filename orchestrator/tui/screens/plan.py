"""PlanScreen(Screen[bool]) per design doc §4.6.

New screen, not in the mockups: the wizard has an explicit plan phase and a
"Proceed to run phase now?" gate (mariadb-migrator:2339,2398) -- the last
review point before the run phase starts making writes. This screen shells
out to ``migrationctl plan`` (mariadb-migrator always invokes it with
``--config config/migration.yaml``, mariadb-migrator:2344,2380) so the CLI's
own ``_require_env``/``_validate_mode`` validation runs for real; any
non-zero exit surfaces the merged output as an error rather than letting the
operator proceed on a plan the CLI itself would have rejected.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Collapsible, DataTable, Footer, Header, Pretty, Static

from orchestrator.tui import modes, rundir
from orchestrator.tui.configio import redact
from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.models import ConfigDraft, ModeInfo, StepSpec
from orchestrator.tui.runner_async import run_command_async

# mariadb-migrator:2344,2380 -- the plan phase always reads config/migration.yaml
# (unlike assess, which prefers config/source.yaml when present).
CONFIG_REL_PATH = Path("config") / "migration.yaml"

SKIP_MARK = "⊘"  # "⊘"

# Display-only hint of which env var is behind each self-skip id, so the
# "will skip (...)" note names the relevant variable. modes.expected_skips()
# remains the single source of truth for WHETHER a step actually skips --
# this table only decides what to tell the operator about why.
_SKIP_HINT_ENV: dict[str, str] = {
    "install_target_mariadb": "INSTALL_TARGET_MARIADB",
    "replace_slave_install_mariadb": "INSTALL_TARGET_MARIADB",
    "migrate_app_users": "MIGRATE_APP_USERS",
    "analyze_target": "STAGED_PHASE",
    "inplace_install_mariadb": "INPLACE_EXECUTE",
    "inplace_upgrade": "INPLACE_EXECUTE",
    "replace_slave_cleanup_old_mysql": "REPLACE_DELETE_OLD_MYSQL_DATA",
    "staged_dump": "STAGED_PHASE",
    "staged_load": "STAGED_PHASE",
    "staged_finalize": "STAGED_PHASE",
}


def _skip_note(step_id: str, env: Mapping[str, str]) -> str:
    var = _SKIP_HINT_ENV.get(step_id)
    if var is None:
        return f"{SKIP_MARK} will skip"
    return f"{SKIP_MARK} will skip ({var}={env.get(var, '')})"


class PlanScreen(Screen[bool]):
    BINDINGS = [
        Binding("p", "proceed", "proceed to run", show=True),
        Binding("escape", "go_back", "back", show=True),
        Binding("q", "confirm_quit", "quit", show=True),
    ]

    def __init__(
        self,
        mode: ModeInfo,
        repo_root: Path,
        plan_dir: Path,
        draft: ConfigDraft,
        env: Mapping[str, str],
        step_map: dict,
        *,
        staged_phase: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.mode = mode
        self.repo_root = repo_root
        self.plan_dir = plan_dir
        self.draft = draft
        self.env = dict(env)
        self.step_map = step_map
        self.staged_phase = staged_phase
        # Local resolve_steps() output is the display fallback -- authoritative
        # once the CLI run below succeeds and report.json's plan block loads.
        self._steps: tuple[StepSpec, ...] = modes.resolve_steps(step_map, mode.key)
        self._plan_ok: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static("", id="header")
            yield DataTable(id="steps", cursor_type="row")
            yield Collapsible(Pretty(redact(self.env)), title="Environment", id="env")
            yield Static("", id="error")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#steps", DataTable)
        table.add_columns("#", "id", "name", "script")
        self._render_header()
        self._render_steps()
        self.run_worker(self._run_plan(), exclusive=True)

    def _render_header(self) -> None:
        ts = rundir.default_ts()
        run_dir = self.repo_root / rundir.new_run_dir(self.mode.key, ts)
        lines = (
            f"mode: {self.mode.label}",
            f"staged phase: {self.staged_phase or '-'}",
            f"steps: {len(self._steps)}",
            f"run dir (will be created): {run_dir}",
        )
        self.query_one("#header", Static).update("\n".join(lines))

    def _render_steps(self) -> None:
        table = self.query_one("#steps", DataTable)
        table.clear()
        skips = modes.expected_skips(self.mode.key, self.env)
        for i, step in enumerate(self._steps, start=1):
            if step.id in skips:
                note = _skip_note(step.id, self.env)
                table.add_row(
                    Text(str(i), style="dim"),
                    Text(step.id, style="dim"),
                    Text(step.name, style="dim"),
                    Text(f"{step.script}  {note}", style="dim"),
                    key=step.id,
                )
            else:
                table.add_row(str(i), step.id, step.name, step.script, key=step.id)

    def _write_log(self, output: str) -> None:
        try:
            self.plan_dir.mkdir(parents=True, exist_ok=True)
            (self.plan_dir / "plan.log").write_text(output)
        except OSError:
            pass

    async def _run_plan(self) -> None:
        cfg_path = self.repo_root / CONFIG_REL_PATH
        argv = [
            sys.executable,
            "-m",
            "orchestrator.migrationctl",
            "plan",
            "--config",
            str(cfg_path),
            "--mode",
            self.mode.key,
            "--out",
            str(self.plan_dir),
        ]
        try:
            returncode, output = await run_command_async(
                argv,
                cwd=self.repo_root,
                # The real wizard exports these as real shell env vars before
                # invoking migrationctl (mariadb-migrator's `export SRC_HOST=...`
                # etc.); mirror that here so the CLI's own validation sees the
                # operator's actual inputs, including any secret the operator
                # chose not to persist to config/migration.yaml. Falsy values
                # are filtered so an unset field doesn't silently overwrite an
                # exported shell var of the same name with "".
                env={**os.environ, **{k: v for k, v in self.env.items() if v}},
            )
        except asyncio.TimeoutError:
            self._plan_ok = False
            self._show_error("migrationctl plan timed out.")
            self.refresh_bindings()
            return
        self._write_log(output)
        if returncode != 0:
            self._plan_ok = False
            self._show_error(output or f"migrationctl plan exited with status {returncode}")
            self.refresh_bindings()
            return
        self._plan_ok = True
        self.refresh_bindings()
        self._load_authoritative_steps()

    def _load_authoritative_steps(self) -> None:
        report_path = self.plan_dir / "report.json"
        try:
            report_data = json.loads(report_path.read_text())
        except (OSError, ValueError):
            return
        plan_block = report_data.get("plan") or {}
        raw_steps = plan_block.get("steps")
        if not raw_steps:
            return
        self._steps = tuple(
            StepSpec(
                id=raw["id"],
                name=raw["name"],
                script=raw["script"],
                args=tuple(raw.get("args", []) or []),
            )
            for raw in raw_steps
        )
        self._render_header()
        self._render_steps()

    def _show_error(self, text: str) -> None:
        self.query_one("#error", Static).update(text)

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        if action == "proceed" and self._plan_ok is not True:
            return None
        return True

    def action_proceed(self) -> None:
        if self._plan_ok is not True:
            self.app.bell()
            return
        # Binding dispatch awaits actions directly in the app's own task, not
        # inside a worker -- but push_screen_wait requires get_current_worker()
        # to succeed (see ConfigureScreen.action_quick_save for the same fix).
        # run_worker gives the awaited flow its own worker context. exclusive
        # + a named group ensures a second fast "p" press can't start a
        # second, independent _proceed_flow() worker (which would push a
        # second ConfirmModal and could double-dismiss the screen).
        self.run_worker(self._proceed_flow(), exclusive=True, group="proceed")

    async def _proceed_flow(self) -> None:
        proceed = await self.app.push_screen_wait(
            ConfirmModal("Proceed to run phase now?", default=True)
        )
        if proceed:
            self.dismiss(True)

    def action_go_back(self) -> None:
        self.dismiss(None)
