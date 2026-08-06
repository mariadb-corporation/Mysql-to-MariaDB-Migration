"""Pilot tests for orchestrator.tui.screens.staged_phase.StagedPhaseScreen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import OptionList

from orchestrator.tui.modals.offline_ack import OfflineAckModal
from orchestrator.tui.modes import expected_skips
from orchestrator.tui.screens.staged_phase import StagedPhaseScreen


class _StubModeSelectScreen(Screen):
    """Stands in for the real ModeSelectScreen the nav graph pops back to."""

    def compose(self) -> ComposeResult:
        return
        yield


class _StagedPhaseApp(App):
    def __init__(self, env=None):
        super().__init__()
        self._env = env
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(_StubModeSelectScreen())
        self.push_screen(StagedPhaseScreen(env=self._env), self._set_result)

    def _set_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_phases_option_list_has_three_in_order():
    app = _StagedPhaseApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        assert phases.option_count == 3
        assert [o.id for o in phases.options] == [
            "dump_and_load",
            "dump_only",
            "load_only",
        ]


@pytest.mark.asyncio
async def test_effect_updates_when_highlight_changes_to_dump_only():
    expected_skips_value = expected_skips("staged", {"STAGED_PHASE": "dump_only"})
    app = _StagedPhaseApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.highlighted = 1  # dump_only
        await pilot.pause()
        effect = pilot.app.screen.query_one("#effect")
        expected_text = f"will skip: {', '.join(sorted(expected_skips_value))}"
        assert str(effect.content) == expected_text


@pytest.mark.asyncio
async def test_effect_shows_default_dump_and_load_skips_on_mount():
    expected_skips_value = expected_skips("staged", {"STAGED_PHASE": "dump_and_load"})
    app = _StagedPhaseApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        effect = pilot.app.screen.query_one("#effect")
        expected_text = f"will skip: {', '.join(sorted(expected_skips_value))}"
        assert str(effect.content) == expected_text


@pytest.mark.asyncio
async def test_selecting_load_only_dismisses_directly_no_modal():
    app = _StagedPhaseApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.focus()
        phases.highlighted = 2  # load_only
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result == "load_only"
        assert len(pilot.app.screen_stack) == 2


@pytest.mark.asyncio
async def test_selecting_dump_and_load_pushes_offline_ack_when_env_unset():
    app = _StagedPhaseApp(env={})
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.focus()
        phases.highlighted = 0  # dump_and_load
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert len(pilot.app.screen_stack) == 4
        assert isinstance(pilot.app.screen, OfflineAckModal)


@pytest.mark.asyncio
async def test_selecting_dump_and_load_dismisses_directly_when_env_set():
    app = _StagedPhaseApp(env={"STAGED_CONFIRM_OFFLINE": "1"})
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.focus()
        phases.highlighted = 0  # dump_and_load
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result == "dump_and_load"
        assert len(pilot.app.screen_stack) == 2


@pytest.mark.asyncio
async def test_confirming_offline_ack_dismisses_with_phase():
    app = _StagedPhaseApp(env={})
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.focus()
        phases.highlighted = 1  # dump_only
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.result == "dump_only"


@pytest.mark.asyncio
async def test_declining_offline_ack_pops_back_past_staged_phase():
    # Nav graph §5.10: OfflineAckModal --No--> ModeSelectScreen, i.e. this
    # screen is popped too, not just the modal.
    app = _StagedPhaseApp(env={})
    async with app.run_test() as pilot:
        await pilot.pause()
        phases = pilot.app.screen.query_one("#phases", OptionList)
        phases.focus()
        phases.highlighted = 0  # dump_and_load
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.result == "unset"
        assert isinstance(pilot.app.screen, _StubModeSelectScreen)
        assert len(pilot.app.screen_stack) == 2


@pytest.mark.asyncio
async def test_go_back_dismisses_with_none():
    # App.pop_screen() drops the registered result callback entirely, so a
    # future push_screen_wait(StagedPhaseScreen()) caller would hang forever
    # on back-navigation unless this goes through dismiss(None) instead.
    app = _StagedPhaseApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is None
