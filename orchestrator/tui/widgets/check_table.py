"""CheckTable(Widget): wraps a DataTable#checks per design doc's AssessScreen.

DataTable.add_row takes plain *cells: CellType with no per-row CSS class
parameter (textual 8.2.8) -- the design doc's "row-level CSS classes drive
colour" phrase doesn't map onto a real API. Icon + colour are instead
carried per-cell as a Rich Text(..., style=...), which is the actual
mechanism DataTable supports for this.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable

from orchestrator.tui.models import CheckLevel, CheckRow

_ICON_STYLE = {
    CheckLevel.PASS: ("✓", "green"),
    CheckLevel.FAIL: ("✗", "red"),
    CheckLevel.HIGH: ("●", "bright_red"),
    CheckLevel.MEDIUM: ("●", "dim yellow"),
    CheckLevel.LOW: ("·", "grey62"),
}


def _icon_cell(level: CheckLevel) -> Text:
    icon, style = _ICON_STYLE[level]
    return Text(icon, style=style)


class CheckTable(Widget):
    class RowHighlighted(Message):
        def __init__(self, check_table: "CheckTable", row: CheckRow | None) -> None:
            super().__init__()
            self.check_table = check_table
            self.row = row

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._rows_by_key: dict[str, CheckRow] = {}
        self._highlighted_row: CheckRow | None = None

    def compose(self) -> ComposeResult:
        table = DataTable(id="checks", cursor_type="row")
        table.add_columns("", "name", "level", "summary")
        yield table

    def load(self, rows: tuple[CheckRow, ...]) -> None:
        table = self.query_one("#checks", DataTable)
        table.clear()
        self._rows_by_key = {}
        self._highlighted_row = None
        for i, row in enumerate(rows):
            key = str(i)
            table.add_row(
                _icon_cell(row.level),
                row.name,
                row.level.value,
                row.summary,
                key=key,
            )
            self._rows_by_key[key] = row

    @property
    def highlighted_row(self) -> CheckRow | None:
        return self._highlighted_row

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        row = self._rows_by_key.get(event.row_key.value)
        self._highlighted_row = row
        self.post_message(self.RowHighlighted(self, row))
