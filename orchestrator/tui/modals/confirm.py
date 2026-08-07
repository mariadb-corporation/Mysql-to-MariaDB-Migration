"""ConfirmModal(ModalScreen[bool]) per design doc §5.1.

Generic reusable yes/no modal. RootUserBlockModal and OfflineAckModal are
thin subclasses that fix the question/body/labels.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm_yes", "Yes", show=False),
        Binding("n", "confirm_no", "No", show=False),
        Binding("escape", "confirm_no", "Cancel", show=False),
        # priority=True: a focused Button's own ENTER/SPACE key handling
        # would otherwise swallow the enter key before this binding runs
        # (verified empirically with a pilot test against 8.2.8).
        Binding("enter", "confirm_default", "Confirm", show=False, priority=True),
    ]

    def __init__(
        self,
        question: str,
        *,
        default: bool = True,
        danger: bool = False,
        body: str | None = None,
        yes_label: str = "Yes",
        no_label: str = "No",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.question = question
        self.default = default
        self.danger = danger
        self.body = body
        self.yes_label = yes_label
        self.no_label = no_label

    def compose(self) -> ComposeResult:
        classes = "confirm-dialog danger" if self.danger else "confirm-dialog"
        with Vertical(id="dialog", classes=classes):
            yield Label(self.question, id="question")
            if self.body is not None:
                yield Static(self.body, id="body")
            yield Button(
                self.yes_label,
                variant="error" if self.danger else "primary",
                id="yes",
            )
            yield Button(self.no_label, id="no")

    def on_mount(self) -> None:
        # ModalScreen auto-focuses the first composed Button ("yes") regardless
        # of self.default -- without this, a modal built with default=False
        # visually highlights "yes" while Enter (action_confirm_default) still
        # dismisses False, a mismatch between what looks selected and what
        # pressing Enter actually does.
        self.query_one("#yes" if self.default else "#no", Button).focus()

    def action_confirm_yes(self) -> None:
        self.dismiss(True)

    def action_confirm_no(self) -> None:
        self.dismiss(False)

    def action_confirm_default(self) -> None:
        self.dismiss(self.default)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")
