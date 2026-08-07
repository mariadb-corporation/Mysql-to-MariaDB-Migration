"""Pilot tests for orchestrator.tui.modals.resume.ResumeChoiceModal."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.modals.resume import ResumeChoiceModal
from orchestrator.tui.models import ResumeCandidate, ResumeDecision, ResumeVerdict


def _candidate() -> ResumeCandidate:
    return ResumeCandidate(
        raw_pointer="run_staged_20260101120000",
        run_dir=Path("/tmp/artifacts/run_staged_20260101120000"),
        dir_mode="staged",
        dir_ts="20260101120000",
        last_sig="abc123",
        state_mtime=0.0,
    )


class _ResumeApp(App):
    def __init__(self, decision: ResumeDecision):
        super().__init__()
        self._decision = decision
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(ResumeChoiceModal(self._decision), self._set_result)

    def _set_result(self, result: str) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_resume_enabled_dismisses_resume():
    decision = ResumeDecision(
        verdict=ResumeVerdict.RESUME_UNCONDITIONAL,
        candidate=_candidate(),
        message="",
        mode_mismatch=False,
    )
    app = _ResumeApp(decision)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        resume_button = modal.query_one("#resume")
        assert resume_button.disabled is False
        await pilot.click("#resume")
        await pilot.pause()
        assert pilot.app.result == "resume"


@pytest.mark.asyncio
async def test_resume_disabled_shows_message():
    decision = ResumeDecision(
        verdict=ResumeVerdict.FRESH_INPUTS_CHANGED,
        candidate=_candidate(),
        message="Inputs changed since the previous run; a fresh run is required.",
        mode_mismatch=False,
    )
    app = _ResumeApp(decision)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        resume_button = modal.query_one("#resume")
        assert resume_button.disabled is True
        reason = modal.query_one("#reason")
        assert str(reason.content) == decision.message


@pytest.mark.asyncio
async def test_cancel_dismisses_cancel():
    decision = ResumeDecision(
        verdict=ResumeVerdict.RESUME_UNCONDITIONAL,
        candidate=_candidate(),
        message="",
        mode_mismatch=False,
    )
    app = _ResumeApp(decision)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
        assert pilot.app.result == "cancel"


@pytest.mark.asyncio
async def test_start_fresh_dismisses_fresh():
    decision = ResumeDecision(
        verdict=ResumeVerdict.RESUME_UNCONDITIONAL,
        candidate=_candidate(),
        message="",
        mode_mismatch=False,
    )
    app = _ResumeApp(decision)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#fresh")
        await pilot.pause()
        assert pilot.app.result == "fresh"


@pytest.mark.asyncio
async def test_escape_dismisses_cancel():
    decision = ResumeDecision(
        verdict=ResumeVerdict.RESUME_UNCONDITIONAL,
        candidate=_candidate(),
        message="",
        mode_mismatch=False,
    )
    app = _ResumeApp(decision)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result == "cancel"
