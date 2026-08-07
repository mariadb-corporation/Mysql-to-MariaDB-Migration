"""DemoScreen(Screen[None]) -- side-door capability demo, not a real
migration feature. See ``tui-phase4-demo-progress-screen.plan.md``.

Reachable only from ``WelcomeScreen``'s "demo" option
(``app.py._on_welcome_done``) -- not part of the production nav graph
(design doc §5.10). Reuses ``StepList``/``ThroughputReadout`` (the same
widgets ``RunScreen`` uses for a real run) fed entirely by
``orchestrator.tui.demo_data.next_frame``, a pure function of an integer
tick with no I/O, no ``ConfigDraft``/subprocess/network access at all --
this screen must never be able to do anything real, by construction.
"""

from __future__ import annotations

from collections import deque

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical
from textual.screen import Screen
from textual.widgets import Digits, Footer, Header, Log, Sparkline, Static

from orchestrator.tui.demo_data import DemoFrame, next_frame
from orchestrator.tui.widgets.step_list import StepList
from orchestrator.tui.widgets.throughput_readout import ThroughputReadout

_TICK_INTERVAL_S = 0.5
_HISTORY_MAXLEN = 60

_BANNER_TEXT = "DEMO ONLY -- synthetic data, no live connection, no real migration"


# Panel id -> border_title, in on_mount()'s set order. A plain dict literal
# rather than a loop over compose()'s own yields: Textual panels get their
# border_title assigned after mount, not at construction time, so this is
# the one place the id<->title pairing needs to be spelled out.
_PANEL_TITLES: dict[str, str] = {
    "panel-rowsec": "Rows/sec",
    "panel-throughput": "Throughput",
    "panel-lag": "Repl. lag (synthetic)",
    "panel-tables": "Tables",
    "panel-elapsed": "Elapsed",
    "panel-steps": "Steps",
    "panel-log": "Live log (demo)",
}


class DemoScreen(Screen[None]):
    """Dolphie-style multi-panel dashboard (a MySQL/MariaDB monitoring TUI
    that lays live metrics out as a grid of bordered, titled panels rather
    than one long vertical scroll) -- deliberately no logo/banner art here,
    since the panels themselves are the whole point; ``#demo-banner`` is a
    safety marker, not decoration, and stays.

        Header
        Static#demo-banner                 persistent "DEMO ONLY" marker
        Static#status-pill                 "* RUNNING" / "* DONE -- press r to replay"
        Grid#stats-row (5 columns x 1 row)
        +- Vertical#panel-rowsec           "Rows/sec"
        |  +- Digits#rowsec_value
        |  +- Sparkline#rowsec_trend
        +- Vertical#panel-throughput       "Throughput"
        |  +- ThroughputReadout#tput
        |  +- Sparkline#tput_trend
        +- Vertical#panel-lag              "Repl. lag (synthetic)"
        |  +- Digits#lag_value
        |  +- Sparkline#lag_trend
        +- Vertical#panel-tables           "Tables"
        |  +- Digits#tables_value
        +- Vertical#panel-elapsed          "Elapsed"
        |  +- Digits#elapsed_value
        Grid#main-row (2 columns x 1 row)
        +- Vertical#panel-steps            "Steps"
        |  +- StepList#steps
        +- Vertical#panel-log              "Live log (demo)"
        |  +- Log#out                      canned lines, texture only
        Footer

    The demo runs once through ``DEMO_STEPS`` and then holds at "DONE" (it
    no longer loops); pressing "r" (rerun demo) resets it back to tick 0.
    """

    BINDINGS = [
        Binding("escape", "go_back", "back", show=True),
        Binding("q", "go_back", "back", show=True),
        Binding("r", "rerun_demo", "rerun demo", show=True),
    ]

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._tick_count = 0
        self._rowsec_history: deque[float] = deque(maxlen=_HISTORY_MAXLEN)
        self._lag_history: deque[float] = deque(maxlen=_HISTORY_MAXLEN)
        self._tput_history: deque[float] = deque(maxlen=_HISTORY_MAXLEN)
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(_BANNER_TEXT, id="demo-banner")
        yield Static("", id="status-pill")
        with Grid(id="stats-row"):
            with Vertical(id="panel-rowsec", classes="panel stat-panel"):
                yield Digits("0", id="rowsec_value")
                yield Sparkline([], id="rowsec_trend", summary_function=max)
            with Vertical(id="panel-throughput", classes="panel stat-panel"):
                yield ThroughputReadout(id="tput")
                yield Sparkline([], id="tput_trend", summary_function=max)
            with Vertical(id="panel-lag", classes="panel stat-panel"):
                yield Digits("0", id="lag_value")
                yield Sparkline([], id="lag_trend", summary_function=max)
            with Vertical(id="panel-tables", classes="panel stat-panel"):
                yield Digits("0/0", id="tables_value")
            with Vertical(id="panel-elapsed", classes="panel stat-panel"):
                yield Digits("00:00", id="elapsed_value")
        with Grid(id="main-row"):
            with Vertical(id="panel-steps", classes="panel"):
                yield StepList(id="steps")
            with Vertical(id="panel-log", classes="panel"):
                yield Log(id="out", max_lines=200)
        yield Footer()

    async def on_mount(self) -> None:
        self.sub_title = "demo"
        for panel_id, title in _PANEL_TITLES.items():
            self.query_one(f"#{panel_id}").border_title = title
        await self._render_frame(next_frame(0))
        self._timer = self.set_interval(_TICK_INTERVAL_S, self._tick)

    async def _tick(self) -> None:
        self._tick_count += 1
        await self._render_frame(next_frame(self._tick_count))

    async def _render_frame(self, frame: DemoFrame) -> None:
        await self.query_one("#steps", StepList).update_rows(frame.rows)
        self._update_status_pill(frame)
        self.query_one("#tput", ThroughputReadout).update_sample(frame.sample)
        self._update_rowsec(frame)
        self._update_lag(frame.seconds_behind)
        self._update_tables(frame)
        self._update_elapsed(frame)
        self._update_throughput_trend(frame)
        if frame.log_line is not None:
            self.query_one("#out", Log).write_line(frame.log_line)
        if frame.status == "DONE" and self._timer is not None:
            # Hold at the finished frame instead of continuing to tick a
            # dead run -- action_rerun_demo is what restarts the timer.
            self._timer.stop()
            self._timer = None

    def _update_status_pill(self, frame: DemoFrame) -> None:
        pill = self.query_one("#status-pill", Static)
        if frame.status == "RUNNING":
            pill.set_classes("running")
            pill.update("● RUNNING")
        else:
            pill.set_classes("done")
            pill.update("● DONE -- press r to replay")

    def _update_rowsec(self, frame: DemoFrame) -> None:
        self.query_one("#rowsec_value", Digits).update(f"{frame.rows_per_sec:,}")
        self._rowsec_history.append(float(frame.rows_per_sec))
        self.query_one("#rowsec_trend", Sparkline).data = list(self._rowsec_history)

    def _update_lag(self, seconds_behind: int) -> None:
        self.query_one("#lag_value", Digits).update(str(seconds_behind))
        self._lag_history.append(float(seconds_behind))
        self.query_one("#lag_trend", Sparkline).data = list(self._lag_history)

    def _update_tables(self, frame: DemoFrame) -> None:
        # No spaces around the "/" -- Digits' 3-cols-per-digit font plus
        # "22 / 22"'s two padding spaces (19 cols) overflows a 5-column
        # card's ~17-19 usable width; "22/22" (13 cols) fits.
        self.query_one("#tables_value", Digits).update(
            f"{frame.tables_done}/{frame.tables_total}"
        )

    def _update_elapsed(self, frame: DemoFrame) -> None:
        # MM:SS, not HH:MM:SS -- the whole demo runs 20s
        # (TICKS_PER_STEP * len(DEMO_STEPS) * _TICK_INTERVAL_S), and
        # "00:00:00" (24 cols in Digits' font) overflows this card's
        # ~17-19 usable width; "00:00" (15 cols) fits.
        total_seconds = round(frame.elapsed_ticks * _TICK_INTERVAL_S)
        minutes, seconds = divmod(total_seconds, 60)
        self.query_one("#elapsed_value", Digits).update(f"{minutes:02d}:{seconds:02d}")

    def _update_throughput_trend(self, frame: DemoFrame) -> None:
        rate = frame.sample.rate_bytes_s if frame.sample is not None else 0.0
        self._tput_history.append(float(rate or 0.0))
        self.query_one("#tput_trend", Sparkline).data = list(self._tput_history)

    async def action_rerun_demo(self) -> None:
        self._tick_count = 0
        self._rowsec_history.clear()
        self._lag_history.clear()
        self._tput_history.clear()
        self.query_one("#out", Log).clear()
        if self._timer is None:
            self._timer = self.set_interval(_TICK_INTERVAL_S, self._tick)
        await self._render_frame(next_frame(0))

    def action_go_back(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.dismiss(None)
