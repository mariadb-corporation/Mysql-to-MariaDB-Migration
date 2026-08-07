"""ResumeChoiceModal(ModalScreen[str]) per design doc §5.4.

Renders a pre-computed ResumeDecision. Does not call rundir.resume_decision()
itself and knows nothing about ConfigDraft/mode selection -- the caller
(WelcomeScreen, not yet built) owns that.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from orchestrator.tui.models import ResumeDecision


class ResumeChoiceModal(ModalScreen[str]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, decision: ResumeDecision, id: str | None = None) -> None:
        super().__init__(id=id)
        self.decision = decision

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("A previous run was found.", id="heading")
            if not self.decision.can_resume:
                yield Static(self.decision.message, id="reason")
            yield Button("Resume", id="resume", disabled=not self.decision.can_resume)
            yield Button("Start fresh run", id="fresh")
            yield Button("Cancel", id="cancel")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        self.dismiss(event.button.id)
