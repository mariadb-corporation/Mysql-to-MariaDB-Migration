"""ConfigureScreen(Screen[ConfigDraft]) per design doc §4.4 -- Part A.

Part A only: layout, field groups, and mode-based visibility. Validation-
on-confirm, root-user blocking, and the save/secrets flow are Part B, a
separate follow-up -- this screen intentionally has no ``confirm`` binding
yet.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Collapsible, Footer, Header, Label

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
    candidates = sorted(
        repo_root.glob("artifacts/run_staged_*/dumps"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if (candidate / "manifest.txt").is_file():
            return str(candidate)
    return ""


def _sw(value: str, fallback: bool = False) -> bool:
    """Switch default: prefer the draft's own "0"/"1" convention over a
    hardcoded fallback, but only when the draft actually has a value."""
    return value == "1" if value else fallback


class ConfigureScreen(Screen[ConfigDraft]):
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
            if self.mode.key == "staged" and not staged_dump_dir_default:
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
        install_on = bool(install_field.value)
        self.query_one("#tgtssh", Collapsible).display = self._tgtssh_visible(install_on)

    def on_labeled_field_changed(self, message: LabeledField.Changed) -> None:
        if message.key != "INSTALL_TARGET_MARIADB":
            return
        self.query_one("#tgtssh", Collapsible).display = self._tgtssh_visible(
            bool(message.value)
        )
