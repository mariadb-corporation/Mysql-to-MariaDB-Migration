"""ModeSelectScreen(Screen[ModeInfo]) per design doc §4.2."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Collapsible, Footer, Header, Label, OptionList, Static
from textual.widgets.option_list import Option

from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.models import ModeInfo
from orchestrator.tui.modes import MODE_CATALOG


def _make_option(index: int, info: ModeInfo) -> Option:
    text = Text.from_markup(f"{index}  {info.label}\n[dim]   {info.subtitle}[/dim]")
    return Option(text, id=info.key)


class ModeSelectScreen(Screen[ModeInfo]):
    BINDINGS = [
        Binding("1", "select_direct(1)", "1", show=False),
        Binding("2", "select_direct(2)", "2", show=False),
        Binding("3", "select_direct(3)", "3", show=False),
        Binding("4", "select_direct(4)", "4", show=False),
        Binding("d", "toggle_details", "d details", show=True),
        Binding("b", "go_back", "b back", show=True),
        Binding("escape", "go_back", "back", show=False),
        Binding("q", "quit_app", "q quit", show=True),
    ]

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._non_advanced = tuple(m for m in MODE_CATALOG if not m.advanced)
        self._advanced = tuple(m for m in MODE_CATALOG if m.advanced)
        self._details_visible = False

    def compose(self) -> ComposeResult:
        yield Header()
        counter_text = f"{len(self._non_advanced)} modes · {len(self._advanced)} advanced"
        with Vertical(id="body"):
            yield Label(counter_text, id="counter")
            yield OptionList(
                *(
                    _make_option(i + 1, info)
                    for i, info in enumerate(self._non_advanced)
                ),
                id="modes",
            )
            yield Collapsible(
                OptionList(
                    *(
                        _make_option(i + 1, info)
                        for i, info in enumerate(self._advanced)
                    ),
                    id="advanced_modes",
                ),
                title="Advanced: in-place upgrade, replace MySQL slave",
                id="advanced",
            )
            yield Static("", id="details")
        yield Footer()

    @on(OptionList.OptionSelected, "#modes")
    def _on_mode_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._non_advanced[event.option_index])

    @on(OptionList.OptionSelected, "#advanced_modes")
    def _on_advanced_selected(self, event: OptionList.OptionSelected) -> None:
        mode = self._advanced[event.option_index]

        def _callback(confirmed: bool | None) -> None:
            if confirmed:
                self.dismiss(mode)

        self.app.push_screen(
            ConfirmModal(f"{mode.label} is an advanced mode. Continue?"),
            _callback,
        )

    def action_select_direct(self, n: int) -> None:
        index = n - 1
        if 0 <= index < len(self._non_advanced):
            self.dismiss(self._non_advanced[index])

    def action_toggle_details(self) -> None:
        self._details_visible = not self._details_visible
        details = self.query_one("#details", Static)
        if not self._details_visible:
            details.update("")
            return
        modes_list = self.query_one("#modes", OptionList)
        idx = modes_list.highlighted
        if idx is not None and 0 <= idx < len(self._non_advanced):
            details.update(self._non_advanced[idx].subtitle)
        else:
            details.update("")

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_quit_app(self) -> None:
        self.app.exit()
