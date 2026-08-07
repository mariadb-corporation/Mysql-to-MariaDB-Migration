"""Pilot tests for orchestrator.tui.screens.welcome.WelcomeScreen."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static

from orchestrator.tui import rundir
from orchestrator.tui.modals.resume import ResumeChoiceModal
from orchestrator.tui.screens.welcome import WelcomeScreen


class _WelcomeApp(App):
    def __init__(self, repo_root: Path):
        super().__init__()
        self._repo_root = repo_root
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(
            WelcomeScreen(repo_root=self._repo_root), self._set_result
        )

    def _set_result(self, result) -> None:
        self.result = result


def _make_resumable_run(repo_root: Path, *, mode: str = "binlog") -> None:
    run_dir = repo_root / "artifacts" / f"run_{mode}_20260101_120000"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text("{}")
    rundir.write_pointers(
        repo_root, Path("artifacts") / f"run_{mode}_20260101_120000", "MODE=binlog"
    )


@pytest.mark.asyncio
async def test_banner_shows_product_name_and_version(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = pilot.app.screen.query_one("#banner", Static)
        text = str(banner.content)
        assert "MySQL to MariaDB Migration Tool" in text
        assert "1.4.0-beta" in text


@pytest.mark.asyncio
async def test_actions_option_list_has_three_in_order(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        actions = pilot.app.screen.query_one("#actions", OptionList)
        assert actions.option_count == 3
        assert [o.id for o in actions.options] == ["assess_plan", "all", "quit"]


@pytest.mark.asyncio
async def test_selecting_assess_plan_dismisses_with_that_value(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        actions = pilot.app.screen.query_one("#actions", OptionList)
        actions.focus()
        actions.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result == "assess_plan"


@pytest.mark.asyncio
async def test_selecting_all_dismisses_with_that_value(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        actions = pilot.app.screen.query_one("#actions", OptionList)
        actions.focus()
        actions.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result == "all"


@pytest.mark.asyncio
async def test_pressing_q_dismisses_with_quit(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert pilot.app.result == "quit"


@pytest.mark.asyncio
async def test_no_candidate_no_hint_no_modal(tmp_path):
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        hint = pilot.app.screen.query_one("#hint", Static)
        assert str(hint.content) == ""
        assert len(pilot.app.screen_stack) == 2  # default + WelcomeScreen


@pytest.mark.asyncio
async def test_candidate_shows_hint_and_pushes_resume_modal(tmp_path):
    _make_resumable_run(tmp_path)
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # ResumeChoiceModal is pushed on top of WelcomeScreen, so the
        # underlying WelcomeScreen (which owns #hint) is one below the top
        # of the stack.
        welcome_screen = pilot.app.screen_stack[-2]
        hint = welcome_screen.query_one("#hint", Static)
        assert "resumable run" in str(hint.content)
        assert isinstance(pilot.app.screen, ResumeChoiceModal)


@pytest.mark.asyncio
async def test_choosing_resume_dismisses_welcome_with_resume(tmp_path):
    # binlog is one of rundir.UNCONDITIONAL_RESUME_MODES, so Resume is
    # enabled without needing a matching current_sig.
    _make_resumable_run(tmp_path, mode="binlog")
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, ResumeChoiceModal)
        await pilot.click("#resume")
        await pilot.pause()
        assert pilot.app.result == "resume"


@pytest.mark.asyncio
async def test_choosing_start_fresh_leaves_action_list_actionable(tmp_path):
    _make_resumable_run(tmp_path, mode="binlog")
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#fresh")
        await pilot.pause()
        assert pilot.app.result == "unset"
        assert isinstance(pilot.app.screen, WelcomeScreen)
        actions = pilot.app.screen.query_one("#actions", OptionList)
        actions.focus()
        actions.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.result == "assess_plan"


@pytest.mark.asyncio
async def test_choosing_cancel_leaves_action_list_actionable(tmp_path):
    _make_resumable_run(tmp_path, mode="binlog")
    app = _WelcomeApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause()
        assert pilot.app.result == "unset"
        assert isinstance(pilot.app.screen, WelcomeScreen)
