"""Tests for orchestrator.tui.runner_async (design doc Sec 7.4, 7.6).

Uses real subprocesses (python3 -c "...") rather than mocks -- this
project's TDD philosophy (design doc Sec 4) is fixture/reality-backed
tests. ``asyncio_mode = auto`` (pytest.ini) means plain ``async def
test_*`` needs no decorator.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
import pytest
from orchestrator.tui.runner_async import run_command_async


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
