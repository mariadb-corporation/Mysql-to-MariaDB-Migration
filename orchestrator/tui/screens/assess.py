"""AssessScreen(Screen[bool]) per design doc §4.5.

Shells out to ``python3 -m orchestrator.migrationctl assess`` via
``run_command_async`` (an @work worker), then re-reads the freshly written
``report.json`` and renders it. Reusing the CLI here is deliberate -- assess
is a single short-lived call whose output is already fully captured in
``report.json``; re-implementing it would duplicate migrationctl.py's own
MIGRATE_APP_USERS side-flow (migrationctl.py:200-233).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Collapsible, Footer, Header, Pretty, Static

from orchestrator.tui import gates
from orchestrator.tui.models import (
    AssessView,
    CheckKind,
    CheckLevel,
    ConfigDraft,
    ModeInfo,
)
from orchestrator.tui.runner_async import run_command_async
from orchestrator.tui.widgets.check_table import CheckTable

_REPORT_FILENAME = "report.json"


def _config_path(repo_root: Path) -> Path:
    """config/source.yaml if present, else config/migration.yaml.

    Per mariadb-migrator:2324-2331 (repeated verbatim at :2261-2270 and
    :2379-2386): assess always prefers config/source.yaml when it exists.
    """
    source_cfg = repo_root / "config" / "source.yaml"
    if source_cfg.is_file():
        return source_cfg
    return repo_root / "config" / "migration.yaml"


def _binlog_advisory_text(details: Mapping[str, Any]) -> str:
    """Reproduce migrationctl.py:246-287 verbatim (including its own
    inconsistent "Parallel Restartable Streaming Copy" vs "Parallel Streaming
    Copy" wording between the two branches -- not a typo to fix here, it's
    the engine's own operator-facing prose and must not be "corrected" out
    from under it)."""
    failures = (details or {}).get("failures") or {}
    json_cols = failures.get("json_columns") or []
    bad_fmt = failures.get("binlog_format")
    src_ver = failures.get("source_version")

    lines: list[str] = []
    if src_ver:
        lines += [
            "",
            f"ERROR: Replication mode is not supported from MySQL {src_ver} sources.",
            "Replication-based migration requires a MySQL 8.0+ source.",
            "",
            f"For a MySQL {src_ver} source, use one of the offline migration modes:",
            "",
            "  - Serial Streaming Copy",
            "  - Parallel Restartable Streaming Copy",
            "  - Offline Copy",
            "",
        ]
        return "\n".join(lines)

    lines += [
        "",
        "ERROR: Source is not compatible with replication mode.",
        "",
    ]

    if json_cols:
        lines.append("Detected JSON columns:")
        for col in json_cols:
            lines.append(f"  {col}")
        lines += [
            "",
            "JSON column types are not supported for online replication-based migration.",
            "Please use one of the offline migration modes:",
            "",
            "  - Serial Streaming Copy",
            "  - Parallel Streaming Copy",
            "  - Offline Copy",
            "",
        ]

    if bad_fmt:
        lines += [
            f"Source binlog_format is '{bad_fmt}'. Replication mode requires 'ROW'.",
            "",
            "To remediate, set the following in the source MySQL configuration",
            "(e.g. /etc/my.cnf or /etc/mysql/my.cnf) and restart the source server:",
            "",
            "  [mysqld]",
            "  binlog_format = ROW",
            "",
        ]

    return "\n".join(lines)


class AssessScreen(Screen[bool]):
    BINDINGS = [
        Binding("r", "rerun", "rerun", show=True),
        Binding("p", "proceed", "proceed to plan", show=True),
        Binding("escape", "go_back", "back", show=True),
        Binding("q", "confirm_quit", "quit", show=True),
    ]

    def __init__(
        self,
        mode: ModeInfo,
        repo_root: Path,
        assess_dir: Path,
        draft: ConfigDraft,
        *,
        staged_phase: str | None = None,
        autorun: bool = True,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.mode = mode
        self.repo_root = repo_root
        self.assess_dir = assess_dir
        self.draft = draft
        self.staged_phase = staged_phase
        self.autorun = autorun
        self._view: AssessView | None = None
        self._proceeded = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static("", id="summary")
            yield CheckTable(id="checks")
            yield Pretty({}, id="detail")
            with Collapsible(title="inventory", id="inventory"):
                yield Pretty({}, id="inventory_pretty")
            yield Static("", id="verdict")
            yield Static("", id="advisory")
            yield Static("", id="error")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"assess · {self.mode.key}"
        if self.autorun:
            self._run_assess()
        else:
            report_path = self.assess_dir / _REPORT_FILENAME
            if report_path.is_file():
                self._load_and_render()
            else:
                self._run_assess()

    # -- execution ---------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        # migrationctl.py's own STAGED_PHASE (load_only skip) and
        # MIGRATE_APP_USERS side-flow checks read os.environ directly, not
        # the config file -- so those must be forwarded into the subprocess.
        # Mirror plan.py's approach and forward the operator's whole draft
        # (filtered for falsy values, excluding "mode"), so assess doesn't
        # fail on missing creds the operator declined to persist to
        # config/migration.yaml while plan (later) succeeds.
        env = os.environ.copy()
        env.update(
            {k: v for k, v in dataclasses.asdict(self.draft).items() if k != "mode" and v}
        )
        if self.staged_phase:
            env["STAGED_PHASE"] = self.staged_phase
        if self.draft.MIGRATE_APP_USERS:
            env["MIGRATE_APP_USERS"] = self.draft.MIGRATE_APP_USERS
        return env

    def _write_log(self, output: str) -> None:
        try:
            self.assess_dir.mkdir(parents=True, exist_ok=True)
            (self.assess_dir / "assess.log").write_text(output)
        except OSError:
            pass

    def _show_error(self, text: str) -> None:
        self.query_one("#error", Static).update(text)

    @work(exclusive=True)
    async def _run_assess(self) -> None:
        cfg = _config_path(self.repo_root)
        argv = [
            sys.executable,
            "-m",
            "orchestrator.migrationctl",
            "assess",
            "--config",
            str(cfg),
            "--mode",
            self.mode.key,
            "--out",
            str(self.assess_dir),
        ]
        try:
            returncode, output = await run_command_async(
                argv, cwd=self.repo_root, env=self._build_env()
            )
        except asyncio.TimeoutError:
            self.notify("Assessment timed out.", severity="error")
            self._load_and_render()
            return
        self._write_log(output)
        if returncode != 0:
            self._show_error(output or f"migrationctl assess exited with status {returncode}")
        self._load_and_render()

    # -- rendering -----------------------------------------------------------

    def _load_and_render(self) -> None:
        report_path = self.assess_dir / _REPORT_FILENAME
        if report_path.is_file():
            try:
                report_data = json.loads(report_path.read_text())
            except (OSError, ValueError):
                report_data = {}
        else:
            report_data = {}

        # migrationctl.py:168-173 short-circuits assess entirely for
        # staged+load_only -- the written report has no gates/warnings and a
        # message string, but the authoritative signal for "this was
        # skipped, not a clean pass" is the mode/phase combination itself,
        # not string-matching report_data["message"].
        skipped = self.mode.key == "staged" and self.staged_phase == "load_only"

        view = AssessView(
            mode=self.mode.key,
            source=report_data.get("source") or {},
            target=report_data.get("target") or {},
            rows=gates.to_check_rows(report_data),
            inventory=report_data.get("inventory") or {},
            success=report_data.get("success"),
            message=report_data.get("message", ""),
            skipped=skipped,
        )
        self._view = view
        self._update_widgets(view)
        self.refresh_bindings()

    def _update_widgets(self, view: AssessView) -> None:
        summary = self.query_one("#summary", Static)
        src = view.source
        tgt = view.target
        src_line = (
            f"{src.get('type', '')} {src.get('version', '')} "
            f"@ {src.get('host', '')}:{src.get('port', '')}"
        )
        tgt_line = f"{tgt.get('type', '')} {tgt.get('version', '')}"
        summary.update(f"{src_line}\n{tgt_line}")

        checks = self.query_one(CheckTable)
        checks.load(view.rows)

        detail = self.query_one("#detail", Pretty)
        highlighted = checks.highlighted_row
        detail.update(highlighted.details if highlighted else {})

        inventory_pretty = self.query_one("#inventory_pretty", Pretty)
        inventory_pretty.update(view.inventory)

        verdict = self.query_one("#verdict", Static)
        verdict.update(self._verdict_text(view))

        advisory = self.query_one("#advisory", Static)
        advisory.update(self._advisory_text(view))

    def _verdict_text(self, view: AssessView) -> str:
        if view.skipped:
            return "ASSESSMENT: SKIPPED (load_only: no source in scope)"
        if view.success:
            return "ASSESSMENT: PASS — ready to plan/run"
        return view.message or "ASSESSMENT: FAIL"

    def _advisory_text(self, view: AssessView) -> str:
        for row in view.rows:
            if (
                row.kind == CheckKind.GATE
                and row.name == "binlog_source_compatibility"
                and row.level == CheckLevel.FAIL
            ):
                return _binlog_advisory_text(row.details)
        return ""

    def _has_failing_gate(self) -> bool:
        if self._view is None:
            return False
        return any(
            row.kind == CheckKind.GATE and row.level == CheckLevel.FAIL
            for row in self._view.rows
        )

    # -- message handlers ------------------------------------------------

    def on_check_table_row_highlighted(self, message: CheckTable.RowHighlighted) -> None:
        detail = self.query_one("#detail", Pretty)
        detail.update(message.row.details if message.row else {})

    # -- actions -----------------------------------------------------------

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool | None:
        # migrationctl.py exits 2 (hard failure) as soon as any gate FAILs --
        # "p" (proceed to plan) must be disabled+grayed the same way, not
        # merely ignored on press.
        if action == "proceed" and self._has_failing_gate():
            return None
        return True

    def action_rerun(self) -> None:
        self._run_assess()

    def action_proceed(self) -> None:
        if self._proceeded:
            return
        if self._has_failing_gate():
            self.app.bell()
            return
        self._proceeded = True
        self.dismiss(True)

    def action_go_back(self) -> None:
        self.dismiss(False)
