"""Tests for orchestrator.tui.configio: ConfigDraft <-> migration.yaml.

TDD unit 3 (design doc tui-phase0-design.md Sec 3.1).
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest
import yaml

from orchestrator.tui.configio import (
    SECRET_KEYS,
    draft_to_yaml,
    redact,
    redact_text,
    weak_secret_keys,
    yaml_to_draft,
)
from orchestrator.tui.models import ConfigDraft

# Exact grouped key order the writer must emit inside `env:`, one list per
# `#`-comment header group (design doc Sec 3.1 / task ground truth). Computed
# once here so the order is easy to audit against the design doc table.
GROUP_HEADERS_AND_KEYS: list[tuple[str, list[str]]] = [
    (
        "# Source",
        [
            "SRC_HOST",
            "SRC_PORT",
            "SRC_USER",
            "SRC_PASS",
            "SRC_DB",
            "SRC_DBS",
            "SRC_SSL_MODE",
            "SRC_ADMIN_USER",
            "SRC_ADMIN_PASS",
        ],
    ),
    (
        "# Target",
        [
            "TGT_HOST",
            "TGT_PORT",
            "TGT_USER",
            "TGT_PASS",
            "TGT_ADMIN_USER",
            "TGT_ADMIN_PASS",
            "TGT_SSH_HOST",
            "TGT_SSH_USER",
            "TGT_SSH_OPTS",
        ],
    ),
    (
        "# User migration",
        [
            "MIGRATE_APP_USERS",
            "APP_USER_PWD_EXPIRE",
            "ANALYZE_TARGET",
            "APP_USER_DEFAULT_PASSWORD",
        ],
    ),
    (
        "# Replication (binlog / replace_slave)",
        ["REPL_USER", "REPL_PASS"],
    ),
    (
        "# In-place",
        [
            "INPLACE_BACKUP_DIR",
            "INPLACE_EXECUTE",
            "INPLACE_TARGET_OS",
            "INPLACE_MARIADB_VERSION",
            "INPLACE_STOP_CMD",
            "INPLACE_START_CMD",
            "INPLACE_UPGRADE_CMD",
        ],
    ),
    (
        "# Replace-slave",
        [
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
        ],
    ),
    (
        "# Staged",
        ["STAGED_PHASE", "STAGED_COMPRESS", "STAGED_PV", "STAGED_PARALLEL"],
    ),
]

EXPECTED_ENV_KEY_ORDER: list[str] = [
    key for _, keys in GROUP_HEADERS_AND_KEYS for key in keys
]

# Fields that ARE part of the canonical migration.yaml write list (i.e. the
# fields draft_to_yaml/yaml_to_draft actually round-trip). Everything else on
# ConfigDraft (mode, SRC_DBS_INPUT, INSTALL_TARGET_MARIADB,
# TARGET_INSTALL_OS, TARGET_MARIADB_VERSION, STAGED_DUMP_DIR) is TUI-session-only
# state or a legacy/wizard-only field not written back out by this module.
WRITE_LIST_FIELDS = frozenset(EXPECTED_ENV_KEY_ORDER)


def _sample_draft(**overrides: str) -> ConfigDraft:
    """Build a ConfigDraft with distinct, representative values in every
    field group covered by the write list."""
    values: dict[str, Any] = {
        "mode": "one_step",
        "SRC_HOST": "source-db-host",
        "SRC_PORT": "3306",
        "SRC_USER": "src_user",
        "SRC_PASS": "src_pass_secret",
        # SRC_DB / SRC_DBS are derived by draft_to_yaml from SRC_DBS_INPUT
        # (Rule 2); set consistent with that derivation (single db, no
        # comma -> both keys carry the same value) so the round-trip
        # comparison below holds.
        "SRC_DB": "sakila",
        "SRC_DBS": "sakila",
        "SRC_DBS_INPUT": "sakila",
        "SRC_SSL_MODE": "DISABLED",
        "SRC_ADMIN_USER": "src_admin",
        "SRC_ADMIN_PASS": "src_admin_secret",
        "TGT_HOST": "target-db-host",
        "TGT_PORT": "3307",
        "TGT_USER": "tgt_user",
        "TGT_PASS": "tgt_pass_secret",
        "TGT_ADMIN_USER": "tgt_admin",
        "TGT_ADMIN_PASS": "tgt_admin_secret",
        "TGT_SSH_HOST": "target-ssh-host",
        "TGT_SSH_USER": "root",
        "TGT_SSH_OPTS": "-o StrictHostKeyChecking=no",
        "INSTALL_TARGET_MARIADB": "1",
        "TARGET_INSTALL_OS": "",
        "TARGET_MARIADB_VERSION": "",
        "MIGRATE_APP_USERS": "1",
        "APP_USER_PWD_EXPIRE": "1",
        "ANALYZE_TARGET": "1",
        "APP_USER_DEFAULT_PASSWORD": "app_user_secret",
        "REPL_USER": "repl",
        "REPL_PASS": "repl_secret",
        "INPLACE_BACKUP_DIR": "artifacts/inplace_backup",
        "INPLACE_EXECUTE": "0",
        "INPLACE_TARGET_OS": "ubuntu",
        "INPLACE_MARIADB_VERSION": "11.8",
        "INPLACE_STOP_CMD": "sudo systemctl stop mysql",
        "INPLACE_START_CMD": "sudo systemctl start mariadb",
        "INPLACE_UPGRADE_CMD": "mariadb-upgrade",
        "REPLACE_BACKUP_CMD": "tar -czf backup.tgz /var/lib/mysql",
        "REPLACE_STOP_MYSQL_CMD": "sudo systemctl stop mysql",
        "REPLACE_UNINSTALL_MYSQL_CMD": "apt-get remove -y mysql-server",
        "REPLACE_TARGET_OS": "rocky",
        "REPLACE_MARIADB_VERSION": "11.8",
        "REPLACE_START_MARIADB_CMD": "sudo systemctl start mariadb",
        "REPLACE_CONFIGURE_BIND_ADDRESS": "1",
        "REPLACE_MARIADB_BIND_ADDRESS": "0.0.0.0",
        "REPLACE_AUTO_GRANT_TARGET_ADMIN": "1",
        "REPLACE_TARGET_ADMIN_HOST_PATTERN": "%",
        "REPLACE_DELETE_OLD_MYSQL_DATA": "0",
        "REPLACE_CLEANUP_CMD": "rm -rf /tmp/old_mysql",
        "STAGED_PHASE": "dump_and_load",
        "STAGED_COMPRESS": "1",
        "STAGED_PV": "1",
        "STAGED_PARALLEL": "4",
    }
    values.update(overrides)
    return ConfigDraft(**values)


def _write_list_subset(draft: ConfigDraft) -> dict:
    """Project a ConfigDraft down to only the fields configio actually
    writes to / reads from migration.yaml."""
    as_dict = dataclasses.asdict(draft)
    return {k: v for k, v in as_dict.items() if k in WRITE_LIST_FIELDS}


# --- Round-trip --------------------------------------------------------


def test_round_trip_write_list_fields() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=True)
    restored = yaml_to_draft(text)

    # Fields not in the write list (mode, SRC_DBS_INPUT, INSTALL_TARGET_MARIADB,
    # TARGET_INSTALL_OS, TARGET_MARIADB_VERSION, STAGED_DUMP_DIR) are not
    # written to the YAML at all, so they do NOT round-trip -- only compare
    # the subset of fields that ARE part of the write list.
    assert _write_list_subset(restored) == _write_list_subset(draft)


# --- Rule 1: always-quoted strings --------------------------------------


def test_all_env_values_are_quoted_strings() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    for key, value in parsed["env"].items():
        assert isinstance(value, str), f"{key} did not parse as a str: {value!r}"

    # Regex-check the raw text so a bare unquoted-looking numeric scalar
    # (which would still parse as str for most cases) cannot slip through.
    assert re.search(r'^\s*SRC_PORT:\s*"3306"\s*$', text, re.MULTILINE)
    assert re.search(r'^\s*TGT_PORT:\s*"3307"\s*$', text, re.MULTILINE)
    assert re.search(r'^\s*STAGED_PARALLEL:\s*"4"\s*$', text, re.MULTILINE)


# --- Rule 2: SRC_DB / SRC_DBS derived from SRC_DBS_INPUT -----------------
#
# Matches the wizard's CURRENT export logic (mariadb-migrator:2077-2085,
# fixed there after a prior bug where only one of the two keys was ever
# populated): a comma-separated SRC_DBS_INPUT writes SRC_DBS only (SRC_DB
# empty); a single db writes BOTH keys to the SAME value, not "one empty".
# draft_to_yaml derives both from SRC_DBS_INPUT -- it does not trust
# draft.SRC_DB/draft.SRC_DBS as independent inputs.


def test_multi_db_input_writes_src_dbs_and_empties_src_db() -> None:
    draft = _sample_draft(SRC_DBS_INPUT="sakila,world")
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    assert parsed["env"]["SRC_DBS"] == "sakila,world"
    assert parsed["env"]["SRC_DB"] == ""


def test_single_db_input_writes_both_src_db_and_src_dbs() -> None:
    draft = _sample_draft(SRC_DBS_INPUT="sakila")
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    assert parsed["env"]["SRC_DB"] == "sakila"
    assert parsed["env"]["SRC_DBS"] == "sakila"


# --- Rule 3: no writer-side admin/user mirroring -------------------------


def test_divergent_user_and_admin_user_pass_through_unmodified() -> None:
    draft = _sample_draft(
        SRC_USER="distinct_src_user",
        SRC_ADMIN_USER="distinct_src_admin",
        TGT_USER="distinct_tgt_user",
        TGT_ADMIN_USER="distinct_tgt_admin",
    )
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    assert parsed["env"]["SRC_USER"] == "distinct_src_user"
    assert parsed["env"]["SRC_ADMIN_USER"] == "distinct_src_admin"
    assert parsed["env"]["TGT_USER"] == "distinct_tgt_user"
    assert parsed["env"]["TGT_ADMIN_USER"] == "distinct_tgt_admin"


# --- Rule 4: secret redaction --------------------------------------------


def test_include_secrets_false_blanks_all_secret_keys() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=False)
    parsed = yaml.safe_load(text)

    for key in SECRET_KEYS:
        assert key in parsed["env"], f"{key} must never be omitted"
        assert parsed["env"][key] == "", f"{key} must be blanked, not a placeholder"


def test_include_secrets_true_writes_real_values() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    assert parsed["env"]["SRC_PASS"] == "src_pass_secret"
    assert parsed["env"]["SRC_ADMIN_PASS"] == "src_admin_secret"
    assert parsed["env"]["TGT_PASS"] == "tgt_pass_secret"
    assert parsed["env"]["TGT_ADMIN_PASS"] == "tgt_admin_secret"
    assert parsed["env"]["REPL_PASS"] == "repl_secret"
    assert parsed["env"]["APP_USER_DEFAULT_PASSWORD"] == "app_user_secret"


def test_secret_keys_frozenset_contents() -> None:
    assert SECRET_KEYS == frozenset(
        {
            "SRC_PASS",
            "SRC_ADMIN_PASS",
            "TGT_PASS",
            "TGT_ADMIN_PASS",
            "REPL_PASS",
            "APP_USER_DEFAULT_PASSWORD",
        }
    )


def test_redact_blanks_secret_keys_only() -> None:
    mapping = {
        "SRC_HOST": "host",
        "SRC_PASS": "shh",
        "TGT_ADMIN_PASS": "shh2",
        "REPL_USER": "repl",
    }
    original = dict(mapping)

    result = redact(mapping)

    assert result == {
        "SRC_HOST": "host",
        "SRC_PASS": "",
        "TGT_ADMIN_PASS": "",
        "REPL_USER": "repl",
    }
    # Must not mutate the input mapping.
    assert mapping == original


# --- Key order + comment headers -----------------------------------------


def test_env_key_order_matches_grouped_spec() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=True)
    parsed = yaml.safe_load(text)

    assert list(parsed.keys()) == ["mode", "env"]
    assert list(parsed["env"].keys()) == EXPECTED_ENV_KEY_ORDER


def test_comment_group_headers_present_in_order() -> None:
    draft = _sample_draft()
    text = draft_to_yaml(draft, include_secrets=True)

    headers = [header for header, _ in GROUP_HEADERS_AND_KEYS]
    positions = [text.index(header) for header in headers]
    assert positions == sorted(positions)


# --- ALLOW_ROOT_USERS override (design doc Sec 5.2) -----------------------
#
# Not in config/migration.yaml.example's key list and not part of _GROUPS,
# so it must be emitted conditionally, only when actually set.


def test_allow_root_users_blank_omits_overrides_section() -> None:
    draft = _sample_draft(ALLOW_ROOT_USERS="")
    text = draft_to_yaml(draft, include_secrets=True)

    assert "Overrides" not in text
    assert "ALLOW_ROOT_USERS" not in text


def test_allow_root_users_set_emits_overrides_section() -> None:
    draft = _sample_draft(ALLOW_ROOT_USERS="1")
    text = draft_to_yaml(draft, include_secrets=True)

    assert "  # Overrides\n  ALLOW_ROOT_USERS: \"1\"\n" in text


def test_allow_root_users_round_trip_when_set() -> None:
    draft = _sample_draft(ALLOW_ROOT_USERS="1")
    text = draft_to_yaml(draft, include_secrets=True)
    restored = yaml_to_draft(text)

    assert restored.ALLOW_ROOT_USERS == "1"


def test_allow_root_users_round_trip_when_blank() -> None:
    draft = _sample_draft(ALLOW_ROOT_USERS="")
    text = draft_to_yaml(draft, include_secrets=True)
    restored = yaml_to_draft(text)

    assert restored.ALLOW_ROOT_USERS == ""


# --- redact_text (design doc Sec 7.3, output-path redaction) ------------


def test_redact_text_replaces_secret_value_substring() -> None:
    text = "connecting with MYSQL_PWD=supersecret123 to host"
    secrets = {"SRC_PASS": "supersecret123"}

    result = redact_text(text, secrets)

    assert "supersecret123" not in result
    assert "********" in result
    assert result == "connecting with MYSQL_PWD=******** to host"


def test_redact_text_ignores_non_secret_keys() -> None:
    text = "host=dbhost123 unrelated"
    secrets = {"SRC_HOST": "dbhost123"}

    result = redact_text(text, secrets)

    # SRC_HOST is not in SECRET_KEYS, so its value must not be touched.
    assert result == text


def test_redact_text_no_match_returns_text_unchanged() -> None:
    text = "nothing sensitive here"
    secrets = {"SRC_PASS": "supersecret123"}

    assert redact_text(text, secrets) == text


def test_redact_text_longest_value_first_avoids_partial_clobber() -> None:
    # "secret" is a literal substring of "secretlong" -- redacting the
    # shorter value first would leave "********long" behind instead of a
    # single clean "********" for the full password.
    text = "pwd=secretlong end"
    secrets = {"SRC_PASS": "secretlong", "TGT_PASS": "secret"}

    result = redact_text(text, secrets)

    assert result == "pwd=******** end"
    assert "long" not in result


def test_redact_text_skips_values_shorter_than_four_chars() -> None:
    text = "pwd=ab in the middle of ab normal text"
    secrets = {"SRC_PASS": "ab"}

    # Too short to redact safely -- must be left untouched, not replaced.
    assert redact_text(text, secrets) == text


def test_redact_text_skips_empty_values() -> None:
    text = "pwd= trailing"
    secrets = {"SRC_PASS": ""}

    assert redact_text(text, secrets) == text


def test_redact_text_handles_multiple_distinct_secrets() -> None:
    text = "src=srcpassword tgt=tgtpassword"
    secrets = {"SRC_PASS": "srcpassword", "TGT_PASS": "tgtpassword"}

    result = redact_text(text, secrets)

    assert result == "src=******** tgt=********"


def test_weak_secret_keys_flags_only_short_non_empty_secret_values() -> None:
    secrets = {
        "SRC_PASS": "ab",  # too short
        "TGT_PASS": "longenoughpass",  # fine
        "REPL_PASS": "",  # empty -- not flagged, nothing to redact
        "SRC_HOST": "x",  # not a SECRET_KEYS member at all
    }

    assert weak_secret_keys(secrets) == frozenset({"SRC_PASS"})


def test_weak_secret_keys_empty_when_all_secrets_long_enough() -> None:
    secrets = {"SRC_PASS": "longenough", "TGT_PASS": "alsolongenough"}

    assert weak_secret_keys(secrets) == frozenset()
