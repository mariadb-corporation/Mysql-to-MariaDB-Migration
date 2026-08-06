"""StepList(Widget) / StepRow(Widget) per design doc §5.7.

Vertical list, one StepRow per StepRowVM. A DataTable was considered and
rejected in the design doc: two-line rows with per-row styling are awkward
in DataTable, and this list is bounded (10 rows max, replace_slave).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Static

from orchestrator.tui.models import StepRowVM, StepUiStatus

_ICONS = {
    StepUiStatus.PENDING: "○",
    StepUiStatus.RUNNING: "▶",
    StepUiStatus.DONE: "✓",
    StepUiStatus.FAILED: "✗",
    StepUiStatus.SKIPPED_RESUME: "✓",
    StepUiStatus.SKIPPED_BY_PHASE: "⊘",
}


class StepRow(Widget):
    def __init__(self, vm: StepRowVM, id: str | None = None) -> None:
        super().__init__(id=id)
        self.vm = vm

    def compose(self) -> ComposeResult:
        icon = _ICONS[self.vm.status]
        status_class = f"status-{self.vm.status.value.lower()}"
        yield Static(
            f"{icon}  {self.vm.spec.name}",
            classes=f"step-name {status_class}",
        )
        yield Static(self.vm.detail, classes="step-detail")


class StepList(Widget):
    def compose(self) -> ComposeResult:
        yield Vertical(id="rows")

    async def update_rows(self, rows: tuple[StepRowVM, ...]) -> None:
        # remove_children()/mount() must both be awaited: DOM removal happens
        # via an internally posted Prune message, processed on a later
        # message-pump turn, not synchronously when remove_children()
        # returns -- calling mount() right after without awaiting races the
        # removal and raises DuplicateIds for the reused "step-{i}" ids.
        container = self.query_one("#rows", Vertical)
        await container.remove_children()
        await container.mount(
            *(StepRow(vm, id=f"step-{i}") for i, vm in enumerate(rows))
        )
