"""Process-group-aware subprocess helpers (design doc Sec 7.4, 7.6).

`_terminate_group` and `run_command_async` are the one-shot sibling of
`run_step_async` (Phase 2, not built here). They exist for bounded, non-
streaming CLI invocations: the `assess`/`plan` CLI calls and each
`SHOW REPLICA STATUS` poll.

`run_command_async` raises `asyncio.TimeoutError` if `timeout_s` elapses
before the process exits, and re-raises `asyncio.CancelledError` if the
awaiting task is cancelled while waiting on the process. In both cases the
child's whole process group is torn down (SIGTERM, then SIGKILL after
`grace` seconds) before the exception propagates -- callers (screens/assess.py,
screens/plan.py) must catch `asyncio.TimeoutError` themselves; cancellation
propagates to whatever cancelled the awaiting task.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Mapping, Sequence


async def _terminate_group(proc: asyncio.subprocess.Process, grace: float) -> None:
    """Escalate SIGTERM -> SIGKILL to `proc`'s whole process group.

    No-op if the process group is already gone. Returns as soon as `proc`
    is reaped, or after both signals have been sent and their grace
    periods have elapsed.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.shield(asyncio.wait_for(proc.wait(), grace))
            return
        except (asyncio.TimeoutError, asyncio.CancelledError):
            continue


async def run_command_async(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 300.0,
    grace: float = 5.0,
) -> tuple[int, str]:
    """Run `argv` to completion, merging stdout/stderr into one string.

    No streaming -- callers get the whole decoded output at once, which is
    correct for the bounded, small calls this is used for (assess/plan CLI
    invocations, SHOW REPLICA STATUS polls). Decoding uses errors="replace"
    so invalid bytes never raise `UnicodeDecodeError`.

    Raises `asyncio.TimeoutError` if the process does not exit within
    `timeout_s`, and re-raises `asyncio.CancelledError` if cancelled while
    waiting. In both cases the process's whole group is signalled (SIGTERM
    then SIGKILL, configurable `grace` seconds, default 5.0) before the
    exception propagates, so no orphaned process group is left behind.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        raw_output, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            await _terminate_group(proc, grace=grace)
        raise
    returncode = proc.returncode
    assert returncode is not None
    return returncode, raw_output.decode("utf-8", errors="replace")
