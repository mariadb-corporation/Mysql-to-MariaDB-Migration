"""Pilot tests for orchestrator.tui.screens.log_view.LogScreen.

Design doc §4.10. Uses real ``run.log`` files under ``tmp_path`` since
LogScreen does pure local file reads with no shelling out.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog

from orchestrator.tui.screens.log_view import _NO_LOG_PLACEHOLDER, LogScreen


class _LogScreenApp(App):
    def __init__(self, run_dir):
        super().__init__()
        self._run_dir = run_dir
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(LogScreen(self._run_dir), self._set_result)

    def _set_result(self, result) -> None:
        self.result = result


def _rendered_lines(pilot) -> list[str]:
    log = pilot.app.screen.query_one("#full", RichLog)
    return [strip.text for strip in log.lines]


@pytest.mark.asyncio
async def test_real_run_log_renders_all_lines(tmp_path):
    (tmp_path / "run.log").write_text(
        "==> step preflight starting\n"
        "preflight ok\n"
        "==> step target_install starting\n"
        "installing mariadb\n"
    )
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, LogScreen)
        assert screen._lines == [
            "==> step preflight starting",
            "preflight ok",
            "==> step target_install starting",
            "installing mariadb",
        ]
        assert _rendered_lines(pilot) == screen._lines


@pytest.mark.asyncio
async def test_missing_run_log_renders_placeholder_without_raising(tmp_path):
    # No run.log at all under tmp_path -- OSError on read_text is expected
    # and swallowed, not an error state.
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen._lines == []
        assert _rendered_lines(pilot) == [_NO_LOG_PLACEHOLDER]


@pytest.mark.asyncio
async def test_typing_filter_narrows_rendered_lines(tmp_path):
    (tmp_path / "run.log").write_text(
        "line one\n"
        "line two ERROR boom\n"
        "line three\n"
        "another ERROR here\n"
    )
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(_rendered_lines(pilot)) == 4

        filter_input = pilot.app.screen.query_one("#filter", Input)
        filter_input.focus()
        await pilot.pause()
        for ch in "ERROR":
            await pilot.press(ch)
        await pilot.pause()

        rendered = _rendered_lines(pilot)
        assert rendered == ["line two ERROR boom", "another ERROR here"]

        # Clearing the filter restores every line.
        for _ in "ERROR":
            await pilot.press("backspace")
        await pilot.pause()
        assert _rendered_lines(pilot) == [
            "line one",
            "line two ERROR boom",
            "line three",
            "another ERROR here",
        ]


@pytest.mark.asyncio
async def test_filter_with_no_matches_renders_nothing(tmp_path):
    (tmp_path / "run.log").write_text("alpha\nbeta\n")
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        filter_input = pilot.app.screen.query_one("#filter", Input)
        filter_input.focus()
        await pilot.pause()
        for ch in "zzz":
            await pilot.press(ch)
        await pilot.pause()
        assert _rendered_lines(pilot) == []


@pytest.mark.asyncio
async def test_top_and_bottom_bindings_do_not_raise(tmp_path):
    (tmp_path / "run.log").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        await pilot.press("G")
        await pilot.pause()
        # Reaching here without an exception is the assertion; screen is
        # still the LogScreen, nothing was dismissed by g/G.
        assert isinstance(pilot.app.screen, LogScreen)
        assert pilot.app.result == "unset"


@pytest.mark.asyncio
async def test_escape_dismisses_with_none(tmp_path):
    (tmp_path / "run.log").write_text("some log line\n")
    app = _LogScreenApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is None
        assert len(pilot.app.screen_stack) == 1
