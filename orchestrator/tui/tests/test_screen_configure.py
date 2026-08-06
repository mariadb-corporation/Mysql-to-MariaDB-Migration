"""Pilot tests for orchestrator.tui.screens.configure.ConfigureScreen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Switch

from orchestrator.tui.models import ConfigDraft
from orchestrator.tui.modes import MODE_CATALOG
from orchestrator.tui.screens.configure import ConfigureScreen
from orchestrator.tui.widgets.field_row import LabeledField

_ALL_GROUP_IDS = (
    "src",
    "srcdb",
    "tgt",
    "tgtssh",
    "install",
    "appusers",
    "analyze",
    "repl",
    "inplace",
    "replace",
    "staged",
)


def _mode(key: str):
    for info in MODE_CATALOG:
        if info.key == key:
            return info
    raise KeyError(key)


def _expected_visible(mode: str, phase: str | None, install_on: bool = False) -> set[str]:
    """Same boolean expressions as the design doc / screen, written
    independently here so a real logic bug in the screen still gets caught."""
    src = not (mode == "staged" and phase == "load_only")
    tgt = mode != "inplace" and not (mode == "staged" and phase == "dump_only")
    static = {
        "src": src,
        "srcdb": src and mode != "inplace",
        "tgt": tgt,
        "install": mode in ("one_step", "two_step", "staged")
        and not (mode == "staged" and phase == "dump_only"),
        "appusers": mode != "inplace",
        "analyze": mode in ("one_step", "two_step", "staged"),
        "repl": mode in ("binlog", "replace_slave"),
        "inplace": mode == "inplace",
        "replace": mode == "replace_slave",
        "staged": mode == "staged",
        "tgtssh": tgt and (install_on or mode == "replace_slave"),
    }
    return {gid for gid, visible in static.items() if visible}


class _ConfigureApp(App):
    def __init__(self, mode, draft, staged_phase=None, repo_root=None):
        super().__init__()
        self._mode = mode
        self._draft = draft
        self._staged_phase = staged_phase
        self._repo_root = repo_root
        self.result = "unset"

    def compose(self) -> ComposeResult:
        return
        yield

    def on_mount(self) -> None:
        self.push_screen(
            ConfigureScreen(
                self._mode,
                self._draft,
                staged_phase=self._staged_phase,
                repo_root=self._repo_root,
            ),
            self._set_result,
        )

    def _set_result(self, result) -> None:
        self.result = result


def _visible_ids(screen) -> set[str]:
    return {
        gid
        for gid in _ALL_GROUP_IDS
        if screen.query_one(f"#{gid}", Collapsible).display
    }


_SCENARIOS = [
    ("one_step", None),
    ("two_step", None),
    ("binlog", None),
    ("inplace", None),
    ("replace_slave", None),
    ("staged", "dump_and_load"),
    ("staged", "dump_only"),
    ("staged", "load_only"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_key,phase", _SCENARIOS)
async def test_group_visibility_matches_predicates(mode_key, phase, tmp_path):
    app = _ConfigureApp(_mode(mode_key), ConfigDraft(), staged_phase=phase, repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        expected = _expected_visible(mode_key, phase, install_on=False)
        assert _visible_ids(screen) == expected


@pytest.mark.asyncio
async def test_draft_values_seed_fields(tmp_path):
    draft = ConfigDraft(SRC_HOST="realhost")
    app = _ConfigureApp(_mode("one_step"), draft, repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        field = screen.query_one("#src", Collapsible).query(LabeledField).first()
        # #src's first field is SRC_HOST.
        assert field.key == "SRC_HOST"
        assert field.value == "realhost"


@pytest.mark.asyncio
async def test_toggling_install_switch_live_updates_tgtssh_one_step(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        tgtssh = screen.query_one("#tgtssh", Collapsible)
        assert tgtssh.display is False

        install_switch = screen.query_one("#install", Collapsible).query_one(Switch)
        install_switch.value = True
        await pilot.pause()
        assert tgtssh.display is True

        install_switch.value = False
        await pilot.pause()
        assert tgtssh.display is False


@pytest.mark.asyncio
async def test_tgtssh_visible_for_replace_slave_regardless_of_switch(tmp_path):
    app = _ConfigureApp(_mode("replace_slave"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        tgtssh = screen.query_one("#tgtssh", Collapsible)
        assert tgtssh.display is True


@pytest.mark.asyncio
async def test_staged_dump_dir_autodetect_picks_newest_manifest_dir(tmp_path):
    older = tmp_path / "artifacts" / "run_staged_20260101_000000" / "dumps"
    newer = tmp_path / "artifacts" / "run_staged_20260102_000000" / "dumps"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    (older / "manifest.txt").write_text("old")
    (newer / "manifest.txt").write_text("new")
    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    draft = ConfigDraft(STAGED_DUMP_DIR="")
    app = _ConfigureApp(
        _mode("staged"), draft, staged_phase="load_only", repo_root=tmp_path
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        field = screen.query_one("#staged", Collapsible).query(LabeledField).first()
        assert field.key == "STAGED_DUMP_DIR"
        assert field.value == str(newer)
