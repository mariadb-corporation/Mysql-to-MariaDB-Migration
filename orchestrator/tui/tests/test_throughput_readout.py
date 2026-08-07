"""Pilot tests for orchestrator.tui.widgets.throughput_readout.ThroughputReadout."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from orchestrator.tui.models import ProgressSample, ProgressSource
from orchestrator.tui.widgets.throughput_readout import (
    NO_SIGNAL_TEXT,
    ThroughputReadout,
    format_sample,
)


def _sample(**kwargs) -> ProgressSample:
    defaults = dict(
        source=ProgressSource.PV,
        label=None,
        percent=None,
        elapsed_s=None,
        eta_s=None,
        bytes_done=None,
        rate_bytes_s=None,
    )
    defaults.update(kwargs)
    return ProgressSample(**defaults)


class _ReadoutApp(App):
    def compose(self) -> ComposeResult:
        yield ThroughputReadout(id="tput")


def test_format_sample_none_is_honest_empty_state():
    assert format_sample(None) == NO_SIGNAL_TEXT


def test_format_sample_omits_none_fields_and_tags_source():
    sample = _sample(label="sakila", percent=44.0, source=ProgressSource.PV)
    text = format_sample(sample)
    assert "sakila" in text
    assert "44%" in text
    assert "[pv]" in text
    # No rate/ETA were provided -- must not render "None" anywhere.
    assert "None" not in text


def test_format_sample_renders_rate_and_eta_when_present():
    sample = _sample(
        label="dump",
        percent=50.0,
        rate_bytes_s=34_000_000.0,
        eta_s=125.0,
        source=ProgressSource.FILE_PROBE,
    )
    text = format_sample(sample)
    assert "50%" in text
    assert "MiB/s" in text
    assert "ETA 2:05" in text
    assert "[file-probe]" in text


def test_format_sample_all_fields_none_is_distinct_from_no_sample():
    sample = _sample(source=ProgressSource.STAT)
    text = format_sample(sample)
    assert text != NO_SIGNAL_TEXT
    assert "[stat]" in text


@pytest.mark.asyncio
async def test_widget_starts_with_no_signal_text():
    app = _ReadoutApp()
    async with app.run_test() as pilot:
        static = pilot.app.query_one("#tput", ThroughputReadout).query_one("#readout", Static)
        assert str(static.render()) == NO_SIGNAL_TEXT


@pytest.mark.asyncio
async def test_update_sample_then_reset_round_trips():
    app = _ReadoutApp()
    async with app.run_test() as pilot:
        readout = pilot.app.query_one("#tput", ThroughputReadout)
        sample = _sample(label="sakila", percent=10.0, source=ProgressSource.DONE_LINE)
        readout.update_sample(sample)
        await pilot.pause()
        static = readout.query_one("#readout", Static)
        assert "sakila" in str(static.render())
        assert readout.sample is sample

        readout.reset()
        await pilot.pause()
        assert str(static.render()) == NO_SIGNAL_TEXT
        assert readout.sample is None
