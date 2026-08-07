"""Tests for orchestrator.tui.modes: resolve_steps and expected_skips."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.tui.modes import MODE_CATALOG, expected_skips, resolve_steps

STEP_MAP_PATH = Path(__file__).resolve().parents[2] / "step_map.yaml"

EXPECTED_IDS = {
    "one_step": (
        "install_target_mariadb",
        "preflight_one_step",
        "migrate_app_users",
        "precheck",
        "one_step_dump_restore",
        "analyze_target",
        "validate",
    ),
    "two_step": (
        "install_target_mariadb",
        "preflight_two_step",
        "migrate_app_users",
        "precheck",
        "two_step_schema_only",
        "two_step_parallel_data",
        "two_step_finalize_objects",
        "analyze_target",
        "validate",
    ),
    "binlog": (
        "preflight_binlog",
        "migrate_app_users",
        "precheck",
        "binlog_seed_dump_restore",
        "binlog_start_replication",
        "binlog_verify_replication",
    ),
    "inplace": (
        "preflight_inplace",
        "inplace_backup",
        "inplace_install_mariadb",
        "inplace_upgrade",
        "inplace_validate",
    ),
    "replace_slave": (
        "preflight_replace_slave",
        "precheck",
        "replace_slave_backup",
        "replace_slave_install_mariadb",
        "replace_slave_switch_engine",
        "binlog_seed_dump_restore",
        "binlog_start_replication",
        "binlog_verify_replication",
        "replace_slave_cleanup_old_mysql",
    ),
    "staged": (
        "install_target_mariadb",
        "preflight_staged",
        "migrate_app_users",
        "staged_dump",
        "staged_load",
        "analyze_target",
        "staged_finalize",
    ),
}


@pytest.fixture(scope="module")
def step_map() -> dict:
    assert STEP_MAP_PATH.exists(), f"step_map.yaml not found at {STEP_MAP_PATH}"
    with STEP_MAP_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.mark.parametrize("mode", sorted(EXPECTED_IDS))
def test_resolve_steps_ids(step_map: dict, mode: str) -> None:
    steps = resolve_steps(step_map, mode)
    assert tuple(s.id for s in steps) == EXPECTED_IDS[mode]


def test_resolve_steps_args_is_tuple(step_map: dict) -> None:
    steps = resolve_steps(step_map, "one_step")
    for s in steps:
        assert isinstance(s.args, tuple)
        assert s.args == ()


def test_resolve_steps_unknown_mode_returns_empty(step_map: dict) -> None:
    assert resolve_steps(step_map, "not_a_real_mode") == ()


def test_resolve_steps_migrate_app_users_present_in_each_prepare_mode(
    step_map: dict,
) -> None:
    for mode in ("one_step", "two_step", "binlog", "staged"):
        ids = [s.id for s in resolve_steps(step_map, mode)]
        assert ids.count("migrate_app_users") == 1


def test_resolve_steps_install_ids_are_distinct(step_map: dict) -> None:
    for mode in ("one_step", "two_step", "staged"):
        ids = [s.id for s in resolve_steps(step_map, mode)]
        assert "install_target_mariadb" in ids
        assert "replace_slave_install_mariadb" not in ids

    replace_slave_ids = [s.id for s in resolve_steps(step_map, "replace_slave")]
    assert "replace_slave_install_mariadb" in replace_slave_ids
    assert "install_target_mariadb" not in replace_slave_ids

    # Both point at the same underlying script but are distinct step ids.
    install_step = next(
        s for s in resolve_steps(step_map, "one_step") if s.id == "install_target_mariadb"
    )
    replace_slave_install_step = next(
        s
        for s in resolve_steps(step_map, "replace_slave")
        if s.id == "replace_slave_install_mariadb"
    )
    assert install_step.script == replace_slave_install_step.script
    assert install_step.id != replace_slave_install_step.id


# --- expected_skips ---------------------------------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, frozenset({"install_target_mariadb", "migrate_app_users"})),
        (
            {"STAGED_PHASE": "dump_and_load"},
            frozenset({"install_target_mariadb", "migrate_app_users"}),
        ),
        (
            {"STAGED_PHASE": "dump_only"},
            frozenset(
                {
                    "install_target_mariadb",
                    "migrate_app_users",
                    "staged_load",
                    "staged_finalize",
                    "analyze_target",
                }
            ),
        ),
        (
            {"STAGED_PHASE": "load_only"},
            frozenset({"install_target_mariadb", "migrate_app_users", "staged_dump"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "0"},
            frozenset({"install_target_mariadb", "migrate_app_users"}),
        ),
        ({"INSTALL_TARGET_MARIADB": "1"}, frozenset({"migrate_app_users"})),
        (
            {"INSTALL_TARGET_MARIADB": "true"},
            frozenset({"install_target_mariadb", "migrate_app_users"}),
        ),
        (
            {"STAGED_PHASE": "dump_only", "INSTALL_TARGET_MARIADB": "1"},
            frozenset(
                {"staged_load", "staged_finalize", "migrate_app_users", "analyze_target"}
            ),
        ),
        (
            {"STAGED_PHASE": "load_only", "INSTALL_TARGET_MARIADB": "true"},
            frozenset({"install_target_mariadb", "staged_dump", "migrate_app_users"}),
        ),
        (
            # MIGRATE_APP_USERS and ANALYZE_TARGET explicitly set to their
            # "run it" values so only the STAGED_PHASE-driven rules fire.
            {
                "STAGED_PHASE": "dump_only",
                "INSTALL_TARGET_MARIADB": "1",
                "MIGRATE_APP_USERS": "1",
                "ANALYZE_TARGET": "1",
            },
            # analyze_target still skips: STAGED_PHASE == "dump_only" is an
            # OR-branch independent of ANALYZE_TARGET.
            frozenset({"staged_load", "staged_finalize", "analyze_target"}),
        ),
    ],
)
def test_expected_skips_staged(env: dict, expected: frozenset) -> None:
    assert expected_skips("staged", env) == expected


@pytest.mark.parametrize("mode", ["one_step", "two_step"])
@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, frozenset({"install_target_mariadb", "migrate_app_users"})),
        (
            {"INSTALL_TARGET_MARIADB": "0"},
            frozenset({"install_target_mariadb", "migrate_app_users"}),
        ),
        ({"INSTALL_TARGET_MARIADB": "1"}, frozenset({"migrate_app_users"})),
        (
            {"INSTALL_TARGET_MARIADB": "true"},
            frozenset({"install_target_mariadb", "migrate_app_users"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "1", "MIGRATE_APP_USERS": "1"},
            frozenset(),
        ),
    ],
)
def test_expected_skips_one_step_two_step(
    mode: str, env: dict, expected: frozenset
) -> None:
    assert expected_skips(mode, env) == expected


@pytest.mark.parametrize(
    "env,expected",
    [
        (
            {},
            frozenset({"replace_slave_install_mariadb", "replace_slave_cleanup_old_mysql"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "0"},
            frozenset({"replace_slave_install_mariadb", "replace_slave_cleanup_old_mysql"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "1"},
            frozenset({"replace_slave_cleanup_old_mysql"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "true"},
            frozenset({"replace_slave_install_mariadb", "replace_slave_cleanup_old_mysql"}),
        ),
        (
            {"INSTALL_TARGET_MARIADB": "1", "REPLACE_DELETE_OLD_MYSQL_DATA": "1"},
            frozenset(),
        ),
    ],
)
def test_expected_skips_replace_slave(env: dict, expected: frozenset) -> None:
    assert expected_skips("replace_slave", env) == expected


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, frozenset({"migrate_app_users"})),
        ({"MIGRATE_APP_USERS": "0"}, frozenset({"migrate_app_users"})),
        ({"MIGRATE_APP_USERS": "1"}, frozenset()),
        ({"MIGRATE_APP_USERS": "true"}, frozenset({"migrate_app_users"})),
    ],
)
def test_expected_skips_binlog(env: dict, expected: frozenset) -> None:
    assert expected_skips("binlog", env) == expected


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, frozenset({"inplace_install_mariadb", "inplace_upgrade"})),
        ({"INPLACE_EXECUTE": "0"}, frozenset({"inplace_install_mariadb", "inplace_upgrade"})),
        ({"INPLACE_EXECUTE": "1"}, frozenset()),
        (
            {"INPLACE_EXECUTE": "true"},
            frozenset({"inplace_install_mariadb", "inplace_upgrade"}),
        ),
    ],
)
def test_expected_skips_inplace(env: dict, expected: frozenset) -> None:
    assert expected_skips("inplace", env) == expected


@pytest.mark.parametrize(
    "mode", ["one_step", "two_step", "binlog", "staged"]
)
@pytest.mark.parametrize(
    "env,skipped",
    [
        ({}, True),
        ({"MIGRATE_APP_USERS": "0"}, True),
        ({"MIGRATE_APP_USERS": "1"}, False),
        ({"MIGRATE_APP_USERS": "true"}, True),
    ],
)
def test_expected_skips_migrate_app_users_across_modes(
    mode: str, env: dict, skipped: bool
) -> None:
    result = "migrate_app_users" in expected_skips(mode, env)
    assert result is skipped


@pytest.mark.parametrize("mode", ["one_step", "two_step", "staged"])
@pytest.mark.parametrize(
    "env,skipped",
    [
        ({}, False),
        ({"ANALYZE_TARGET": "1"}, False),
        ({"ANALYZE_TARGET": "0"}, True),
        ({"ANALYZE_TARGET": "true"}, True),
    ],
)
def test_expected_skips_analyze_target_across_modes(
    mode: str, env: dict, skipped: bool
) -> None:
    result = "analyze_target" in expected_skips(mode, env)
    assert result is skipped


def test_expected_skips_analyze_target_staged_dump_only_or_branch() -> None:
    # ANALYZE_TARGET explicitly set to "1" (would normally run), but
    # STAGED_PHASE=="dump_only" is an independent OR-branch that still
    # forces the skip because there is no load on this host.
    env = {"ANALYZE_TARGET": "1", "STAGED_PHASE": "dump_only"}
    assert "analyze_target" in expected_skips("staged", env)


def test_expected_skips_one_step_never_leaks_replace_slave_install() -> None:
    for env in ({}, {"INSTALL_TARGET_MARIADB": "0"}, {"INSTALL_TARGET_MARIADB": "true"}):
        result = expected_skips("one_step", env)
        assert "install_target_mariadb" in result
        assert "replace_slave_install_mariadb" not in result


# --- MODE_CATALOG ------------------------------------------------------------


def test_mode_catalog_has_six_entries_in_key_order() -> None:
    assert [m.key for m in MODE_CATALOG] == [
        "one_step",
        "two_step",
        "staged",
        "binlog",
        "inplace",
        "replace_slave",
    ]


def test_mode_catalog_keys_match_step_map_modes(step_map: dict) -> None:
    assert {m.key for m in MODE_CATALOG} == set(step_map["modes"].keys())


def test_mode_catalog_advanced_flags() -> None:
    advanced = [m for m in MODE_CATALOG if m.advanced]
    assert {m.key for m in advanced} == {"inplace", "replace_slave"}
    for m in advanced:
        assert m.doc_anchor is None


def test_mode_catalog_non_advanced_have_doc_anchors() -> None:
    non_advanced = [m for m in MODE_CATALOG if not m.advanced]
    assert len(non_advanced) == 4
    for m in non_advanced:
        assert isinstance(m.doc_anchor, str)
        assert m.doc_anchor
