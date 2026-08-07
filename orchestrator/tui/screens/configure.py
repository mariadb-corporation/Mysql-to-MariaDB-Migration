"""ConfigureScreen(Screen[ConfigDraft]) per design doc §4.4.

Part A: layout, field groups, and mode-based visibility.
Part B (this revision): validation-on-confirm, root-user blocking, and the
save/secrets flow.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Header, Label

from orchestrator.tui.configio import draft_to_yaml
from orchestrator.tui.modals.confirm import ConfirmModal
from orchestrator.tui.modals.root_user import RootUserBlockModal
from orchestrator.tui.modals.save_secrets import SaveSecretsModal
from orchestrator.tui.models import ConfigDraft, ModeInfo
from orchestrator.tui.widgets.field_row import LabeledField

_OS_OPTIONS = [(v, v) for v in ("ubuntu", "debian", "rocky", "rhel", "centos7", "sles")]
_SSL_MODE_OPTIONS = [
    (v, v) for v in ("DISABLED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY")
]

# Groups whose whole-group visibility depends only on mode/phase, not on any
# other field's live value -- #tgtssh is the one exception (also depends on
# the #install switch) and is computed separately in _tgtssh_visible.
_STATIC_GROUP_IDS = (
    "src",
    "srcdb",
    "tgt",
    "install",
    "appusers",
    "analyze",
    "repl",
    "inplace",
    "replace",
    "staged",
)


def _autodetect_staged_dump_dir(repo_root: Path) -> str:
    """Newest run dir with a completed dump, mirroring mariadb-migrator:1470-1480.

    Only meaningful for mode=="staged" (callers gate on that); a fresh
    directory without ``manifest.txt`` means the dump never finished, so it
    is skipped in favor of an older-but-complete one.
    """
    try:
        candidates = sorted(
            repo_root.glob("artifacts/run_staged_*/dumps"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            if (candidate / "manifest.txt").is_file():
                return str(candidate.relative_to(repo_root))
    except OSError:
        # A directory removed by a concurrent cleanup between glob() and
        # stat(), or a dangling symlink, must not take the whole screen down
        # during compose() -- the bash equivalent silences this the same
        # way (`ls -1dt ... 2>/dev/null`).
        return ""
    return ""


def _sw(value: str, fallback: bool = False) -> bool:
    """Switch default: prefer the draft's own "0"/"1" convention over a
    hardcoded fallback, but only when the draft actually has a value."""
    return value == "1" if value else fallback


def _valid_dbs_input(value: str) -> bool:
    # mariadb-migrator rejects a database list that is blank after trimming
    # (`while [[ -z "$(printf "%s" "$SRC_DBS_INPUT" | xargs)" ]]`, :1552) --
    # extended here to reject any individual blank component too (a bare
    # "," or a dangling trailing comma), which would otherwise silently
    # become a garbage entry when split on "," downstream.
    return all(part.strip() for part in value.split(","))


def _required_keys(mode: str, phase: str | None, install_on: bool) -> frozenset[str]:
    """Ground-truthed against migrationctl.py's real _require_env calls
    (lines 338-420) -- includes a real asymmetry: TGT_SSH_USER is required
    only for replace_slave, never one_step/two_step/staged, even when the
    #tgtssh group is visible there too."""
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
            keys.update({"TGT_SSH_HOST", "TGT_SSH_USER", "REPLACE_TARGET_OS", "REPLACE_MARIADB_VERSION"})
        return frozenset(keys)
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
        return frozenset(keys)
    if mode == "inplace":
        return frozenset({
            "SRC_HOST", "SRC_ADMIN_USER", "SRC_ADMIN_PASS",
            "INPLACE_BACKUP_DIR", "INPLACE_TARGET_OS", "INPLACE_MARIADB_VERSION",
        })
    return frozenset()


class ConfigureScreen(Screen[ConfigDraft]):
    BINDINGS = [
        Binding("escape", "go_back", "back", show=True),
        Binding("ctrl+s", "quick_save", "quick save", show=True),
    ]

    def __init__(
        self,
        mode: ModeInfo,
        draft: ConfigDraft,
        *,
        staged_phase: str | None = None,
        repo_root: Path | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.mode = mode
        self.draft = draft
        self.staged_phase = staged_phase
        self.repo_root = repo_root if repo_root is not None else Path.cwd()
        # Shared by action_confirm and _quick_save_flow -- they can't sensibly
        # run concurrently with each other either, so one flag guards both.
        self._busy = False

    def compose(self) -> ComposeResult:
        draft = self.draft
        yield Header()
        yield Label(f"configure · {self.mode.label}", id="stepper")
        with VerticalScroll(id="form"):
            yield Collapsible(
                LabeledField("SRC_HOST", "Source host", default=draft.SRC_HOST),
                LabeledField(
                    "SRC_PORT", "Source port", default=draft.SRC_PORT or "3306"
                ),
                LabeledField(
                    "SRC_ADMIN_USER", "Source admin user", default=draft.SRC_ADMIN_USER
                ),
                LabeledField(
                    "SRC_ADMIN_PASS",
                    "Source admin password",
                    secret=True,
                    default=draft.SRC_ADMIN_PASS,
                ),
                LabeledField(
                    "SRC_SSL_MODE",
                    "SSL mode",
                    field_type="select",
                    select_options=_SSL_MODE_OPTIONS,
                    default=draft.SRC_SSL_MODE or "DISABLED",
                ),
                title="Source",
                id="src",
            )
            yield Collapsible(
                LabeledField(
                    "SRC_DBS_INPUT",
                    "Database(s) (comma-separated)",
                    default=draft.SRC_DBS_INPUT,
                    validator=_valid_dbs_input,
                ),
                title="Source database(s)",
                id="srcdb",
            )
            yield Collapsible(
                LabeledField("TGT_HOST", "Target host", default=draft.TGT_HOST),
                LabeledField(
                    "TGT_PORT", "Target port", default=draft.TGT_PORT or "3306"
                ),
                LabeledField(
                    "TGT_ADMIN_USER", "Target admin user", default=draft.TGT_ADMIN_USER
                ),
                LabeledField(
                    "TGT_ADMIN_PASS",
                    "Target admin password",
                    secret=True,
                    default=draft.TGT_ADMIN_PASS,
                ),
                title="Target",
                id="tgt",
            )
            yield Collapsible(
                LabeledField(
                    "TGT_SSH_HOST", "Target SSH host", default=draft.TGT_SSH_HOST
                ),
                LabeledField(
                    "TGT_SSH_USER",
                    "Target SSH user",
                    default=draft.TGT_SSH_USER or "root",
                ),
                LabeledField(
                    "TGT_SSH_OPTS",
                    "Target SSH options",
                    default=draft.TGT_SSH_OPTS or "-o StrictHostKeyChecking=no",
                ),
                title="Target SSH",
                id="tgtssh",
            )
            yield Collapsible(
                LabeledField(
                    "INSTALL_TARGET_MARIADB",
                    "Install MariaDB on target",
                    field_type="switch",
                    default=draft.INSTALL_TARGET_MARIADB == "1",
                ),
                title="Install MariaDB",
                id="install",
            )
            yield Collapsible(
                LabeledField(
                    "MIGRATE_APP_USERS",
                    "Migrate app users",
                    field_type="switch",
                    default=_sw(draft.MIGRATE_APP_USERS),
                ),
                LabeledField(
                    "APP_USER_DEFAULT_PASSWORD",
                    "App user default password",
                    secret=True,
                    default=draft.APP_USER_DEFAULT_PASSWORD,
                ),
                LabeledField(
                    "APP_USER_PWD_EXPIRE",
                    "Expire app user passwords",
                    field_type="switch",
                    default=_sw(draft.APP_USER_PWD_EXPIRE),
                ),
                title="App user migration",
                id="appusers",
            )
            yield Collapsible(
                LabeledField(
                    "ANALYZE_TARGET",
                    "Analyze target after migration",
                    field_type="switch",
                    default=_sw(draft.ANALYZE_TARGET, True),
                ),
                title="Analyze target",
                id="analyze",
            )
            yield Collapsible(
                LabeledField("REPL_USER", "Replication user", default=draft.REPL_USER or "repl"),
                LabeledField(
                    "REPL_PASS", "Replication password", secret=True, default=draft.REPL_PASS
                ),
                title="Replication",
                id="repl",
            )
            yield Collapsible(
                LabeledField(
                    "INPLACE_BACKUP_DIR", "Backup directory", default=draft.INPLACE_BACKUP_DIR
                ),
                LabeledField(
                    "INPLACE_EXECUTE",
                    "Execute upgrade",
                    field_type="switch",
                    default=_sw(draft.INPLACE_EXECUTE),
                ),
                LabeledField(
                    "INPLACE_TARGET_OS",
                    "Target OS",
                    field_type="select",
                    select_options=_OS_OPTIONS,
                    default=draft.INPLACE_TARGET_OS,
                ),
                LabeledField(
                    "INPLACE_MARIADB_VERSION",
                    "MariaDB version",
                    default=draft.INPLACE_MARIADB_VERSION,
                ),
                LabeledField(
                    "INPLACE_STOP_CMD", "Stop command", default=draft.INPLACE_STOP_CMD
                ),
                LabeledField(
                    "INPLACE_START_CMD", "Start command", default=draft.INPLACE_START_CMD
                ),
                LabeledField(
                    "INPLACE_UPGRADE_CMD",
                    "Upgrade command",
                    default=draft.INPLACE_UPGRADE_CMD,
                ),
                title="In-place upgrade",
                id="inplace",
            )
            yield Collapsible(
                LabeledField(
                    "REPLACE_TARGET_OS",
                    "Target OS",
                    field_type="select",
                    select_options=_OS_OPTIONS,
                    default=draft.REPLACE_TARGET_OS,
                ),
                LabeledField(
                    "REPLACE_MARIADB_VERSION",
                    "MariaDB version",
                    default=draft.REPLACE_MARIADB_VERSION,
                ),
                LabeledField(
                    "REPLACE_BACKUP_CMD", "Backup command", default=draft.REPLACE_BACKUP_CMD
                ),
                LabeledField(
                    "REPLACE_STOP_MYSQL_CMD",
                    "Stop MySQL command",
                    default=draft.REPLACE_STOP_MYSQL_CMD,
                ),
                LabeledField(
                    "REPLACE_UNINSTALL_MYSQL_CMD",
                    "Uninstall MySQL command",
                    default=draft.REPLACE_UNINSTALL_MYSQL_CMD,
                ),
                LabeledField(
                    "REPLACE_START_MARIADB_CMD",
                    "Start MariaDB command",
                    default=draft.REPLACE_START_MARIADB_CMD,
                ),
                LabeledField(
                    "REPLACE_CONFIGURE_BIND_ADDRESS",
                    "Configure bind address",
                    field_type="switch",
                    default=_sw(draft.REPLACE_CONFIGURE_BIND_ADDRESS),
                ),
                LabeledField(
                    "REPLACE_MARIADB_BIND_ADDRESS",
                    "MariaDB bind address",
                    default=draft.REPLACE_MARIADB_BIND_ADDRESS,
                ),
                LabeledField(
                    "REPLACE_AUTO_GRANT_TARGET_ADMIN",
                    "Auto-grant target admin",
                    field_type="switch",
                    default=_sw(draft.REPLACE_AUTO_GRANT_TARGET_ADMIN),
                ),
                LabeledField(
                    "REPLACE_TARGET_ADMIN_HOST_PATTERN",
                    "Target admin host pattern",
                    default=draft.REPLACE_TARGET_ADMIN_HOST_PATTERN,
                ),
                LabeledField(
                    "REPLACE_DELETE_OLD_MYSQL_DATA",
                    "Delete old MySQL data",
                    field_type="switch",
                    default=_sw(draft.REPLACE_DELETE_OLD_MYSQL_DATA),
                ),
                LabeledField(
                    "REPLACE_CLEANUP_CMD", "Cleanup command", default=draft.REPLACE_CLEANUP_CMD
                ),
                title="Replace MySQL slave",
                id="replace",
            )
            staged_dump_dir_default = draft.STAGED_DUMP_DIR
            if (
                self.mode.key == "staged"
                and self.staged_phase == "load_only"
                and not staged_dump_dir_default
            ):
                staged_dump_dir_default = _autodetect_staged_dump_dir(self.repo_root)
            yield Collapsible(
                LabeledField(
                    "STAGED_DUMP_DIR", "Dump directory", default=staged_dump_dir_default
                ),
                LabeledField(
                    "STAGED_COMPRESS",
                    "Compress dump",
                    field_type="switch",
                    default=_sw(draft.STAGED_COMPRESS),
                ),
                LabeledField(
                    "STAGED_PV",
                    "Show pv progress",
                    field_type="switch",
                    default=_sw(draft.STAGED_PV),
                ),
                LabeledField(
                    "STAGED_PARALLEL", "Parallel workers", default=draft.STAGED_PARALLEL
                ),
                title="Staged offline copy",
                id="staged",
            )
        yield Button("Confirm", id="confirm-btn")
        yield Footer()

    def _static_visibility(self) -> dict[str, bool]:
        """Every group's visibility except #tgtssh, which also depends on
        the live #install switch value (see _tgtssh_visible)."""
        mode = self.mode.key
        phase = self.staged_phase
        src = not (mode == "staged" and phase == "load_only")
        tgt = mode != "inplace" and not (mode == "staged" and phase == "dump_only")
        return {
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
        }

    def _tgtssh_visible(self, install_on: bool) -> bool:
        tgt_visible = self._static_visibility()["tgt"]
        return tgt_visible and (install_on or self.mode.key == "replace_slave")

    def on_mount(self) -> None:
        visibility = self._static_visibility()
        for group_id in _STATIC_GROUP_IDS:
            self.query_one(f"#{group_id}", Collapsible).display = visibility[group_id]
        install_field = self.query_one("#install", Collapsible).query_one(LabeledField)
        # If #install itself isn't visible, INSTALL_TARGET_MARIADB is forced
        # to "0" (see _sync_draft_from_fields) -- treat install_on as False
        # from the start so #tgtssh and the required-flags computation agree
        # with that invariant even before the first sync happens.
        install_on = visibility["install"] and bool(install_field.value)
        self.query_one("#tgtssh", Collapsible).display = self._tgtssh_visible(install_on)
        self._apply_required_flags(install_on)

    def on_labeled_field_changed(self, message: LabeledField.Changed) -> None:
        if message.key != "INSTALL_TARGET_MARIADB":
            return
        self.query_one("#tgtssh", Collapsible).display = self._tgtssh_visible(
            bool(message.value)
        )
        self._apply_required_flags(bool(message.value))

    def _apply_required_flags(self, install_on: bool) -> None:
        required = _required_keys(self.mode.key, self.staged_phase, install_on)
        for field in self.query(LabeledField):
            field.required = field.key in required
            field.refresh_mark()

    def _visible_fields(self) -> list[LabeledField]:
        group_ids = _STATIC_GROUP_IDS + ("tgtssh",)
        fields: list[LabeledField] = []
        for group_id in group_ids:
            group = self.query_one(f"#{group_id}", Collapsible)
            if group.display:
                fields.extend(group.query(LabeledField))
        return fields

    def _sync_draft_from_fields(self) -> None:
        for field in self.query(LabeledField):
            value = field.value
            if isinstance(value, bool):
                value = "1" if value else "0"
            setattr(self.draft, field.key, value)
        # mariadb-migrator:1724 (and the redundant :1497 force for
        # staged/dump_only) pins this to "0" whenever #install isn't
        # applicable, regardless of the hidden switch's own value -- a
        # resume-signature key (#15), so a stale "1" here would silently
        # break resume parity with the wizard.
        if not self._static_visibility()["install"]:
            self.draft.INSTALL_TARGET_MARIADB = "0"

    async def action_confirm(self) -> None:
        # Guards against a double button-press/trigger starting two
        # concurrent push_screen_wait chains (two stacked modals, two
        # dismiss(draft) calls). Set synchronously before the first await
        # below, so a second call arriving while this one is still running
        # sees it and returns immediately instead of racing it.
        if self._busy:
            return
        self._busy = True
        try:
            invalid = [f for f in self._visible_fields() if not f.is_valid]
            if invalid:
                self.notify("Fix the highlighted fields before continuing.", severity="warning")
                return
            if not await self._finalize_draft():
                return
            draft = self.draft
            should_save = await self.app.push_screen_wait(
                ConfirmModal("Save inputs to config/migration.yaml?", default=False)
            )
            if should_save:
                include_secrets = await self.app.push_screen_wait(SaveSecretsModal())
                self._write_config(include_secrets)
            self.dismiss(draft)
        finally:
            self._busy = False

    async def _finalize_draft(self) -> bool:
        """Sync fields, mirror admin users, and enforce the root-user block.

        Returns False if the operator declined a root-user override (caller
        should abort without saving or dismissing). Mirroring must happen
        BEFORE the root-user check: a stale SRC_USER="root" left over from a
        previously-loaded config must be overwritten by the current
        SRC_ADMIN_USER before the check runs, or it would falsely trip on a
        non-root admin user.
        """
        self._sync_draft_from_fields()
        draft = self.draft
        draft.SRC_USER = draft.SRC_ADMIN_USER
        draft.TGT_USER = draft.TGT_ADMIN_USER
        if draft.ALLOW_ROOT_USERS != "1" and (
            draft.SRC_ADMIN_USER == "root" or draft.TGT_ADMIN_USER == "root"
        ):
            override = await self.app.push_screen_wait(RootUserBlockModal())
            if not override:
                return False
            draft.ALLOW_ROOT_USERS = "1"
        return True

    def _write_config(self, include_secrets: bool) -> bool:
        # run_worker defaults to exit_on_error=True, which would tear down
        # the whole app on an unhandled exception here (read-only checkout,
        # full disk, "config" existing as a plain file, ...) -- a save
        # hiccup must surface as a notification, matching the wizard's own
        # "print an error and keep going" behavior, not crash the TUI.
        config_dir = self.repo_root / "config"
        target = config_dir / "migration.yaml"
        tmp = config_dir / "migration.yaml.tmp"
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            # Back up before overwriting -- this file can hold plaintext DB
            # passwords, so losing the previous version to a bad write is
            # worse than for an ordinary config file.
            if target.exists():
                shutil.copy2(target, config_dir / "migration.yaml.bak")
            tmp.write_text(draft_to_yaml(self.draft, include_secrets=include_secrets))
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)  # atomic on POSIX -- no half-written file is ever visible
        except OSError as exc:
            self.notify(f"Failed to save config/migration.yaml: {exc}", severity="error")
            return False
        return True

    def action_quick_save(self) -> None:
        # Binding dispatch (Textual's App._dispatch_action) awaits this
        # action directly in the app's own task, not inside a worker --
        # but push_screen_wait requires get_current_worker() to succeed
        # (textual/app.py raises NoActiveWorker otherwise). Confirmed
        # empirically: an async action_quick_save calling push_screen_wait
        # directly crashes when triggered via the ctrl+s binding. run_worker
        # here gives the awaited flow its own worker context, same as the
        # confirm-button handler below does for action_confirm.
        self.run_worker(self._quick_save_flow())

    async def _quick_save_flow(self) -> None:
        # Shares _busy with action_confirm -- the two flows can't sensibly
        # run concurrently with each other either.
        if self._busy:
            return
        self._busy = True
        try:
            if not await self._finalize_draft():
                return
            include_secrets = await self.app.push_screen_wait(SaveSecretsModal())
            if self._write_config(include_secrets):
                self.notify("Saved to config/migration.yaml")
        finally:
            self._busy = False

    def action_go_back(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-btn":
            self.run_worker(self.action_confirm())
