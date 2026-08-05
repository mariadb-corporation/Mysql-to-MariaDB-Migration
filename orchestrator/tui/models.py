"""Data models for the Textual TUI.

Pure data holders (dataclasses and enums) used by the TUI layer. These are
TUI-only view models and are intentionally separate from the engine's own
dataclasses in ``orchestrator/report.py`` (``Gate``, ``WarningItem``,
``GateStatus``, ``StepStatus``); nothing here imports from that module.

No logic, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ModeInfo:
    key: str
    label: str
    subtitle: str
    badge: str
    advanced: bool
    doc_anchor: str | None


@dataclass(frozen=True)
class StepSpec:
    id: str
    name: str
    script: str
    args: tuple[str, ...]


class StepUiStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED_RESUME = "SKIPPED_RESUME"
    SKIPPED_BY_PHASE = "SKIPPED_BY_PHASE"


@dataclass(frozen=True)
class StepRowVM:
    spec: StepSpec
    status: StepUiStatus
    detail: str
    elapsed_s: float | None


@dataclass(frozen=True)
class RunView:
    mode: str
    staged_phase: str | None
    rows: tuple[StepRowVM, ...]
    current_index: int | None
    total: int
    completed: int
    run_dir: Path
    finished: bool
    success: bool | None


class CheckKind(str, Enum):
    GATE = "GATE"
    WARNING = "WARNING"


class CheckLevel(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class CheckRow:
    kind: CheckKind
    name: str
    level: CheckLevel
    summary: str
    details: Mapping[str, Any]
    expandable: bool


@dataclass(frozen=True)
class AssessView:
    mode: str
    source: Mapping[str, Any]
    target: Mapping[str, Any]
    rows: tuple[CheckRow, ...]
    inventory: Mapping[str, Any]
    success: bool | None
    message: str
    skipped: bool


class ProgressSource(str, Enum):
    PV = "PV"
    FILE_PROBE = "FILE_PROBE"
    HEARTBEAT = "HEARTBEAT"
    DONE_LINE = "DONE_LINE"
    STAT = "STAT"


@dataclass(frozen=True)
class ProgressSample:
    source: ProgressSource
    label: str | None
    percent: float | None
    elapsed_s: float | None
    eta_s: float | None
    bytes_done: int | None
    rate_bytes_s: float | None


@dataclass(frozen=True)
class ResumeCandidate:
    """A structurally valid previous run found by ``rundir.discover``.

    Existence of this object means exactly what the wizard's shared
    predicate means (``mariadb-migrator:2287``, ``:2292``, ``:2298``):
    ``-n "$RUN_DIR" && -d "$RUN_DIR" && -f "$RUN_DIR/state.json"``.
    It carries no eligibility judgment -- ``discover()`` runs on app
    mount, before a mode is chosen or a draft exists, so mode/signature/
    force checks cannot have happened yet. See ``rundir.resume_decision``.
    """

    raw_pointer: str
    run_dir: Path
    dir_mode: str | None
    dir_ts: str | None
    last_sig: str
    state_mtime: float


class ResumeVerdict(str, Enum):
    """Which branch of the wizard's resume dispatch a situation lands in."""

    RESUME_UNCONDITIONAL = "RESUME_UNCONDITIONAL"
    RESUME_SIGNATURE_MATCH = "RESUME_SIGNATURE_MATCH"
    FRESH_FORCED = "FRESH_FORCED"
    FRESH_TWO_STEP = "FRESH_TWO_STEP"
    FRESH_INPUTS_CHANGED = "FRESH_INPUTS_CHANGED"
    FRESH_NO_CANDIDATE = "FRESH_NO_CANDIDATE"


@dataclass(frozen=True)
class ResumeDecision:
    verdict: ResumeVerdict
    candidate: ResumeCandidate | None
    message: str
    # True when candidate.dir_mode differs from the mode just selected.
    # Advisory only -- never changes verdict, since the wizard doesn't
    # check this either (mariadb-migrator:2287 tests live $MODE only).
    mode_mismatch: bool

    @property
    def can_resume(self) -> bool:
        return self.verdict in (
            ResumeVerdict.RESUME_UNCONDITIONAL,
            ResumeVerdict.RESUME_SIGNATURE_MATCH,
        )


@dataclass(frozen=True)
class ReplicaStatus:
    io_running: bool | None
    sql_running: bool | None
    seconds_behind: int | None
    last_io_error: str
    last_sql_error: str
    # One of "replica" | "slave" | "unknown" -- the naming used by the
    # server's SHOW REPLICA/SLAVE STATUS output that produced this status.
    raw_naming: str


@dataclass
class ConfigDraft:
    """Mutable backing store for the setup wizard's form fields.

    Every env-var field is a plain string defaulting to "" -- the wizard
    always round-trips these as quoted strings, so there is no bool/int
    typing here even for boolean-ish-looking vars like MIGRATE_APP_USERS.
    Field order mirrors the YAML group order operators diff against.
    """

    mode: str = ""

    # Source connection
    SRC_HOST: str = ""
    SRC_PORT: str = ""
    SRC_USER: str = ""
    SRC_PASS: str = ""
    SRC_DB: str = ""
    SRC_DBS: str = ""
    # Raw operator input string for the database(s) field, before the
    # SRC_DB/SRC_DBS comma-split normalization -- run_signature() needs the
    # raw string, not the split result (mariadb-migrator:272).
    SRC_DBS_INPUT: str = ""
    SRC_SSL_MODE: str = ""
    SRC_ADMIN_USER: str = ""
    SRC_ADMIN_PASS: str = ""

    # Target connection
    TGT_HOST: str = ""
    TGT_PORT: str = ""
    TGT_USER: str = ""
    TGT_PASS: str = ""
    TGT_ADMIN_USER: str = ""
    TGT_ADMIN_PASS: str = ""
    TGT_SSH_HOST: str = ""
    TGT_SSH_USER: str = ""
    TGT_SSH_OPTS: str = ""
    # mariadb-migrator:1724 hardcodes this to "0" unconditionally before
    # run_signature() runs (the CFG_INSTALL_TARGET_MARIADB config-file
    # override path is dead code -- never assigned back). Default "0" here,
    # not "", so a fresh ConfigDraft's signature is byte-parity with every
    # wizard-written .migration_last_signature.
    INSTALL_TARGET_MARIADB: str = "0"
    TARGET_INSTALL_OS: str = ""      # mariadb-migrator:1725, always ""
    TARGET_MARIADB_VERSION: str = "" # mariadb-migrator:1726, always ""

    # App users / analyze
    MIGRATE_APP_USERS: str = ""
    APP_USER_PWD_EXPIRE: str = ""
    ANALYZE_TARGET: str = ""
    APP_USER_DEFAULT_PASSWORD: str = ""

    # Replication
    REPL_USER: str = ""
    REPL_PASS: str = ""

    # In-place upgrade
    INPLACE_BACKUP_DIR: str = ""
    INPLACE_EXECUTE: str = ""
    INPLACE_TARGET_OS: str = ""
    INPLACE_MARIADB_VERSION: str = ""
    INPLACE_STOP_CMD: str = ""
    INPLACE_START_CMD: str = ""
    INPLACE_UPGRADE_CMD: str = ""

    # Replace (in-place replace) mode
    REPLACE_BACKUP_CMD: str = ""
    REPLACE_STOP_MYSQL_CMD: str = ""
    REPLACE_UNINSTALL_MYSQL_CMD: str = ""
    REPLACE_TARGET_OS: str = ""
    REPLACE_MARIADB_VERSION: str = ""
    REPLACE_START_MARIADB_CMD: str = ""
    REPLACE_CONFIGURE_BIND_ADDRESS: str = ""
    REPLACE_MARIADB_BIND_ADDRESS: str = ""
    REPLACE_AUTO_GRANT_TARGET_ADMIN: str = ""
    REPLACE_TARGET_ADMIN_HOST_PATTERN: str = ""
    REPLACE_DELETE_OLD_MYSQL_DATA: str = ""
    REPLACE_CLEANUP_CMD: str = ""

    # Staged migration
    STAGED_PHASE: str = ""
    # Signature line 3 (mariadb-migrator:268). Per-run, NOT persisted to
    # config/migration.yaml (mariadb-migrator:2022-2026) -- configio.py must
    # exclude this from draft_to_yaml's key list.
    STAGED_DUMP_DIR: str = ""
    STAGED_COMPRESS: str = ""
    STAGED_PV: str = ""
    STAGED_PARALLEL: str = ""
