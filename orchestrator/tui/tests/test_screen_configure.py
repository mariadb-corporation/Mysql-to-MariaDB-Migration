"""Pilot tests for orchestrator.tui.screens.configure.ConfigureScreen."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Collapsible, Switch

from orchestrator.tui import configio
from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.modals.root_user import RootUserBlockModal
from orchestrator.tui.modals.save_secrets import SaveSecretsModal
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
        # Repo-relative, not absolute -- STAGED_DUMP_DIR is resume-signature
        # key #3, and a wizard-run and TUI-run signature must byte-match
        # regardless of checkout path.
        assert field.value == "artifacts/run_staged_20260102_000000/dumps"
        assert not Path(field.value).is_absolute()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dump_and_load", "dump_only"])
async def test_staged_dump_dir_autodetect_skipped_for_non_load_only_phases(
    phase, tmp_path
):
    # Ground truth mariadb-migrator:1470-1480 only autodetects for
    # STAGED_PHASE=load_only -- for dump phases the field must stay blank so
    # scripts/25_staged_dump.sh derives its own ${RUN_DIR}/dumps path.
    run_dir = tmp_path / "artifacts" / "run_staged_20260102_000000" / "dumps"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.txt").write_text("new")

    draft = ConfigDraft(STAGED_DUMP_DIR="")
    app = _ConfigureApp(_mode("staged"), draft, staged_phase=phase, repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        field = screen.query_one("#staged", Collapsible).query(LabeledField).first()
        assert field.key == "STAGED_DUMP_DIR"
        assert field.value == ""


# ---------------------------------------------------------------------------
# Part B: validation-on-confirm, root-user blocking, save/secrets flow.
# ---------------------------------------------------------------------------


def _expected_required(mode: str, phase: str | None, install_on: bool) -> set[str]:
    """Independent re-derivation of the required-fields table (mirrors
    _expected_visible's approach in Part A: retyped, not imported, so a real
    logic bug in _required_keys still gets caught)."""
    if mode in ("one_step", "two_step", "binlog", "replace_slave"):
        keys = {
            "SRC_HOST", "TGT_HOST",
            "SRC_ADMIN_USER", "SRC_ADMIN_PASS",
            "TGT_ADMIN_USER", "TGT_ADMIN_PASS",
            "SRC_DBS_INPUT",
        }
        if mode in ("one_step", "two_step") and install_on:
            keys.add("TGT_SSH_HOST")
        if mode in ("binlog", "replace_slave"):
            keys.update({"REPL_USER", "REPL_PASS"})
        if mode == "replace_slave":
            keys.update(
                {"TGT_SSH_HOST", "TGT_SSH_USER", "REPLACE_TARGET_OS", "REPLACE_MARIADB_VERSION"}
            )
        return keys
    if mode == "staged":
        keys = set()
        if phase != "load_only":
            keys.update({"SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS", "SRC_DBS_INPUT"})
        if phase != "dump_only":
            keys.update({"TGT_HOST", "TGT_ADMIN_USER", "TGT_ADMIN_PASS"})
            if install_on:
                keys.add("TGT_SSH_HOST")
        if phase == "load_only":
            keys.add("STAGED_DUMP_DIR")
        return keys
    if mode == "inplace":
        return {
            "SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS",
            "INPLACE_BACKUP_DIR", "INPLACE_TARGET_OS", "INPLACE_MARIADB_VERSION",
        }
    return set()


def _visible_field_keys(screen) -> set[str]:
    keys: set[str] = set()
    for gid in _ALL_GROUP_IDS:
        group = screen.query_one(f"#{gid}", Collapsible)
        if group.display:
            keys.update(f.key for f in group.query(LabeledField))
    return keys


def _required_field_keys(screen) -> set[str]:
    return {f.key for f in screen.query(LabeledField) if f.required}


def _field(screen, key: str) -> LabeledField:
    for f in screen.query(LabeledField):
        if f.key == key:
            return f
    raise KeyError(key)


def _set_value(screen, key: str, value) -> None:
    field = _field(screen, key)
    control = field.query_one("#control")
    if isinstance(control, Switch):
        control.value = bool(value)
    else:
        control.value = value


async def _drain(pilot, times: int = 3) -> None:
    for _ in range(times):
        await pilot.pause()


_ONE_STEP_HAPPY_VALUES = {
    "SRC_HOST": "srchost",
    "TGT_HOST": "tgthost",
    "SRC_ADMIN_USER": "admin",
    "SRC_ADMIN_PASS": "srcpw",
    "TGT_ADMIN_USER": "admin2",
    "TGT_ADMIN_PASS": "tgtpw",
    "SRC_DBS_INPUT": "mydb",
}


def _fill_one_step_happy(screen, overrides: dict | None = None) -> None:
    values = dict(_ONE_STEP_HAPPY_VALUES)
    if overrides:
        values.update(overrides)
    for key, value in values.items():
        _set_value(screen, key, value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode_key,phase,install_on",
    [
        ("one_step", None, False),
        ("one_step", None, True),
        ("replace_slave", None, False),
        ("staged", "load_only", False),
        ("inplace", None, False),
    ],
)
async def test_required_flags_match_expected(mode_key, phase, install_on, tmp_path):
    app = _ConfigureApp(_mode(mode_key), ConfigDraft(), staged_phase=phase, repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        if install_on:
            install_switch = screen.query_one("#install", Collapsible).query_one(Switch)
            install_switch.value = True
            await pilot.pause()
        visible = _visible_field_keys(screen)
        expected = _expected_required(mode_key, phase, install_on) & visible
        actual = _required_field_keys(screen) & visible
        assert actual == expected


@pytest.mark.asyncio
async def test_tgt_ssh_user_required_only_for_replace_slave(tmp_path):
    one_step_app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with one_step_app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        install_switch = screen.query_one("#install", Collapsible).query_one(Switch)
        install_switch.value = True
        await pilot.pause()
        assert "tgtssh" in {
            gid for gid in _ALL_GROUP_IDS if screen.query_one(f"#{gid}", Collapsible).display
        }
        assert "TGT_SSH_USER" not in _required_field_keys(screen)

    replace_app = _ConfigureApp(_mode("replace_slave"), ConfigDraft(), repo_root=tmp_path)
    async with replace_app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#tgtssh", Collapsible).display is True
        assert "TGT_SSH_USER" in _required_field_keys(screen)


@pytest.mark.asyncio
async def test_confirm_blocked_on_invalid_fields(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)
        assert pilot.app.result == "unset"
        assert pilot.app.screen_stack[-1] is screen


@pytest.mark.asyncio
async def test_confirm_happy_path_dismisses_with_synced_draft(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _fill_one_step_happy(screen)
        screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)

        save_modal = pilot.app.screen_stack[-1]
        assert type(save_modal) is ConfirmModal
        save_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert isinstance(pilot.app.result, ConfigDraft)
        draft = pilot.app.result
        for key, value in _ONE_STEP_HAPPY_VALUES.items():
            assert getattr(draft, key) == value
        assert draft.SRC_USER == draft.SRC_ADMIN_USER


@pytest.mark.asyncio
async def test_root_user_block_then_override(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _fill_one_step_happy(screen, {"SRC_ADMIN_USER": "root"})
        screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)

        block_modal = pilot.app.screen_stack[-1]
        assert isinstance(block_modal, RootUserBlockModal)
        block_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert pilot.app.result == "unset"
        assert pilot.app.screen_stack[-1] is screen

        screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)
        block_modal = pilot.app.screen_stack[-1]
        assert isinstance(block_modal, RootUserBlockModal)
        block_modal.query_one("#yes", Button).press()
        await _drain(pilot)

        save_modal = pilot.app.screen_stack[-1]
        assert type(save_modal) is ConfirmModal
        save_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert isinstance(pilot.app.result, ConfigDraft)
        assert pilot.app.result.ALLOW_ROOT_USERS == "1"


@pytest.mark.asyncio
async def test_save_flow_writes_config_with_secrets_blanked(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _fill_one_step_happy(screen)
        screen.query_one("#confirm-btn", Button).press()
        await _drain(pilot)

        save_modal = pilot.app.screen_stack[-1]
        assert type(save_modal) is ConfirmModal
        save_modal.query_one("#yes", Button).press()
        await _drain(pilot)

        secrets_modal = pilot.app.screen_stack[-1]
        assert isinstance(secrets_modal, SaveSecretsModal)
        secrets_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert isinstance(pilot.app.result, ConfigDraft)
        config_path = tmp_path / "config" / "migration.yaml"
        assert config_path.is_file()
        loaded = configio.yaml_to_draft(config_path.read_text())
        assert loaded.SRC_ADMIN_PASS == ""
        assert loaded.TGT_ADMIN_PASS == ""
        assert loaded.SRC_HOST == "srchost"


@pytest.mark.asyncio
async def test_ctrl_s_quick_save_skips_confirm_gate_and_stays_on_screen(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _set_value(screen, "SRC_HOST", "quicksavehost")

        await pilot.press("ctrl+s")
        await _drain(pilot)

        secrets_modal = pilot.app.screen_stack[-1]
        assert isinstance(secrets_modal, SaveSecretsModal)
        secrets_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert pilot.app.result == "unset"
        assert pilot.app.screen_stack[-1] is screen
        config_path = tmp_path / "config" / "migration.yaml"
        assert config_path.is_file()
        loaded = configio.yaml_to_draft(config_path.read_text())
        assert loaded.SRC_HOST == "quicksavehost"


# ---------------------------------------------------------------------------
# Regression tests for opus-reviewer batch pass (items 7-9).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_go_back_dismisses_with_none(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen.action_go_back()
        await _drain(pilot)
        assert pilot.app.result is None


@pytest.mark.asyncio
async def test_double_confirm_press_guarded_against_reentrancy(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _fill_one_step_happy(screen)
        confirm_btn = screen.query_one("#confirm-btn", Button)
        confirm_btn.press()
        confirm_btn.press()
        await _drain(pilot)

        # Only one ConfirmModal stacked -- the second press was a no-op,
        # guarded by _busy, not a second concurrent push_screen_wait chain.
        assert len(pilot.app.screen_stack) == 3
        save_modal = pilot.app.screen_stack[-1]
        assert type(save_modal) is ConfirmModal

        save_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert isinstance(pilot.app.result, ConfigDraft)
        assert len(pilot.app.screen_stack) == 1


@pytest.mark.asyncio
async def test_write_config_backs_up_atomically_writes_and_restricts_permissions(
    tmp_path,
):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen

        screen.draft.SRC_HOST = "firstwrite"
        screen._write_config(include_secrets=False)

        config_dir = tmp_path / "config"
        config_path = config_dir / "migration.yaml"
        assert config_path.is_file()
        assert not (config_dir / "migration.yaml.tmp").exists()
        assert not (config_dir / "migration.yaml.bak").exists()
        assert config_path.stat().st_mode & 0o777 == 0o600
        first_content = config_path.read_text()

        screen.draft.SRC_HOST = "secondwrite"
        screen._write_config(include_secrets=False)

        bak_path = config_dir / "migration.yaml.bak"
        assert bak_path.is_file()
        assert bak_path.read_text() == first_content
        assert not (config_dir / "migration.yaml.tmp").exists()
        assert config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_quick_save_blocks_on_root_user(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _set_value(screen, "SRC_ADMIN_USER", "root")

        await pilot.press("ctrl+s")
        await _drain(pilot)

        block_modal = pilot.app.screen_stack[-1]
        assert isinstance(block_modal, RootUserBlockModal)
        block_modal.query_one("#no", Button).press()
        await _drain(pilot)

        # Declined -- quick-save must abort without writing.
        assert not (tmp_path / "config" / "migration.yaml").exists()
        assert pilot.app.screen_stack[-1] is screen


@pytest.mark.asyncio
async def test_quick_save_mirrors_src_user_from_admin_user(tmp_path):
    app = _ConfigureApp(_mode("one_step"), ConfigDraft(), repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        _set_value(screen, "SRC_ADMIN_USER", "someadmin")

        await pilot.press("ctrl+s")
        await _drain(pilot)

        secrets_modal = pilot.app.screen_stack[-1]
        assert isinstance(secrets_modal, SaveSecretsModal)
        secrets_modal.query_one("#no", Button).press()
        await _drain(pilot)

        assert screen.draft.SRC_USER == screen.draft.SRC_ADMIN_USER == "someadmin"


@pytest.mark.asyncio
async def test_sync_draft_forces_install_target_mariadb_off_when_group_hidden(
    tmp_path,
):
    # binlog mode: #install is not in the static-visible set at all, so a
    # stale INSTALL_TARGET_MARIADB="1" (e.g. from a loaded config) must be
    # forced back to "0" -- it's resume-signature key #15.
    draft = ConfigDraft(INSTALL_TARGET_MARIADB="1")
    app = _ConfigureApp(_mode("binlog"), draft, repo_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        screen._sync_draft_from_fields()
        assert screen.draft.INSTALL_TARGET_MARIADB == "0"
