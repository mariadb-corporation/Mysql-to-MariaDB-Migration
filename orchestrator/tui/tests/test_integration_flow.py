"""Integration test for the real MigrationApp navigation graph.

Per design doc §9.1/§10.4 build item 12: "fake step_map.yaml + stub scripts,
assess -> plan end to end". Drives the real ``MigrationApp(App[None])`` from
``orchestrator/tui/app.py`` through the full
Welcome -> ModeSelect -> Configure -> Assess -> Plan -> Summary chain for the
``phase_mode="assess_plan"`` + ``mode="one_step"`` path -- the simplest path
through the graph (not staged, so ``StagedPhaseScreen`` is skipped; not
``"all"``, so the Phase-2 ``_RunPhaseStubScreen`` is never reached).

Two real subprocess seams are stubbed at the module level, exactly like every
other screen test in this package (see test_screen_assess.py /
test_screen_plan.py): ``AssessScreen`` and ``PlanScreen`` each import
``run_command_async`` directly into their own module namespace, so the stub
is installed via ``monkeypatch.setattr(<module>, "run_command_async", ...)``.
No real ``python3 -m orchestrator.migrationctl`` subprocess is ever spawned.
This is explicitly scoped to stop at SummaryScreen -- the run phase (Phase 2)
is not built yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from textual.widgets import Button, OptionList, Static, Switch

from orchestrator.tui.app import MigrationApp
from orchestrator.tui.screens import assess as assess_module
from orchestrator.tui.screens import plan as plan_module
from orchestrator.tui.screens.assess import AssessScreen
from orchestrator.tui.screens.configure import ConfigureScreen
from orchestrator.tui.screens.summary import SummaryScreen
from orchestrator.tui.widgets.field_row import LabeledField

# ---------------------------------------------------------------------------
# Fixtures: a minimal fake step_map.yaml (one mode, one phase, one step) and
# the two fake report.json payloads the stubbed subprocess call writes.
# ---------------------------------------------------------------------------


def _step_map() -> dict:
    return {
        "modes": {"one_step": ["phase_a"]},
        "phases": {
            "phase_a": [
                {"id": "step1", "name": "Step One", "script": "scripts/step1.sh"},
            ]
        },
    }


def _assess_report() -> dict:
    # Modeled on test_screen_assess.py::_pass_report() -- a clean PASS with
    # no failing gates, so AssessScreen's "p" (proceed) is not disabled.
    return {
        "success": True,
        "message": "Assessment passed. Ready to plan/run.",
        "source": {"type": "mysql", "version": "8.0.36", "host": "srchost", "port": "3306"},
        "target": {"type": "mariadb", "version": "10.11"},
        "gates": [
            {"name": "mysql_version_supported", "status": "PASS", "details": {}},
            {"name": "innodb_file_per_table_is_1", "status": "PASS", "details": {"value": "1"}},
        ],
        "warnings": [],
        "inventory": {"tables": {"count": 3}},
    }


def _plan_report() -> dict:
    # Modeled on the {"plan": {"steps": [...]}} shape
    # PlanScreen._load_authoritative_steps() expects.
    return {
        "plan": {
            "steps": [
                {"id": "step1", "name": "Step One", "script": "scripts/step1.sh", "args": []},
            ]
        }
    }


def _make_stub_run_command_async():
    """Stand-in for the real subprocess call both AssessScreen and PlanScreen
    make via run_command_async (`python3 -m orchestrator.migrationctl
    assess|plan --config ... --mode ... --out <dir>`). Never execs anything;
    instead it writes a realistic report.json into whichever --out directory
    the real screen code passed in argv (assess_dir / plan_dir, decided by
    MigrationApp._push_assess / _push_plan under
    <repo_root>/artifacts/{assess,plan}_<ts>) and returns (0, "") as if the
    CLI call had succeeded.
    """

    async def _stub(argv, *, cwd=None, env=None, timeout_s=300.0):
        out_dir = Path(argv[argv.index("--out") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _assess_report() if "assess" in argv else _plan_report()
        (out_dir / "report.json").write_text(json.dumps(report))
        return (0, "")

    return _stub


# ---------------------------------------------------------------------------
# ConfigureScreen field helpers -- same convention as test_screen_configure.py.
# ---------------------------------------------------------------------------

_ONE_STEP_HAPPY_VALUES = {
    "SRC_HOST": "srchost",
    "TGT_HOST": "tgthost",
    "SRC_ADMIN_USER": "admin",
    "SRC_ADMIN_PASS": "srcpw",
    "TGT_ADMIN_USER": "admin2",
    "TGT_ADMIN_PASS": "tgtpw",
    "SRC_DBS_INPUT": "mydb",
}


def _set_value(screen, key: str, value) -> None:
    field = None
    for f in screen.query(LabeledField):
        if f.key == key:
            field = f
            break
    if field is None:
        raise KeyError(key)
    control = field.query_one("#control")
    if isinstance(control, Switch):
        control.value = bool(value)
    else:
        control.value = value


def _fill_one_step_happy(screen) -> None:
    for key, value in _ONE_STEP_HAPPY_VALUES.items():
        _set_value(screen, key, value)


async def _drain(pilot, times: int = 3) -> None:
    for _ in range(times):
        await pilot.pause()


# ---------------------------------------------------------------------------
# The integration test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assess_plan_one_step_flow_reaches_summary(tmp_path, monkeypatch):
    stub = _make_stub_run_command_async()
    monkeypatch.setattr(assess_module, "run_command_async", stub)
    monkeypatch.setattr(plan_module, "run_command_async", stub)

    step_map_path = tmp_path / "orchestrator" / "step_map.yaml"
    step_map_path.parent.mkdir(parents=True, exist_ok=True)
    step_map_path.write_text(yaml.safe_dump(_step_map()))

    app = MigrationApp(repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        # -- WelcomeScreen: pick "Assess & Plan" (phase_mode == "assess_plan").
        # Same focus/highlight/enter convention as test_screen_welcome.py.
        welcome = pilot.app.screen
        actions = welcome.query_one("#actions", OptionList)
        actions.focus()
        actions.highlighted = 0
        await pilot.pause()
        await pilot.press("enter")
        await _drain(pilot)
        assert pilot.app.phase_mode == "assess_plan"

        # -- ModeSelectScreen: "1" is bound directly to the first non-advanced
        # mode, which is one_step (design doc §2.1 table, row 1).
        await pilot.press("1")
        await _drain(pilot)
        assert pilot.app.mode is not None
        assert pilot.app.mode.key == "one_step"

        # -- ConfigureScreen: fill the one_step required fields, confirm, and
        # decline the "save to config/migration.yaml?" prompt -- this test is
        # about navigation wiring, not the config-write path (already covered
        # by test_screen_configure.py).
        configure_screen = pilot.app.screen
        _fill_one_step_happy(configure_screen)
        configure_screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)

        save_modal = pilot.app.screen_stack[-1]
        save_modal.query_one("#no", Button).press()
        await _drain(pilot)

        # -- AssessScreen: the stubbed run_command_async already wrote a
        # passing report.json into app.assess_dir by the time the worker
        # finishes; verify the verdict rendered from it, then proceed.
        assert pilot.app.assess_dir is not None
        await pilot.pause()
        assess_screen = pilot.app.screen
        verdict = assess_screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "ASSESSMENT: PASS — ready to plan/run"
        await pilot.press("p")
        await _drain(pilot)

        # -- PlanScreen: the stubbed call wrote the plan report.json; "p"
        # brings up "Proceed to run phase now?", confirmed with "y".
        assert pilot.app.plan_dir is not None
        await pilot.press("p")
        await _drain(pilot)
        # default screen + PlanScreen + ConfirmModal.
        assert len(pilot.app.screen_stack) == 3
        await pilot.press("y")
        await _drain(pilot)

        # -- SummaryScreen: terminal state for phase_mode == "assess_plan"
        # (no run phase is ever reached -- see _on_plan_done in app.py).
        assert isinstance(pilot.app.screen, SummaryScreen)
        summary = pilot.app.screen
        assert summary.success is True
        assert summary.mode == "one_step"

        # The report data our stubs wrote actually made it to disk under the
        # real assess_dir / plan_dir MigrationApp computed.
        assess_report = json.loads((pilot.app.assess_dir / "report.json").read_text())
        assert assess_report["success"] is True
        plan_report = json.loads((pilot.app.plan_dir / "report.json").read_text())
        assert plan_report["plan"]["steps"][0]["id"] == "step1"

        # Terminal SummaryScreen for the assess_plan path is explicitly a
        # plan-only summary -- nothing was ever migrated (app.py finding #3).
        assert summary.plan_only is True
        verdict = summary.query_one("#verdict", Static)
        assert str(verdict.render()) == "PLAN COMPLETE"


def _make_counting_stub_run_command_async():
    """Same behavior as _make_stub_run_command_async, but also records how
    many times it was invoked -- used to prove a nav "back" edge re-renders
    an already-written report.json instead of re-invoking migrationctl."""
    calls: list[list[str]] = []

    async def _stub(argv, *, cwd=None, env=None, timeout_s=300.0):
        calls.append(list(argv))
        out_dir = Path(argv[argv.index("--out") + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        report = _assess_report() if "assess" in argv else _plan_report()
        (out_dir / "report.json").write_text(json.dumps(report))
        return (0, "")

    return _stub, calls


async def _drive_to_assess_screen(pilot) -> None:
    """Shared setup: Welcome -> ModeSelect(one_step) -> Configure -> AssessScreen,
    identical to the happy-path steps in
    test_assess_plan_one_step_flow_reaches_summary above."""
    welcome = pilot.app.screen
    actions = welcome.query_one("#actions", OptionList)
    actions.focus()
    actions.highlighted = 0
    await pilot.pause()
    await pilot.press("enter")
    await _drain(pilot)

    await pilot.press("1")
    await _drain(pilot)

    configure_screen = pilot.app.screen
    _fill_one_step_happy(configure_screen)
    configure_screen.query_one("#confirm-btn", Button).press()
    await _drain(pilot)

    save_modal = pilot.app.screen_stack[-1]
    save_modal.query_one("#no", Button).press()
    await _drain(pilot)

    await pilot.pause()


async def _drive_to_plan_screen(pilot) -> None:
    """Same as _drive_to_assess_screen, plus AssessScreen's "p" (proceed)."""
    await _drive_to_assess_screen(pilot)
    await pilot.press("p")
    await _drain(pilot)


@pytest.mark.asyncio
async def test_escape_on_assess_returns_to_fresh_configure(tmp_path, monkeypatch):
    # app.py finding #8: AssessScreen's "escape" -> go_back -> dismiss(False)
    # must land back on ConfigureScreen, not be a dead end.
    stub = _make_stub_run_command_async()
    monkeypatch.setattr(assess_module, "run_command_async", stub)
    monkeypatch.setattr(plan_module, "run_command_async", stub)

    step_map_path = tmp_path / "orchestrator" / "step_map.yaml"
    step_map_path.parent.mkdir(parents=True, exist_ok=True)
    step_map_path.write_text(yaml.safe_dump(_step_map()))

    app = MigrationApp(repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _drive_to_assess_screen(pilot)
        assert isinstance(pilot.app.screen, AssessScreen)

        await pilot.press("escape")
        await _drain(pilot)

        assert isinstance(pilot.app.screen, ConfigureScreen)


@pytest.mark.asyncio
async def test_decline_plan_back_to_assess_does_not_rerun_assess(tmp_path, monkeypatch):
    # app.py finding #7: PlanScreen's back edge re-pushes AssessScreen with
    # autorun=False, so the already-written report.json is re-rendered
    # instead of re-invoking migrationctl assess a second time.
    stub, calls = _make_counting_stub_run_command_async()
    monkeypatch.setattr(assess_module, "run_command_async", stub)
    monkeypatch.setattr(plan_module, "run_command_async", stub)

    step_map_path = tmp_path / "orchestrator" / "step_map.yaml"
    step_map_path.parent.mkdir(parents=True, exist_ok=True)
    step_map_path.write_text(yaml.safe_dump(_step_map()))

    app = MigrationApp(repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _drive_to_plan_screen(pilot)

        assert pilot.app.plan_dir is not None
        assess_calls = sum(1 for argv in calls if "assess" in argv)
        plan_calls = sum(1 for argv in calls if "plan" in argv and "assess" not in argv)
        assert assess_calls == 1
        assert plan_calls == 1

        # PlanScreen's back edge ("escape" -> action_go_back -> dismiss(None)).
        await pilot.press("escape")
        await _drain(pilot)

        assert isinstance(pilot.app.screen, AssessScreen)
        # No new migrationctl assess invocation -- same report.json rendered.
        assess_calls_after = sum(1 for argv in calls if "assess" in argv)
        assert assess_calls_after == 1
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "ASSESSMENT: PASS — ready to plan/run"
