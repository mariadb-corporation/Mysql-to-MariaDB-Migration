"""ThroughputReadout(Widget) per design doc §5.9.

Binds to the latest `ProgressSample` for whichever step is currently
running. Renders `label · percent · rate · ETA`, with each field omitted
when `None` -- fields are never coerced to a fake `0` or `0%`. A
`ProgressSource` tag is appended so the operator knows whether the number
came from `pv`, the script's own file-size probe, a `[done]` completion
line, the TUI's own `stat` poll, or the idle heartbeat. When no sample has
ever arrived for the current step, this renders an honest
`no progress signal from this step` rather than a fabricated 0%.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from orchestrator.tui.models import ProgressSample, ProgressSource

NO_SIGNAL_TEXT = "no progress signal from this step"

_SOURCE_LABEL: dict[ProgressSource, str] = {
    ProgressSource.PV: "pv",
    ProgressSource.FILE_PROBE: "file-probe",
    ProgressSource.HEARTBEAT: "heartbeat",
    ProgressSource.DONE_LINE: "done-line",
    ProgressSource.STAT: "stat",
}


def _format_bytes_rate(value: float) -> str:
    """`value` bytes/sec -> a human string, e.g. `34.2MiB/s`."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    n = float(value)
    for unit in units[:-1]:
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}/s"
        n /= 1024.0
    return f"{n:.1f}{units[-1]}/s"


def _format_eta(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def format_sample(sample: ProgressSample | None) -> str:
    """Render one `ProgressSample` (or `None`) as the readout's text.

    Pure function -- kept separate from the widget so formatting can be
    unit-tested without mounting anything. `None` (no sample has ever
    arrived for the current step) is the one case that does not get a
    source tag: there is no source to name.
    """
    if sample is None:
        return NO_SIGNAL_TEXT

    parts: list[str] = []
    if sample.label:
        parts.append(sample.label)
    if sample.percent is not None:
        parts.append(f"{sample.percent:.0f}%")
    if sample.rate_bytes_s is not None:
        parts.append(_format_bytes_rate(sample.rate_bytes_s))
    if sample.eta_s is not None:
        parts.append(f"ETA {_format_eta(sample.eta_s)}")

    source = _SOURCE_LABEL.get(sample.source, sample.source.value.lower())
    if not parts:
        # Every optional field on this particular sample happens to be
        # None (e.g. a bare pv timer with no -s/-r) -- still an honest
        # signal, just an empty one; distinct from NO_SIGNAL_TEXT, which
        # means no sample has arrived at all.
        return f"(tracking, no fields yet)  [{source}]"
    return " · ".join(parts) + f"  [{source}]"


class ThroughputReadout(Widget):
    """Renders the latest `ProgressSample` for the step currently running.

    `update_sample(None)` (called by `RunScreen` on every `StepStarted`) is
    how a stale sample from the *previous* step is prevented from lingering
    and being mistaken for the new step's progress.
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._sample: ProgressSample | None = None

    def compose(self) -> ComposeResult:
        yield Static(NO_SIGNAL_TEXT, id="readout")

    def update_sample(self, sample: ProgressSample | None) -> None:
        self._sample = sample
        self.query_one("#readout", Static).update(format_sample(sample))

    def reset(self) -> None:
        self.update_sample(None)

    @property
    def sample(self) -> ProgressSample | None:
        return self._sample
