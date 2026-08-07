"""``RunSession`` -- the in-process driver for the run/resume phase (design
doc tui-phase0-design.md Sec 7.5).

``RunScreen`` (Phase 2, built in the next stage -- not this module) owns the
Textual worker that calls ``drive()`` and renders the ``StepRowVM``/``RunView``
snapshots this module produces. This module has **no Textual widget
dependency** beyond ``textual.message.Message`` itself, so it stays unit
testable without a running App.

``drive()`` reproduces ``orchestrator/migrationctl.py:569-604``'s step loop
step for step: skip a step whose ``state.json`` entry is already ``DONE``,
otherwise run it via ``run_step_async`` (``orchestrator/tui/runner_async.py``),
recording the identical ``meta`` dict shape into both ``state.json``
(``orchestrator/state.py``) and ``report.json`` (``orchestrator/report.py``)
that the CLI path would have written, and failing fast (no further steps) on
the first failure. A step whose id is in the ``skips`` set passed to
``__init__`` is **not** skipped client-side -- the underlying script itself
no-ops (see ``orchestrator/tui/modes.py:expected_skips``) -- only the row's
UI label changes to ``SKIPPED_BY_PHASE``.

**Message batching (load-bearing).** ``StepOutput`` is batched on a 100 ms
tick (``_flush_ticker``), never one ``post_message`` per output line -- a
fast dump can produce thousands of lines/sec and would saturate the Textual
message pump. The tick is the *normal* flush trigger; appending a line never
eagerly flushes just because the buffer reached ``MAX_BUFFER_LINES`` (2000)
-- only an emergency backstop well above that (``EMERGENCY_BUFFER_LINES``,
2x) does, as insurance against the tick being starved under load. Whatever
flush actually fires, tick-triggered or emergency, is capped at
``MAX_BUFFER_LINES`` lines; on overflow the first/last ``KEEP_EDGE_LINES``
(1000) survive with an omission marker in between (see
``_cap_output_lines``). The *full* stream still reaches ``run.log``
regardless of what gets batched to the UI, except that an idle-timeout
partial re-emission (see ``run.log`` writing, below) identical to the
immediately-preceding one is not written again -- see
``_write_run_log_line``.

**``run.log`` writing (load-bearing).** ``Report.log`` opens+closes the file
on every call (``orchestrator/report.py:60-64``); calling it per output line
at dump throughput would stutter the event loop badly. Instead this module
opens ``<run_dir>/run.log`` itself, once, in buffered append mode, for the
life of the session, and writes output lines with the *identical* format
``Report.log`` uses (``f"{ts} OUT {line}\\n"``, UTC ISO timestamp), flushed on
the same 100 ms tick as the message batching. ``Report.log`` itself is still
used, unchanged, for the low-frequency structured lines (START/RUN/SKIP/CMD/
FINISH) so those interleave where the CLI puts them.

**Abort/cancellation state integrity.** A cancelled step is never
``mark_done``. On ``asyncio.CancelledError`` the three writes --
``state.mark_failed(..., meta={..., "aborted": True})``, then
``report.add_step(..., FAILED, ...)``, then
``report.finish_run(success=False, ...)`` -- happen with **no ``await``
between them**: ``StateStore``/``Report`` are synchronous blocking file IO,
which is exactly what makes them safe from being interrupted mid-write by a
second cancellation. Do not wrap them in ``asyncio.to_thread`` -- that would
reintroduce an await point inside the critical section. ``finish_run`` always
runs, even on a deliberate abort, so ``report.json`` never sits at
``finished_at: null`` looking like a crash.

**``.tui.lock`` and observer mode (design doc Sec 7.4).** ``drive()`` first
tries to acquire ``<run_dir>/.tui.lock`` via
``os.open(path, O_CREAT | O_EXCL | O_WRONLY)``, writing this process's pid.
If the file exists and its pid is alive, ``RunDirLocked`` is raised instead
of driving -- the caller is expected to fall back to ``observe()``, the
read-only polling coroutine that never writes and tolerates torn reads
(``json.JSONDecodeError`` / a missing file) by keeping the previous snapshot.
If the file exists but its pid is dead, ``StaleLock`` is raised instead --
distinct from ``RunDirLocked`` so the caller can offer the operator a
takeover choice rather than silently succeeding or silently refusing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence, TextIO

from textual.message import Message

from orchestrator.report import Report, StepStatus
from orchestrator.state import StateStore
from orchestrator.tui.models import (
    ModeInfo,
    ProgressSample,
    RunView,
    StepRowVM,
    StepSpec,
    StepUiStatus,
)
from orchestrator.tui.pvparse import parse_file_probe_line, parse_pv_line
from orchestrator.tui.runner_async import EventKind, StepEvent, run_step_async

__all__ = [
    "RunSession",
    "RunDirLocked",
    "StaleLock",
    "StepStarted",
    "StepOutput",
    "StepProgress",
    "StepFinished",
    "RunFinished",
]


# --- Messages posted to `sink` (design doc Sec 7.5) -------------------------
#
# Plain textual.message.Message subclasses, defined here (not nested in a
# Widget/Screen) because RunScreen (Phase 2) imports them from this module.


class StepStarted(Message):
    """Posted once, right before a step's script is spawned."""

    def __init__(self, index: int, spec: StepSpec) -> None:
        super().__init__()
        self.index = index
        self.spec = spec


class StepOutput(Message):
    """A batch of output lines for the step at `index`.

    `partial=True` means `lines` is a single-element tuple holding a live
    `\\r`-terminated pv fragment that has not yet resolved to a full line --
    a consumer should *replace* its previous display of this step's partial
    line, not append. `partial=False` batches of finalized lines should be
    appended in order.
    """

    def __init__(self, index: int, lines: tuple[str, ...], partial: bool) -> None:
        super().__init__()
        self.index = index
        self.lines = lines
        self.partial = partial


class StepProgress(Message):
    """A parsed progress sample (pv meter line or file_size_probe fallback)."""

    def __init__(self, index: int, sample: ProgressSample) -> None:
        super().__init__()
        self.index = index
        self.sample = sample


class StepFinished(Message):
    """A step reached a terminal state -- success, failure, or abort."""

    def __init__(self, index: int, ok: bool, meta: Mapping[str, Any]) -> None:
        super().__init__()
        self.index = index
        self.ok = ok
        self.meta = meta


class RunFinished(Message):
    """The whole run reached a terminal state."""

    def __init__(self, success: bool, message: str) -> None:
        super().__init__()
        self.success = success
        self.message = message


# --- Lock-related exceptions (design doc Sec 7.4) ---------------------------


class RunDirLocked(Exception):
    """`<run_dir>/.tui.lock` exists and its pid is alive.

    The caller must not drive this session concurrently -- fall back to
    `RunSession.observe()` instead.
    """

    def __init__(self, pid: int, lock_path: Path) -> None:
        super().__init__(f"run dir locked by live pid {pid} ({lock_path})")
        self.pid = pid
        self.lock_path = lock_path


class StaleLock(Exception):
    """`<run_dir>/.tui.lock` exists but its pid is dead.

    Distinct from `RunDirLocked` so the caller can offer the operator a
    takeover choice (remove the lock file and retry) rather than silently
    succeeding or silently refusing.
    """

    def __init__(self, pid: Optional[int], lock_path: Path) -> None:
        super().__init__(f"stale lock file (pid {pid}, not running) at {lock_path}")
        self.pid = pid
        self.lock_path = lock_path


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process this user can at least see.

    `os.kill(pid, 0)` sends no signal, just probes existence/permission.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, just owned by someone else -- still alive.
        return True
    except OSError:
        return False
    return True


def _cap_output_lines(lines: Sequence[str], max_lines: int, keep_edge: int) -> list[str]:
    """Cap a batch of output lines for UI display (design doc Sec 7.5).

    Pure function. Below `max_lines`, returns `lines` unchanged. Above it,
    keeps the first and last `keep_edge` lines with a single omission marker
    in between -- the full, uncapped stream still reaches run.log regardless
    (see `RunSession._write_run_log_line`), so nothing is actually lost.
    """
    if len(lines) <= max_lines:
        return list(lines)
    omitted = len(lines) - 2 * keep_edge
    marker = f"... {omitted} lines omitted (see run.log)"
    return list(lines[:keep_edge]) + [marker] + list(lines[-keep_edge:])


class MessageTarget:
    """Typing stand-in for whatever `sink` RunScreen passes in.

    Not imported from Textual on purpose -- both `Widget` and `Screen`
    satisfy this shape structurally (they subclass `MessagePump`, which
    defines `post_message`), and tests can pass a plain recorder object
    without inheriting from any Textual base.
    """

    def post_message(self, message: Message) -> bool:  # pragma: no cover
        raise NotImplementedError


_TERMINAL_STATUSES: frozenset[StepUiStatus] = frozenset(
    {
        StepUiStatus.DONE,
        StepUiStatus.FAILED,
        StepUiStatus.SKIPPED_RESUME,
        StepUiStatus.SKIPPED_BY_PHASE,
    }
)


class RunSession:
    """Drives one migration run's step loop in-process (design doc Sec 7.5).

    Construction is cheap and side-effect-light: it builds a `StateStore`
    (which -- matching `orchestrator/state.py`'s own `__post_init__` --
    creates `run_dir` and a fresh empty `state.json` only if neither already
    exists) and a `Report` (which touches no filesystem state until
    `start_run()` is called from inside `drive()`). The initial `RunView` is
    computed once here, from `state.is_done()` per step plus `skips` -- there
    is no live `state.json` watcher while this session is the one driving.
    """

    STATE_FILENAME = "state.json"
    REPORT_FILENAME = "report.json"
    LOG_FILENAME = "run.log"
    LOCK_FILENAME = ".tui.lock"

    FLUSH_INTERVAL_S = 0.1
    MAX_BUFFER_LINES = 2000
    KEEP_EDGE_LINES = 1000
    TAIL_LINES = 50
    # Emergency backstop only -- the 100ms tick (`_flush_ticker`) is the
    # normal flush trigger. Set well above `MAX_BUFFER_LINES` so the common
    # case never eagerly flushes the instant the buffer reaches exactly
    # `MAX_BUFFER_LINES`, which would make `_cap_output_lines`' overflow/
    # omission-marker branch unreachable. This only fires if output arrives
    # faster than the tick can drain it (or the tick itself is delayed).
    EMERGENCY_BUFFER_LINES = MAX_BUFFER_LINES * 2

    def __init__(
        self,
        repo_root: Path,
        run_dir: Path,
        mode: ModeInfo,
        steps: Sequence[StepSpec],
        env: Mapping[str, str],
        skips: frozenset[str],
        sink: MessageTarget,
    ) -> None:
        self._repo_root = repo_root
        self._run_dir = run_dir
        self._mode = mode
        self._steps: tuple[StepSpec, ...] = tuple(steps)
        self._env: dict[str, str] = dict(env)
        self._skips: frozenset[str] = frozenset(skips)
        self._sink = sink

        self._state_path = run_dir / self.STATE_FILENAME
        self._report_path = run_dir / self.REPORT_FILENAME
        self._log_path = run_dir / self.LOG_FILENAME
        self._lock_path = run_dir / self.LOCK_FILENAME

        # StateStore.__post_init__ creates run_dir + a fresh state.json if
        # (and only if) neither already exists -- safe to construct
        # unconditionally, including for an observer-only session pointed
        # at a run_dir another process is actively driving.
        self._state = StateStore(self._state_path)
        # Report is a plain dataclass -- constructing it touches no disk.
        # `start_run()` (called from `drive()`) is what populates `_data`.
        self._report = Report(self._report_path, self._log_path)

        self._staged_phase: Optional[str] = self._env.get("STAGED_PHASE") or None
        self._lock_acquired = False
        self._run_log_fh: Optional[TextIO] = None

        self._statuses: list[StepUiStatus] = []
        self._details: list[str] = []
        self._elapsed: list[Optional[float]] = []
        for spec in self._steps:
            status, detail = self._initial_status(spec)
            self._statuses.append(status)
            self._details.append(detail)
            self._elapsed.append(None)

        self._current_index: Optional[int] = None
        self._current_step_id: Optional[str] = None
        self._finished = False
        self._success: Optional[bool] = None

        # Per-step scratch state, reset at the top of each iteration.
        self._out_buffer: list[str] = []
        self._tail_buffer: list[str] = []
        self._last_partial_text: Optional[str] = None

    def _initial_status(self, spec: StepSpec) -> tuple[StepUiStatus, str]:
        if self._state.is_done(spec.id):
            return StepUiStatus.SKIPPED_RESUME, "already done"
        if spec.id in self._skips:
            return StepUiStatus.SKIPPED_BY_PHASE, "phase no-op (will still run)"
        return StepUiStatus.PENDING, ""

    # -- Public API -----------------------------------------------------

    def snapshot(self) -> RunView:
        """The session's own current view, built from in-memory tracking.

        Never re-reads state.json/report.json -- this session is the sole
        writer while `drive()` is running, so a re-poll of its own writes
        would buy nothing and risks a torn read of a write still in flight.
        """
        rows = tuple(
            StepRowVM(
                spec=spec,
                status=self._statuses[i],
                detail=self._details[i],
                elapsed_s=self._elapsed[i],
            )
            for i, spec in enumerate(self._steps)
        )
        return self._view_from_rows(
            rows,
            current_index=self._current_index,
            finished=self._finished,
            success=self._success,
        )

    def _view_from_rows(
        self,
        rows: tuple[StepRowVM, ...],
        *,
        current_index: Optional[int],
        finished: bool,
        success: Optional[bool],
    ) -> RunView:
        completed = sum(1 for row in rows if row.status in _TERMINAL_STATUSES)
        return RunView(
            mode=self._mode.key,
            staged_phase=self._staged_phase,
            rows=rows,
            current_index=current_index,
            total=len(self._steps),
            completed=completed,
            run_dir=self._run_dir,
            finished=finished,
            success=success,
        )

    # -- .tui.lock (design doc Sec 7.4) ----------------------------------

    def _acquire_lock(self) -> None:
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = self._read_lock_pid()
            if pid is not None and _pid_alive(pid):
                raise RunDirLocked(pid, self._lock_path)
            raise StaleLock(pid, self._lock_path)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
        finally:
            os.close(fd)
        self._lock_acquired = True

    def _read_lock_pid(self) -> Optional[int]:
        try:
            text = self._lock_path.read_text(encoding="ascii").strip()
            return int(text)
        except (OSError, ValueError):
            return None

    def _release_lock(self) -> None:
        if self._lock_acquired:
            self._lock_path.unlink(missing_ok=True)
            self._lock_acquired = False

    # -- drive() (design doc Sec 7.5) ------------------------------------

    async def drive(self) -> None:
        """Run every step in order, fail-fast, writing state/report/run.log.

        Raises `RunDirLocked` or `StaleLock` (without touching state.json,
        report.json, or run.log) if `.tui.lock` says someone else is -- or
        was -- already driving this run_dir. Callers should fall back to
        `observe()` on `RunDirLocked`, or offer a takeover on `StaleLock`.
        """
        self._acquire_lock()
        try:
            self._run_log_fh = open(self._log_path, "a", encoding="utf-8")
            try:
                self._report.start_run(mode=self._mode.key, config_path="")
                await self._run_loop()
            except asyncio.CancelledError:
                self._handle_abort()
                raise
        finally:
            self._teardown()

    def _teardown(self) -> None:
        if self._run_log_fh is not None:
            try:
                self._run_log_fh.flush()
                self._run_log_fh.close()
            except OSError:
                pass
            self._run_log_fh = None
        self._release_lock()

    async def _run_loop(self) -> None:
        """Reproduces migrationctl.py:569-604's step loop, step for step."""
        for index, spec in enumerate(self._steps):
            if self._state.is_done(spec.id):
                self._report.log(f"SKIP {spec.id} ({spec.name}) - already DONE")
                self._report.add_step(
                    spec.id, spec.name, StepStatus.SKIPPED,
                    details={"reason": "already_done"},
                )
                self._statuses[index] = StepUiStatus.SKIPPED_RESUME
                self._details[index] = "already done"
                continue

            phase_skip = spec.id in self._skips
            if phase_skip:
                self._statuses[index] = StepUiStatus.SKIPPED_BY_PHASE
                self._details[index] = "phase no-op (will still run)"
            else:
                self._statuses[index] = StepUiStatus.RUNNING

            self._report.log(f"RUN  {spec.id} ({spec.name}) -> {spec.script}")
            self._current_index = index
            self._current_step_id = spec.id
            self._out_buffer = []
            self._tail_buffer = []
            self._last_partial_text = None
            self._sink.post_message(StepStarted(index, spec))

            started_at = time.monotonic()
            ok = False
            meta: dict[str, Any] = {}

            flush_task = asyncio.create_task(self._flush_ticker(index))
            try:
                async with contextlib.aclosing(
                    run_step_async(
                        self._repo_root,
                        spec.script,
                        list(spec.args),
                        self._env,
                    )
                ) as stream:
                    async for ev in stream:
                        self._handle_event(index, ev)
                        if ev.kind is EventKind.EXIT:
                            ok = ev.returncode == 0
                            meta = dict(ev.meta or {})
                        elif ev.kind is EventKind.ERROR:
                            ok = False
                            meta = dict(ev.meta or {})
            finally:
                flush_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await flush_task
                self._flush_output(index)

            self._elapsed[index] = time.monotonic() - started_at
            self._current_step_id = None
            self._current_index = None

            if ok:
                self._state.mark_done(spec.id, meta=meta)
                self._report.add_step(spec.id, spec.name, StepStatus.DONE, details=meta)
                if not phase_skip:
                    self._statuses[index] = StepUiStatus.DONE
                    self._details[index] = f"exit {meta.get('returncode')}"
                self._sink.post_message(StepFinished(index, True, meta))
            else:
                self._state.mark_failed(spec.id, meta=meta)
                self._report.add_step(spec.id, spec.name, StepStatus.FAILED, details=meta)
                self._statuses[index] = StepUiStatus.FAILED
                self._details[index] = str(meta.get("error") or f"exit {meta.get('returncode')}")
                self._success = False
                self._sink.post_message(StepFinished(index, False, meta))
                break
        else:
            self._success = True

        self._finished = True
        message = "Run completed successfully." if self._success else "Run failed."
        self._report.finish_run(success=bool(self._success), message=message)
        self._sink.post_message(RunFinished(bool(self._success), message))

    # -- Per-event handling -----------------------------------------------

    def _handle_event(self, index: int, ev: StepEvent) -> None:
        if ev.kind is EventKind.CMD:
            # Low-frequency structured line -- goes through Report.log
            # unchanged, same as RUN/SKIP/FINISH, so it interleaves where
            # the CLI puts it (design doc Sec 7.5).
            self._report.log(ev.text)
            return
        if ev.kind is not EventKind.OUT:
            # EXIT/ERROR carry no text; the caller (_run_loop) reads their
            # .returncode/.meta directly.
            return

        # The full stream reaches run.log, regardless of what the UI batch
        # below ends up showing -- except a partial fragment that is an
        # exact repeat of the immediately-preceding partial write. Runner's
        # idle_flush_s branch (runner_async.py) re-yields the same
        # unresolved carry unchanged every idle_flush_s while nothing new
        # arrives, purely so the UI keeps a live fragment on screen; writing
        # each of those re-emissions to run.log would fill it with
        # duplicate near-identical lines during a long silent stretch (e.g.
        # `pv` waiting on a slow disk). Finalized lines are never
        # duplicates by construction, so they are always written and always
        # clear the tracked partial so a later new partial stretch compares
        # against nothing stale.
        if ev.partial:
            if ev.text != self._last_partial_text:
                self._write_run_log_line(ev.text)
                self._last_partial_text = ev.text
        else:
            self._write_run_log_line(ev.text)
            self._last_partial_text = None

        sample = parse_pv_line(ev.text) or parse_file_probe_line(ev.text)
        if sample is not None:
            self._sink.post_message(StepProgress(index, sample))

        if ev.partial:
            # A live \r fragment: flush whatever finalized lines are
            # pending first, then post the fragment on its own so a
            # consumer can replace (not append) its previous display of it.
            self._flush_output(index)
            self._sink.post_message(StepOutput(index, (ev.text,), True))
            return

        self._tail_buffer.append(ev.text)
        overflow = len(self._tail_buffer) - self.TAIL_LINES
        if overflow > 0:
            del self._tail_buffer[:overflow]

        self._out_buffer.append(ev.text)
        if len(self._out_buffer) >= self.EMERGENCY_BUFFER_LINES:
            self._flush_output(index)

    def _write_run_log_line(self, line: str) -> None:
        if self._run_log_fh is None:
            return
        ts = datetime.now(timezone.utc).isoformat()
        self._run_log_fh.write(f"{ts} OUT {line}\n")

    def _flush_output(self, index: int) -> None:
        if not self._out_buffer:
            return
        lines = self._out_buffer
        self._out_buffer = []
        capped = _cap_output_lines(lines, self.MAX_BUFFER_LINES, self.KEEP_EDGE_LINES)
        self._sink.post_message(StepOutput(index, tuple(capped), False))
        if self._run_log_fh is not None:
            try:
                self._run_log_fh.flush()
            except OSError:
                pass

    async def _flush_ticker(self, index: int) -> None:
        """Background tick: flushes the output buffer every 100 ms.

        Cancelled from `_run_loop`'s `finally` block once a step's stream
        ends; a final `_flush_output` call there catches whatever
        accumulated since the last tick.
        """
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL_S)
            self._flush_output(index)

    # -- Abort/cancellation (design doc Sec 7.4) ---------------------------

    def _handle_abort(self) -> None:
        """Runs synchronously inside `except asyncio.CancelledError:` in
        `drive()`, with no `await` anywhere in this method -- that is the
        property that keeps these writes safe from a second cancellation
        landing mid-write. Order matches migrationctl.py:589-590 and :596
        exactly: mark_failed, then add_step(FAILED), then finish_run.
        """
        index = self._current_index
        step_id = self._current_step_id
        if step_id is not None and index is not None:
            spec = self._steps[index]
            meta: dict[str, Any] = {
                "script": spec.script,
                "args": list(spec.args),
                "returncode": None,
                "output_tail": list(self._tail_buffer),
                "aborted": True,
            }
            self._state.mark_failed(step_id, meta=meta)
            self._report.add_step(step_id, spec.name, StepStatus.FAILED, details=meta)
            self._report.finish_run(
                success=False, message=f"Run aborted at step: {step_id}"
            )
            self._statuses[index] = StepUiStatus.FAILED
            self._details[index] = "aborted"
            self._sink.post_message(StepFinished(index, False, meta))
        else:
            self._report.finish_run(success=False, message="Run aborted.")

        self._success = False
        self._finished = True
        self._current_index = None
        self._current_step_id = None
        self._sink.post_message(RunFinished(False, "Run aborted."))

    # -- Observer mode (design doc Sec 7.4) --------------------------------

    async def observe(self, interval_s: float = 1.0) -> AsyncIterator[RunView]:
        """Read-only polling loop, for when `.tui.lock` says someone (or
        something) else is already driving this run_dir.

        Never writes state.json, report.json, or run.log, and never
        acquires the lock. Yields `RunView` snapshots built from disk on
        each tick; a torn read (a `json.JSONDecodeError`, or a file that
        does not exist yet) is expected from a live concurrent writer, not
        exceptional -- the previous snapshot is kept rather than raising.
        """
        view = self.snapshot()
        yield view
        while True:
            await asyncio.sleep(interval_s)
            state_data = self._read_json_safe(self._state_path)
            report_data = self._read_json_safe(self._report_path)
            if state_data is not None and report_data is not None:
                view = self._compose_disk_view(state_data, report_data)
            yield view

    @staticmethod
    def _read_json_safe(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _compose_disk_view(self, state_data: dict, report_data: dict) -> RunView:
        steps_state = (state_data or {}).get("steps") or {}
        rows: list[StepRowVM] = []
        for spec in self._steps:
            entry = steps_state.get(spec.id) or {}
            status_str = entry.get("status")
            if status_str == "DONE":
                status = (
                    StepUiStatus.SKIPPED_BY_PHASE
                    if spec.id in self._skips
                    else StepUiStatus.DONE
                )
            elif status_str == "FAILED":
                status = StepUiStatus.FAILED
            else:
                status = StepUiStatus.PENDING
            rows.append(StepRowVM(spec=spec, status=status, detail="", elapsed_s=None))

        finished = report_data.get("finished_at") is not None
        success = report_data.get("success")
        return self._view_from_rows(
            tuple(rows), current_index=None, finished=finished, success=success
        )
