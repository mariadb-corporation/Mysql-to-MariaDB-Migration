"""Pilot tests for orchestrator.tui.widgets.check_table.CheckTable."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from orchestrator.tui.models import CheckKind, CheckLevel, CheckRow
from orchestrator.tui.widgets.check_table import CheckTable


def _row(name, level, summary="", details=None, kind=CheckKind.GATE):
    return CheckRow(
        kind=kind,
        name=name,
        level=level,
        summary=summary,
        details=details or {},
        expandable=bool(details),
    )


class _CheckTableApp(App):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.highlighted_messages: list[CheckTable.RowHighlighted] = []

    def compose(self) -> ComposeResult:
        yield CheckTable()

    def on_mount(self) -> None:
        self.query_one(CheckTable).load(self.rows)

    def on_check_table_row_highlighted(self, message: "CheckTable.RowHighlighted") -> None:
        self.highlighted_messages.append(message)


@pytest.mark.asyncio
async def test_load_renders_one_row_per_check_row():
    rows = (
        _row("mysql_version_supported", CheckLevel.FAIL, "unsupported version"),
        _row("innodb_file_per_table_is_1", CheckLevel.PASS, "ok"),
        _row("some_warning", CheckLevel.MEDIUM, "watch out"),
    )
    app = _CheckTableApp(rows)
    async with app.run_test() as pilot:
        table = pilot.app.query_one(DataTable)
        assert table.row_count == 3


@pytest.mark.asyncio
async def test_fail_and_pass_icons_and_styles_differ():
    rows = (
        _row("mysql_version_supported", CheckLevel.FAIL, "unsupported version"),
        _row("innodb_file_per_table_is_1", CheckLevel.PASS, "ok"),
    )
    app = _CheckTableApp(rows)
    async with app.run_test() as pilot:
        table = pilot.app.query_one(DataTable)
        fail_cell = table.get_cell_at((0, 0))
        pass_cell = table.get_cell_at((1, 0))
        assert str(fail_cell) != str(pass_cell)
        assert fail_cell.style != pass_cell.style


@pytest.mark.asyncio
async def test_high_and_medium_share_glyph_but_differ_in_style():
    rows = (
        _row("high_check", CheckLevel.HIGH, "risky"),
        _row("medium_check", CheckLevel.MEDIUM, "meh"),
    )
    app = _CheckTableApp(rows)
    async with app.run_test() as pilot:
        table = pilot.app.query_one(DataTable)
        high_cell = table.get_cell_at((0, 0))
        medium_cell = table.get_cell_at((1, 0))
        assert high_cell.plain == medium_cell.plain
        assert high_cell.style != medium_cell.style


@pytest.mark.asyncio
async def test_highlighting_row_updates_highlighted_row_property():
    rows = (
        _row("mysql_version_supported", CheckLevel.FAIL, "unsupported version", details={"a": 1}),
        _row("innodb_file_per_table_is_1", CheckLevel.PASS, "ok"),
    )
    app = _CheckTableApp(rows)
    async with app.run_test() as pilot:
        check_table = pilot.app.query_one(CheckTable)
        table = pilot.app.query_one(DataTable)
        table.focus()
        await pilot.pause()
        table.move_cursor(row=1)
        await pilot.pause()
        assert check_table.highlighted_row is not None
        assert check_table.highlighted_row.name == "innodb_file_per_table_is_1"
        assert app.highlighted_messages
        assert app.highlighted_messages[-1].row.name == "innodb_file_per_table_is_1"


@pytest.mark.asyncio
async def test_load_replaces_rows_not_appends():
    first = (_row("a", CheckLevel.PASS),)
    second = (_row("b", CheckLevel.FAIL), _row("c", CheckLevel.LOW))
    app = _CheckTableApp(first)
    async with app.run_test() as pilot:
        check_table = pilot.app.query_one(CheckTable)
        check_table.load(second)
        await pilot.pause()
        table = pilot.app.query_one(DataTable)
        assert table.row_count == 2
