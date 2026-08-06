"""Pilot tests for orchestrator.tui.widgets.step_list.StepList / StepRow."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from orchestrator.tui.models import StepRowVM, StepSpec, StepUiStatus
from orchestrator.tui.widgets.step_list import StepList, StepRow


def _vm(step_id, name, status, detail="", elapsed_s=None):
    return StepRowVM(
        spec=StepSpec(id=step_id, name=name, script=f"scripts/{step_id}.sh", args=()),
        status=status,
        detail=detail,
        elapsed_s=elapsed_s,
    )


class _StepListApp(App):
    def compose(self) -> ComposeResult:
        yield StepList()


@pytest.mark.asyncio
async def test_update_rows_mounts_one_steprow_per_vm_in_order():
    vms = (
        _vm("dump", "mariadb-dump", StepUiStatus.DONE, "2.1 GB/4.8 GB"),
        _vm("load", "mariadb", StepUiStatus.RUNNING, "34 MB/s"),
        _vm("finalize", "finalize", StepUiStatus.PENDING),
    )
    app = _StepListApp()
    async with app.run_test() as pilot:
        step_list = pilot.app.query_one(StepList)
        await step_list.update_rows(vms)
        await pilot.pause()
        rows = list(pilot.app.query(StepRow))
        assert len(rows) == 3
        assert [row.vm.spec.id for row in rows] == ["dump", "load", "finalize"]


@pytest.mark.asyncio
async def test_each_status_maps_to_expected_icon():
    expected_icons = {
        StepUiStatus.PENDING: "○",
        StepUiStatus.RUNNING: "▶",
        StepUiStatus.DONE: "✓",
        StepUiStatus.FAILED: "✗",
        StepUiStatus.SKIPPED_RESUME: "✓",
        StepUiStatus.SKIPPED_BY_PHASE: "⊘",
    }
    vms = tuple(
        _vm(f"step{i}", f"step {i}", status)
        for i, status in enumerate(expected_icons)
    )
    app = _StepListApp()
    async with app.run_test() as pilot:
        step_list = pilot.app.query_one(StepList)
        await step_list.update_rows(vms)
        await pilot.pause()
        rows = list(pilot.app.query(StepRow))
        for row, (status, icon) in zip(rows, expected_icons.items()):
            first_line = row.query(".step-name")[0]
            assert str(first_line.content).startswith(icon)


@pytest.mark.asyncio
async def test_skipped_resume_gets_distinct_css_class():
    vms = (_vm("dump", "mariadb-dump", StepUiStatus.SKIPPED_RESUME),)
    app = _StepListApp()
    async with app.run_test() as pilot:
        step_list = pilot.app.query_one(StepList)
        await step_list.update_rows(vms)
        await pilot.pause()
        row = pilot.app.query_one(StepRow)
        first_line = row.query(".step-name")[0]
        assert first_line.has_class("status-skipped_resume")


@pytest.mark.asyncio
async def test_update_rows_replaces_not_appends():
    app = _StepListApp()
    async with app.run_test() as pilot:
        step_list = pilot.app.query_one(StepList)
        await step_list.update_rows((_vm("a", "a", StepUiStatus.PENDING),))
        await pilot.pause()
        await step_list.update_rows(
            (
                _vm("b", "b", StepUiStatus.RUNNING),
                _vm("c", "c", StepUiStatus.DONE),
            )
        )
        await pilot.pause()
        rows = list(pilot.app.query(StepRow))
        assert len(rows) == 2
        assert [row.vm.spec.id for row in rows] == ["b", "c"]


@pytest.mark.asyncio
async def test_detail_line_carries_step_detail_class():
    vms = (_vm("dump", "mariadb-dump", StepUiStatus.RUNNING, detail="34 MB/s"),)
    app = _StepListApp()
    async with app.run_test() as pilot:
        step_list = pilot.app.query_one(StepList)
        await step_list.update_rows(vms)
        await pilot.pause()
        row = pilot.app.query_one(StepRow)
        detail_line = row.query(".step-detail")[0]
        assert str(detail_line.content) == "34 MB/s"
