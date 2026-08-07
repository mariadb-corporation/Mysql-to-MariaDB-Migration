"""Unit tests for orchestrator.tui.demo_data.next_frame -- pure, no I/O."""

from __future__ import annotations

from orchestrator.tui.demo_data import DEMO_STEPS, TICKS_PER_STEP, next_frame
from orchestrator.tui.models import StepUiStatus


def test_tick_zero_first_step_running_rest_pending():
    frame = next_frame(0)
    assert frame.current_index == 0
    assert frame.rows[0].status == StepUiStatus.RUNNING
    assert all(row.status == StepUiStatus.PENDING for row in frame.rows[1:])
    assert frame.completed == 0
    assert frame.total == len(DEMO_STEPS)


def test_advancing_past_a_step_marks_it_done_and_starts_the_next():
    frame = next_frame(TICKS_PER_STEP)
    assert frame.current_index == 1
    assert frame.rows[0].status == StepUiStatus.DONE
    assert frame.rows[1].status == StepUiStatus.RUNNING
    assert frame.completed == 1


def test_pause_after_last_step_has_no_current_index_and_all_done():
    running_ticks = TICKS_PER_STEP * len(DEMO_STEPS)
    frame = next_frame(running_ticks)
    assert frame.current_index is None
    assert frame.completed == len(DEMO_STEPS)
    assert all(row.status == StepUiStatus.DONE for row in frame.rows)
    assert frame.status == "DONE"
    assert frame.tables_done == frame.tables_total


def test_holds_at_done_state_indefinitely_instead_of_looping():
    # A tick far past the run's length must reproduce the exact same
    # finished state as the tick where it first finished -- next_frame does
    # not wrap back around to a fresh "RUNNING" state on its own. DemoScreen
    # is the only thing that ever resets the tick counter back to 0.
    running_ticks = TICKS_PER_STEP * len(DEMO_STEPS)
    just_finished = next_frame(running_ticks)
    long_after = next_frame(running_ticks + 500)
    assert long_after.status == "DONE"
    assert long_after.rows == just_finished.rows
    assert long_after.completed == just_finished.completed
    assert long_after.elapsed_ticks == just_finished.elapsed_ticks == running_ticks


def test_completion_log_line_fires_once_then_goes_quiet():
    running_ticks = TICKS_PER_STEP * len(DEMO_STEPS)
    assert next_frame(running_ticks).log_line is not None
    assert next_frame(running_ticks + 1).log_line is None
    assert next_frame(running_ticks + 50).log_line is None


def test_status_is_running_while_a_step_is_in_progress():
    assert next_frame(0).status == "RUNNING"


def test_same_tick_is_always_the_same_frame():
    assert next_frame(5) == next_frame(5)


def test_sample_present_only_while_a_step_is_running():
    running = next_frame(0)
    assert running.sample is not None
    assert running.sample.label == DEMO_STEPS[0].name

    running_ticks = TICKS_PER_STEP * len(DEMO_STEPS)
    paused = next_frame(running_ticks)
    assert paused.sample is None


def test_seconds_behind_is_never_negative():
    for tick in range(50):
        assert next_frame(tick).seconds_behind >= 0


def test_log_line_is_always_one_of_the_canned_lines():
    from orchestrator.tui.demo_data import _LOG_LINES

    for tick in range(20):
        assert next_frame(tick).log_line in _LOG_LINES
