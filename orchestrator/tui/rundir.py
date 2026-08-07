"""Run-dir discovery, signature, and resume-decision logic.

Reproduces the wizard's run-dir and resume contract exactly (design doc
tui-phase0-design.md Sec 5.4) so the TUI and the wizard can resume each
other's runs. Pure logic plus a handful of small file-I/O helpers -- no
Textual imports here.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Mapping

from orchestrator.tui.models import ConfigDraft, ResumeCandidate, ResumeDecision, ResumeVerdict

RUN_DIR_FILE = ".migration_last_run"  # mariadb-migrator:2183
RUN_SIG_FILE = ".migration_last_signature"  # mariadb-migrator:2184
STATE_FILE = "state.json"
ARTIFACTS_DIR = "artifacts"
TS_FORMAT = "%Y%m%d_%H%M%S"  # mariadb-migrator:2180, LOCAL time (no -u)

UNCONDITIONAL_RESUME_MODES: frozenset[str] = frozenset({"binlog", "replace_slave"})
NO_RESUME_MODES: frozenset[str] = frozenset({"two_step"})

# Greedy `.+` backtracks to the LAST `_<8digits>_<6digits>` suffix, so mode
# keys with internal underscores (one_step, two_step, replace_slave) parse
# correctly.
RUN_DIR_NAME_RE = re.compile(r"^run_(?P<mode>.+)_(?P<ts>[0-9]{8}_[0-9]{6})\Z")

# Exactly 25 keys, this order, mirroring mariadb-migrator:266-290.
# Deliberately no passwords -- a credential rotation alone must not
# invalidate a resume.
SIGNATURE_KEYS: tuple[str, ...] = (
    "MODE",
    "STAGED_PHASE",
    "STAGED_DUMP_DIR",
    "SRC_HOST",
    "SRC_PORT",
    "SRC_ADMIN_USER",
    "SRC_DBS_INPUT",
    "TGT_HOST",
    "TGT_PORT",
    "TGT_ADMIN_USER",
    "TGT_SSH_HOST",
    "TGT_SSH_USER",
    "MIGRATE_APP_USERS",
    "ANALYZE_TARGET",
    "INSTALL_TARGET_MARIADB",
    "TARGET_INSTALL_OS",
    "TARGET_MARIADB_VERSION",
    "REPL_USER",
    "INPLACE_BACKUP_DIR",
    "INPLACE_EXECUTE",
    "INPLACE_TARGET_OS",
    "INPLACE_MARIADB_VERSION",
    "REPLACE_TARGET_OS",
    "REPLACE_MARIADB_VERSION",
    "REPLACE_DELETE_OLD_MYSQL_DATA",
)

_MSG_FORCED = (
    "FORCE_NEW_RUN=1 set; ignoring previous run and starting a new run directory."
)
_MSG_TWO_STEP = (
    "two_step does not resume a previous run; starting a fresh run.\n"
    "If the target still holds the previous run's data, drop it and re-run."
)
_MSG_INPUTS_CHANGED = (
    "Previous run found, but inputs changed. Starting a new run directory."
)


def run_signature(draft: ConfigDraft) -> str:
    """Render the 25 KEY=value signature lines, joined with no trailing newline.

    Matches how the wizard's `printf "%s\\n"` output gets captured via
    `$(...)` (which strips the trailing newline) and how it is later
    written back via `printf "%s"` with no added newline.
    """
    lines: list[str] = []
    for key in SIGNATURE_KEYS:
        if key == "MODE":
            value = draft.mode
        else:
            value = getattr(draft, key)
        lines.append(f"{key}={value or ''}")
    return "\n".join(lines)


def default_ts(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(TS_FORMAT)


def new_run_dir(mode: str, ts: str) -> Path:
    """Return a repo-relative run-dir path -- never make it absolute (a
    wizard run from a different checkout path must still resolve the same
    pointer string)."""
    return Path(ARTIFACTS_DIR) / f"run_{mode}_{ts}"


def parse_run_dir_name(name: str) -> tuple[str, str] | None:
    """Match a bare directory basename (not a path) against RUN_DIR_NAME_RE."""
    match = RUN_DIR_NAME_RE.match(name)
    if match is None:
        return None
    return match.group("mode"), match.group("ts")


def force_new_run_from_env(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("FORCE_NEW_RUN", "") == "1"


def read_pointer(repo_root: Path) -> str:
    """Read .migration_last_run, deleting ALL newlines (matches the
    script's `cat FILE | tr -d '\\n'` -- do NOT use .strip(), which would
    also eat spaces the script preserves). Returns "" if absent."""
    path = repo_root / RUN_DIR_FILE
    if not path.is_file():
        return ""
    return path.read_text().replace("\n", "")


def read_last_signature(repo_root: Path) -> str:
    """Read .migration_last_signature, stripping only trailing newlines
    (matches `$(cat FILE)` command-substitution semantics -- interior
    newlines must survive). Returns "" if absent."""
    path = repo_root / RUN_SIG_FILE
    if not path.is_file():
        return ""
    return path.read_text().rstrip("\n")


def write_pointers(repo_root: Path, run_dir: Path, current_sig: str) -> None:
    """Write both pointer files. Only call on the fresh-start path --
    resuming leaves both pointers untouched."""
    (repo_root / RUN_DIR_FILE).write_text(f"{run_dir}\n")
    (repo_root / RUN_SIG_FILE).write_text(current_sig)


def clear_pointers(repo_root: Path) -> None:
    """Idempotent, never raises."""
    (repo_root / RUN_DIR_FILE).unlink(missing_ok=True)
    (repo_root / RUN_SIG_FILE).unlink(missing_ok=True)


def discover(repo_root: Path) -> ResumeCandidate | None:
    """Structural-only check, no eligibility judgment (mode/signature/force
    aren't available yet -- this runs from WelcomeScreen.on_mount, before a
    mode is chosen). Never mutates anything."""
    raw = read_pointer(repo_root)
    if not raw:
        return None

    run_dir = repo_root / raw
    if not run_dir.is_dir():
        return None

    state = run_dir / STATE_FILE
    if not state.is_file():
        return None

    # Parse the directory's *basename* (not raw), so a pointer with a
    # `./` prefix or trailing slash still parses.
    parsed = parse_run_dir_name(run_dir.name)
    dir_mode, dir_ts = parsed if parsed is not None else (None, None)

    return ResumeCandidate(
        raw_pointer=raw,
        run_dir=run_dir,
        dir_mode=dir_mode,
        dir_ts=dir_ts,
        last_sig=read_last_signature(repo_root),
        state_mtime=state.stat().st_mtime,
    )


def resume_decision(
    candidate: ResumeCandidate | None,
    *,
    mode: str,
    current_sig: str,
    force_new_run: bool,
) -> ResumeDecision:
    """Reproduce the wizard's full two-branch resume dispatch
    (mariadb-migrator:2287-2312 and the character-identical :2356-2377),
    pure function, zero I/O.

    Important, do not "fix": branch A (unconditional resume) tests the
    *selected* mode against UNCONDITIONAL_RESUME_MODES and never checks
    that the candidate's dir_mode actually matches -- so a one_step run
    directory followed by picking binlog resumes unconditionally with
    mode_mismatch=True. This is a real quirk in the actual wizard,
    confirmed intentional-to-preserve for interop -- reproduce it exactly,
    surfaced only via the advisory mode_mismatch flag.
    """
    mismatch = (
        candidate is not None
        and candidate.dir_mode is not None
        and candidate.dir_mode != mode
    )

    if candidate is None:
        return ResumeDecision(
            verdict=ResumeVerdict.FRESH_NO_CANDIDATE,
            candidate=None,
            message="",
            mode_mismatch=False,
        )

    # Branch A: unconditional resume, no signature check at all.
    if mode in UNCONDITIONAL_RESUME_MODES and not force_new_run:
        return ResumeDecision(
            verdict=ResumeVerdict.RESUME_UNCONDITIONAL,
            candidate=candidate,
            message="",
            mode_mismatch=mismatch,
        )

    # Branch B: general case.
    if (
        candidate.last_sig
        and current_sig == candidate.last_sig
        and not force_new_run
        and mode not in NO_RESUME_MODES
    ):
        return ResumeDecision(
            verdict=ResumeVerdict.RESUME_SIGNATURE_MATCH,
            candidate=candidate,
            message="",
            mode_mismatch=mismatch,
        )

    # Fresh start -- precedence: force checked BEFORE two_step, matching
    # the script's own test order.
    if force_new_run:
        verdict, message = ResumeVerdict.FRESH_FORCED, _MSG_FORCED
    elif mode in NO_RESUME_MODES:
        verdict, message = ResumeVerdict.FRESH_TWO_STEP, _MSG_TWO_STEP
    else:
        verdict, message = ResumeVerdict.FRESH_INPUTS_CHANGED, _MSG_INPUTS_CHANGED

    return ResumeDecision(
        verdict=verdict,
        candidate=candidate,
        message=message,
        mode_mismatch=mismatch,
    )
