"""Process-group-aware subprocess helpers (design doc Sec 7.2-7.6).

`_terminate_group` is shared teardown for both async runners in this module:

- `run_command_async` -- the one-shot, non-streaming sibling (Sec 7.6). Used
  for bounded, small CLI invocations: the `assess`/`plan` CLI calls and each
  `SHOW REPLICA STATUS` poll.
- `run_step_async` -- the streaming runner (Sec 7.2-7.4) `RunSession` drives
  each migration step through.

`run_command_async` raises `asyncio.TimeoutError` if `timeout_s` elapses
before the process exits, and re-raises `asyncio.CancelledError` if the
awaiting task is cancelled while waiting on the process. In both cases the
child's whole process group is torn down (SIGTERM, then SIGKILL after
`grace` seconds) before the exception propagates -- callers (screens/assess.py,
screens/plan.py) must catch `asyncio.TimeoutError` themselves; cancellation
propagates to whatever cancelled the awaiting task.

`run_step_async` is an **async generator**, not a callback or a queue (Sec
7.2 explains why: a callback either blocks the event loop or must itself be
awaitable, and a queue needs a sentinel plus a separate producer task and
exception marshalling across the queue boundary -- a generator gives natural
back-pressure and a `finally:` block that runs on cancellation, which is
exactly where process teardown belongs).

**Mandatory consumption pattern -- read this before calling
`run_step_async`.** Plain `async for ev in run_step_async(...):` does **not**
close the generator if the consumer is cancelled mid-stream; cleanup is then
deferred to garbage collection, and the subprocess (plus everything in its
process group -- `mariadb-dump`, `pv`, `gzip`, background dump workers) keeps
running. Every consumer MUST wrap the stream in `contextlib.aclosing`:

    async with contextlib.aclosing(run_step_async(...)) as stream:
        async for ev in stream:
            ...

This is a hard requirement (design doc Sec 7.2), not a style preference.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import shlex
import signal
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Optional, Sequence

from orchestrator.tui.configio import redact_text


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


class EventKind(str, Enum):
    """Kind of `StepEvent` a `run_step_async` stream yields (design doc Sec 7.2)."""

    CMD = "CMD"      # the resolved argv, once, before spawn
    OUT = "OUT"      # one output line (or, if partial, an in-progress \r fragment)
    EXIT = "EXIT"    # process reaped; carries returncode + meta
    ERROR = "ERROR"  # spawn failed (script_not_found, OSError); carries meta


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One event from a `run_step_async` stream (design doc Sec 7.2)."""

    kind: EventKind
    text: str = ""
    partial: bool = False  # provisional \r-fragment, not yet terminated
    returncode: Optional[int] = None
    meta: Optional[Mapping[str, Any]] = None


_LINE_END_RE = re.compile(r"\r\n|\r|\n")


def split_lines(text: str, carry: str) -> tuple[list[str], str]:
    """Split `carry + text` into finalized lines plus a new unterminated carry.

    `\\r\\n`, bare `\\n`, and bare `\\r` are all treated as equally valid line
    terminators (design doc Sec 7.3) -- `pv` rewrites a progress line in
    place with `\\r` and only emits a real `\\n` once, at the end of a
    transfer. Splitting on `\\n` alone (`readline()`, `async for line in
    stream`) would buffer the entire live progress stream until the step
    finished, defeating a live-updating run dashboard, and `StreamReader`'s
    64 KiB line-length cap would eventually raise on a long `\\r`-only
    stream.

    The unterminated tail (text with no terminator yet, e.g. a `pv` fragment
    still being written) is returned as the new `carry`, to be concatenated
    with the next chunk on the following call. Pure function, no I/O.
    """
    combined = carry + text
    lines: list[str] = []
    pos = 0
    for match in _LINE_END_RE.finditer(combined):
        lines.append(combined[pos : match.start()])
        pos = match.end()
    return lines, combined[pos:]


async def run_step_async(
    repo_root: Path,
    script: str,
    args: Optional[Sequence[str]] = None,
    extra_env: Optional[Mapping[str, str]] = None,
    *,
    tail_lines: int = 50,
    read_chunk: int = 65536,
    idle_flush_s: float = 0.5,
    kill_grace_s: float = 5.0,
    on_spawn: Optional[Callable[["asyncio.subprocess.Process"], None]] = None,
) -> AsyncIterator[StepEvent]:
    """Run one migration step script, streaming its output (design doc Sec 7.2-7.4).

    Positional parameters 1-4 mirror `orchestrator.runner.run_step(repo_root,
    script, args, extra_env, log)`; the fifth (`log`) is replaced by the
    stream itself. This is **not** a superset of `run_step` and is not meant
    to be swappable into `migrationctl.py` -- it yields a stream, not a
    `(bool, dict)` tuple. The one contract kept byte-for-byte identical is
    the `EXIT` event's `meta` dict (see below), so `state.json`/`report.json`
    look the same whichever runner produced them.

    **Mandatory consumption pattern.** A plain `async for ev in
    run_step_async(...):` does not close this generator if the consumer is
    cancelled mid-stream -- the `finally:` block below (which tears the
    subprocess down) would then run only at garbage-collection time, leaving
    the child (and its whole process group) running. Every caller MUST use:

        async with contextlib.aclosing(run_step_async(...)) as stream:
            async for ev in stream:
                ...

    Pre-flight is identical to `run_step`: resolve
    `script_path = (repo_root / script).resolve()`; if it does not exist,
    yield a single `StepEvent(ERROR, meta={"error": "script_not_found",
    "script": script, "path": str(script_path)})` and return -- the same
    meta shape `run_step` returns on the same failure. `chmod +x` is
    attempted best-effort in a bare `try/except`, same as `run_step`.

    The child is spawned with `stderr=STDOUT` (pv writes its meter to
    stderr and scripts rely on it being merged with stdout), `stdin=DEVNULL`
    (a script that prompts must not hang reading the TUI's keystrokes), and
    `start_new_session=True` (puts the child in its own process group, which
    is what makes `_terminate_group`'s `killpg`-based abort safe -- see
    design doc Sec 7.4). `on_spawn(proc)`, if given, runs immediately after
    spawn so a caller can record the pid/pgid for out-of-band teardown.

    Output is read in fixed-size chunks (`proc.stdout.read(read_chunk)`),
    never with `readline()` or `async for line in proc.stdout` -- both split
    only on `\\n`, and `pv` writes its live progress with bare `\\r`,
    emitting a real `\\n` only once the transfer finishes. Reading by line
    would buffer the entire live progress stream until the step completed
    (defeating a live run dashboard) and would eventually hit
    `StreamReader`'s 64 KiB line-length cap on a long `\\r`-only stream.
    `split_lines()` (this module) does the actual splitting.

    If no chunk arrives within `idle_flush_s` (default 0.5s), the current
    unterminated `carry` is yielded as `StepEvent(OUT, text=carry,
    partial=True)` *without* clearing `carry` -- this is what makes a live
    `pv` fragment visible before its line terminates; a consumer should
    replace, not accumulate, a `partial=True` OUT event's displayed text.

    Every `OUT` event's `text` (partial or not) passes through
    `configio.redact_text()` against `extra_env` before it leaves this
    generator, so secrets in echoed command lines / SQLines configs never
    reach the log or the TUI.

    On `EXIT`, `meta` is byte-for-byte the same 4-key shape as
    `run_step`'s: `{"script", "args", "returncode", "output_tail"}` -- no
    TUI-only extra keys, since this dict is persisted into `state.json` and
    `report.json` and read by `migrationctl._failure_hint_from_meta`.
    `output_tail` is built only from finalized (non-`partial`) lines --
    including `pv` fragments would fill it with meter noise and bury the
    `"ERROR:"` text that hint-matching looks for.

    On cancellation (including `idle_flush_s`/read `asyncio.TimeoutError`
    propagating as `CancelledError` from the caller side) or after the
    normal exit path, the `finally:` block reaps/tears down the child via
    the module's `_terminate_group` if it is still running -- never
    reimplemented here.
    """
    args = list(args) if args is not None else []
    secrets: Mapping[str, str] = extra_env or {}
    script_path = (repo_root / script).resolve()

    if not script_path.exists():
        yield StepEvent(
            kind=EventKind.ERROR,
            meta={
                "error": "script_not_found",
                "script": script,
                "path": str(script_path),
            },
        )
        return

    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except Exception:
        pass

    cmd = [str(script_path)] + args
    env = {**os.environ, **secrets}

    proc: Optional[asyncio.subprocess.Process] = None
    out_lines: list[str] = []
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo_root),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            yield StepEvent(
                kind=EventKind.ERROR,
                meta={
                    "error": "spawn_failed",
                    "script": script,
                    "path": str(script_path),
                    "detail": str(exc),
                },
            )
            return

        if on_spawn is not None:
            on_spawn(proc)

        yield StepEvent(
            kind=EventKind.CMD,
            text="CMD " + " ".join(shlex.quote(c) for c in cmd),
        )

        assert proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        carry = ""
        while True:
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(read_chunk), idle_flush_s
                )
            except asyncio.TimeoutError:
                if carry:
                    yield StepEvent(
                        kind=EventKind.OUT,
                        text=redact_text(carry, secrets),
                        partial=True,
                    )
                continue
            if not chunk:
                break
            lines, carry = split_lines(decoder.decode(chunk), carry)
            for line in lines:
                redacted = redact_text(line, secrets)
                out_lines.append(redacted)
                yield StepEvent(kind=EventKind.OUT, text=redacted)

        if carry:
            redacted = redact_text(carry, secrets)
            out_lines.append(redacted)
            yield StepEvent(kind=EventKind.OUT, text=redacted)

        rc = await proc.wait()
        meta = {
            "script": script,
            "args": args,
            "returncode": rc,
            "output_tail": out_lines[-tail_lines:],
        }
        yield StepEvent(kind=EventKind.EXIT, returncode=rc, meta=meta)
    finally:
        if proc is not None and proc.returncode is None:
            await _terminate_group(proc, kill_grace_s)
