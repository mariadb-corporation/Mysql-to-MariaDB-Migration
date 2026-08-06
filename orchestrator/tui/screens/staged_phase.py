"""StagedPhaseScreen(Screen[str]) per design doc §4.3.

Only reached for mode.key == "staged" -- a fact for the caller, not
something this screen enforces itself.
"""

from __future__ import annotations

import os
from typing import Mapping

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from orchestrator.tui.modals.offline_ack import OfflineAckModal
from orchestrator.tui.modes import expected_skips

_PHASES: tuple[tuple[str, str, str], ...] = (
    ("dump_and_load", "Dump and load", "Full end-to-end on this host (default)."),
    ("dump_only", "Dump only", "Produce dump files on disk, then exit."),
    ("load_only", "Load only", "Load an existing dump into the target."),
)


def _offline_ack_supplied(env: Mapping[str, str] | None = None) -> bool:
    # mariadb-migrator's `[[ -z "$STAGED_CONFIRM_OFFLINE" ]]` check: any
    # non-empty value counts as supplied, not just "1".
    source = os.environ if env is None else env
    return bool(source.get("STAGED_CONFIRM_OFFLINE", ""))


def _make_option(label: str, description: str, value: str) -> Option:
    text = Text.from_markup(f"{label}\n[dim]{description}[/dim]")
    return Option(text, id=value)


class StagedPhaseScreen(Screen[str]):
    BINDINGS = [
        Binding("escape", "go_back", "back", show=True),
    ]

    def __init__(
        self,
        id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(id=id)
        self._env = env

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield OptionList(
                *(_make_option(label, desc, value) for value, label, desc in _PHASES),
                id="phases",
            )
            yield Static("", id="effect")
        yield Footer()

    def on_mount(self) -> None:
        self._update_effect(0)

    def _update_effect(self, index: int) -> None:
        phase = _PHASES[index][0]
        skips = expected_skips("staged", {"STAGED_PHASE": phase})
        effect = self.query_one("#effect", Static)
        if skips:
            effect.update(f"will skip: {', '.join(sorted(skips))}")
        else:
            effect.update("no steps will be skipped")

    @on(OptionList.OptionHighlighted, "#phases")
    def _on_phase_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._update_effect(event.option_index)

    @on(OptionList.OptionSelected, "#phases")
    def _on_phase_selected(self, event: OptionList.OptionSelected) -> None:
        phase = _PHASES[event.option_index][0]
        needs_ack = phase in ("dump_and_load", "dump_only")
        if needs_ack and not _offline_ack_supplied(self._env):

            def _callback(confirmed: bool | None) -> None:
                if confirmed:
                    self.dismiss(phase)
                else:
                    # Nav graph §5.10: OfflineAckModal --No--> ModeSelectScreen,
                    # i.e. pop past this screen too, not just the modal.
                    # Screen.dismiss() calls this callback *before* its own
                    # trailing self.app.pop_screen() that removes the modal --
                    # so the pop_screen() here removes the modal (still on
                    # stack at this point) and dismiss()'s own pop then
                    # removes this StagedPhaseScreen, landing one level back
                    # on ModeSelectScreen. Verified empirically: without this
                    # call, decline would incorrectly leave StagedPhaseScreen
                    # on top.
                    self.app.pop_screen()

            self.app.push_screen(OfflineAckModal(), _callback)
        else:
            self.dismiss(phase)

    def action_go_back(self) -> None:
        self.app.pop_screen()
