"""Pilot tests for orchestrator.tui.modals.save_secrets.SaveSecretsModal."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.configio import SECRET_KEYS
from orchestrator.tui.modals.save_secrets import SaveSecretsModal


class _SaveSecretsApp(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(SaveSecretsModal(), self._set_result)

    def _set_result(self, result: bool) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_question_text():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert str(modal.query_one("#question").content) == (
            "Include passwords/secrets in saved config?"
        )


@pytest.mark.asyncio
async def test_body_names_a_real_secret_key_not_a_made_up_one():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        body_text = str(modal.query_one("#body").content)
        assert "SRC_PASS" in body_text
        assert "NOT_A_REAL_KEY" not in body_text
        for key in SECRET_KEYS:
            assert key in body_text


@pytest.mark.asyncio
async def test_default_is_false():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert modal.default is False


@pytest.mark.asyncio
async def test_yes_button_dismisses_true():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_no_button_dismisses_false():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#no")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_y_key_dismisses_true():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_n_key_dismisses_false():
    app = _SaveSecretsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.result is False
