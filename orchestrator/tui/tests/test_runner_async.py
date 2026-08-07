"""Tests for orchestrator.tui.runner_async (design doc Sec 7.4, 7.6).

Uses real subprocesses (python3 -c "...") rather than mocks -- this
project's TDD philosophy (design doc Sec 4) is fixture/reality-backed
tests. ``asyncio_mode = auto`` (pytest.ini) means plain ``async def
test_*`` needs no decorator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from orchestrator.tui.runner_async import (
    EventKind,
    StepEvent,
    run_command_async,
    run_step_async,
    split_lines,
)


async def test_normal_exit_merges_stdout_and_stderr_in_order():
    code = (
        "import sys\n"
        "sys.stdout.write('out-line\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err-line\\n'); sys.stderr.flush()\n"
    )
    rc, output = await run_command_async([sys.executable, "-c", code], timeout_s=10.0)
    assert rc == 0
    assert "out-line" in output
    assert "err-line" in output
    assert output.index("out-line") < output.index("err-line")


async def test_non_zero_exit_code_is_returned():
    rc, output = await run_command_async(
        [sys.executable, "-c", "import sys; sys.exit(3)"], timeout_s=10.0
    )
    assert rc == 3


async def test_timeout_raises_and_kills_the_process_group(tmp_path: Path):
    pidfile = tmp_path / "pid"
    code = (
        "import os, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(5)\n"
    )
    with pytest.raises(asyncio.TimeoutError):
        await run_command_async([sys.executable, "-c", code], timeout_s=0.5)

    # Wait briefly for the pidfile to appear if the child was slow to start.
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.1)
    assert pidfile.exists() and pidfile.read_text(), "child never reported its pid"
    pid = int(pidfile.read_text())

    # The process group must be gone: SIGTERM/SIGKILL escalation reaped it.
    for _ in range(50):
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"pid {pid} (process group leader) still alive after timeout teardown")


async def test_race_timeout_after_reap_does_not_call_terminate_group(monkeypatch):
    """Regression test for the reap/PID-recycling race (design doc Sec 7.4):
    if the timeout fires only after the child has already been reaped by
    `communicate()`'s internal `proc.wait()`, `_terminate_group` must NOT be
    called -- calling `os.getpgid()` on an already-reaped PID could, in the
    unlucky case that the OS recycled the PID, signal an unrelated process
    group. This race is impractical to hit deterministically with real
    subprocess timing, so it is simulated with a monkeypatched fake process
    (a deliberate, documented exception to this file's real-subprocess
    convention).
    """
    import orchestrator.tui.runner_async as runner_async

    class FakeProc:
        pid = 12345
        returncode = None

        async def communicate(self):
            # Simulate the child exiting and being reaped just before the
            # timeout is observed by run_command_async.
            self.returncode = 0
            raise asyncio.TimeoutError()

    fake_proc = FakeProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    terminate_calls = []

    async def fake_terminate_group(proc, grace):
        terminate_calls.append(proc)

    monkeypatch.setattr(
        runner_async.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(runner_async, "_terminate_group", fake_terminate_group)

    with pytest.raises(asyncio.TimeoutError):
        await runner_async.run_command_async(["unused"], timeout_s=10.0)

    assert terminate_calls == []


async def test_decode_errors_replace_does_not_raise_on_invalid_utf8():
    code = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe')"
    rc, output = await run_command_async([sys.executable, "-c", code], timeout_s=10.0)
    assert rc == 0
    assert isinstance(output, str)


async def test_start_new_session_puts_child_in_its_own_process_group():
    code = "import os, sys; sys.stdout.write(str(os.getpgid(0)))"
    rc, output = await run_command_async([sys.executable, "-c", code], timeout_s=10.0)
    assert rc == 0
    child_pgid = int(output)
    assert child_pgid != os.getpgid(0)


# --- split_lines (design doc Sec 7.3) -- pure function, its own tests ----


def test_split_lines_crlf_and_lf_terminators():
    assert split_lines("a\r\nb\n", "") == (["a", "b"], "")


def test_split_lines_bare_cr_terminators():
    assert split_lines("x: 10%\rx: 20%\r", "") == (["x: 10%", "x: 20%"], "")


def test_split_lines_no_terminator_returns_all_as_carry():
    assert split_lines("partial", "") == ([], "partial")


def test_split_lines_carry_continuation_across_two_calls():
    lines1, carry1 = split_lines("par", "")
    assert (lines1, carry1) == ([], "par")

    lines2, carry2 = split_lines("tial\n", carry1)
    assert (lines2, carry2) == (["partial"], "")


# --- run_step_async: helpers ----------------------------------------------


async def _collect(stream) -> List[StepEvent]:
    events: List[StepEvent] = []
    async with contextlib.aclosing(stream) as agen:
        async for ev in agen:
            events.append(ev)
    return events


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    """Write a tiny python script with a real, working shebang.

    Deliberately left without the executable bit -- run_step_async's
    best-effort chmod is what's supposed to make it runnable, exactly as it
    would need to for a freshly-checked-out scripts/*.sh.
    """
    script_path = tmp_path / name
    script_path.write_text(f"#!{sys.executable}\n{body}")
    script_path.chmod(script_path.stat().st_mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
    return name


# --- run_step_async: pre-flight -------------------------------------------


async def test_script_not_found_yields_single_error_event(tmp_path: Path):
    events = await _collect(run_step_async(tmp_path, "does_not_exist.sh"))

    assert len(events) == 1
    ev = events[0]
    assert ev.kind == EventKind.ERROR
    assert ev.meta == {
        "error": "script_not_found",
        "script": "does_not_exist.sh",
        "path": str((tmp_path / "does_not_exist.sh").resolve()),
    }


# --- run_step_async: REAL subprocess (design doc + phase2 plan Sec 7) -----
#
# These run an actual script through create_subprocess_exec, not a
# monkeypatched process -- Phase 1's all-monkeypatched subprocess tests let
# a hardcoded "python3" vs sys.executable bug through undetected across 346
# passing tests (tui-phase2-workflow-notes.plan.md Sec 7). The script's own
# shebang line points at sys.executable, so a wrong-interpreter bug in
# run_step_async's argv construction would show up as a real spawn failure
# here, not just in a mock's recorded call args.


async def test_real_subprocess_stdout_and_exit_event(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "hello.py",
        "import sys\nsys.stdout.write('hello-from-real-subprocess\\n')\n",
    )

    events = await _collect(run_step_async(tmp_path, script))

    kinds = [ev.kind for ev in events]
    assert kinds[0] == EventKind.CMD
    assert kinds[-1] == EventKind.EXIT

    out_texts = [ev.text for ev in events if ev.kind == EventKind.OUT]
    assert "hello-from-real-subprocess" in out_texts

    exit_ev = events[-1]
    assert exit_ev.returncode == 0
    assert exit_ev.meta is not None
    assert exit_ev.meta["returncode"] == 0


async def test_real_subprocess_cmd_event_text_matches_run_step_format(tmp_path: Path):
    script = _write_script(tmp_path, "noop.py", "pass\n")

    events = await _collect(run_step_async(tmp_path, script, args=["--flag"]))

    cmd_ev = events[0]
    assert cmd_ev.kind == EventKind.CMD
    assert cmd_ev.text.startswith("CMD ")
    assert "--flag" in cmd_ev.text


async def test_real_subprocess_nonzero_exit_code_in_exit_event(tmp_path: Path):
    script = _write_script(tmp_path, "fails.py", "import sys\nsys.exit(3)\n")

    events = await _collect(run_step_async(tmp_path, script))

    exit_ev = events[-1]
    assert exit_ev.kind == EventKind.EXIT
    assert exit_ev.returncode == 3
    assert exit_ev.meta["returncode"] == 3


async def test_exit_meta_has_exactly_the_run_step_four_keys(tmp_path: Path):
    script = _write_script(tmp_path, "meta_shape.py", "pass\n")

    events = await _collect(run_step_async(tmp_path, script, args=["a", "b"]))

    exit_ev = events[-1]
    assert exit_ev.kind == EventKind.EXIT
    assert set(exit_ev.meta.keys()) == {"script", "args", "returncode", "output_tail"}
    assert exit_ev.meta["script"] == script
    assert exit_ev.meta["args"] == ["a", "b"]


async def test_stdin_devnull_does_not_hang_on_prompting_script(tmp_path: Path):
    # A script that reads stdin must see immediate EOF, not block forever
    # waiting on keystrokes the TUI is reading for itself (design doc Sec 7.3
    # point 2).
    script = _write_script(
        tmp_path,
        "prompts.py",
        (
            "import sys\n"
            "try:\n"
            "    sys.stdin.readline()\n"
            "except Exception:\n"
            "    pass\n"
            "sys.stdout.write('did-not-hang\\n')\n"
        ),
    )

    events = await asyncio.wait_for(_collect(run_step_async(tmp_path, script)), timeout=10.0)

    out_texts = [ev.text for ev in events if ev.kind == EventKind.OUT]
    assert "did-not-hang" in out_texts


# --- run_step_async: redaction on the output path (design doc Sec 7.3) ---


async def test_out_events_redact_secret_env_values(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "echoes_secret.py",
        (
            "import os, sys\n"
            "sys.stdout.write('MYSQL_PWD=' + os.environ['SRC_PASS'] + '\\n')\n"
        ),
    )

    events = await _collect(
        run_step_async(tmp_path, script, extra_env={"SRC_PASS": "supersecretvalue123"})
    )

    out_texts = [ev.text for ev in events if ev.kind == EventKind.OUT]
    joined = "\n".join(out_texts)
    assert "supersecretvalue123" not in joined
    assert "********" in joined


async def test_out_events_leave_non_secret_env_values_unredacted(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "echoes_host.py",
        "import os, sys\nsys.stdout.write(os.environ['SRC_HOST'] + '\\n')\n",
    )

    events = await _collect(
        run_step_async(tmp_path, script, extra_env={"SRC_HOST": "some-db-host"})
    )

    out_texts = [ev.text for ev in events if ev.kind == EventKind.OUT]
    assert "some-db-host" in "\n".join(out_texts)


# --- run_step_async: idle-flush partial events + output_tail (Sec 7.3) ---


async def test_idle_flush_emits_partial_event_then_finalizes(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "pv_like.py",
        (
            "import sys, time\n"
            "sys.stdout.write('progress: 10%')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.3)\n"
            "sys.stdout.write('\\rprogress: 100%\\n')\n"
            "sys.stdout.flush()\n"
        ),
    )

    events = await _collect(run_step_async(tmp_path, script, idle_flush_s=0.05))

    partial_events = [ev for ev in events if ev.kind == EventKind.OUT and ev.partial]
    assert any(ev.text == "progress: 10%" for ev in partial_events)

    finalized_out = [ev.text for ev in events if ev.kind == EventKind.OUT and not ev.partial]
    assert finalized_out == ["progress: 10%", "progress: 100%"]


async def test_output_tail_excludes_partial_fragments(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "pv_like2.py",
        (
            "import sys, time\n"
            "sys.stdout.write('progress: 10%')\n"
            "sys.stdout.flush()\n"
            "time.sleep(0.3)\n"
            "sys.stdout.write('\\rprogress: 100%\\n')\n"
            "sys.stdout.flush()\n"
        ),
    )

    events = await _collect(run_step_async(tmp_path, script, idle_flush_s=0.05))

    # There must be at least one partial fragment emitted (proves the idle
    # path fired), but it must not leak into output_tail.
    assert any(ev.kind == EventKind.OUT and ev.partial for ev in events)

    exit_ev = events[-1]
    assert exit_ev.kind == EventKind.EXIT
    assert exit_ev.meta["output_tail"] == ["progress: 10%", "progress: 100%"]


# --- run_step_async: cancellation tears down the process group (Sec 7.4) -


async def test_cancellation_via_aclosing_terminates_process_group(tmp_path: Path):
    pidfile = tmp_path / "pid"
    script = _write_script(
        tmp_path,
        "sleeper.py",
        f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(5)\n",
    )

    async def consume() -> None:
        async with contextlib.aclosing(run_step_async(tmp_path, script)) as stream:
            async for _ev in stream:
                pass

    task = asyncio.ensure_future(consume())

    for _ in range(50):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.1)
    assert pidfile.exists() and pidfile.read_text(), "child never reported its pid"
    pid = int(pidfile.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(50):
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"pid {pid} (process group leader) still alive after cancellation teardown")
