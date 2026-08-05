"""ConfigDraft <-> config/migration.yaml round-trip (design doc Sec 3.1).

`draft_to_yaml` hand-formats the output (rather than `yaml.dump()`) because
PyYAML cannot reproduce the per-group `#`-comment headers that operators
rely on when diffing/hand-editing `config/migration.yaml`. `yaml_to_draft`
uses `yaml.safe_load()` since comments do not need to round-trip on read.
"""

from __future__ import annotations

import re
from typing import Mapping

import yaml

from orchestrator.tui.models import ConfigDraft

SECRET_KEYS: frozenset[str] = frozenset(
    {
        "SRC_PASS",
        "SRC_ADMIN_PASS",
        "TGT_PASS",
        "TGT_ADMIN_PASS",
        "REPL_PASS",
        "APP_USER_DEFAULT_PASSWORD",
    }
)

# Each group is a (comment header, keys) pair. This defines both the
# canonical write order inside `env:` and the set of ConfigDraft fields that
# are actually written back out to migration.yaml -- fields not listed here
# (mode is handled separately; SRC_DBS_INPUT, INSTALL_TARGET_MARIADB,
# TARGET_INSTALL_OS, TARGET_MARIADB_VERSION, STAGED_DUMP_DIR) are
# TUI-session-only or legacy-only and are never written by this module.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "# Source",
        (
            "SRC_HOST",
            "SRC_PORT",
            "SRC_USER",
            "SRC_PASS",
            "SRC_DB",
            "SRC_DBS",
            "SRC_SSL_MODE",
            "SRC_ADMIN_USER",
            "SRC_ADMIN_PASS",
        ),
    ),
    (
        "# Target",
        (
            "TGT_HOST",
            "TGT_PORT",
            "TGT_USER",
            "TGT_PASS",
            "TGT_ADMIN_USER",
            "TGT_ADMIN_PASS",
            "TGT_SSH_HOST",
            "TGT_SSH_USER",
            "TGT_SSH_OPTS",
        ),
    ),
    (
        "# User migration",
        (
            "MIGRATE_APP_USERS",
            "APP_USER_PWD_EXPIRE",
            "ANALYZE_TARGET",
            "APP_USER_DEFAULT_PASSWORD",
        ),
    ),
    (
        "# Replication (binlog / replace_slave)",
        ("REPL_USER", "REPL_PASS"),
    ),
    (
        "# In-place",
        (
            "INPLACE_BACKUP_DIR",
            "INPLACE_EXECUTE",
            "INPLACE_TARGET_OS",
            "INPLACE_MARIADB_VERSION",
            "INPLACE_STOP_CMD",
            "INPLACE_START_CMD",
            "INPLACE_UPGRADE_CMD",
        ),
    ),
    (
        "# Replace-slave",
        (
            "REPLACE_BACKUP_CMD",
            "REPLACE_STOP_MYSQL_CMD",
            "REPLACE_UNINSTALL_MYSQL_CMD",
            "REPLACE_TARGET_OS",
            "REPLACE_MARIADB_VERSION",
            "REPLACE_START_MARIADB_CMD",
            "REPLACE_CONFIGURE_BIND_ADDRESS",
            "REPLACE_MARIADB_BIND_ADDRESS",
            "REPLACE_AUTO_GRANT_TARGET_ADMIN",
            "REPLACE_TARGET_ADMIN_HOST_PATTERN",
            "REPLACE_DELETE_OLD_MYSQL_DATA",
            "REPLACE_CLEANUP_CMD",
        ),
    ),
    (
        "# Staged",
        ("STAGED_PHASE", "STAGED_COMPRESS", "STAGED_PV", "STAGED_PARALLEL"),
    ),
)

# Flat key order, derived once from _GROUPS.
_ENV_KEY_ORDER: tuple[str, ...] = tuple(
    key for _, keys in _GROUPS for key in keys
)


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _quote(value: str) -> str:
    """Render a value as a double-quoted YAML scalar.

    Folds newlines to a space first, matching the wizard's own
    ``yaml_quote()`` (``mariadb-migrator:1871-1877``: ``s="${s//$'\\n'/ }"``
    before escaping), then escapes backslashes and double quotes. Also
    strips any remaining C0 control characters (bash's ``yaml_quote``
    doesn't handle these either, but leaving them in produces a scalar
    ``yaml.safe_load`` cannot parse -- corrupting the very file this
    function just wrote).
    """
    value = re.sub(r"[\r\n]+", " ", value)
    value = _CONTROL_CHARS_RE.sub("", value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def draft_to_yaml(draft: ConfigDraft, *, include_secrets: bool) -> str:
    """Render a ConfigDraft as migration.yaml text.

    Hand-formatted (not yaml.dump()) to preserve the per-group comment
    headers operators diff/hand-edit against.
    """
    lines: list[str] = []
    lines.append(f'mode: {_quote(draft.mode)}')
    lines.append("env:")

    # Rule 2: derive SRC_DB / SRC_DBS from SRC_DBS_INPUT, the raw operator
    # input, matching the wizard's current export logic exactly
    # (mariadb-migrator:2077-2085, fixed there after a prior bug where only
    # one of the two keys was ever populated): a comma-separated input
    # writes SRC_DBS only (SRC_DB empty); a single db writes BOTH keys to
    # the same value, not "one empty" -- design doc Sec3.1's "one empty"
    # rule describes the older migrationctl.py:97-102 prompt-fill path, not
    # this writer's contract, and predates the wizard's own fix.
    combined = draft.SRC_DBS_INPUT
    if "," in combined:
        src_db_value = ""
        src_dbs_value = combined
    else:
        src_db_value = combined
        src_dbs_value = combined

    overrides = {"SRC_DB": src_db_value, "SRC_DBS": src_dbs_value}

    for header, keys in _GROUPS:
        lines.append(f"  {header}")
        for key in keys:
            if key in overrides:
                value = overrides[key]
            else:
                value = getattr(draft, key)

            if not include_secrets and key in SECRET_KEYS:
                value = ""

            lines.append(f"  {key}: {_quote(value)}")

    return "\n".join(lines) + "\n"


def yaml_to_draft(text: str) -> ConfigDraft:
    """Parse migration.yaml text into a ConfigDraft.

    Populates every write-list field from `env:`, plus `mode`. Fields that
    are not part of the canonical write list (SRC_DBS_INPUT,
    INSTALL_TARGET_MARIADB, TARGET_INSTALL_OS, TARGET_MARIADB_VERSION) are
    still real ConfigDraft fields needed elsewhere (resume signature) --
    they are just never written back out by draft_to_yaml. SRC_DBS_INPUT
    and INSTALL_TARGET_MARIADB get derived defaults (proxied from
    SRC_DBS/SRC_DB, and "0", respectively) rather than a blank "" fallback
    -- see the inline comments below for why a blank default breaks
    signature parity with the wizard.
    """
    parsed = yaml.safe_load(text) or {}
    env = parsed.get("env") or {}

    kwargs: dict[str, str] = {key: str(env.get(key, "")) for key in _ENV_KEY_ORDER}

    # SRC_DBS_INPUT is never written by draft_to_yaml, so it can't be read
    # back directly from a TUI-written config -- but the wizard's own
    # export logic sets SRC_DBS="$SRC_DBS_INPUT" verbatim in both branches
    # of its comma test (mariadb-migrator:2079,2082), so SRC_DBS is an exact
    # proxy whenever the loaded config didn't carry SRC_DBS_INPUT itself.
    kwargs["SRC_DBS_INPUT"] = str(
        env.get("SRC_DBS_INPUT") or env.get("SRC_DBS") or env.get("SRC_DB") or ""
    )

    # mariadb-migrator:1724 pins INSTALL_TARGET_MARIADB="0" unconditionally
    # before every wizard-written signature; a legacy config missing (or
    # blank on) this key must resolve to that same "0", not "", or a
    # loaded-then-resaved draft's signature silently stops matching every
    # wizard-written .migration_last_signature (see review F2).
    kwargs["INSTALL_TARGET_MARIADB"] = str(env.get("INSTALL_TARGET_MARIADB") or "0")
    kwargs["TARGET_INSTALL_OS"] = str(env.get("TARGET_INSTALL_OS", ""))
    kwargs["TARGET_MARIADB_VERSION"] = str(env.get("TARGET_MARIADB_VERSION", ""))

    return ConfigDraft(mode=str(parsed.get("mode", "")), **kwargs)


def redact(mapping: Mapping[str, str]) -> dict[str, str]:
    """Return a new dict with SECRET_KEYS blanked, all else unchanged.

    Never mutates the input mapping.
    """
    return {
        key: ("" if key in SECRET_KEYS else value) for key, value in mapping.items()
    }
