"""Pilot tests for orchestrator.tui.modals.offline_ack.OfflineAckModal."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.modals.offline_ack import OfflineAckModal


class _OfflineAckApp(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(OfflineAckModal(), self._set_result)

    def _set_result(self, result: bool) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_question_text():
    app = _OfflineAckApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert str(modal.query_one("#question").content) == (
            "Have you stopped writes to the source?"
        )


@pytest.mark.asyncio
async def test_body_contains_distinctive_line():
    app = _OfflineAckApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        body_text = str(modal.query_one("#body").content)
        assert "IMPORTANT: Offline Copy IS AN OFFLINE MIGRATION" in body_text
        assert "Resume traffic on the TARGET only after the load completes." in body_text


@pytest.mark.asyncio
async def test_default_is_false():
    app = _OfflineAckApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        assert modal.default is False
