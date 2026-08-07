"""Tolerant, token-wise parsers for progress-signal lines (§8.1 of
tui-phase0-design.md).

Three independent parsers, none of which ever raise on non-matching input --
each returns ``None`` for a line it doesn't recognize:

- ``parse_pv_line``          -- a ``pv -pet ...`` meter-line update.
- ``parse_file_probe_line``  -- ``25_staged_dump.sh``'s ``file_size_probe()``
  fallback line, used when ``pv`` isn't installed.
- ``parse_done_line``        -- the ``    [done] <db>: ...`` per-database
  completion line emitted by both the staged dump and staged load scripts.

``pv``'s own line shape varies by version (1.6.x vs 1.8.x) and by which flags
resolved (``-N``, ``-b``, ``-r``, ``-s``), so ``parse_pv_line`` matches each
field independently with its own regex rather than one monolithic pattern,
and combines them via the acceptance rule documented on that function.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator.tui.models import ProgressSample, ProgressSource

# --- pv meter-line tokens (§8.1(a)) -------------------------------------
#
# Each token is independently optional on the line; only present when the
# corresponding pv flag resolved. Matched with re.search (not anchored) so
# token order/spacing quirks across pv versions don't matter.

_RE_NAME = re.compile(r"^\s*(?P<name>[^:\[\]]{1,64}):\s")
_RE_TIMER = re.compile(r"(?P<timer>\d+:\d{2}:\d{2})")
_RE_BYTES = re.compile(r"(?P<bytes>[\d.]+\s?[KMGTP]?i?B)\b")
_RE_RATE = re.compile(r"\[\s*(?P<rate>[\d.]+\s?[KMGTP]?i?B/s)\s*\]")
_RE_BAR = re.compile(r"\[[^\]]*[=><][^\]]*\]")
_RE_PCT = re.compile(r"(?P<pct>\d{1,3})%")
_RE_ETA = re.compile(r"ETA\s+(?P<eta>[\d:]+)")

# pv always renders binary (1024-based) magnitudes; some builds print the
# suffix without the "i" (e.g. "5.2MB" meaning 5.2 MiB). Both spellings are
# treated the same way here -- there is no decimal-KB spelling in pv output.
_UNIT_MULT = {
    "": 1.0,
    "K": 1024.0,
    "M": 1024.0**2,
    "G": 1024.0**3,
    "T": 1024.0**4,
    "P": 1024.0**5,
}

_RE_SIZE_TOKEN = re.compile(r"(?P<num>[\d.]+)\s?(?P<unit>[KMGTP]?)i?B")


def _parse_size_token(token: str) -> float | None:
    """Parse a pv-style size/rate token (e.g. ``5.2MiB``, ``10 KB/s`` minus
    the trailing ``/s``) into a raw float count of the base unit. Returns
    None if the token doesn't match the expected shape -- never raises.
    """
    match = _RE_SIZE_TOKEN.match(token)
    if not match:
        return None
    try:
        num = float(match.group("num"))
    except ValueError:
        return None
    return num * _UNIT_MULT.get(match.group("unit"), 1.0)


def _parse_timer_to_seconds(token: str) -> float | None:
    """Parse an ``H:MM:SS``-shaped (or shorter, e.g. ``MM:SS``) token into
    total seconds. Returns None on any non-numeric part rather than raising.
    """
    parts = token.split(":")
    try:
        int_parts = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for part in int_parts:
        seconds = seconds * 60 + part
    return float(seconds)


def parse_pv_line(line: str) -> ProgressSample | None:
    """Parse a single ``pv`` meter-line update into a ``ProgressSample``, or
    ``None`` if the line doesn't look like one.

    ACCEPTANCE RULE (load-bearing): a line only counts as a pv meter line
    when it has a ``timer`` AND (a ``bar`` OR a ``percent``). Without that
    anchor, the ``name`` token alone would false-match an ordinary
    ``mariadb-dump: Warning: ...`` line or an ``ERROR: ...`` line -- both
    look exactly like "name: rest of line" to the name regex.

    Every field is independently optional on the returned sample; a token
    absent from this particular pv build/flag combination is ``None``,
    never ``0``.
    """
    if not line:
        return None

    working = line

    # Pull ETA out first and strip it from the working text: its value is
    # itself an H:MM:SS-shaped token, and would otherwise race the timer
    # regex for the same shape when both are present on one line.
    eta_match = _RE_ETA.search(working)
    eta_s = None
    if eta_match:
        eta_s = _parse_timer_to_seconds(eta_match.group("eta"))
        working = working[: eta_match.start()] + working[eta_match.end() :]

    timer_match = _RE_TIMER.search(working)
    bar_match = _RE_BAR.search(working)
    pct_match = _RE_PCT.search(working)

    if not timer_match or not (bar_match or pct_match):
        return None

    name_match = _RE_NAME.match(working)
    bytes_match = _RE_BYTES.search(working)
    rate_match = _RE_RATE.search(working)

    label = name_match.group("name").strip() if name_match else None
    elapsed_s = _parse_timer_to_seconds(timer_match.group("timer"))
    percent = float(pct_match.group("pct")) if pct_match else None

    bytes_done = None
    if bytes_match:
        size = _parse_size_token(bytes_match.group("bytes"))
        bytes_done = int(size) if size is not None else None

    rate_bytes_s = None
    if rate_match:
        rate_bytes_s = _parse_size_token(rate_match.group("rate"))

    return ProgressSample(
        source=ProgressSource.PV,
        label=label,
        percent=percent,
        elapsed_s=elapsed_s,
        eta_s=eta_s,
        bytes_done=bytes_done,
        rate_bytes_s=rate_bytes_s,
    )


# --- file_size_probe fallback line (§8.1(b)) ----------------------------

_RE_FILE_PROBE = re.compile(
    r"^(?P<name>.+?): writing\.\.\. (?P<bytes>\S+) after (?P<elapsed>\d+)s "
    r"\((?P<rate>\d+) KB/s\)(?: \[(?P<pct>\d+)%\])?$"
)


def parse_file_probe_line(line: str) -> ProgressSample | None:
    """Parse a ``25_staged_dump.sh``'s ``file_size_probe()`` fallback line,
    e.g. ``sakila: writing... 1.2GiB after 120s (10240 KB/s) [44%]``.

    This is the only script-emitted progress source that carries a rate.
    Its percent is capped at 99 by the script's own design (a compressed
    dump size estimate never reaches 100%) -- that 99% is passed through
    as-is; it is not "stuck", just the source format's ceiling.

    Returns ``None`` (never raises) if ``line`` doesn't match the shape.
    """
    if not line:
        return None

    match = _RE_FILE_PROBE.match(line)
    if not match:
        return None

    bytes_done = None
    size = _parse_size_token(match.group("bytes"))
    if size is not None:
        bytes_done = int(size)

    # Script emits KB/s as plain integer KB (1024-based, matching pv).
    rate_bytes_s = float(match.group("rate")) * 1024.0

    pct = match.group("pct")
    percent = float(pct) if pct is not None else None

    return ProgressSample(
        source=ProgressSource.FILE_PROBE,
        label=match.group("name"),
        percent=percent,
        elapsed_s=float(match.group("elapsed")),
        eta_s=None,
        bytes_done=bytes_done,
        rate_bytes_s=rate_bytes_s,
    )


# --- [done] completion line (§8.1(c)) -----------------------------------


@dataclass(frozen=True)
class DoneLineResult:
    """One parsed ``    [done] <db>: <size>, <rows> approx rows, <secs>s``
    completion line, emitted by both ``25_staged_dump.sh:338`` and
    ``26_staged_load.sh``'s equivalent line.

    ``size_bytes`` is the best-effort parse of ``size_text`` into raw bytes
    (via the same binary-unit convention as the pv parsers above); it is
    ``None`` if ``size_text`` didn't match the expected ``<num><unit>B``
    shape, which should not happen for a well-formed line but is not worth
    raising over.
    """

    db: str
    size_text: str
    size_bytes: int | None
    approx_rows: int
    duration_s: int


_RE_DONE_LINE = re.compile(
    r"^\s*\[done\]\s+(?P<db>[^:]+):\s+(?P<size>[\d.]+\s?[KMGTP]?i?B),\s+"
    r"(?P<rows>\d+)\s+approx rows,\s+(?P<dur>\d+)s\s*$"
)


def parse_done_line(line: str) -> DoneLineResult | None:
    """Parse a ``[done]`` per-database completion line into a
    ``DoneLineResult``, or ``None`` if ``line`` doesn't match the shape.
    Never raises.
    """
    if not line:
        return None

    match = _RE_DONE_LINE.match(line)
    if not match:
        return None

    size_text = match.group("size")
    size = _parse_size_token(size_text)

    return DoneLineResult(
        db=match.group("db"),
        size_text=size_text,
        size_bytes=int(size) if size is not None else None,
        approx_rows=int(match.group("rows")),
        duration_s=int(match.group("dur")),
    )
