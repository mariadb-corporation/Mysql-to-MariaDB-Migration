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
        yield Static(self._name_text(), classes=self._name_classes(), id="name")
        yield Static(self.vm.detail, classes="step-detail", id="detail")

    def _name_text(self) -> str:
        return f"{_ICONS[self.vm.status]}  {self.vm.spec.name}"

    def _name_classes(self) -> str:
        return f"step-name status-{self.vm.status.value.lower()}"

    def update_vm(self, vm: StepRowVM) -> None:
        """Refresh this row's own text/classes in place for a new ``vm``.

        The alternative -- remove and remount a fresh ``StepRow`` -- is
        what ``StepList.update_rows`` used to always do, and on a screen
        that redraws every tick (``DemoScreen``, every 0.5s) that reads as
        a visible flicker: the row briefly disappears before its
        replacement mounts. Updating the existing widget's content has no
        such gap.
        """
        self.vm = vm
        name = self.query_one("#name", Static)
        name.update(self._name_text())
        name.set_classes(self._name_classes())
        self.query_one("#detail", Static).update(vm.detail)


class StepList(Widget):
    def compose(self) -> ComposeResult:
        yield Vertical(id="rows")

    async def update_rows(self, rows: tuple[StepRowVM, ...]) -> None:
        container = self.query_one("#rows", Vertical)
        existing = list(container.query(StepRow))
        if len(existing) == len(rows):
            # Same row count as last time -- just refresh each row's
            # content instead of tearing down and remounting every row
            # (see StepRow.update_vm's docstring for why that flickers).
            for row_widget, vm in zip(existing, rows):
                row_widget.update_vm(vm)
            return
        # remove_children()/mount() must both be awaited: DOM removal happens
        # via an internally posted Prune message, processed on a later
        # message-pump turn, not synchronously when remove_children()
        # returns -- calling mount() right after without awaiting races the
        # removal and raises DuplicateIds for the reused "step-{i}" ids.
        await container.remove_children()
        await container.mount(
            *(StepRow(vm, id=f"step-{i}") for i, vm in enumerate(rows))
        )
