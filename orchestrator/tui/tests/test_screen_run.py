"""Pilot tests for orchestrator.tui.screens.run.RunScreen.

Per Phase 2 process notes item 7, this module deliberately drives a REAL
``RunSession``/``run_step_async`` through a real (trivial, throwaway)
subprocess in every test below -- no test here monkeypatches the run loop
itself. The stub script + ``FakeMarker`` pattern mirrors
``test_runsession.py`` exactly (same shared argparse-driven script), so a
step's real completion can be proven via a marker file rather than trusted
blindly from mocked return values.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.models import ModeInfo, StepSpec, StepUiStatus
from orchestrator.tui.screens.run import RunScreen
from orchestrator.tui.widgets.step_list import StepRow

_MODE = ModeInfo("test_mode", "Test Mode", "subtitle", "OFFLINE", False, None)

_STUB_SCRIPT = '''#!{python}
import argparse
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("--exit-code", type=int, default=0)
p.add_argument("--sleep", type=float, default=0.0)
p.add_argument("--marker")
p.add_argument("--line", action="append", default=[])
args = p.parse_args()

for line in args.line:
    print(line)
    sys.stdout.flush()

if args.sleep:
    time.sleep(args.sleep)

if args.marker:
    with open(args.marker, "w") as f:
        f.write("ran")

sys.exit(args.exit_code)
'''


def _write_stub_script(repo_root: Path, rel_path: str = "scripts/stub.py") -> str:
    script_path = repo_root / rel_path
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_STUB_SCRIPT.format(python=sys.executable))
    return rel_path


def _step(step_id: str, script: str, *args: str) -> StepSpec:
    return StepSpec(
        id=step_id,
        name=step_id.replace("_", " ").title(),
        script=script,
        args=tuple(args),
    )


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _RunScreenApp(App):
    def __init__(self, *, repo_root, run_dir, mode, steps, env=None, skips=None):
        super().__init__()
        self._repo_root = repo_root
        self._run_dir = run_dir
        self._mode = mode
        self._steps = steps
        self._env = env or {}
        self._skips = frozenset(skips or ())
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(
            RunScreen(
                self._repo_root,
                self._run_dir,
                self._mode,
                self._steps,
                self._env,
                self._skips,
            ),
            self._set_result,
        )

    def _set_result(self, result) -> None:
        self.result = result


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    """Poll `predicate()` on a real wall clock until it is truthy or `timeout`
    elapses. Used instead of a fixed number of `pilot.pause()` calls because
    these tests drive real subprocesses -- their completion timing is not
    deterministic the way a fully-mocked worker's would be."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(interval)


def _step_rows(pilot) -> dict[str, StepRow]:
    return {row.vm.spec.id: row for row in pilot.app.screen.query(StepRow)}


# ---------------------------------------------------------------------------
# Initial render: a mix of step statuses (real subprocess).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_render_reflects_mix_of_step_statuses(tmp_path):
    from orchestrator.state import StateStore

    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    # Pre-seed state.json so RunSession's own __init__ (constructed inside
    # RunScreen.__init__, i.e. before the app even mounts) computes
    # step_done as SKIPPED_RESUME from the start -- exactly the "resume"
    # path design doc §7.5 describes.
    StateStore(run_dir / "state.json").mark_done("step_done")

    steps = (
        _step("step_done", script),
        _step("step_running", script, "--sleep", "1.0"),
        _step("step_pending", script),
    )
    app = _RunScreenApp(repo_root=tmp_path, run_dir=run_dir, mode=_MODE, steps=steps)
    async with app.run_test() as pilot:
        await _wait_until(
            lambda: _step_rows(pilot).get("step_running")
            and _step_rows(pilot)["step_running"].vm.status == StepUiStatus.RUNNING,
            timeout=3.0,
        )
        rows = _step_rows(pilot)
        assert rows["step_done"].vm.status == StepUiStatus.SKIPPED_RESUME
        assert rows["step_running"].vm.status == StepUiStatus.RUNNING
        assert rows["step_pending"].vm.status == StepUiStatus.PENDING

        # The stepper label and Static#current should agree about which
        # step is active.
        from textual.widgets import Label, Static

        stepper = pilot.app.screen.query_one("#stepper", Label)
        assert "step 2 / 3" in str(stepper.render())
        current = pilot.app.screen.query_one("#current", Static)
        assert "step_running" in str(current.render()) or "Step Running" in str(current.render())


# ---------------------------------------------------------------------------
# A StepFinished message updates StepList (real subprocess).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_finished_message_updates_step_list_to_done(tmp_path):
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker = tmp_path / "m1"
    steps = (
        _step("step1", script, "--marker", str(marker)),
        _step("step2", script, "--sleep", "1.0"),
    )
    app = _RunScreenApp(repo_root=tmp_path, run_dir=run_dir, mode=_MODE, steps=steps)
    async with app.run_test() as pilot:
        await _wait_until(lambda: marker.exists(), timeout=3.0)
        await _wait_until(
            lambda: _step_rows(pilot).get("step1")
            and _step_rows(pilot)["step1"].vm.status == StepUiStatus.DONE,
            timeout=3.0,
        )
        row = _step_rows(pilot)["step1"]
        assert row.vm.status == StepUiStatus.DONE
        assert row.vm.detail == "exit 0"

        state = _read_json(run_dir / "state.json")
        assert state["steps"]["step1"]["status"] == "DONE"


# ---------------------------------------------------------------------------
# RunFinished(success=True) dismisses True (real subprocess, no monkeypatch).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_finished_success_dismisses_true(tmp_path):
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script), _step("step2", script))
    app = _RunScreenApp(repo_root=tmp_path, run_dir=run_dir, mode=_MODE, steps=steps)
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.result, bool), timeout=5.0)
        assert app.result is True

        report = _read_json(run_dir / "report.json")
        assert report["success"] is True
        assert report["finished_at"] is not None


# ---------------------------------------------------------------------------
# RunFinished(success=False) dismisses False (real subprocess, no monkeypatch).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_finished_failure_dismisses_false(tmp_path):
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker = tmp_path / "never_written"
    steps = (
        _step("step1", script, "--exit-code", "1"),
        _step("step2", script, "--marker", str(marker)),
    )
    app = _RunScreenApp(repo_root=tmp_path, run_dir=run_dir, mode=_MODE, steps=steps)
    async with app.run_test() as pilot:
        await _wait_until(lambda: isinstance(app.result, bool), timeout=5.0)
        assert app.result is False

        # step1's nonzero exit must have short-circuited the run -- step2
        # never ran, proving this is the real "run failed" path and not a
        # coincidental False from some other route.
        assert not marker.exists()

        # RunScreen has already dismissed (popped) by this point, so its
        # StepList is gone -- unlike test_step_finished_message_updates_
        # step_list_to_done, which asserts the row mid-run. state.json is
        # the durable record of step1's terminal status here.
        state = _read_json(run_dir / "state.json")
        assert state["steps"]["step1"]["status"] == "FAILED"

        report = _read_json(run_dir / "report.json")
        assert report["success"] is False
        assert report["message"] == "Run failed."
        assert report["finished_at"] is not None


# ---------------------------------------------------------------------------
# ctrl+c -> ConfirmModal -> confirmed abort cancels the "run" worker group.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_c_confirm_aborts_run_and_kills_subprocess(tmp_path):
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker = tmp_path / "never_written"
    steps = (_step("slow_step", script, "--sleep", "5.0", "--marker", str(marker)),)
    app = _RunScreenApp(repo_root=tmp_path, run_dir=run_dir, mode=_MODE, steps=steps)
    async with app.run_test() as pilot:
        await _wait_until(
            lambda: _step_rows(pilot).get("slow_step")
            and _step_rows(pilot)["slow_step"].vm.status == StepUiStatus.RUNNING,
            timeout=3.0,
        )

        await pilot.press("ctrl+c")
        await pilot.pause()
        # RunScreen + its ConfirmModal on top of the app's default screen.
        assert len(pilot.app.screen_stack) == 3

        await pilot.press("y")
        await _wait_until(lambda: isinstance(app.result, bool), timeout=5.0)
        assert app.result is False

        # The 5s sleep never completed -- proves the subprocess was really
        # torn down (SIGTERM via _terminate_group), not merely abandoned.
        await asyncio.sleep(0.3)
        assert not marker.exists()

        state = _read_json(run_dir / "state.json")
        assert state["steps"]["slow_step"]["status"] == "FAILED"
        assert state["steps"]["slow_step"]["meta"]["aborted"] is True

        report = _read_json(run_dir / "report.json")
        assert report["success"] is False
        assert report["finished_at"] is not None
