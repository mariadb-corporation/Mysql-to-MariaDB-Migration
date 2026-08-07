"""Pilot tests for orchestrator.tui.screens.demo.DemoScreen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Digits, Log, Static

from orchestrator.tui.screens.demo import DemoScreen
from orchestrator.tui.widgets.step_list import StepList


class _DemoApp(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(DemoScreen(), self._set_result)

    def _set_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_demo_banner_is_visible_on_mount():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = pilot.app.screen.query_one("#demo-banner", Static)
        assert "DEMO ONLY" in str(banner.content)


@pytest.mark.asyncio
async def test_step_list_and_lag_gauge_populated_on_mount():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        rows = pilot.app.screen.query_one("#steps", StepList).query("StepRow")
        assert len(rows) == 5
        digits = pilot.app.screen.query_one("#lag_value", Digits)
        assert str(digits.value).strip() != ""


@pytest.mark.asyncio
async def test_timer_advances_state_over_multiple_ticks():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen._tick_count == 0
        # _TICK_INTERVAL_S is 0.5s; wait past several ticks.
        await pilot.pause(1.7)
        assert screen._tick_count >= 3


@pytest.mark.asyncio
async def test_escape_dismisses_and_stops_the_timer():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is None
        assert screen._timer is not None
        assert screen._timer._task is None


@pytest.mark.asyncio
async def test_q_also_dismisses():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert pilot.app.result is None


@pytest.mark.asyncio
async def test_log_receives_canned_lines():
    app = _DemoApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause(1.1)
        log = pilot.app.screen.query_one("#out", Log)
        assert log.line_count >= 1
