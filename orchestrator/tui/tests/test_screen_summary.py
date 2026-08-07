"""Pilot tests for orchestrator.tui.screens.summary.SummaryScreen.

Design doc §4.9. Covers the four ``nextsteps_markdown`` branches (both at the
function level and via a couple of full-screen pilot checks), the steps
table's status icons, and the "l"/"q" bindings.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from orchestrator.tui.screens.log_view import LogScreen
from orchestrator.tui.screens.summary import (
    SummaryScreen,
    _verdict_text,
    nextsteps_markdown,
)


def _report(steps=None, target=None, source=None):
    return {
        "steps": steps or [],
        "target": target or {},
        "source": source or {},
    }


class _SummaryScreenApp(App):
    def __init__(self, **kwargs):
        super().__init__()
        self._kwargs = kwargs
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(SummaryScreen(**self._kwargs), self._set_result)

    def _set_result(self, result) -> None:
        self.result = result


# ---------------------------------------------------------------------------
# nextsteps_markdown / _verdict_text — direct function-level branch coverage
# ---------------------------------------------------------------------------


def test_verdict_text_failure():
    assert _verdict_text(False, None) == "MIGRATION FAILED"


def test_verdict_text_dump_only():
    assert _verdict_text(True, "dump_only") == "DUMP COMPLETE"


def test_verdict_text_load_only():
    assert _verdict_text(True, "load_only") == "LOAD COMPLETE"


def test_verdict_text_full_migration():
    assert _verdict_text(True, None) == "MIGRATION SUCCESSFUL"


def test_verdict_text_plan_only():
    assert _verdict_text(True, None, plan_only=True) == "PLAN COMPLETE"
    # plan_only wins over a staged_phase that happens to be set too.
    assert _verdict_text(True, "dump_only", plan_only=True) == "PLAN COMPLETE"


def test_nextsteps_markdown_failure_branch(tmp_path):
    body = nextsteps_markdown(
        success=False,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    assert "What to do next:" in body
    assert str(tmp_path / "run.log") in body


def test_nextsteps_markdown_dump_only_branch(tmp_path):
    body = nextsteps_markdown(
        success=True,
        mode="staged",
        staged_phase="dump_only",
        run_dir=tmp_path,
        report_data=_report(),
    )
    assert "NOT** touched" in body
    assert str(tmp_path / "dumps") in body
    assert str(tmp_path / "dumps" / "manifest.txt") in body


def test_nextsteps_markdown_load_only_branch(tmp_path):
    body = nextsteps_markdown(
        success=True,
        mode="staged",
        staged_phase="load_only",
        run_dir=tmp_path,
        report_data=_report(source={"staged_dump_dir": "/dumps/foo"}),
    )
    assert "validate the loaded data" in body
    assert "/dumps/foo/manifest.txt" in body


def test_nextsteps_markdown_full_migration_branch(tmp_path):
    body = nextsteps_markdown(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(target={"host": "tgt1", "port": "3306", "user": "admin"}),
    )
    assert "Row-count parity check" in body
    assert "mariadb -h tgt1 -P 3306 -u admin" in body


def test_nextsteps_markdown_plan_only_branch(tmp_path):
    assess_dir = tmp_path / "assess"
    plan_dir = tmp_path / "plan"
    body = nextsteps_markdown(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=plan_dir,
        report_data=_report(),
        plan_only=True,
        assess_dir=assess_dir,
    )
    assert "No data was moved" in body
    assert str(assess_dir / "report.json") in body
    assert str(plan_dir / "report.json") in body
    # Must not fall through to the dump_only or full_migration bodies.
    assert "scp -r" not in body
    assert "dumps" not in body
    assert "information_schema.tables" not in body


def test_nextsteps_markdown_plan_only_wins_over_staged_phase(tmp_path):
    # plan_only must short-circuit before the staged_phase branches are
    # even consulted, regardless of what staged_phase happens to be.
    body = nextsteps_markdown(
        success=True,
        mode="staged",
        staged_phase="dump_only",
        run_dir=tmp_path,
        report_data=_report(),
        plan_only=True,
        assess_dir=None,
    )
    assert "No data was moved" in body
    assert "scp -r" not in body


# ---------------------------------------------------------------------------
# Full-screen pilot tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verdict_and_body_render_for_full_migration_success(tmp_path):
    app = _SummaryScreenApp(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "MIGRATION SUCCESSFUL"


@pytest.mark.asyncio
async def test_verdict_and_body_render_for_plan_only(tmp_path):
    assess_dir = tmp_path / "assess"
    plan_dir = tmp_path / "plan"
    app = _SummaryScreenApp(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=plan_dir,
        report_data=_report(),
        assess_dir=assess_dir,
        plan_only=True,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "PLAN COMPLETE"
        from textual.widgets import Markdown

        nextsteps = pilot.app.screen.query_one("#nextsteps", Markdown)
        source = nextsteps.source or ""
        assert str(assess_dir / "report.json") in source
        assert str(plan_dir / "report.json") in source
        assert "scp -r" not in source
        assert "information_schema.tables" not in source


@pytest.mark.asyncio
async def test_verdict_renders_for_failure(tmp_path):
    app = _SummaryScreenApp(
        success=False,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "MIGRATION FAILED"


@pytest.mark.asyncio
async def test_steps_table_reflects_report_data_with_status_icons(tmp_path):
    steps = [
        {"id": "preflight", "name": "Preflight checks", "status": "DONE"},
        {"id": "target_install", "name": "Install target", "status": "FAILED"},
        {"id": "staged_dump", "name": "Staged dump", "status": "SKIPPED"},
        {"id": "mystery_step", "name": "Mystery", "status": "WEIRD"},
    ]
    app = _SummaryScreenApp(
        success=False,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(steps=steps),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#steps", DataTable)
        assert table.row_count == 4
        assert table.get_row_at(0) == ["preflight", "Preflight checks", "✓ DONE"]
        assert table.get_row_at(1) == ["target_install", "Install target", "✗ FAILED"]
        assert table.get_row_at(2) == ["staged_dump", "Staged dump", "⊘ SKIPPED"]
        assert table.get_row_at(3) == ["mystery_step", "Mystery", "? WEIRD"]


@pytest.mark.asyncio
async def test_pressing_l_pushes_log_screen(tmp_path):
    app = _SummaryScreenApp(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(pilot.app.screen_stack) == 2
        await pilot.press("l")
        await pilot.pause()
        assert len(pilot.app.screen_stack) == 3
        assert isinstance(pilot.app.screen, LogScreen)
        assert pilot.app.screen.run_dir == tmp_path


@pytest.mark.asyncio
async def test_pressing_q_dismisses_with_none(tmp_path):
    app = _SummaryScreenApp(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert pilot.app.result is None
        assert len(pilot.app.screen_stack) == 1


@pytest.mark.asyncio
async def test_pressing_o_notifies_without_dismissing(tmp_path):
    app = _SummaryScreenApp(
        success=True,
        mode="one_step",
        staged_phase=None,
        run_dir=tmp_path,
        report_data=_report(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        notified = []
        pilot.app.screen.notify = lambda message, *a, **k: notified.append(message)
        await pilot.press("o")
        await pilot.pause()
        assert notified == [f"Run directory: {tmp_path}"]
        assert pilot.app.result == "unset"
        assert len(pilot.app.screen_stack) == 2
