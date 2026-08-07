"""RunScreen(Screen[bool]) per design doc §4.7.

Thin consumer of `orchestrator.tui.runsession.RunSession`/`RunView` and the
five `Message` subclasses that module defines (`StepStarted`, `StepOutput`,
`StepProgress`, `StepFinished`, `RunFinished`). This screen owns no run-loop
logic of its own -- it constructs one `RunSession` (cheap, side-effect-light
per that module's own docstring) with itself as `sink`, drives it via a
single `@work` worker, and renders whatever `RunView` snapshot the session
hands back after each message.

No `p` pause binding. Dropped per design doc §10.3 decision 2: there is no
pause primitive in `run_step_async`, and stopping a
`mariadb-dump | mariadb` pipe mid-stream is not safe. This is a deliberate
omission, not a gap -- do not add one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Log, ProgressBar, Static

from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.models import ModeInfo, RunView, StepSpec
from orchestrator.tui.runsession import (
    RunFinished,
    RunSession,
    StepFinished,
    StepOutput,
    StepProgress,
    StepStarted,
)
from orchestrator.tui.screens.log_view import LogScreen
from orchestrator.tui.widgets.step_list import StepList
from orchestrator.tui.widgets.throughput_readout import ThroughputReadout

_ABORT_QUESTION = "Abort the run? The current step will be terminated."


class RunScreen(Screen[bool]):
    """Layout per design doc §4.7:

        Vertical#body
        +- Label#stepper                  "Running · step 4 / 6"
        +- Horizontal#top
        |  +- StepList#steps              (§5.7)
        |  +- Vertical#meters
        |     +- ThroughputReadout#tput   (§5.9)
        |     +- Static#current           current step name + script
        +- ProgressBar#overall            steps-completed based; see §8.1
        +- Log#out                        streaming output, max_lines=5000

    `Static#error` is not in the mockup's tree -- it exists purely to
    surface an unhandled worker exception (see `_run_worker`'s
    `exit_on_error=False`) without dying, per design doc §7.5.
    """

    BINDINGS = [
        Binding("l", "show_log", "full log", show=True),
        Binding("ctrl+c", "abort", "abort", show=True, priority=True),
    ]

    def __init__(
        self,
        repo_root: Path,
        run_dir: Path,
        mode: ModeInfo,
        steps: Sequence[StepSpec],
        env: Mapping[str, str],
        skips: frozenset[str],
        *,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.mode = mode
        self.env = dict(env)
        self.skips = frozenset(skips)
        # RunSession's own construction is cheap and side-effect-light
        # (StateStore/__post_init__ only creates run_dir + a fresh
        # state.json if neither already exists; Report touches no disk
        # until start_run()) -- safe to build here in __init__ rather than
        # deferring to on_mount. `self` is a valid `sink` from the moment
        # this object exists: MessagePump.post_message needs no mount.
        self.session = RunSession(repo_root, run_dir, mode, steps, dict(env), frozenset(skips), sink=self)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Label("", id="stepper")
            with Horizontal(id="top"):
                yield StepList(id="steps")
                with Vertical(id="meters"):
                    yield ThroughputReadout(id="tput")
                    yield Static("", id="current")
            yield ProgressBar(id="overall", show_eta=False)
            yield Log(id="out", max_lines=5000)
            yield Static("", id="error")
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = f"run · {self.mode.key}"
        await self._render_view(self.session.snapshot())
        self._run_worker()

    # -- rendering ----------------------------------------------------------

    async def _render_view(self, view: RunView) -> None:
        # StepList is the *only* consumer of step status (design doc §4.7)
        # -- it never reads state.json directly, it only ever sees whatever
        # RunView snapshot this method was handed. update_rows is async
        # (DOM removal is a posted message, not synchronous) -- must be
        # awaited, per that widget's own comment.
        await self.query_one("#steps", StepList).update_rows(view.rows)
        self._update_stepper(view)
        self._update_progress(view)
        self._update_current(view)

    def _update_stepper(self, view: RunView) -> None:
        if view.finished:
            label = f"Finished · {view.completed} / {view.total} steps"
        elif view.current_index is not None:
            label = f"Running · step {view.current_index + 1} / {view.total}"
        else:
            label = f"Running · {view.completed} / {view.total} steps done"
        self.query_one("#stepper", Label).update(label)

    def _update_progress(self, view: RunView) -> None:
        bar = self.query_one("#overall", ProgressBar)
        bar.update(total=view.total or None, progress=view.completed)

    def _update_current(self, view: RunView) -> None:
        text = ""
        if view.current_index is not None and 0 <= view.current_index < len(view.rows):
            spec = view.rows[view.current_index].spec
            text = f"{spec.name}\n{spec.script}"
        self.query_one("#current", Static).update(text)

    # -- RunSession message handlers -----------------------------------------
    #
    # Textual dispatches each of these by the message class's own (unnested)
    # name -- StepStarted -> on_step_started, etc. (verified against
    # StepStarted.handler_name et al.) `RunSession` posts these to `self`
    # (it is this screen's `sink`), so there is no bubbling concern here;
    # `.stop()` is defensive against MigrationApp ever gaining a same-named
    # handler of its own.

    async def on_step_started(self, message: StepStarted) -> None:
        message.stop()
        # A stale sample from the *previous* step must not linger and be
        # mistaken for this step's progress (ThroughputReadout's own
        # docstring on `reset()`).
        self.query_one("#tput", ThroughputReadout).reset()
        await self._render_view(self.session.snapshot())

    def on_step_output(self, message: StepOutput) -> None:
        message.stop()
        # RunSession already batches on a 100ms tick (design doc §7.5) --
        # one Log.write_lines call per StepOutput message, no re-batching
        # or throttling here.
        self.query_one("#out", Log).write_lines(list(message.lines))

    def on_step_progress(self, message: StepProgress) -> None:
        message.stop()
        self.query_one("#tput", ThroughputReadout).update_sample(message.sample)

    async def on_step_finished(self, message: StepFinished) -> None:
        message.stop()
        await self._render_view(self.session.snapshot())

    async def on_run_finished(self, message: RunFinished) -> None:
        message.stop()
        await self._render_view(self.session.snapshot())
        # dismiss(True) on success; dismiss(False) on failure/abort. Per
        # design doc §5.10, mode.key in {"binlog", "replace_slave"} should
        # instead land on ReplicationWatchScreen -- that screen is Phase 4
        # scope (design doc §10.4's build-order table; not built here), so
        # for now EVERY mode -- including binlog/replace_slave -- dismisses
        # straight through to SummaryScreen, same as every other mode. This
        # is a deliberate, temporary parity gap, not a permanent decision;
        # app.py's caller routes the dismissed bool to SummaryScreen either
        # way. Revisit when ReplicationWatchScreen lands.
        self.dismiss(message.success)

    # -- worker ---------------------------------------------------------------

    @work(exclusive=True, group="run", exit_on_error=False, description="migration run")
    async def _run_worker(self) -> None:
        # exit_on_error=False is load-bearing (design doc §7.5): the
        # Textual default on an unhandled worker exception is to exit the
        # whole app, and exiting mid-migration with a live subprocess still
        # running is the worst failure mode here. RunSession.drive()'s own
        # finally/cancellation path (not this except block) does the real
        # process teardown either way.
        try:
            await self.session.drive()
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, see above
            self._show_error(str(exc))

    def _show_error(self, text: str) -> None:
        self.query_one("#error", Static).update(f"Run worker error: {text}")
        self.notify(f"Run worker error: {text}", severity="error", timeout=10)

    # -- bindings ---------------------------------------------------------------

    def action_show_log(self) -> None:
        self.app.push_screen(LogScreen(self.run_dir, log_filename="run.log"))

    def action_abort(self) -> None:
        # run_worker (not a bare coroutine call) so push_screen_wait below
        # has a worker context to await inside (same fix as
        # PlanScreen.action_proceed/ConfigureScreen.action_quick_save for
        # the same push_screen_wait requirement). exclusive + a dedicated
        # group means a second fast ctrl+c can't stack a second ConfirmModal.
        self.run_worker(self._abort_flow(), exclusive=True, group="abort_confirm")

    async def _abort_flow(self) -> None:
        confirmed = await self.app.push_screen_wait(
            ConfirmModal(_ABORT_QUESTION, danger=True)
        )
        if confirmed:
            # Cancel the "run" worker group -- RunSession.drive()'s own
            # `except asyncio.CancelledError` handler (_handle_abort) does
            # the actual state.json/report.json/process teardown; this
            # screen does not (and must not) touch that directly.
            self.workers.cancel_group(self, "run")
