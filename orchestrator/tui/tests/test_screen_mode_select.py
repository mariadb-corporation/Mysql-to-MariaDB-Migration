"""Pilot tests for orchestrator.tui.screens.mode_select.ModeSelectScreen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.modes import MODE_CATALOG
from orchestrator.tui.screens.mode_select import ModeSelectScreen


class _ModeSelectApp(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(ModeSelectScreen(), self._set_result)

    def _set_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_counter_text():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        counter = pilot.app.screen.query_one("#counter")
        assert str(counter.content) == "4 modes · 2 advanced"


@pytest.mark.asyncio
async def test_modes_option_list_has_four_in_catalog_order():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modes_list = pilot.app.screen.query_one("#modes", OptionList)
        assert modes_list.option_count == 4
        non_advanced = [m for m in MODE_CATALOG if not m.advanced]
        for i, option in enumerate(modes_list.options):
            assert option.id == non_advanced[i].key


@pytest.mark.asyncio
async def test_advanced_option_list_has_two():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        advanced_list = pilot.app.screen.query_one("#advanced_modes", OptionList)
        assert advanced_list.option_count == 2
        advanced = [m for m in MODE_CATALOG if m.advanced]
        for i, option in enumerate(advanced_list.options):
            assert option.id == advanced[i].key


@pytest.mark.asyncio
async def test_pressing_1_dismisses_with_first_mode_catalog_entry():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        non_advanced = [m for m in MODE_CATALOG if not m.advanced]
        assert pilot.app.result == non_advanced[0]


@pytest.mark.asyncio
async def test_selecting_advanced_option_pushes_confirm_modal():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        advanced_collapsible = pilot.app.screen.query_one("#advanced")
        advanced_collapsible.collapsed = False
        await pilot.pause()
        advanced_list = pilot.app.screen.query_one("#advanced_modes", OptionList)
        advanced_list.focus()
        advanced_list.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert len(pilot.app.screen_stack) == 3
        assert isinstance(pilot.app.screen, ConfirmModal)


@pytest.mark.asyncio
async def test_confirming_advanced_modal_dismisses_with_advanced_mode():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        advanced_collapsible = pilot.app.screen.query_one("#advanced")
        advanced_collapsible.collapsed = False
        await pilot.pause()
        advanced_list = pilot.app.screen.query_one("#advanced_modes", OptionList)
        advanced_list.focus()
        advanced_list.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        advanced = [m for m in MODE_CATALOG if m.advanced]
        assert pilot.app.result == advanced[0]


@pytest.mark.asyncio
async def test_declining_advanced_modal_stays_on_mode_select():
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        advanced_collapsible = pilot.app.screen.query_one("#advanced")
        advanced_collapsible.collapsed = False
        await pilot.pause()
        advanced_list = pilot.app.screen.query_one("#advanced_modes", OptionList)
        advanced_list.focus()
        advanced_list.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.result == "unset"
        assert isinstance(pilot.app.screen, ModeSelectScreen)


@pytest.mark.asyncio
async def test_go_back_dismisses_with_none():
    # App.pop_screen() drops the registered result callback entirely
    # (_pop_result_callback never invokes it) -- a future
    # push_screen_wait(ModeSelectScreen()) caller would hang forever on
    # back-navigation unless this goes through dismiss(None) instead.
    app = _ModeSelectApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        assert pilot.app.result is None
