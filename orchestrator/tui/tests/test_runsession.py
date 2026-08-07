"""Tests for orchestrator.tui.runsession (design doc Sec 7.5).

Real end-to-end drives, no mocking of run_step_async or the subprocess
layer -- this project's TDD philosophy (design doc Sec 4) is
fixture/reality-backed tests, same convention as test_runner_async.py and
test_integration_flow.py. Rather than a step_map.yaml + resolve_steps
(RunSession's constructor takes already-resolved `StepSpec`s directly, so
that indirection buys nothing here), each test builds a small tuple of
`StepSpec`s pointing at one shared, argparse-driven stub script written into
`tmp_path`. The script's CLI flags let each step independently control its
exit code, stdout lines, a sleep (for the cancellation test), and a marker
file it writes on completion -- the side effect tests use to prove a step
actually executed (as opposed to being skipped client-side).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator.tui.models import ModeInfo, StepSpec, StepUiStatus
from orchestrator.tui.runner_async import EventKind, StepEvent
from orchestrator.tui.runsession import (
    RunDirLocked,
    RunFinished,
    RunSession,
    StaleLock,
    StepFinished,
    StepOutput,
    StepStarted,
)

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


class FakeSink:
    """Records every posted Message in arrival order -- stands in for the
    Textual MessagePump RunScreen will eventually pass as `sink`."""

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def post_message(self, message: Any) -> bool:
        self.messages.append(message)
        return True


def _write_stub_script(repo_root: Path, rel_path: str = "scripts/stub.py") -> str:
    script_path = repo_root / rel_path
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_STUB_SCRIPT.format(python=sys.executable))
    # run_step_async chmods +x itself (best-effort); no need to do it here.
    return rel_path


def _step(step_id: str, script: str, *args: str) -> StepSpec:
    return StepSpec(id=step_id, name=step_id.replace("_", " ").title(), script=script, args=tuple(args))


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Normal all-success run.
# ---------------------------------------------------------------------------


async def test_normal_all_success_run(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (
        _step("step1", script, "--line", "hello-from-step1", "--marker", str(tmp_path / "m1")),
        _step("step2", script, "--line", "hello-from-step2", "--marker", str(tmp_path / "m2")),
    )
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)

    await session.drive()

    assert (tmp_path / "m1").read_text() == "ran"
    assert (tmp_path / "m2").read_text() == "ran"

    state = _read_json(run_dir / "state.json")
    assert state["steps"]["step1"]["status"] == "DONE"
    assert state["steps"]["step2"]["status"] == "DONE"

    report = _read_json(run_dir / "report.json")
    assert report["success"] is True
    assert report["finished_at"] is not None
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses == {"step1": "DONE", "step2": "DONE"}

    run_log = (run_dir / "run.log").read_text()
    assert "RUN  step1" in run_log
    assert "RUN  step2" in run_log
    assert "OUT hello-from-step1" in run_log
    assert "OUT hello-from-step2" in run_log
    assert "FINISH success=True" in run_log

    # .tui.lock is created transiently and removed on teardown.
    assert not (run_dir / ".tui.lock").exists()

    view = session.snapshot()
    assert view.finished is True
    assert view.success is True
    assert view.completed == 2
    assert view.total == 2
    assert [row.status for row in view.rows] == [StepUiStatus.DONE, StepUiStatus.DONE]

    kinds = [type(m) for m in sink.messages]
    assert kinds.count(StepStarted) == 2
    assert kinds.count(StepFinished) == 2
    assert kinds.count(RunFinished) == 1
    assert any(isinstance(m, StepOutput) and "hello-from-step1" in m.lines for m in sink.messages)
    finished_msgs = [m for m in sink.messages if isinstance(m, StepFinished)]
    assert all(m.ok for m in finished_msgs)
    run_finished = [m for m in sink.messages if isinstance(m, RunFinished)][0]
    assert run_finished.success is True


# ---------------------------------------------------------------------------
# Mid-run failure: fail-fast, no further steps.
# ---------------------------------------------------------------------------


async def test_mid_run_failure_stops_subsequent_steps(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker3 = tmp_path / "m3"
    steps = (
        _step("step1", script, "--marker", str(tmp_path / "m1")),
        _step("step2", script, "--exit-code", "1", "--line", "boom"),
        _step("step3", script, "--marker", str(marker3)),
    )
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)

    await session.drive()

    assert not marker3.exists()  # step3 never ran

    state = _read_json(run_dir / "state.json")
    assert state["steps"]["step1"]["status"] == "DONE"
    assert state["steps"]["step2"]["status"] == "FAILED"
    assert "step3" not in state["steps"]

    report = _read_json(run_dir / "report.json")
    assert report["success"] is False
    assert report["finished_at"] is not None
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses == {"step1": "DONE", "step2": "FAILED"}

    view = session.snapshot()
    assert view.finished is True
    assert view.success is False
    assert [row.status for row in view.rows] == [
        StepUiStatus.DONE,
        StepUiStatus.FAILED,
        StepUiStatus.PENDING,
    ]

    run_finished = [m for m in sink.messages if isinstance(m, RunFinished)][0]
    assert run_finished.success is False


# ---------------------------------------------------------------------------
# Resume: a step already DONE in state.json is skipped, not re-executed.
# ---------------------------------------------------------------------------


async def test_resume_skips_already_done_step(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    run_dir.mkdir(parents=True)
    marker1 = tmp_path / "m1"
    state_path = run_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
                "steps": {"step1": {"status": "DONE", "updated_at": "", "meta": {}}},
            }
        )
    )
    steps = (
        _step("step1", script, "--marker", str(marker1)),
        _step("step2", script, "--marker", str(tmp_path / "m2")),
    )
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)

    # Row for the already-done step is labeled SKIPPED_RESUME before drive()
    # even starts, purely from the state.json read at construction time.
    initial = session.snapshot()
    assert initial.rows[0].status == StepUiStatus.SKIPPED_RESUME

    await session.drive()

    assert not marker1.exists()  # step1's script never ran
    assert (tmp_path / "m2").read_text() == "ran"

    report = _read_json(run_dir / "report.json")
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses["step1"] == "SKIPPED"
    assert statuses["step2"] == "DONE"

    started_ids = [m.spec.id for m in sink.messages if isinstance(m, StepStarted)]
    assert started_ids == ["step2"]  # StepStarted never posted for step1

    view = session.snapshot()
    assert view.rows[0].status == StepUiStatus.SKIPPED_RESUME
    assert view.rows[1].status == StepUiStatus.DONE
    assert view.success is True


# ---------------------------------------------------------------------------
# SKIPPED_BY_PHASE: the underlying script still actually executes.
# ---------------------------------------------------------------------------


async def test_skipped_by_phase_step_still_executes(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker = tmp_path / "phase_skip_marker"
    steps = (
        _step("staged_dump", script, "--marker", str(marker)),
        _step("other_step", script, "--marker", str(tmp_path / "m2")),
    )
    sink = FakeSink()
    session = RunSession(
        tmp_path, run_dir, _MODE, steps, {}, frozenset({"staged_dump"}), sink
    )

    before = session.snapshot()
    assert before.rows[0].status == StepUiStatus.SKIPPED_BY_PHASE

    await session.drive()

    # The side effect proves the script actually ran, not a client-side skip.
    assert marker.read_text() == "ran"

    state = _read_json(run_dir / "state.json")
    assert state["steps"]["staged_dump"]["status"] == "DONE"

    report = _read_json(run_dir / "report.json")
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses["staged_dump"] == "DONE"  # engine-level status is DONE

    started_ids = [m.spec.id for m in sink.messages if isinstance(m, StepStarted)]
    assert "staged_dump" in started_ids  # it really was started/executed

    # UI-facing row label stays SKIPPED_BY_PHASE even though it succeeded --
    # only the icon/label changes, per design doc Sec 7.5.
    after = session.snapshot()
    assert after.rows[0].status == StepUiStatus.SKIPPED_BY_PHASE
    assert after.rows[1].status == StepUiStatus.DONE


# ---------------------------------------------------------------------------
# Cancellation: never mark_done a cancelled step; always finish_run.
# ---------------------------------------------------------------------------


async def test_cancelled_run_marks_failed_aborted_and_finishes_run(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    marker = tmp_path / "slow_marker"
    steps = (
        _step("slow_step", script, "--sleep", "5", "--marker", str(marker)),
    )
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)

    task = asyncio.create_task(session.drive())
    await asyncio.sleep(0.3)  # give the subprocess time to spawn and start sleeping
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The 5s sleep never completed -- proves the process was actually torn
    # down, not left running past cancellation.
    assert not marker.exists()

    state = _read_json(run_dir / "state.json")
    assert state["steps"]["slow_step"]["status"] == "FAILED"
    assert state["steps"]["slow_step"]["meta"]["aborted"] is True

    report = _read_json(run_dir / "report.json")
    assert report["success"] is False
    assert report["finished_at"] is not None  # finish_run always runs on abort
    statuses = {s["id"]: s["status"] for s in report["steps"]}
    assert statuses["slow_step"] == "FAILED"

    view = session.snapshot()
    assert view.finished is True
    assert view.success is False
    assert view.rows[0].status == StepUiStatus.FAILED

    # .tui.lock is still released even on an aborted run.
    assert not (run_dir / ".tui.lock").exists()


# ---------------------------------------------------------------------------
# .tui.lock: created while driving, removed after; a second session on the
# same run_dir refuses to drive concurrently and falls back to observer mode.
# ---------------------------------------------------------------------------


async def test_lock_created_while_driving_and_removed_after(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script, "--sleep", "0.3"),)
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)

    lock_path = run_dir / ".tui.lock"
    task = asyncio.create_task(session.drive())
    for _ in range(50):
        if lock_path.exists():
            break
        await asyncio.sleep(0.02)
    assert lock_path.exists()
    assert lock_path.read_text().strip() != ""

    await task
    assert not lock_path.exists()


async def test_second_session_refused_while_lock_held_by_live_pid(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    run_dir.mkdir(parents=True)
    marker = tmp_path / "should_not_run"
    steps = (_step("step1", script, "--marker", str(marker)),)

    # This test process's own pid is guaranteed alive for the duration of
    # the test -- simplest reliable stand-in for "another live process".
    (run_dir / ".tui.lock").write_text(str(os.getpid()))

    session2 = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), FakeSink())

    with pytest.raises(RunDirLocked) as excinfo:
        await session2.drive()
    assert excinfo.value.pid == os.getpid()

    # Refused, not silently allowed to drive: nothing executed, nothing written.
    assert not marker.exists()
    assert not (run_dir / "report.json").exists()

    # Routed to observer mode instead: read-only, never raises, never writes.
    view = await session2.observe(interval_s=0.01).__anext__()
    assert view.total == 1
    assert not (run_dir / "report.json").exists()
    # The other process's lock is left untouched by the refused session.
    assert (run_dir / ".tui.lock").read_text().strip() == str(os.getpid())


async def test_stale_lock_is_distinguished_from_live_lock(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    run_dir.mkdir(parents=True)
    steps = (_step("step1", script),)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (run_dir / ".tui.lock").write_text(str(dead.pid))

    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), FakeSink())

    with pytest.raises(StaleLock) as excinfo:
        await session.drive()
    assert excinfo.value.pid == dead.pid
    # Stale lock is left in place -- takeover is the caller's decision, not
    # something drive() does silently.
    assert (run_dir / ".tui.lock").exists()


# ---------------------------------------------------------------------------
# Output buffering: the omission-marker branch is only reachable if the
# buffer actually grows past MAX_BUFFER_LINES *between* flushes -- i.e.
# output arriving faster than the 100ms tick can drain it. Exercised here by
# driving `_handle_event` directly, without the flush ticker running, so the
# buffer is free to grow past 2000 before a flush is manually triggered.
# ---------------------------------------------------------------------------


async def test_flush_caps_overflowed_buffer_with_omission_marker(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script),)
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)
    session._run_log_fh = open(run_dir / "run.log", "a", encoding="utf-8")

    total_lines = 2500  # > MAX_BUFFER_LINES (2000), < EMERGENCY_BUFFER_LINES (4000)
    for i in range(total_lines):
        session._handle_event(0, StepEvent(kind=EventKind.OUT, text=f"line-{i}"))

    # No tick ran and the emergency backstop (4000) was never reached, so
    # the per-append path must not have eagerly flushed at 2000 -- the
    # buffer should still hold every line, proving the overflow/omission
    # branch in _cap_output_lines is genuinely reachable at flush time.
    assert len(session._out_buffer) == total_lines

    session._flush_output(0)

    outputs = [m for m in sink.messages if isinstance(m, StepOutput) and not m.partial]
    assert len(outputs) == 1
    lines = outputs[0].lines
    assert len(lines) == 2 * session.KEEP_EDGE_LINES + 1
    assert list(lines[: session.KEEP_EDGE_LINES]) == [f"line-{i}" for i in range(1000)]
    assert list(lines[-session.KEEP_EDGE_LINES :]) == [
        f"line-{i}" for i in range(total_lines - 1000, total_lines)
    ]
    omitted = total_lines - 2 * session.KEEP_EDGE_LINES
    assert lines[session.KEEP_EDGE_LINES] == f"... {omitted} lines omitted (see run.log)"

    session._run_log_fh.close()


async def test_buffer_does_not_eagerly_flush_at_exactly_max_lines(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script),)
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)
    session._run_log_fh = open(run_dir / "run.log", "a", encoding="utf-8")

    for i in range(session.MAX_BUFFER_LINES):
        session._handle_event(0, StepEvent(kind=EventKind.OUT, text=f"line-{i}"))

    # Reaching exactly MAX_BUFFER_LINES (2000) must not trigger an eager
    # per-append flush -- only the 100ms tick (or the emergency backstop
    # well above MAX_BUFFER_LINES) may flush.
    assert len(session._out_buffer) == session.MAX_BUFFER_LINES
    assert not any(isinstance(m, StepOutput) and not m.partial for m in sink.messages)

    session._run_log_fh.close()


# ---------------------------------------------------------------------------
# run.log deduplication: idle-timeout re-emissions of an unchanged partial
# carry must not each produce a new run.log line, but genuinely new partial
# text (progress advancing) must still be written.
# ---------------------------------------------------------------------------


async def test_repeated_idle_partial_writes_one_run_log_line(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script),)
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)
    log_path = run_dir / "run.log"
    session._run_log_fh = open(log_path, "a", encoding="utf-8")

    # Two consecutive identical idle-timeout re-emissions of the same
    # unresolved carry (runner_async.py's idle_flush_s branch re-yields the
    # same partial text unchanged every idle_flush_s while nothing new has
    # arrived).
    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="42% [===>]", partial=True))
    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="42% [===>]", partial=True))

    session._run_log_fh.flush()
    log_lines = log_path.read_text().splitlines()
    assert len(log_lines) == 1
    assert log_lines[0].endswith("OUT 42% [===>]")

    # Genuinely new partial text (progress advancing) is still written.
    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="43% [====>]", partial=True))
    session._run_log_fh.flush()
    log_lines = log_path.read_text().splitlines()
    assert len(log_lines) == 2
    assert log_lines[1].endswith("OUT 43% [====>]")

    session._run_log_fh.close()


async def test_finalized_line_resets_tracked_partial_text(tmp_path: Path) -> None:
    script = _write_stub_script(tmp_path)
    run_dir = tmp_path / "rundir"
    steps = (_step("step1", script),)
    sink = FakeSink()
    session = RunSession(tmp_path, run_dir, _MODE, steps, {}, frozenset(), sink)
    log_path = run_dir / "run.log"
    session._run_log_fh = open(log_path, "a", encoding="utf-8")

    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="50% [=====>]", partial=True))
    # A finalized line for the same carry -- e.g. the transfer completed --
    # must always be written (never a duplicate by construction) and must
    # clear the tracked partial so a later, unrelated partial stretch is not
    # compared against stale text from a previous transfer.
    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="done", partial=False))
    session._handle_event(0, StepEvent(kind=EventKind.OUT, text="50% [=====>]", partial=True))

    session._run_log_fh.flush()
    log_lines = log_path.read_text().splitlines()
    assert len(log_lines) == 3
    assert log_lines[0].endswith("OUT 50% [=====>]")
    assert log_lines[1].endswith("OUT done")
    assert log_lines[2].endswith("OUT 50% [=====>]")

    session._run_log_fh.close()
