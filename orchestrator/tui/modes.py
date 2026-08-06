"""Mode -> step resolution and self-skip prediction for the Textual TUI.

Mirrors the phase/step resolution logic in ``migrationctl.py`` (see
``migrationctl.py:445-448`` / ``migrationctl.py:321-324``) and the
self-skip behavior of every shell script that can self-skip, via the
exhaustive ``_SKIP_RULES`` table in ``expected_skips``.

No I/O beyond what the caller passes in: ``resolve_steps`` takes an
already-loaded ``step_map`` dict, and ``expected_skips`` takes an
already-resolved environment mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from orchestrator.tui.models import ModeInfo, StepSpec

MODE_CATALOG: tuple[ModeInfo, ...] = (
    ModeInfo(
        "one_step",
        "Serial Streaming Copy (mariadb-dump)",
        "mariadb-dump, piped end-to-end · no disk staging",
        "OFFLINE",
        False,
        "docs/mode1-serial-streaming-copy-guide.md",
    ),
    ModeInfo(
        "two_step",
        "Parallel Restartable Streaming Copy (mariadb-mtk)",
        "mariadb-dump (schema) + mariadb-mtk (data) · parallel, auto-retry",
        "OFFLINE",
        False,
        "docs/mode2-parallel-restartable-streaming-guide.md",
    ),
    ModeInfo(
        "staged",
        "Offline Copy (mariadb-dump)",
        "two-phase dump/load via on-disk files · resumable, inspectable",
        "OFFLINE",
        False,
        "docs/mode3-offline-copy-guide.md",
    ),
    ModeInfo(
        "binlog",
        "Replication (binlog)",
        "mariadb-dump snapshot + binlog replication catch-up",
        "ONLINE",
        False,
        "docs/mode4-replication-binlog-guide.md",
    ),
    ModeInfo(
        "inplace",
        "In-place upgrade",
        "upgrade MySQL to MariaDB in place, same host",
        "ADVANCED",
        True,
        None,
    ),
    ModeInfo(
        "replace_slave",
        "Replace MySQL slave",
        "replace a MySQL replica with a MariaDB target",
        "ADVANCED",
        True,
        None,
    ),
)


def resolve_steps(step_map: dict, mode: str) -> tuple[StepSpec, ...]:
    """Resolve the ordered steps for ``mode`` from ``step_map``.

    Mirrors migrationctl.py's forgiving ``.get(..., [])`` pattern exactly:
    an unknown mode or phase resolves to an empty list rather than raising.
    No de-duplication is performed -- a step id (e.g. ``migrate_app_users``)
    may legitimately appear once per mode via a different phase.
    """
    phases = step_map.get("modes", {}).get(mode, [])
    steps: list[StepSpec] = []
    for phase in phases:
        raw_steps = step_map.get("phases", {}).get(phase, [])
        for raw in raw_steps:
            steps.append(
                StepSpec(
                    id=raw["id"],
                    name=raw["name"],
                    script=raw["script"],
                    args=tuple(raw.get("args", []) or []),
                )
            )
    return tuple(steps)


@dataclass(frozen=True)
class _SkipRule:
    """A single step id's self-skip predicate, scoped to the modes it appears in.

    Each rule mirrors one shell script's own strict string-equality gate
    exactly (not the looser truthy check migrationctl.py uses elsewhere for
    unrelated variables). ``modes`` limits which ``expected_skips(mode, ...)``
    calls this rule can ever contribute to, since the same step id can be
    reused by scripts with the same underlying script (e.g.
    ``install_target_mariadb`` vs ``replace_slave_install_mariadb``) but must
    never leak across modes.
    """

    step_id: str
    modes: frozenset[str]
    predicate: Callable[[Mapping[str, str]], bool]


def _installs_target_mariadb(env: Mapping[str, str]) -> bool:
    # scripts/23_install_mariadb.sh
    return env.get("INSTALL_TARGET_MARIADB", "0") != "1"


def _migrates_app_users(env: Mapping[str, str]) -> bool:
    # scripts/09_migrate_app_users.sh:34-38
    return env.get("MIGRATE_APP_USERS", "0") != "1"


def _analyzes_target(env: Mapping[str, str]) -> bool:
    # scripts/28_analyze_target.sh:39-49
    return (
        env.get("ANALYZE_TARGET", "1") != "1"
        or env.get("STAGED_PHASE", "dump_and_load") == "dump_only"
    )


def _inplace_execute_skipped(env: Mapping[str, str]) -> bool:
    # scripts/18_inplace_install_mariadb.sh:6,10 and scripts/18_inplace_upgrade.sh:6,15
    return env.get("INPLACE_EXECUTE", "0") != "1"


def _replace_slave_cleanup_skipped(env: Mapping[str, str]) -> bool:
    # scripts/24_replace_slave_cleanup.sh:9,12
    return env.get("REPLACE_DELETE_OLD_MYSQL_DATA", "0") != "1"


def _staged_dump_skipped(env: Mapping[str, str]) -> bool:
    # scripts/25_staged_dump.sh
    return env.get("STAGED_PHASE", "dump_and_load") == "load_only"


def _staged_load_skipped(env: Mapping[str, str]) -> bool:
    # scripts/26_staged_load.sh
    return env.get("STAGED_PHASE", "dump_and_load") == "dump_only"


def _staged_finalize_skipped(env: Mapping[str, str]) -> bool:
    # scripts/27_staged_finalize.sh
    return env.get("STAGED_PHASE", "dump_and_load") == "dump_only"


# Exhaustive table of every step id in the system that can self-skip. Adding
# a newly discovered self-skip in a shell script means adding one row here --
# not adding a per-mode branch that's easy to forget in the other modes.
_SKIP_RULES: tuple[_SkipRule, ...] = (
    _SkipRule(
        "install_target_mariadb",
        frozenset({"one_step", "two_step", "staged"}),
        _installs_target_mariadb,
    ),
    _SkipRule(
        "replace_slave_install_mariadb",
        frozenset({"replace_slave"}),
        _installs_target_mariadb,
    ),
    _SkipRule(
        "staged_dump",
        frozenset({"staged"}),
        _staged_dump_skipped,
    ),
    _SkipRule(
        "staged_load",
        frozenset({"staged"}),
        _staged_load_skipped,
    ),
    _SkipRule(
        "staged_finalize",
        frozenset({"staged"}),
        _staged_finalize_skipped,
    ),
    _SkipRule(
        "migrate_app_users",
        frozenset({"one_step", "two_step", "binlog", "staged"}),
        _migrates_app_users,
    ),
    _SkipRule(
        "analyze_target",
        frozenset({"one_step", "two_step", "staged"}),
        _analyzes_target,
    ),
    _SkipRule(
        "inplace_install_mariadb",
        frozenset({"inplace"}),
        _inplace_execute_skipped,
    ),
    _SkipRule(
        "inplace_upgrade",
        frozenset({"inplace"}),
        _inplace_execute_skipped,
    ),
    _SkipRule(
        "replace_slave_cleanup_old_mysql",
        frozenset({"replace_slave"}),
        _replace_slave_cleanup_skipped,
    ),
)


def expected_skips(mode: str, env: Mapping[str, str]) -> frozenset[str]:
    """Predict which step ids will self-skip for ``mode`` given ``env``.

    Driven by ``_SKIP_RULES``, an exhaustive table of every step id in the
    system that can self-skip. Each rule matches the corresponding shell
    script's strict string-equality check and only contributes to the
    result when ``mode`` is one of the modes that step id can appear in.
    """
    return frozenset(
        rule.step_id
        for rule in _SKIP_RULES
        if mode in rule.modes and rule.predicate(env)
    )
