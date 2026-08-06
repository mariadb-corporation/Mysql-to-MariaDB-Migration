"""Pilot tests for orchestrator.tui.modals.root_user.RootUserBlockModal."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.modals.root_user import RootUserBlockModal

_QUESTION = (
    "root user is not allowed for admin or migration users. Set "
    "ALLOW_ROOT_USERS=1 only if explicitly intended."
)


class _RootUserApp(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(RootUserBlockModal(), self._set_result)

    def _set_result(self, result: bool) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_exact_question_text():
    app = _RootUserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert str(modal.query_one("#question").content) == _QUESTION


@pytest.mark.asyncio
async def test_default_is_false():
    app = _RootUserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert modal.default is False


@pytest.mark.asyncio
async def test_button_labels():
    app = _RootUserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        yes_button = modal.query_one("#yes")
        no_button = modal.query_one("#no")
        assert str(yes_button.label) == "Override (sets ALLOW_ROOT_USERS=1)"
        assert str(no_button.label) == "Go back and change the user"


@pytest.mark.asyncio
async def test_override_button_dismisses_true():
    app = _RootUserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_go_back_button_dismisses_false():
    app = _RootUserApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()
        assert pilot.app.result is False
