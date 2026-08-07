"""Pure fake-data generator for ``screens/demo.py``'s ``DemoScreen``.

See ``tui-phase4-demo-progress-screen.plan.md``: this screen is a
sales/capability demo, not a real migration feature, and must be
side-effect free -- a hard constraint. ``next_frame`` takes only an
integer tick counter and returns a ``DemoFrame``; no I/O, no clock reads,
no randomness. The same ``tick`` always produces the same ``DemoFrame``,
which is what makes it safe to drive from a plain ``set_interval`` and
trivial to unit test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from orchestrator.tui.models import (
    ProgressSample,
    ProgressSource,
    StepRowVM,
    StepSpec,
    StepUiStatus,
)

# Fake step names only -- these do not resolve against step_map.yaml and
# must never be passed to anything that runs a real script.
DEMO_STEPS: tuple[StepSpec, ...] = (
    StepSpec("demo_dump_schema", "Dump schema", "mariadb-dump --no-data (demo)", ()),
    StepSpec("demo_dump_data", "Dump data", "mariadb-dump | pv (demo)", ()),
    StepSpec("demo_transfer", "Transfer dump", "rsync (demo)", ()),
    StepSpec("demo_load_schema", "Load schema", "mariadb < schema.sql (demo)", ()),
    StepSpec("demo_load_data", "Load data", "mariadb-mtk load (demo)", ()),
)

TICKS_PER_STEP = 8

# Fixed synthetic table count -- tables_done below climbs toward this in
# step with overall tick progress. Not read from anywhere real; DEMO_STEPS
# above only names 5 pipeline stages, not individual tables.
_TABLES_TOTAL = 22

_LOG_LINES: tuple[str, ...] = (
    "[demo] connected to source (mysql8.internal:3306)",
    "[demo] connected to target (mariadb11.internal:3306)",
    "[demo] dumping table `orders` (2.1M rows)...",
    "[demo] dumping table `customers` (410K rows)...",
    "[demo] transfer: 340 MiB / 1.2 GiB",
    "[demo] loading schema into target...",
    "[demo] applying table `orders`...",
    "[demo] row count check: source=2100544 target=2100544 (match)",
    "[demo] replication thread: IO running, SQL running",
    "[demo] checkpoint written",
)

_DONE_LOG_LINE = "[demo] migration complete -- all steps finished"


@dataclass(frozen=True)
class DemoFrame:
    """Everything ``DemoScreen`` needs to redraw for one tick."""

    rows: tuple[StepRowVM, ...]
    completed: int
    total: int
    current_index: int | None
    sample: ProgressSample | None
    seconds_behind: int
    log_line: str | None
    status: str  # "RUNNING" or "DONE" -- never loops back to "RUNNING" on its own
    elapsed_ticks: int
    rows_per_sec: int
    tables_done: int
    tables_total: int


def next_frame(tick: int) -> DemoFrame:
    """Advance to ``tick``. Runs once through ``DEMO_STEPS`` and then holds
    at the finished state forever -- it does not loop back to the start.
    ``DemoScreen`` is the one place that resets ``tick`` back to 0 (the "r"
    / rerun-demo action), matching a real run: it finishes once, and seeing
    it again is a deliberate replay, not an automatic restart.
    """
    total = len(DEMO_STEPS)
    running_ticks = TICKS_PER_STEP * total
    clamped_tick = min(tick, running_ticks)

    if clamped_tick < running_ticks:
        current_index = clamped_tick // TICKS_PER_STEP
        progress_in_step = clamped_tick % TICKS_PER_STEP
        completed = current_index
        status = "RUNNING"
    else:
        current_index = None
        progress_in_step = 0
        completed = total
        status = "DONE"

    rows = tuple(
        StepRowVM(
            spec=spec,
            status=_status_for(i, current_index, completed),
            detail=_detail_for(i, current_index, progress_in_step),
            elapsed_s=None,
        )
        for i, spec in enumerate(DEMO_STEPS)
    )

    sample = None
    if current_index is not None:
        sample = _fake_sample(DEMO_STEPS[current_index].name, progress_in_step)

    if status == "RUNNING":
        seconds_behind = max(0, round(2 + 1.5 * math.sin(tick * 0.5)))
        rows_per_sec = max(0, round(12_000 + 6_000 * math.sin(tick * 0.35)))
        # tick == running_ticks is the one frame where status flips to DONE
        # -- that's the frame that gets the completion line, every frame
        # after it (tick > running_ticks, still clamped) gets none so the
        # log doesn't spam a finished run.
        log_line = _LOG_LINES[tick % len(_LOG_LINES)]
    else:
        seconds_behind = 0
        rows_per_sec = 0
        log_line = _DONE_LOG_LINE if tick == running_ticks else None

    overall_fraction = clamped_tick / running_ticks
    tables_done = round(_TABLES_TOTAL * overall_fraction)

    return DemoFrame(
        rows=rows,
        completed=completed,
        total=total,
        current_index=current_index,
        sample=sample,
        seconds_behind=seconds_behind,
        log_line=log_line,
        status=status,
        elapsed_ticks=clamped_tick,
        rows_per_sec=rows_per_sec,
        tables_done=tables_done,
        tables_total=_TABLES_TOTAL,
    )


def _status_for(index: int, current_index: int | None, completed: int) -> StepUiStatus:
    if current_index is not None and index == current_index:
        return StepUiStatus.RUNNING
    if index < completed:
        return StepUiStatus.DONE
    return StepUiStatus.PENDING


def _detail_for(index: int, current_index: int | None, progress_in_step: int) -> str:
    if current_index is not None and index == current_index:
        pct = round(progress_in_step / TICKS_PER_STEP * 100)
        return f"{pct}% complete (demo)"
    return ""


def _fake_sample(label: str, progress_in_step: int) -> ProgressSample:
    percent = min(100.0, progress_in_step / TICKS_PER_STEP * 100)
    rate_mib_s = 32.0 + 6.0 * math.sin(progress_in_step * 0.9)
    remaining_ticks = max(0, TICKS_PER_STEP - progress_in_step)
    return ProgressSample(
        source=ProgressSource.PV,
        label=label,
        percent=percent,
        elapsed_s=float(progress_in_step),
        eta_s=float(remaining_ticks),
        bytes_done=None,
        rate_bytes_s=rate_mib_s * 1024 * 1024,
    )
