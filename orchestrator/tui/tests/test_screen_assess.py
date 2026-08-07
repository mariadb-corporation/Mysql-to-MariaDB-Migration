"""Pilot tests for orchestrator.tui.screens.assess.AssessScreen."""

from __future__ import annotations

import json

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Pretty, Static

from orchestrator.tui.models import ConfigDraft, ModeInfo
from orchestrator.tui.screens import assess as assess_module
from orchestrator.tui.screens.assess import AssessScreen


def _mode(key: str = "one_step") -> ModeInfo:
    return ModeInfo(
        key=key,
        label="Serial Streaming Copy (mariadb-dump)",
        subtitle="mariadb-dump · offline · simplest path",
        badge="OFFLINE",
        advanced=False,
        doc_anchor=None,
    )


def _write_report(assess_dir, data: dict) -> None:
    assess_dir.mkdir(parents=True, exist_ok=True)
    (assess_dir / "report.json").write_text(json.dumps(data))


def _pass_report() -> dict:
    return {
        "success": True,
        "message": "Assessment passed. Ready to plan/run.",
        "source": {"type": "mysql", "version": "8.0.36", "host": "src1", "port": "3306"},
        "target": {"type": "mariadb", "version": "10.11"},
        "gates": [
            {"name": "mysql_version_supported", "status": "PASS", "details": {}},
            {"name": "innodb_file_per_table_is_1", "status": "PASS", "details": {"value": "1"}},
        ],
        "warnings": [],
        "inventory": {"tables": {"count": 12}},
    }


def _fail_report() -> dict:
    return {
        "success": False,
        "message": "Assessment failed: one or more hard gates failed.",
        "source": {"type": "mysql", "version": "5.6.51", "host": "src1", "port": "3306"},
        "target": {"type": "mariadb", "version": "10.11"},
        "gates": [
            {
                "name": "mysql_version_supported",
                "status": "FAIL",
                "details": {"version": "5.6", "allowed": ["5.7", "8.0", "8.4"]},
            },
        ],
        "warnings": [],
        "inventory": {},
    }


def _binlog_fail_report() -> dict:
    return {
        "success": False,
        "message": "Assessment failed: one or more hard gates failed.",
        "source": {"type": "mysql", "version": "8.0.36", "host": "src1", "port": "3306"},
        "target": {"type": "mariadb", "version": "10.11"},
        "gates": [
            {
                "name": "binlog_source_compatibility",
                "status": "FAIL",
                "details": {
                    "mode": "binlog",
                    "selected_schemas": ["appdb"],
                    "failures": {"json_columns": ["appdb.orders.payload"]},
                },
            },
        ],
        "warnings": [],
        "inventory": {},
    }


async def _noop_run_command_async(argv, *, cwd=None, env=None, timeout_s=300.0):
    return (0, "")


class _AssessScreenApp(App):
    def __init__(self, *, mode, repo_root, assess_dir, draft, staged_phase=None):
        super().__init__()
        self._mode = mode
        self._repo_root = repo_root
        self._assess_dir = assess_dir
        self._draft = draft
        self._staged_phase = staged_phase
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(
            AssessScreen(
                self._mode,
                self._repo_root,
                self._assess_dir,
                self._draft,
                staged_phase=self._staged_phase,
            ),
            self._set_result,
        )

    def _set_result(self, result) -> None:
        self.result = result


@pytest.mark.asyncio
async def test_pass_report_renders_verdict_and_proceed_dismisses_true(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(assess_dir, _pass_report())

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "ASSESSMENT: PASS — ready to plan/run"

        await pilot.press("p")
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_fail_report_disables_proceed(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(assess_dir, _fail_report())

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.check_action("proceed", ()) is None

        await pilot.press("p")
        await pilot.pause()
        # Disabled: pressing "p" must not dismiss the screen.
        assert pilot.app.result == "unset"
        assert len(pilot.app.screen_stack) == 2

        verdict = screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "Assessment failed: one or more hard gates failed."


@pytest.mark.asyncio
async def test_staged_load_only_renders_skipped_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(
        assess_dir,
        {
            "success": True,
            "message": "Skipped: load_only phase has no source to assess.",
            "gates": [],
            "warnings": [],
            "inventory": {},
        },
    )

    app = _AssessScreenApp(
        mode=_mode("staged"),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
        staged_phase="load_only",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert (
            str(verdict.render())
            == "ASSESSMENT: SKIPPED (load_only: no source in scope)"
        )
        # No gates at all -- proceed must not be disabled by an empty rows set.
        assert pilot.app.screen.check_action("proceed", ()) is True


@pytest.mark.asyncio
async def test_failing_cli_run_shows_error_output(tmp_path, monkeypatch):
    async def _fail(argv, *, cwd=None, env=None, timeout_s=300.0):
        return (1, "ERROR: could not connect to source: Access denied for user")

    monkeypatch.setattr(assess_module, "run_command_async", _fail)
    assess_dir = tmp_path / "assess_1"

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        error = pilot.app.screen.query_one("#error", Static)
        assert "Access denied for user" in str(error.render())
        # Output must also be captured to a log file for later review.
        assert (assess_dir / "assess.log").read_text() == (
            "ERROR: could not connect to source: Access denied for user"
        )


@pytest.mark.asyncio
async def test_invalid_report_json_does_not_crash_render(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    assess_dir.mkdir(parents=True, exist_ok=True)
    (assess_dir / "report.json").write_text("{not valid json")

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        # Must not have crashed the worker -- verdict falls back to FAIL text
        # from an empty report_data, not an unhandled JSONDecodeError.
        verdict = pilot.app.screen.query_one("#verdict", Static)
        assert str(verdict.render()) == "ASSESSMENT: FAIL"


@pytest.mark.asyncio
async def test_escape_goes_back_dismisses_false(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(assess_dir, _pass_report())

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.result is False


@pytest.mark.asyncio
async def test_double_press_p_does_not_double_dismiss(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(assess_dir, _pass_report())

    app = _AssessScreenApp(
        mode=_mode(),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert screen._proceeded is False

        screen.action_proceed()
        assert screen._proceeded is True
        # A second call must be a no-op, not a second dismiss.
        screen.action_proceed()
        await pilot.pause()
        assert pilot.app.result is True


@pytest.mark.asyncio
async def test_binlog_source_compatibility_failure_renders_advisory(tmp_path, monkeypatch):
    monkeypatch.setattr(assess_module, "run_command_async", _noop_run_command_async)
    assess_dir = tmp_path / "assess_1"
    _write_report(assess_dir, _binlog_fail_report())

    app = _AssessScreenApp(
        mode=_mode("binlog"),
        repo_root=tmp_path,
        assess_dir=assess_dir,
        draft=ConfigDraft(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        advisory = pilot.app.screen.query_one("#advisory", Static)
        text = str(advisory.render())
        assert "ERROR: Source is not compatible with replication mode." in text
        assert "Detected JSON columns:" in text
        assert "appdb.orders.payload" in text
