"""Pilot tests for orchestrator.tui.modals.confirm.ConfirmModal."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.modals.confirm import ConfirmModal


class _ConfirmApp(App):
    def __init__(self, **kwargs):
        super().__init__()
        self._kwargs = kwargs
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(ConfirmModal(**self._kwargs), self._set_result)

    def _set_result(self, result: bool) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_renders_question_and_body():
    app = _ConfirmApp(question="Proceed?", body="extra detail")
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert str(modal.query_one("#question").content) == "Proceed?"
        assert str(modal.query_one("#body").content) == "extra detail"


@pytest.mark.asyncio
async def test_y_dismisses_true():
    app = _ConfirmApp(question="Proceed?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_n_dismisses_false():
    app = _ConfirmApp(question="Proceed?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_escape_always_dismisses_false_regardless_of_default():
    app = _ConfirmApp(question="Proceed?", default=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_enter_dismisses_with_default_true():
    app = _ConfirmApp(question="Proceed?", default=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_enter_dismisses_with_default_false():
    app = _ConfirmApp(question="Proceed?", default=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_yes_button_click_dismisses_true():
    app = _ConfirmApp(question="Proceed?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_no_button_click_dismisses_false():
    app = _ConfirmApp(question="Proceed?")
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_danger_applies_danger_css_class():
    app = _ConfirmApp(question="Proceed?", danger=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        dialog = pilot.app.screen.query_one("#dialog")
        assert dialog.has_class("danger")


@pytest.mark.asyncio
async def test_non_danger_does_not_apply_danger_css_class():
    app = _ConfirmApp(question="Proceed?", danger=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        dialog = pilot.app.screen.query_one("#dialog")
        assert not dialog.has_class("danger")
