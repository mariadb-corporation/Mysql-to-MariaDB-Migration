"""Tests for orchestrator/tui/pvparse.py (TDD unit 2, §8.1).

All fixture-driven: every assertion here loads real bytes from
orchestrator/tui/tests/fixtures/pv/ rather than hand-writing stand-in
strings, per the fixtures' own README (which documents provenance of each
file -- real pv capture vs hand-authored-but-format-accurate vs
script-exact reproduction).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.tui.models import ProgressSource
from orchestrator.tui.pvparse import (
    parse_done_line,
    parse_file_probe_line,
    parse_pv_line,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pv"


def _load(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


# --- pv meter lines: real captures --------------------------------------


def test_pv_1_6_capture_four_updates_parsed():
    """fixture_staged_dump_1.6.txt: real pv 1.6.6 capture, `-N sakila -s ...`,
    \\r-delimited updates plus a final \\r\\n-terminated 100% line with
    trailing padding spaces and no ETA.
    """
    text = _load("fixture_staged_dump_1.6.txt")
    lines = text.splitlines()
    assert len(lines) == 4

    samples = [parse_pv_line(line) for line in lines]
    assert all(s is not None for s in samples)

    expected = [
        (10.0, 25.0, 30.0),
        (20.0, 50.0, 20.0),
        (30.0, 75.0, 10.0),
        (39.0, 100.0, None),
    ]
    for sample, (elapsed_s, percent, eta_s) in zip(samples, expected):
        assert sample.source is ProgressSource.PV
        assert sample.label == "sakila"
        assert sample.elapsed_s == elapsed_s
        assert sample.percent == percent
        assert sample.eta_s == eta_s
        assert sample.bytes_done is None
        assert sample.rate_bytes_s is None


def test_pv_1_8_capture_two_updates_parsed():
    """fixture_staged_dump_1.8.txt: hand-authored to the same field grammar
    as the real 1.6.x capture (no pv 1.8.x host was available)."""
    text = _load("fixture_staged_dump_1.8.txt")
    lines = text.splitlines()
    assert len(lines) == 2

    samples = [parse_pv_line(line) for line in lines]
    assert all(s is not None for s in samples)

    expected = [
        (10.0, 75.0, 3.0),
        (13.0, 100.0, None),
    ]
    for sample, (elapsed_s, percent, eta_s) in zip(samples, expected):
        assert sample.source is ProgressSource.PV
        assert sample.label == "sakila"
        assert sample.elapsed_s == elapsed_s
        assert sample.percent == percent
        assert sample.eta_s == eta_s


def test_pv_one_step_no_name_no_size_no_rate():
    """fixture_one_step.txt: real capture, `pv -pet -f -i 10` with no -N/-s.
    No name token (no -N); the final line does carry a bar/percent (pv
    marks 100% completion on EOF even without -s), so this line is a valid
    pv meter line, but bytes/rate stay None since -b/-r were never passed.
    """
    text = _load("fixture_one_step.txt")
    lines = text.splitlines()
    assert len(lines) == 1

    sample = parse_pv_line(lines[0])
    assert sample is not None
    assert sample.source is ProgressSource.PV
    assert sample.label is None
    assert sample.elapsed_s == 7.0
    assert sample.percent == 100.0
    assert sample.eta_s is None
    assert sample.bytes_done is None
    assert sample.rate_bytes_s is None


def test_pv_staged_load_fixture_is_empty_and_parser_handles_empty_input():
    """fixture_staged_load.txt: real capture with `-f` omitted and stderr
    piped to a file (not a tty) -- pv suppresses all output entirely, so
    this fixture is intentionally empty. The assertion here is that the
    parser tolerates an empty string with no error, not that it produces
    any particular sample.
    """
    text = _load("fixture_staged_load.txt")
    assert text == ""
    assert text.splitlines() == []
    assert parse_pv_line(text) is None
    assert parse_pv_line("") is None


# --- negative cases: the acceptance rule's whole reason to exist -------


def test_negative_fixture_lines_all_parse_to_none():
    """fixture_negative.txt: a mariadb-dump warning, an ERROR: line, a
    CREATE TABLE with a literal '100%' inside a string default, and a bare
    timestamp line. Each looks superficially like a pv meter line to a
    naive regex (colon-name, a percent sign, or an H:MM:SS-shaped
    substring) but must parse to None because none of them carry a timer
    AND (bar OR percent) together -- the acceptance rule this module is
    built around.
    """
    text = _load("fixture_negative.txt")
    lines = text.splitlines()
    assert len(lines) == 4

    for line in lines:
        assert parse_pv_line(line) is None, line
        assert parse_file_probe_line(line) is None, line
        assert parse_done_line(line) is None, line


def test_negative_fixture_specific_lines_documented():
    """Pin down which line is which, so a future edit to the fixture that
    accidentally drops one of the four documented cases fails loudly."""
    lines = _load("fixture_negative.txt").splitlines()
    assert "mariadb-dump: Warning:" in lines[0]
    assert lines[1].startswith("ERROR:")
    assert "100%" in lines[2] and "CREATE TABLE" in lines[2]
    assert "12:34:56" in lines[3]


# --- file_size_probe fallback lines -------------------------------------


def test_file_size_probe_lines_parsed():
    """fixture_file_size_probe.txt reproduces 25_staged_dump.sh's
    file_size_probe() format exactly -- a deterministic bash echo, not a
    third-party tool quirk."""
    lines = _load("fixture_file_size_probe.txt").splitlines()
    assert len(lines) == 3

    samples = [parse_file_probe_line(line) for line in lines]
    assert all(s is not None for s in samples)

    expected = [
        ("sakila", 120.0, 10485760.0, 1288490188, 44.0),
        ("sakila", 240.0, 10065920.0, 2254857830, 77.0),
        ("sakila", 360.0, 8089600.0, 2899102924, 99.0),
    ]
    for sample, (name, elapsed_s, rate_bytes_s, bytes_done, percent) in zip(
        samples, expected
    ):
        assert sample.source is ProgressSource.FILE_PROBE
        assert sample.label == name
        assert sample.elapsed_s == elapsed_s
        assert sample.rate_bytes_s == rate_bytes_s
        assert sample.bytes_done == bytes_done
        assert sample.percent == percent
        assert sample.eta_s is None


def test_file_size_probe_99_percent_is_not_stuck():
    """The script never emits 100% by design (its estimate always caps at
    99) -- the parser must pass 99 through as a plain number, not treat it
    as a special "stuck" sentinel or clamp/round it to 100."""
    lines = _load("fixture_file_size_probe.txt").splitlines()
    last = parse_file_probe_line(lines[-1])
    assert last is not None
    assert last.percent == 99.0


def test_file_size_probe_line_without_percent_suffix():
    """The trailing [NN%] group is optional in the documented regex."""
    line = "sakila: writing... 500MiB after 30s (5000 KB/s)"
    sample = parse_file_probe_line(line)
    assert sample is not None
    assert sample.percent is None
    assert sample.label == "sakila"
    assert sample.elapsed_s == 30.0
    assert sample.rate_bytes_s == 5000 * 1024.0


def test_file_size_probe_rejects_non_matching_line():
    assert parse_file_probe_line("not a probe line at all") is None
    assert parse_file_probe_line("") is None


# --- [done] completion lines ---------------------------------------------


def test_done_lines_parsed():
    """fixture_done_lines.txt reproduces 25_staged_dump.sh:338's [done] line
    format exactly, for three databases of varying size units."""
    lines = _load("fixture_done_lines.txt").splitlines()
    assert len(lines) == 3

    results = [parse_done_line(line) for line in lines]
    assert all(r is not None for r in results)

    expected = [
        ("sakila", "1.4GiB", 1503238553, 9900000, 412),
        ("world", "45.2MiB", 47395635, 120000, 18),
        ("employees", "892.1MiB", 935434649, 4000000, 96),
    ]
    for result, (db, size_text, size_bytes, rows, dur) in zip(results, expected):
        assert result.db == db
        assert result.size_text == size_text
        assert result.size_bytes == size_bytes
        assert result.approx_rows == rows
        assert result.duration_s == dur


def test_done_line_rejects_non_matching_line():
    assert parse_done_line("not a done line") is None
    assert parse_done_line("") is None
    assert parse_done_line("[done] missing leading whitespace and fields") is None


@pytest.mark.parametrize(
    "line",
    [
        "mariadb-dump: Warning: Using a password on the command line interface can be insecure.",
        "ERROR: dump failed for database 'x' (partial file removed)",
    ],
)
def test_all_three_parsers_reject_the_same_non_progress_lines(line):
    assert parse_pv_line(line) is None
    assert parse_file_probe_line(line) is None
    assert parse_done_line(line) is None
