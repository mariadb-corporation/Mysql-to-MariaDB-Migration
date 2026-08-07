"""Pilot tests for orchestrator.tui.screens.plan.PlanScreen."""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from orchestrator.tui.models import ConfigDraft, ModeInfo
from orchestrator.tui.screens import plan as plan_module
from orchestrator.tui.screens.plan import PlanScreen


def _mode(key: str = "one_step") -> ModeInfo:
    return ModeInfo(
        key=key,
        label="Serial Streaming Copy (mariadb-dump)",
        subtitle="mariadb-dump · offline · simplest path",
        badge="OFFLINE",
        advanced=False,
        doc_anchor=None,
    )


def _step_map() -> dict:
    return {
        "modes": {"one_step": ["phase_a"]},
        "phases": {
            "phase_a": [
                {"id": "step1", "name": "Step One", "script": "scripts/step1.sh"},
                {"id": "step2", "name": "Step Two", "script": "scripts/step2.sh"},
            ]
        },
    }


def _staged_step_map() -> dict:
    return {
        "modes": {"staged": ["phase_a"]},
        "phases": {
            "phase_a": [
                {"id": "staged_dump", "name": "Staged Dump", "script": "scripts/25_staged_dump.sh"},
                {"id": "staged_load", "name": "Staged Load", "script": "scripts/26_staged_load.sh"},
            ]
        },
    }


async def _noop_run_command_async(argv, *, cwd=None, env=None, timeout_s=300.0):
    return (0, "")


class _PlanScreenApp(App):
    def __init__(
        self,
        *,
        mode,
        repo_root,
        plan_dir,
        draft,
        env=None,
        step_map,
        staged_phase=None,
    ):
        super().__init__()
        self._mode = mode
        self._repo_root = repo_root
        self._plan_dir = plan_dir
        self._draft = draft
        self._env = env or {}
        self._step_map = step_map
        self._staged_phase = staged_phase
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(
            PlanScreen(
                self._mode,
                self._repo_root,
                self._plan_dir,
                self._draft,
                self._env,
                self._step_map,
                staged_phase=self._staged_phase,
            ),
            self._set_result,
        )

    def _set_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_local_steps_render_before_cli_resolves(tmp_path, monkeypatch):
    # Stub never returns, so _run_plan is stuck mid-flight -- the table must
    # already show the locally-resolved steps from step_map.
    async def _hang(argv, *, cwd=None, env=None, timeout_s=300.0):
        import asyncio

        await asyncio.sleep(10)
        return (0, "")

    monkeypatch.setattr(plan_module, "run_command_async", _hang)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#steps", DataTable)
        assert table.row_count == 2
        row = table.get_row("step1")
        assert row == ["1", "step1", "Step One", "scripts/step1.sh"]
        row2 = table.get_row("step2")
        assert row2 == ["2", "step2", "Step Two", "scripts/step2.sh"]


@pytest.mark.asyncio
async def test_successful_cli_run_loads_authoritative_steps_from_report(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plan_1"

    async def _write_report_and_succeed(argv, *, cwd=None, env=None, timeout_s=300.0):
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "report.json").write_text(
            json.dumps(
                {
                    "plan": {
                        "steps": [
                            {
                                "id": "step1",
                                "name": "Step One",
                                "script": "scripts/step1.sh",
                                "args": [],
                            },
                            {
                                "id": "step2",
                                "name": "Step Two",
                                "script": "scripts/step2.sh",
                                "args": [],
                            },
                            {
                                "id": "step3",
                                "name": "Step Three (from report)",
                                "script": "scripts/step3.sh",
                                "args": [],
                            },
                        ]
                    }
                }
            )
        )
        return (0, "")

    monkeypatch.setattr(plan_module, "run_command_async", _write_report_and_succeed)

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        table = pilot.app.screen.query_one("#steps", DataTable)
        assert table.row_count == 3
        row3 = table.get_row("step3")
        assert row3 == ["3", "step3", "Step Three (from report)", "scripts/step3.sh"]

        header = pilot.app.screen.query_one("#header", Static)
        assert "steps: 3" in str(header.render())


@pytest.mark.asyncio
async def test_failing_cli_run_shows_error_output(tmp_path, monkeypatch):
    plan_dir = tmp_path / "plan_1"

    async def _fail(argv, *, cwd=None, env=None, timeout_s=300.0):
        return (1, "BadParameter: TGT_HOST is required for mode one_step")

    monkeypatch.setattr(plan_module, "run_command_async", _fail)

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        error = pilot.app.screen.query_one("#error", Static)
        assert "TGT_HOST is required for mode one_step" in str(error.render())


@pytest.mark.asyncio
async def test_expected_skip_step_renders_with_skip_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "run_command_async", _noop_run_command_async)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode("staged"),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        env={"STAGED_PHASE": "load_only"},
        step_map=_staged_step_map(),
        staged_phase="load_only",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#steps", DataTable)
        row = table.get_row("staged_dump")
        script_cell = str(row[3])
        assert "⊘ will skip (STAGED_PHASE=load_only)" in script_cell

        row2 = table.get_row("staged_load")
        assert "will skip" not in str(row2[3])


@pytest.mark.asyncio
async def test_press_p_and_confirm_dismisses_true(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "run_command_async", _noop_run_command_async)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        # Extra pause: _run_plan's worker needs to finish (returncode == 0
        # branch) and set _plan_ok = True before "p" is pressed, or
        # action_proceed's guard will treat this as a no-op.
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        # ConfirmModal is now on top of the stack; confirm with "y".
        assert len(pilot.app.screen_stack) == 3
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_escape_dismisses_none(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "run_command_async", _noop_run_command_async)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is None


@pytest.mark.asyncio
async def test_proceed_is_noop_before_plan_ok(tmp_path, monkeypatch):
    # _run_plan's worker never returns, so _plan_ok stays None -- "p" must be
    # a no-op (bell, no worker/modal started), not a silent proceed.
    async def _hang(argv, *, cwd=None, env=None, timeout_s=300.0):
        await asyncio.sleep(10)
        return (0, "")

    monkeypatch.setattr(plan_module, "run_command_async", _hang)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen._plan_ok is None
        assert screen.check_action("proceed", ()) is None

        await pilot.press("p")
        await pilot.pause()
        # No ConfirmModal pushed, no dismiss.
        assert len(pilot.app.screen_stack) == 2
        assert pilot.app.result == "unset"


@pytest.mark.asyncio
async def test_double_press_p_only_produces_one_confirm_modal(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_module, "run_command_async", _noop_run_command_async)
    plan_dir = tmp_path / "plan_1"

    app = _PlanScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        plan_dir=plan_dir,
        draft=ConfigDraft(),
        step_map=_step_map(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        await pilot.press("p")
        await pilot.press("p")
        await pilot.pause()
        # Only one ConfirmModal should be on the stack, not two.
        assert len(pilot.app.screen_stack) == 3
        await pilot.press("y")
        await pilot.pause()
        assert pilot.app.result is True
        assert len(pilot.app.screen_stack) == 1
