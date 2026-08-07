"""MigrationApp(App[None]) per design doc §4 (intro + MigrationApp bullets)
and §5.10 (navigation graph).

This is the real app -- every screen wired in here (``ModeSelectScreen``,
``StagedPhaseScreen``, ``ConfigureScreen``, ``AssessScreen``, ``PlanScreen``,
``SummaryScreen``, ``LogScreen``) was previously pilot-tested only through
throwaway single-screen test harnesses. ``app.py`` is the first place they
are wired into a real navigation graph via real ``dismiss()``/callback
chaining, exactly the pattern every one of those screens already commits to.

Important structural note on the chaining pattern used throughout this
module: ``Screen.dismiss(result)`` always pops the dismissing screen off the
stack before invoking its callback. That means the screen stack does NOT
keep every previously-visited screen around the way a naive "just push more
screens" reading might suggest -- e.g. by the time ``PlanScreen`` dismisses,
``AssessScreen`` (and everything before it) has already been popped. Any
"go back" edge in the nav graph that lands on a screen further back than the
immediate parent therefore has to explicitly re-push a fresh instance of
that screen here; it is not free via the stack alone. Each callback below
does exactly that where the design doc's graph (§5.10) calls for it.

There is no ``SessionContext`` class anywhere in this codebase -- run state
(``mode``, ``step_map``, ``assess_dir``, ``plan_dir``, ``run_dir``) is held
as plain attributes directly on ``MigrationApp``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import yaml
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static

from orchestrator.tui import rundir
from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.models import ConfigDraft, ModeInfo
from orchestrator.tui.screens.assess import AssessScreen
from orchestrator.tui.screens.configure import ConfigureScreen
from orchestrator.tui.screens.log_view import LogScreen
from orchestrator.tui.screens.mode_select import ModeSelectScreen
from orchestrator.tui.screens.plan import PlanScreen
from orchestrator.tui.screens.staged_phase import StagedPhaseScreen
from orchestrator.tui.screens.summary import SummaryScreen
from orchestrator.tui.screens.welcome import WelcomeScreen

_RUN_STUB_MESSAGE = (
    "Run phase not yet implemented (Phase 2) -- exiting.\n\n"
    "Press any key to quit."
)
_RESUME_STUB_MESSAGE = (
    "Resume is not yet implemented (Phase 2) -- exiting.\n\n"
    "Press any key to quit."
)


class _RunPhaseStubScreen(Screen[None]):
    """Deliberate, temporary placeholder for RunScreen.

    RunScreen is Phase 2 scope and does not exist in this workflow run. Two
    real navigation edges in design doc §5.10 would otherwise push it:
    PlanScreen's "p" -> ConfirmModal -> Yes for ``phase_mode == "all"``, and
    WelcomeScreen's ResumeChoiceModal "Resume" choice. Both land here
    instead. This is NOT a design decision -- it is a stand-in until
    RunScreen is built; do not mistake it for one.
    """

    BINDINGS = [
        Binding("q", "quit_app", "quit", show=True),
        Binding("enter", "quit_app", "quit", show=False),
        Binding("escape", "quit_app", "quit", show=False),
    ]

    def __init__(self, message: str = _RUN_STUB_MESSAGE, id: str | None = None) -> None:
        super().__init__(id=id)
        self.message = message

    def compose(self) -> ComposeResult:
        yield Static(self.message, id="stub-message")

    def action_quit_app(self) -> None:
        # dismiss(), not a direct self.app.exit() -- keeps this screen on the
        # same dismiss-callback contract as every other screen in the graph;
        # the registered callback (MigrationApp._on_run_stub_done) is what
        # actually exits the app.
        self.dismiss(None)


def _load_step_map(repo_root: Path) -> dict:
    """Port of migrationctl.py's ``_load_step_map`` (reads
    ``orchestrator/step_map.yaml`` relative to ``repo_root``).

    Raises ``FileNotFoundError`` rather than ``typer.BadParameter`` --
    app.py is not a CLI and has no typer context to raise into.
    """
    step_map_path = repo_root / "orchestrator" / "step_map.yaml"
    if not step_map_path.exists():
        raise FileNotFoundError(f"Missing step map: {step_map_path}")
    with step_map_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _draft_env(draft: ConfigDraft) -> dict[str, str]:
    """Every ``ConfigDraft`` field except ``mode`` as a flat env-var mapping.

    ``ConfigDraft``'s field names already match the env-var names the shell
    scripts and ``migrationctl`` read directly (``SRC_HOST``,
    ``STAGED_PHASE``, ...) -- this mirrors the wizard's own ``export
    SRC_HOST=...`` block before invoking ``migrationctl``.
    """
    return {k: v for k, v in dataclasses.asdict(draft).items() if k != "mode"}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# Mirrors the mode-exclusive field groups in screens/configure.py's
# ConfigureScreen.compose() (the "repl"/"inplace"/"replace"/"staged"
# Collapsibles) -- deliberately duplicated here rather than imported,
# since app.py has no live ConfigureScreen instance to query at the point
# a new mode is selected. If those groups' field lists ever change in
# configure.py, update this table to match.
_MODE_EXCLUSIVE_FIELDS: dict[str, tuple[str, ...]] = {
    "repl": ("REPL_USER", "REPL_PASS"),
    "inplace": (
        "INPLACE_BACKUP_DIR", "INPLACE_EXECUTE", "INPLACE_TARGET_OS",
        "INPLACE_MARIADB_VERSION", "INPLACE_STOP_CMD", "INPLACE_START_CMD",
        "INPLACE_UPGRADE_CMD",
    ),
    "replace": (
        "REPLACE_TARGET_OS", "REPLACE_MARIADB_VERSION", "REPLACE_BACKUP_CMD",
        "REPLACE_STOP_MYSQL_CMD", "REPLACE_UNINSTALL_MYSQL_CMD",
        "REPLACE_START_MARIADB_CMD", "REPLACE_CONFIGURE_BIND_ADDRESS",
        "REPLACE_MARIADB_BIND_ADDRESS", "REPLACE_AUTO_GRANT_TARGET_ADMIN",
        "REPLACE_TARGET_ADMIN_HOST_PATTERN", "REPLACE_DELETE_OLD_MYSQL_DATA",
        "REPLACE_CLEANUP_CMD",
    ),
    # STAGED_PHASE itself is handled separately (already reset on mode/phase
    # change elsewhere) -- this only covers the staged-specific extras.
    "staged": ("STAGED_DUMP_DIR", "STAGED_COMPRESS", "STAGED_PV", "STAGED_PARALLEL"),
}


def _groups_for_mode(mode: str) -> frozenset[str]:
    groups: set[str] = set()
    if mode in ("binlog", "replace_slave"):
        groups.add("repl")
    if mode == "inplace":
        groups.add("inplace")
    if mode == "replace_slave":
        groups.add("replace")
    if mode == "staged":
        groups.add("staged")
    return frozenset(groups)


def _clear_stale_mode_fields(draft: ConfigDraft, old_mode: str | None, new_mode: str) -> None:
    """Blank draft fields exclusive to a mode group the operator is leaving,
    unless the new mode still uses that same group (e.g. binlog <-> replace_slave
    both use "repl" -- REPL_USER/REPL_PASS are preserved across that switch)."""
    if old_mode is None or old_mode == new_mode:
        return
    stale_groups = _groups_for_mode(old_mode) - _groups_for_mode(new_mode)
    for group in stale_groups:
        for field in _MODE_EXCLUSIVE_FIELDS[group]:
            setattr(draft, field, "")


class MigrationApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "migrationctl"

    BINDINGS = [
        Binding("q", "confirm_quit", "quit", show=True),
        Binding("l", "show_log", "log", show=True),
    ]

    def __init__(self, repo_root: Path) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.draft: ConfigDraft = ConfigDraft()
        self.mode: ModeInfo | None = None
        self.staged_phase: str | None = None
        self.phase_mode: str | None = None
        self.step_map: dict = {}
        self.assess_dir: Path | None = None
        self.plan_dir: Path | None = None
        self.run_dir: Path | None = None

    # -- startup -------------------------------------------------------------

    def on_mount(self) -> None:
        try:
            self.step_map = _load_step_map(self.repo_root)
        except FileNotFoundError as exc:
            self.step_map = {}
            self.notify(str(exc), severity="error")
        self.sub_title = "welcome"
        self.push_screen(
            WelcomeScreen(repo_root=self.repo_root), self._on_welcome_done
        )

    # -- global bindings -------------------------------------------------------

    def action_confirm_quit(self) -> None:
        self.run_worker(self._confirm_quit_flow())

    async def _confirm_quit_flow(self) -> None:
        # No run worker exists to check for in this phase's scope (RunScreen
        # isn't built yet) -- always confirm, per this build item's spec.
        confirmed = await self.push_screen_wait(
            ConfirmModal("Quit? Any progress not yet saved will be lost.")
        )
        if confirmed:
            self.exit()

    def action_show_log(self) -> None:
        if self.run_dir is not None:
            self.push_screen(LogScreen(self.run_dir, log_filename="run.log"))
        elif self.plan_dir is not None:
            self.push_screen(LogScreen(self.plan_dir, log_filename="plan.log"))
        elif self.assess_dir is not None:
            self.push_screen(LogScreen(self.assess_dir, log_filename="assess.log"))
        else:
            self.bell()

    # -- Welcome ---------------------------------------------------------------

    def _on_welcome_done(self, phase_mode: str | None) -> None:
        if phase_mode is None or phase_mode == "quit":
            self.exit()
            return
        if phase_mode == "resume":
            self._push_run_stub(_RESUME_STUB_MESSAGE)
            return
        self.phase_mode = phase_mode
        self.sub_title = f"select mode · {phase_mode}"
        self.push_screen(ModeSelectScreen(), self._on_mode_selected)

    # -- ModeSelect --------------------------------------------------------------

    def _on_mode_selected(self, mode: ModeInfo | None) -> None:
        if mode is None:
            # Back to Welcome -- ModeSelectScreen was pushed directly from
            # the (now-popped) WelcomeScreen, so re-push a fresh one. The
            # operator already saw and decided on any resumable run at real
            # app startup -- skip re-showing ResumeChoiceModal on this
            # back-edge.
            self.sub_title = "welcome"
            self.push_screen(
                WelcomeScreen(repo_root=self.repo_root, skip_resume_check=True),
                self._on_welcome_done,
            )
            return
        _clear_stale_mode_fields(self.draft, self.mode.key if self.mode else None, mode.key)
        self.mode = mode
        self.draft.mode = mode.key
        if mode.key == "staged":
            self.sub_title = f"staged phase · {mode.key}"
            self.push_screen(StagedPhaseScreen(), self._on_staged_phase_done)
        else:
            self.staged_phase = None
            self.draft.STAGED_PHASE = ""
            self._push_configure()

    # -- StagedPhase -------------------------------------------------------------

    def _on_staged_phase_done(self, phase: str | None) -> None:
        if phase is None:
            # Back to ModeSelect.
            self.sub_title = "select mode"
            self.push_screen(ModeSelectScreen(), self._on_mode_selected)
            return
        self.staged_phase = phase
        self.draft.STAGED_PHASE = phase
        if phase == "dump_only":
            # mariadb-migrator:1497 -- applied here per design doc §4.3.
            self.draft.INSTALL_TARGET_MARIADB = "0"
        self._push_configure()

    # -- Configure -----------------------------------------------------------

    def _push_configure(self) -> None:
        if self.mode is None:
            raise RuntimeError("_push_configure called with no mode selected")
        self.sub_title = f"configure · {self.mode.key}"
        self.push_screen(
            ConfigureScreen(
                self.mode,
                self.draft,
                staged_phase=self.staged_phase,
                repo_root=self.repo_root,
            ),
            self._on_configure_done,
        )

    def _on_configure_done(self, draft: ConfigDraft | None) -> None:
        if self.mode is None:
            raise RuntimeError("_on_configure_done called with no mode selected")
        if draft is None:
            # Back to StagedPhaseScreen (staged mode) or ModeSelectScreen
            # (every other mode) -- whichever fed into Configure.
            if self.mode.key == "staged":
                self.sub_title = f"staged phase · {self.mode.key}"
                self.push_screen(StagedPhaseScreen(), self._on_staged_phase_done)
            else:
                self.sub_title = "select mode"
                self.push_screen(ModeSelectScreen(), self._on_mode_selected)
            return
        self.draft = draft
        self._push_assess()

    # -- Assess --------------------------------------------------------------

    def _push_assess(self, *, autorun: bool = True) -> None:
        if self.mode is None:
            raise RuntimeError("_push_assess called with no mode selected")
        if self.assess_dir is None:
            ts = rundir.default_ts()
            self.assess_dir = self.repo_root / "artifacts" / f"assess_{ts}"
        # AssessScreen sets its own sub_title ("assess · <mode>") on mount.
        self.push_screen(
            AssessScreen(
                self.mode,
                self.repo_root,
                self.assess_dir,
                self.draft,
                staged_phase=self.staged_phase,
                autorun=autorun,
            ),
            self._on_assess_done,
        )

    def _on_assess_done(self, proceeded: bool | None) -> None:
        if proceeded:
            self._push_plan()
            return
        # AssessScreen's "escape" -> go_back -> dismiss(False): back to Configure
        # so the operator can fix a credential typo etc. without losing the draft.
        self._push_configure()

    # -- Plan ------------------------------------------------------------------

    def _push_plan(self) -> None:
        if self.mode is None:
            raise RuntimeError("_push_plan called with no mode selected")
        if self.plan_dir is None:
            ts = rundir.default_ts()
            self.plan_dir = self.repo_root / "artifacts" / f"plan_{ts}"
        self.sub_title = f"plan · {self.mode.key}"
        self.push_screen(
            PlanScreen(
                self.mode,
                self.repo_root,
                self.plan_dir,
                self.draft,
                _draft_env(self.draft),
                self.step_map,
                staged_phase=self.staged_phase,
            ),
            self._on_plan_done,
        )

    def _on_plan_done(self, proceed: bool | None) -> None:
        if not proceed:
            # Back to AssessScreen -- re-push against the same assess_dir
            # (already-written report.json), matching "AssessScreen -> back
            # to AssessScreen" in §5.10 (PlanScreen -> dismiss(None) ->
            # AssessScreen). autorun=False: the report.json from the earlier
            # run is still on disk, so just re-render it instead of
            # re-invoking migrationctl assess.
            self._push_assess(autorun=False)
            return
        if self.phase_mode == "assess_plan":
            self._push_summary_after_plan()
        else:
            # phase_mode == "all": the real edge here is RunScreen, which is
            # Phase 2 scope and not built in this workflow run -- see
            # _RunPhaseStubScreen's own docstring.
            self._push_run_stub(_RUN_STUB_MESSAGE)

    # -- Summary (assess_plan terminal state; no run ever happens) -----------

    def _push_summary_after_plan(self) -> None:
        if self.mode is None:
            raise RuntimeError("_push_summary_after_plan called with no mode selected")
        report_data: dict = {}
        if self.plan_dir is not None:
            report_path = self.plan_dir / "report.json"
            if report_path.is_file():
                report_data = _read_json(report_path)
        self.sub_title = f"summary · {self.mode.key}"
        self.push_screen(
            SummaryScreen(
                success=True,
                mode=self.mode.key,
                staged_phase=self.staged_phase,
                run_dir=self.plan_dir or self.repo_root,
                report_data=report_data,
                plan_only=True,
                assess_dir=self.assess_dir,
            ),
            self._on_summary_done,
        )

    def _on_summary_done(self, _result: None) -> None:
        self.exit()

    # -- Phase 2 stub (RunScreen placeholder) --------------------------------

    def _push_run_stub(self, message: str) -> None:
        self.sub_title = "run · not implemented"
        self.push_screen(_RunPhaseStubScreen(message), self._on_run_stub_done)

    def _on_run_stub_done(self, _result: None) -> None:
        self.exit()
