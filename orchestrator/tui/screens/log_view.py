"""LogScreen(ModalScreen[None]) per design doc §4.10.

Reads ``<run_dir>/run.log`` straight from disk at mount time rather than any
in-memory buffer, so it stays complete even after a RichLog's own max_lines
truncation, and so it keeps working from SummaryScreen long after the
RunScreen worker that produced the log is gone.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog

_NO_LOG_PLACEHOLDER = "(no run.log found in this run directory)"


class LogScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("g", "top", "top", show=True),
        Binding("G", "bottom", "bottom", show=True),
        Binding("escape", "close", "close", show=True),
    ]

    def __init__(self, run_dir: Path, log_filename: str = "run.log", id: str | None = None) -> None:
        super().__init__(id=id)
        self.run_dir = run_dir
        self.log_filename = log_filename
        self._lines: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Input(placeholder="filter...", id="filter")
            yield RichLog(id="full", highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        log_path = self.run_dir / self.log_filename
        try:
            self._lines = log_path.read_text(errors="replace").splitlines()
        except OSError:
            self._lines = []
        self._redraw("")

    def _redraw(self, filter_text: str) -> None:
        log = self.query_one("#full", RichLog)
        log.clear()
        if not self._lines:
            log.write(f"(no {self.log_filename} found in this run directory)")
            return
        needle = filter_text.strip()
        for line in self._lines:
            if not needle or needle in line:
                log.write(line)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._redraw(event.value)

    def action_top(self) -> None:
        self.query_one("#full", RichLog).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one("#full", RichLog).scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)
